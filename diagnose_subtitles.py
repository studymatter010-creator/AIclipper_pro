#!/usr/bin/env python3
"""
Standalone subtitle-pipeline diagnostic for AIClipper.

Usage:
    python diagnose_subtitles.py <clip_id> [--fresh] [--full]

For a given clip_id, traces the EXACT data flow and reports segment counts
at every pipeline stage.  Prints the stage where count drops significantly
so the bug owner is unambiguous.

--fresh   Also re-transcribe the clip audio (slower, needs the clip file).
--full    Print ALL segments at each stage (not just the first 20).
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

# ── Locate project root (one level up from this script's dir or from cwd) ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR  # assume outputs/ is inside the project
if not (PROJECT_ROOT / "backend").exists():
    PROJECT_ROOT = Path.cwd()
if not (PROJECT_ROOT / "backend").exists():
    # Try common locations
    for candidate in [Path.home() / "AIClipper", Path("/sessions/adoring-optimistic-maxwell/mnt/AIClipper")]:
        if (candidate / "backend").exists():
            PROJECT_ROOT = candidate
            break

DB_PATH = PROJECT_ROOT / "data" / "aiclipper.db"

# ── Import project modules (if available) ──────────────────────────────────
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from backend.services.auto_editor import (
        _rebase_transcript_segments,
        _sanitize_segments,
        _build_drawtext_caption_vf,
    )
    HAS_PROJECTMODULES = True
except ImportError as e:
    HAS_PROJECTMODULES = False
    IMPORT_ERR = str(e)

# ── Colour helpers ─────────────────────────────────────────────────────────
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def heading(text):
    print(f"\n{BOLD}{CYAN}{'='*70}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'='*70}{RESET}")

def subheading(text):
    print(f"\n{BOLD}{YELLOW}--- {text} ---{RESET}")

def count_line(label, count, prev_count=None):
    delta = ""
    if prev_count is not None:
        diff = count - prev_count
        if diff < 0:
            delta = f"  {RED}▼ {diff}{RESET}"
        elif diff > 0:
            delta = f"  {GREEN}▲ +{diff}{RESET}"
        else:
            delta = f"  (no change)"
    print(f"  {label}: {BOLD}{count}{RESET}{delta}")

def print_segments(segs, label, limit=None, show_all=False):
    """Pretty-print segment list."""
    if not segs:
        print(f"  (empty)")
        return
    to_show = segs if show_all else (segs[:limit] if limit else segs)
    for i, s in enumerate(to_show):
        start = s.get("start", "?")
        end = s.get("end", "?")
        text = s.get("text", "")[:80]
        extra = ""
        if "avg_logprob" in s and s["avg_logprob"] is not None:
            extra += f"  logp={s['avg_logprob']:.2f}"
        if "no_speech_prob" in s and s["no_speech_prob"] is not None:
            extra += f"  ns={s['no_speech_prob']:.2f}"
        print(f"  [{i:3d}] {start:7.3f} → {end:7.3f}  \"{text}\"{extra}")
    if not show_all and len(segs) > (limit or 20):
        print(f"  ... and {len(segs) - (limit or 20)} more (use --full to show all)")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]
    do_fresh = "--fresh" in args
    show_all = "--full" in args
    args = [a for a in args if not a.startswith("--")]

    if not args:
        print(f"Usage: python {Path(__file__).name} <clip_id> [--fresh] [--full]")
        sys.exit(1)

    clip_id = int(args[0])

    heading(f"SUBTITLE PIPELINE DIAGNOSTIC — clip_id={clip_id}")

    # ── 0. DB access ───────────────────────────────────────────────────────
    if not DB_PATH.exists():
        print(f"{RED}ERROR: Database not found at {DB_PATH}{RESET}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Fetch clip
    cur.execute("SELECT * FROM clips WHERE id = ?", (clip_id,))
    clip_row = cur.fetchone()
    if not clip_row:
        print(f"{RED}ERROR: clip_id={clip_id} not found in clips table{RESET}")
        conn.close()
        sys.exit(1)

    clip = dict(clip_row)
    video_id = clip["video_id"]
    clip_start = clip["start_time"]
    clip_end = clip["end_time"]
    clip_duration = clip["duration"]
    output_path = clip["output_path"]
    edited_path = clip["edited_output_path"]

    subheading("Clip metadata")
    print(f"  id:            {clip['id']}")
    print(f"  video_id:      {video_id}")
    print(f"  start_time:    {clip_start}")
    print(f"  end_time:      {clip_end}")
    print(f"  duration:      {clip_duration}")
    print(f"  output_path:   {output_path}")
    print(f"  edited_path:   {edited_path}")

    # Resolve actual clip file
    clip_file = None
    for candidate in [
        Path(output_path) if output_path else None,
        PROJECT_ROOT / output_path if output_path else None,
    ]:
        if candidate and candidate.exists():
            clip_file = candidate
            break

    if clip_file:
        print(f"  clip file:     {clip_file}  ({clip_file.stat().st_size / 1024:.0f} KB)")
    else:
        print(f"  clip file:     {RED}NOT FOUND{RESET}")

    # Fetch transcript
    cur.execute(
        "SELECT * FROM transcripts WHERE video_id = ? ORDER BY id DESC LIMIT 1",
        (video_id,),
    )
    trans_row = cur.fetchone()
    conn.close()

    if not trans_row:
        print(f"\n  {RED}No transcript found for video_id={video_id}{RESET}")
        has_stored = False
    else:
        has_stored = True

    subheading("Stored transcript metadata")
    if has_stored:
        trans = dict(trans_row)
        print(f"  transcript_id: {trans['id']}")
        print(f"  language:      {trans['language']}")
        raw_segs = trans["content_json"] or []
        raw_words = trans["word_timestamps_json"] or []
        if isinstance(raw_segs, dict):
            raw_segs = raw_segs.get("segments") or raw_segs.get("content") or []
        print(f"  total segments in full video: {len(raw_segs)}")
        print(f"  total words in full video:    {len(raw_words)}")
    else:
        raw_segs = []
        raw_words = []
        print(f"  (none)")

    # ── Project modules check ──────────────────────────────────────────────
    if not HAS_PROJECTMODULES:
        print(f"\n  {YELLOW}WARNING: Could not import project modules ({IMPORT_ERR}){RESET}")
        print(f"  Will use standalone reimplementation of pipeline functions.")

    # ═══════════════════════════════════════════════════════════════════════
    #  PATH A: STORED TRANSCRIPT (fallback path)
    # ═══════════════════════════════════════════════════════════════════════
    heading("PATH A — Stored transcript (fallback)")

    if not has_stored:
        print(f"  {YELLOW}No stored transcript — skipping Path A{RESET}")
    else:
        # Stage 1: Raw overlap filter
        subheading("Stage 1: Raw overlap filter")
        print(f"  Condition: segment.end > {clip_start} AND segment.start < {clip_end}")
        print(f"  (exclude: end <= {clip_start} OR start >= {clip_end})")

        overlapping = []
        for seg in raw_segs:
            s = seg.get("start", 0)
            e = seg.get("end", 0)
            if e <= clip_start or s >= clip_end:
                continue
            overlapping.append(seg)

        count_line("Raw overlapping segments", len(overlapping), len(raw_segs))
        print_segments(overlapping, "Overlapping", limit=20, show_all=show_all)

        # Stage 2: Rebase
        subheading("Stage 2: Rebase to 0-based clip time")
        if HAS_PROJECTMODULES:
            rebased = _rebase_transcript_segments(raw_segs, clip_start, clip_end)
        else:
            # Standalone reimplementation
            rebased = []
            for seg in overlapping:
                s = seg.get("start", 0)
                e = seg.get("end", 0)
                new_start = round(max(s, clip_start) - clip_start, 3)
                new_end = round(min(e, clip_end) - clip_start, 3)
                new_seg = dict(seg)
                new_seg["start"] = max(0.0, new_start)
                new_seg["end"] = min(clip_duration or new_end, new_end)
                if new_seg["end"] < new_seg["start"]:
                    new_seg["end"] = new_seg["start"]
                rebased.append(new_seg)

        count_line("After rebase", len(rebased), len(overlapping))
        print_segments(rebased, "Rebased", limit=20, show_all=show_all)

        # Stage 3: Duplicate analysis
        subheading("Stage 3: Duplicate analysis")
        dup_groups = {}
        for i, seg in enumerate(rebased):
            text_key = seg.get("text", "").strip().lower()
            if text_key not in dup_groups:
                dup_groups[text_key] = []
            dup_groups[text_key].append((i, seg))

        dups_found = False
        for text_key, entries in dup_groups.items():
            if len(entries) > 1:
                dups_found = True
                print(f"\n  {RED}DUPLICATE: \"{text_key}\"{RESET} ({len(entries)} occurrences)")
                for idx, seg in entries:
                    print(f"    [{idx}] start={seg['start']:.3f} end={seg['end']:.3f}  \"{seg.get('text', '')}\"")
        if not dups_found:
            print(f"  No duplicates found.")

        # Stage 4: Sanitize
        subheading("Stage 4: Sanitize (hallucination + confidence filter)")
        pre_sanitize = len(rebased)
        if HAS_PROJECTMODULES:
            sanitized = _sanitize_segments(list(rebased), clip_duration)
        else:
            # Minimal standalone reimplementation
            sanitized = []
            for seg in rebased:
                text = str(seg.get("text", "")).strip()
                if not text:
                    continue
                start = float(seg.get("start", 0))
                end = float(seg.get("end", 0))
                # Confidence
                avg_lp = seg.get("avg_logprob")
                ns_prob = seg.get("no_speech_prob")
                if avg_lp is not None and avg_lp < -1.5:
                    continue
                if ns_prob is not None and ns_prob > 0.75:
                    continue
                # Known hallucinations
                low = text.lower().strip()
                halluc = {
                    "thank you for watching", "thanks for watching",
                    "please subscribe", "like and subscribe",
                    "subscribe to my channel", "i hope you enjoyed",
                    "see you next time", "bye", "thank you.", "thanks.",
                    "you", "bye.", "[laughter]", "[applause]", "[noise]",
                    "♪", "♪♪", "♪♪♪", "the the", "i i", "you you", "we we", "a a",
                }
                if low in halluc:
                    continue
                # Too short
                if end - start < 0.3:
                    continue
                # Consecutive duplicate
                if sanitized and text.lower().strip() == str(sanitized[-1].get("text", "")).lower().strip():
                    continue
                sanitized.append(seg)

        count_line("After sanitize", len(sanitized), pre_sanitize)
        if pre_sanitize - len(sanitized) > 0:
            print(f"  {RED}▲ {pre_sanitize - len(sanitized)} segments REMOVED by sanitize{RESET}")
        print_segments(sanitized, "Sanitized", limit=20, show_all=show_all)

        # Stage 5: Final drawtext cues
        subheading("Stage 5: Final drawtext cues")
        cue_list = [
            {"start": float(sg.get("start", 0)), "end": float(sg.get("end", 0)), "text": sg.get("text", "")}
            for sg in sanitized
        ]
        count_line("Drawtext cues", len(cue_list), len(sanitized))
        print_segments(cue_list, "Cues", limit=20, show_all=show_all)

        # Stage 6: Coverage
        subheading("Stage 6: Time coverage")
        if clip_duration:
            covered = sum(max(0.0, float(s.get("end", 0)) - max(0.0, float(s.get("start", 0)))) for s in cue_list)
            pct = (covered / clip_duration * 100) if clip_duration > 0 else 0
            colour = GREEN if pct > 80 else (YELLOW if pct > 40 else RED)
            print(f"  Clip duration:  {clip_duration:.3f}s")
            print(f"  Covered time:   {covered:.3f}s")
            print(f"  Coverage:       {colour}{pct:.1f}%{RESET}")
            if pct < 50:
                print(f"  {RED}▲ LOW COVERAGE — this explains why subtitles disappear early{RESET}")
            # Show gap analysis
            if cue_list:
                sorted_cues = sorted(cue_list, key=lambda c: c["start"])
                gaps = []
                for i in range(1, len(sorted_cues)):
                    gap_start = sorted_cues[i-1]["end"]
                    gap_end = sorted_cues[i]["start"]
                    gap_len = gap_end - gap_start
                    if gap_len > 1.0:  # >1s gap
                        gaps.append((gap_start, gap_end, gap_len))
                if gaps:
                    print(f"\n  {RED}GAPS > 1.0s:{RESET}")
                    for gs, ge, gl in gaps:
                        print(f"    {gs:.3f} → {ge:.3f}  ({gl:.3f}s gap)")
        else:
            print(f"  {YELLOW}clip_duration unknown — cannot compute coverage{RESET}")

    # ═══════════════════════════════════════════════════════════════════════
    #  PATH B: FRESH TRANSCRIPTION (primary path)
    # ═══════════════════════════════════════════════════════════════════════
    heading("PATH B — Fresh transcription (primary)")
    if not do_fresh:
        print(f"  Skipped (run with --fresh to include)")
        print(f"  This path runs: transcribe_video(clip_path) → _sanitize_segments()")
    elif not clip_file:
        print(f"  {RED}Clip file not found — cannot transcribe{RESET}")
    else:
        print(f"  Transcribing {clip_file} ...")
        try:
            from backend.services.transcription import transcribe_video
            import asyncio
            ct = asyncio.get_event_loop().run_until_complete(
                asyncio.to_thread(transcribe_video, clip_file, language="auto")
            )
            fresh_segs = ct.get("segments", []) or []
            fresh_words = ct.get("words", []) or []

            subheading("Stage 1: Fresh transcription result")
            count_line("Fresh segments", len(fresh_segs))
            count_line("Fresh words", len(fresh_words))
            print(f"  Engine: {ct.get('engine', '?')}")
            print(f"  Language: {ct.get('language', '?')}")
            print_segments(fresh_segs, "Fresh segments", limit=20, show_all=show_all)

            # Confidence distribution
            if fresh_segs:
                logprobs = [s.get("avg_logprob") for s in fresh_segs if s.get("avg_logprob") is not None]
                ns_probs = [s.get("no_speech_prob") for s in fresh_segs if s.get("no_speech_prob") is not None]
                if logprobs:
                    print(f"\n  avg_logprob range: {min(logprobs):.3f} to {max(logprobs):.3f}")
                if ns_probs:
                    print(f"  no_speech_prob range: {min(ns_probs):.3f} to {max(ns_probs):.3f}")

            # Stage 2: Sanitize
            subheading("Stage 2: Sanitize")
            pre = len(fresh_segs)
            sanitized_fresh = _sanitize_segments(list(fresh_segs), clip_duration) if HAS_PROJECTMODULES else fresh_segs
            count_line("After sanitize", len(sanitized_fresh), pre)
            if pre - len(sanitized_fresh) > 0:
                print(f"  {RED}▲ {pre - len(sanitized_fresh)} segments REMOVED by sanitize{RESET}")
            print_segments(sanitized_fresh, "Sanitized fresh", limit=20, show_all=show_all)

            # Stage 3: Cues
            subheading("Stage 3: Drawtext cues")
            cues_fresh = [
                {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)), "text": s.get("text", "")}
                for s in sanitized_fresh
            ]
            count_line("Drawtext cues", len(cues_fresh))
            print_segments(cues_fresh, "Fresh cues", limit=20, show_all=show_all)

            # Coverage
            subheading("Stage 4: Time coverage")
            if clip_duration:
                covered = sum(max(0.0, c["end"] - max(0.0, c["start"])) for c in cues_fresh)
                pct = (covered / clip_duration * 100) if clip_duration > 0 else 0
                colour = GREEN if pct > 80 else (YELLOW if pct > 40 else RED)
                print(f"  Clip duration:  {clip_duration:.3f}s")
                print(f"  Covered time:   {covered:.3f}s")
                print(f"  Coverage:       {colour}{pct:.1f}%{RESET}")

            # Stage 5: Drawtext filter preview
            subheading("Stage 5: Drawtext filter string (first 500 chars)")
            if HAS_PROJECTMODULES and cues_fresh:
                try:
                    vf = _build_drawtext_caption_vf(
                        cues_fresh, clip_duration or 15.0, caption_font="Arial",
                    )
                    print(f"  Length: {len(vf)} chars")
                    print(f"  Preview:\n    {vf[:500]}")
                    if len(vf) > 500:
                        print(f"    ... ({len(vf) - 500} more chars)")
                except Exception as e:
                    print(f"  {RED}Error building drawtext VF: {e}{RESET}")
            else:
                print(f"  (skipped — no project modules or no cues)")

        except Exception as e:
            print(f"  {RED}Fresh transcription failed: {e}{RESET}")
            import traceback
            traceback.print_exc()

    # ═══════════════════════════════════════════════════════════════════════
    #  SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    heading("DIAGNOSTIC SUMMARY")
    print(f"  Report the following counts to Claude:")
    print(f"")
    if has_stored:
        print(f"  Path A (stored transcript):")
        print(f"    Raw overlapping segments:  {len(overlapping)}")
        print(f"    After rebase:              {len(rebased)}")
        print(f"    After sanitize:            {len(sanitized)}")
        print(f"    Final drawtext cues:       {len(cue_list)}")
    else:
        print(f"  Path A: NO STORED TRANSCRIPT")

    if do_fresh and clip_file:
        print(f"  Path B (fresh transcription):")
        print(f"    Fresh segments:            {len(fresh_segs)}")
        print(f"    After sanitize:            {len(sanitized_fresh)}")
        print(f"    Final drawtext cues:       {len(cues_fresh)}")

    print(f"\n  {BOLD}Which count dropped first/most?{RESET}")
    print(f"  That stage owns the bug.")


if __name__ == "__main__":
    main()
