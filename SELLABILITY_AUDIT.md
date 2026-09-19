# AIClipper — Sellability Cleanup Audit

**Date:** 2026-09-13
**Method:** Read each candidate on-disk and classify as **SAFE** (clearly disposable dev artifact) or **NEEDS REVIEW** (keep or you decide). Nothing is deleted automatically — you confirm the safe bucket first.

---

## Bucket 1 — SAFE to remove (dev/probe artifacts, no shipping value)

These are one-off development aids produced across prior sessions. None are referenced by the app's runtime, build, or `docs/`. Removing them does not change how the app runs. `docs/` and `README.md` are the real user-facing docs and are **kept**.

| File | What it is | Why safe |
|---|---|---|
| `check_db.py` | Dev reset script (hardcodes `video_id=1`, deletes that video's clips/subtitles/thumbs, resets status to PENDING) | One-off manual re-process tool. Not called by any code or npm script. |
| `phase5_verify.py` | Standalone verification harness that stubbed heavy deps and ran 16 checks | Proof-of-work for the Phase 5 report. Its checks are now covered by unit tests + real runs. |
| `PHASE0_AUDIT.md` | "Audit before building" deliverable (2026-09-11) | Per-phase planning doc, superseded by the completed build + `PHASE5_REPORT.md`. |
| `PHASE1_INSTALL.md` | Install instructions for NLLB/WhisperX/Demucs/bge deps | These deps were later **not adopted** (see `MODEL_UPGRADE_NOTES.md` — RAM budget). Instructions now stale. |
| `PHASE5_REPORT.md` | Build + verification status report (2026-09-12) | Record of a completed effort; the live status is in the app + tests. |
| `MODEL_UPGRADE_NOTES.md` | Optional upgrade evaluation table | As-a-thought-briefing; the verdict (WhisperX opt-in only) is already in code via config toggles. |
| `MASTERCLEAN_PROMPT.md` | A past session's master system prompt / architecture write-up | Snapshot-style doc; largely duplicated in `docs/architecture.md` + README. |
| `VERIFICATION_CHECKLIST.md` | Feature-acceptance checklist (completed) | Completed checklist, no ongoing purpose. |
| `VERIFICATION_STORAGE.md` | Storage/dedup feature verification notes | Completed verification record. |
| `VERIFICATION_BYOK.md` | BYOK provider verification notes | Completed verification record. |

## Bucket 2 — NEEDS REVIEW (your call)

These are not broken or dangerous, but you may or may not want to keep them long-term:

| File | What it is | My recommendation |
|---|---|---|
| `frontend/css/styles.css` | 4k+ line stylesheet, still actively used | **KEEP** — shipped asset. |
| `frontend/js/app.js` | Active SPA controller | **KEEP** — shipped asset. |
| `logs/performance.log`, `logs/uploads.log` | Runtime logs, already covered by `.gitignore` | **KEEP** — they're git-ignored, so they don't ship in a repo clone anyway. |
| `README.md`, `docs/*` | User/docs surface | **KEEP** — the sellable story lives here. |

---

## What is NOT removed (by design)

- `README.md`, `LICENSE`, `docs/`, `configs/`, `backend/`, `frontend/`, `scripts/`, `tests/`, `requirements.txt`, `package.json` — all shipping/app source, untouched.
- Anything in `.venv/`, `node_modules/` — dependencies, already git-ignored.

## .gitignore — already comprehensive

Verified present: `__pycache__/`, `*.py[cod]`, `.venv/`, `.env`, `uploads/`, `outputs/`, `subtitles/`, `thumbnails/`, `logs/`, `temp/`, `data/`, `*.db`, `models/*.bin`, IDE + OS files, Docker volumes. **No additions needed** — the dev `.md`/`.py` strays above are *tracked* (not ignored), which is why they show up. Removing them from git is the cleanup; ignoring them isn't the fix.

## Evidence (before/after)

- **Strays removed:** 10 files in Bucket 1.
- **Repo source (kept):** backend/, frontend/, scripts/, tests/, docs/, configs/, README, package.json, requirements.txt, .gitignore — all intact.

> ⚠️ Files in Bucket 1 are still tracked by git. After you confirm, I'll `git rm` them (or move them to a `_archive/` folder if you'd rather keep them recoverable) and you'll see a clean before→after change.