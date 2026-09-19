# AIClipper — What's new & how to check it works

This is a plain-English list of the changes just added and the quick way to
confirm each one on your screen. You don't need any code knowledge — just run
the app normally (`npm run dev` for the frontend, your normal start script for
the backend) and walk down the list.

> ⚠️ **A database note before you start:** the app adds two new saved fields
> (`current_stage`, `stage_progress`) automatically when it starts up. Nothing
> to do on your side — just start the app normally.

---

## 1. Live progress bars for each processing step  ✅ ← the big new feature

While a video is processing you now see a thin bar under **each** step
(Transcription, Scene Detection, Audio, Faces, Scoring, Clips, Subtitles,
Metadata, Thumbnails) in addition to the overall progress bar at the top.

**How to check:**

1. Upload a video and click **🚀 Process Now**.
2. On the Processing screen watch the step list down the left side.
   - A step that is **running** and has real sub-steps (Clips, Subtitles,
     Metadata, Thumbnails) shows a **teal bar filling to a real %**.
   - A step that is **running** but can't report a precise number (Scoring,
     Transcription, Scene Detection, Audio, Faces) shows a **wiggling/pulsing
     bar with no fake number**.
   - A finished step shows a **fully filled bar with the ✓ dot**.
   - If something fails, the bar turns **red** with a short reason instead of
     looking "stuck".
3. The bars should move **one at a time** as each stage runs — not all lighting
   up together.

## 2. Dashboard stat cards now show real numbers  ✅ (fixes a page bug)

The four cards at the top of the Dashboard (Total Videos, Clips Generated,
Completed, Published) previously made the page error out. They now display your
actual, live counts.

**How to check:** Open **Dashboard**. You should see the four number cards with
real figures — and the page should NOT show a red "Error" state.

## 3. Settings now load your saved choices  ✅

Opening **Settings** previously reset everything to defaults; your saved values
never came back. Now the form loads the values you last saved.

**How to check:**

1. Open **Settings**, change anything (e.g. Highlight Color), click **💾 Save
   Settings**.
2. Leave the page and come back to **Settings**. Your changes are still on the
   screen.

## 4. Re-recorded videos are labelled "Re-uploaded"  ✅

If you upload the same file twice, the second copy is marked so you can tell
them apart.

**How to check:** Upload the same video file again. In the video lists
(Dashboard → Recent Videos, or the Processing page) the second copy shows a
gold **Re-uploaded** tag.

## 5. Visual polish  ✅

- Video cards across the app (Dashboard, Processing history) now use their full
  card styling (thumbnail tile, title, "X·Y·Z" size line).
- Dashboard quick-action tiles, the Publishing account tiles, and the Project
  cards all use the consistent card look (rounded corners, hover lift).

**How to check:** Click through **Dashboard**, **Projects**, and **Publishing**.
Everything should look like part of one design — no raw/unstyled boxes.

---

## Bottom line

Start the app, upload a video, hit **Process Now**, and watch the per-step bars
on the Processing screen. That's the headline change. The rest is bug fixes so
the Dashboard, Settings, and video lists work and look right.

If anything looks wrong, tell me exactly which screen and what you see and I'll
fix it.