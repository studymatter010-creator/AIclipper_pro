# Storage Management + Deduplication — Verification Guide

This documents the manual checks to run on your Windows machine after the change.
All backend files were `py_compile`-verified and all frontend JS files `node --check`-verified
in this session; these steps confirm the behaviour end-to-end on the real runtime.

## What changed

| File | Change |
|------|--------|
| `backend/database/models.py` | `videos` gained `source_video_id` (URL dedup) + `content_hash` (upload dedup) |
| `backend/database/engine.py` | auto-migration adds both columns + indexes on existing DBs |
| `backend/services/downloader.py` | `extract_source_id()` derives the YouTube ID from a URL *before* download |
| `backend/api/routes/videos.py` | upload hashes bytes & dedups BEFORE keeping a copy; URL import dedups by source-ID BEFORE re-downloading |
| `backend/api/routes/storage.py` | NEW: usage report, per-video delete (source-only vs everything), orphan scan/cleanup, pending-cleanup, temp-cleanup (all require confirm) |
| `backend/services/pipeline.py` | auto-delete of source file after FULL success, only when `auto_delete_source` setting is on |
| `scripts/dedupe_videos.py` | one-time duplicate sweep (report, then delete with `--yes`) |
| `frontend/js/api.js`, `components.js`, `app.js`, `index.html`, `styles.css` | Storage panel in Settings + per-video delete button with confirm dialog |

## Restart first

Stop the app, delete `data/aiclipper.db` **only if you want to start clean**, else just
launch (the schema auto-migrates). The Storage section auto-delete toggle defaults to **OFF**.

---

## 1 · Upload the same file twice → no second copy

1. In **Upload**, pick any video file and upload it.
2. Upload the **exact same file** again.
3. Expected:
   - The second upload responds with **`"deduped": true`** (backend) — the UI returns
     the existing video instead of creating a new row.
   - **`uploads/`** contains **one** file (not two). Confirm by listing the folder.
   - The Videos list shows **one** entry.

## 2 · Import the same YouTube URL twice → no second download

1. In Upload → **Import from URL**, paste a YouTube link.
2. Import the **same link** again.
3. Expected: the second import returns `"deduped": true` and does **not** download again
   (check the terminal log for the "reusing existing video … no re-download" message, and
   confirm only one new file appeared in `uploads/`).

## 3 · One-time duplicate cleanup reports real savings

1. If you previously imported/uploaded the same content more than once, run from the project root:
   - `python scripts/dedupe_videos.py`  → **report only**, no changes.
2. Expected output: it lists each hash group, marks the kept copy, lists removables, and shows
   a **SUMMARY: N duplicate file(s), X.X.X reclaimable**.
   - Cross-check the reported sizes against `dir uploads`.
3. To actually delete: `python scripts/dedupe_videos.py --yes`
   - It should free roughly the logged amount.

> New uploads/imports are deduplicated automatically going forward, so this script is only
> needed to sweep up copies made *before* this change (which have no content_hash yet).

## 4 · Every delete actually removes files from disk

For each of these, watch the file(s) disappear from the folder, not just the row:

- **Delete source only** (video card trash → "Delete source only"): `uploads/<file>` is gone;
  clips/thumbnails/subtitles stay; the video row stays.
- **Delete everything** (video card trash → "Delete everything"): the source file AND every
  clip output, thumbnail and subtitle file are removed, plus the DB rows (cascade).
- **Settings → Storage → Delete pending videos**: all pending/failed rows removed from DB
  AND their files removed from `uploads/`.
- **Settings → Storage → Clear temp files**: `temp/` emptied.
- **Settings → Storage → Delete orphaned files**: any file under uploads/outputs/thumbnails/
  subtitles that has no DB row is removed.

Every destructive button shows a confirmation naming roughly what will be removed. There is
no silent permanent delete.

## 5 · Auto-delete source only after full success

1. In **Settings → Storage**, turn ON "Automatically delete the source video after all clips
   are generated successfully" → **Save Settings**.
2. Upload a video and let it process to **completed** (all clips generated).
3. Expected: the source file in `uploads/` is gone, but the clips/thumbnails/subtitles remain
   and the video still shows **completed**. Terminal shows `VIDEO_AUTODELETE on video …`.
4. Turn the toggle OFF, upload again, let it process: the source file is **kept**.

Edge cases that must NOT auto-delete: a failed run, a partial run with zero clips, or the
toggle being off.

## Files to double-check if something looks off

- For URL-import dedup not firing: confirm `extract_source_id()` matched your URL form
  (works for `v=`, `/shorts/`, `/live/`, `youtu.be/`, `/watch/`).
- For the Storage panel not showing usage: check the backend endpoint
  `GET /api/storage/usage` returns JSON (open it in the browser).
