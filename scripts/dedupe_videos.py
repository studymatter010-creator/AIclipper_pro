#!/usr/bin/env python3
"""
AIClipper one-time duplicate-video cleanup (storage management).

Scans every video row by stored content-hash, groups rows that reference the
SAME source content, and reports duplicates along with the reclaimable space.
Nothing is deleted unless ``--commit`` (or ``--yes``) is passed, so an operator
can inspect the report first.

Behaviour:
  * A "duplicate" is any video row after the canonical one within a group whose
    members share the same (content_hash).  The canonical member is the OLDEST
    row (lowest id), i.e. the first one captured.
  * With --commit, each duplicate row is deleted (DB cascade removes its derived
    clips/subtitles/thumbnails/edit rows) AND its on-disk source file is removed.
  * If the canonical row's source file no longer exists but a duplicate's does,
    the canonical row is first re-pointed to that surviving file so we never
    leave a "kept" row pointing at a path that is gone.

Only rows that actually resolve to a real file on disk are counted; a row whose
file is already missing is skipped (nothing to reclaim).

Run from the project root:

    python scripts/dedupe_videos.py                 # report only
    python scripts/dedupe_videos.py --yes           # report + delete duplicates
    python scripts/dedupe_videos.py --commit        # same as --yes
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

# --- bootstrapping: import the app as if launched from the project root ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.database import crud  # noqa: E402
from backend.database.engine import get_session_context  # noqa: E402
from backend.utils.config import PROJECT_ROOT  # noqa: E402


def _resolve(p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _human(nbytes: int) -> str:
    val = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f}{unit}" if unit != "B" else f"{val:.0f}B"
        val /= 1024
    return f"{val:.1f}TB"


async def run(commit: bool) -> int:
    from backend.database.models import Video

    async with get_session_context() as session:
        result = await session.execute(select(Video).order_by(Video.id.asc()))
        videos = result.scalars().all()

    # Group rows by content hash.  Ordered by id asc already, so within each
    # group the first member is the canonical (oldest) one.
    by_hash: dict[str, list] = defaultdict(list)
    for v in videos:
        if v.content_hash:
            by_hash[v.content_hash].append(v)

    groups = []
    for h, members in by_hash.items():
        if len(members) < 2:
            continue  # unique content — not a duplicate
        # separate rows whose file really exists (countable) from ghosts.
        live = [m for m in members if _resolve(m.filepath) and _resolve(m.filepath).exists()]
        if len(live) < 2:
            continue  # nothing redundant on disk to reclaim
        groups.append((h, members, live))

    if not groups:
        print("No duplicate video content found. Nothing to do.")
        return 0

    # ---- Report ---------------------------------------------------------
    total_dup_count = 0
    total_reclaimable = 0
    print("=" * 78)
    print("DUPLICATE VIDEO REPORT  (rows sharing identical source content)")
    print("=" * 78)
    for h, members, live in groups:
        canonical = members[0]
        cand_path = _resolve(canonical.filepath)
        dups = live[1:]  # live members after the canonical = removable
        # count size from existing files so the number is truthful
        for d in dups:
            p = _resolve(d.filepath)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            total_dup_count += 1
            total_reclaimable += size
        print(f"\nhash {h[:12]}…  ({len(live)} copies on disk)")
        print(f"   [kept]   video {canonical.id:<5} {cand_path and cand_path.name}")
        for d in dups:
            p = _resolve(d.filepath)
            print(f"   [remove] video {d.id:<5} {p and p.name}  ({_human(size)})")

    print("\n" + "-" * 78)
    print(
        f"SUMMARY: {total_dup_count} duplicate file(s), "
        f"{_human(total_reclaimable)} reclaimable."
    )
    print("Run with  --yes  to actually delete these duplicates.")
    print("=" * 78)

    if not commit:
        return 0

    # ---- Commit ---------------------------------------------------------
    print("\nCommitting…")
    removed, freed = 0, 0
    async with get_session_context() as session:
        for h, members, live in groups:
            canonical = members[0]
            cand_path = _resolve(canonical.filepath)
            if cand_path is None or not cand_path.exists():
                # Re-point the canonical row to a surviving duplicate's file so
                # we keep one usable copy before deleting the rest.
                for d in live[1:]:
                    p = _resolve(d.filepath)
                    if p and p.exists():
                        canonical.filepath = str(p)
                        await session.flush()
                        print(
                            f"  video {canonical.id}: re-pointed source to "
                            f"{p.name} (canonical file was missing)"
                        )
                        break

            for d in live[1:]:
                p = _resolve(d.filepath)
                if p and p.exists():
                    try:
                        size = p.stat().st_size
                        p.unlink(missing_ok=True)
                        freed += size
                    except OSError as exc:
                        print(f"  !! could not delete {p}: {exc}")
                if await crud.delete_video(session, d.id):
                    removed += 1
                    print(f"  deleted video {d.id} (source file + DB row + derived)")
    print(f"\nDone: removed {removed} duplicate video(s), freed {_human(freed)}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report and optionally delete duplicate video content "
        "(identical source bytes kept more than once)."
    )
    parser.add_argument(
        "--yes", "--commit", dest="commit", action="store_true",
        help="Actually delete the duplicates (default is report-only).",
    )
    args = parser.parse_args()
    return asyncio.run(run(commit=args.commit))


if __name__ == "__main__":
    raise SystemExit(main())
