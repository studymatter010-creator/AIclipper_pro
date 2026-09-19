"""
AIClipper Clip Scoring Engine

Slides a window across the video timeline, scores each candidate clip on
multiple dimensions (emotion, dialogue, scene changes, audio energy, face
presence), then applies non-maximum suppression to pick the best
non-overlapping clips.
"""

from __future__ import annotations

from typing import Any, Callable

from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed

logger = get_logger("services.clip_scoring")

# Default scoring weights (mirrors ScoringWeights in config).
# ``reaction`` is a new dimension fed by laughter/crowd-reaction cues in the
# audio analysis — a strong predictor of "keep watching" in short form.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "emotion": 0.28,
    "dialogue": 0.22,
    "scene_change": 0.10,
    "audio": 0.16,
    "face": 0.12,
    "reaction": 0.12,
}

# Part C: dedicated "hook strength" weight held OUT of the other dimensions.
# Short-form platforms are judged almost entirely on the first ~3 seconds, but
# averaging emotion/energy over the whole window hides a weak opening.  We give
# the opening-window its OWN sub-score and reserve this much of the total weight
# for it, renormalising the other six dimensions to keep the sum at 1.0.
_HOOK_WEIGHT = 0.10
_HOOK_WINDOW_SECONDS = 3.0


def _items_in_window(
    items: list[dict[str, Any]],
    win_start: float,
    win_end: float,
    start_key: str = "start",
    end_key: str | None = "end",
) -> list[dict[str, Any]]:
    """Return items whose time span overlaps ``[win_start, win_end)``."""
    result: list[dict[str, Any]] = []
    for item in items:
        item_start = item[start_key]
        item_end = item.get(end_key, item_start) if end_key else item_start
        if item_end > win_start and item_start < win_end:
            result.append(item)
    return result


def _face_items_in_window(
    face_data: list[dict[str, Any]],
    win_start: float,
    win_end: float,
) -> list[dict[str, Any]]:
    """Return face-tracking data points inside the window."""
    return [f for f in face_data if win_start <= f["time"] < win_end]


# ── Scoring helpers ─────────────────────────────────────────────────────

def _emotion_score(audio_segments: list[dict[str, Any]], win_start: float, win_end: float) -> float:
    """Average emotion_intensity from audio segments in window."""
    segs = _items_in_window(audio_segments, win_start, win_end)
    if not segs:
        return 0.0
    return sum(s.get("emotion_intensity", 0.0) for s in segs) / len(segs)


def _dialogue_score(
    transcript: dict[str, Any],
    win_start: float,
    win_end: float,
    global_max_words: int,
) -> float:
    """Word density in window relative to the densest window."""
    segments = transcript.get("segments", [])
    segs = _items_in_window(segments, win_start, win_end)
    word_count = sum(len(s.get("text", "").split()) for s in segs)
    if global_max_words <= 0:
        return 0.0
    return min(word_count / global_max_words, 1.0)


def _scene_change_score(
    scenes: list[dict[str, Any]],
    win_start: float,
    win_end: float,
    max_transitions: int,
) -> float:
    """
    Scene interest in window: number of transitions AND their density score.

    ``scenes`` carry a ``score`` (0-1) measured as inverse scene duration —
    rapid-cut stretches are more visually dynamic and read as "action."  Some
    windows contain few cuts but each is a fast-paced one, so we combine the
    raw transition count with the summed density so both busy and punchy
    segments get rewarded.
    """
    transitions = [s for s in scenes if win_start < s["start"] < win_end]
    if not transitions:
        return 0.0
    count_norm = min(len(transitions) / max(max_transitions, 1), 1.0)
    density = sum(s.get("score", 0.0) for s in transitions)
    density_norm = min(density / max(len(transitions), 1), 1.0)
    return round(0.6 * count_norm + 0.4 * density_norm, 4)


def _audio_score(audio_segments: list[dict[str, Any]], win_start: float, win_end: float) -> float:
    """
    Average energy_score in window, lightly blended with rhythmic pacing.

    Remote ``pacing_score`` (onset density) reinforces the energy reading — a
    window that is both loud AND fast-paced is more engaging.  When a segment
    lacks a pacing_score (older data) it falls back to energy alone.
    """
    segs = _items_in_window(audio_segments, win_start, win_end)
    if not segs:
        return 0.0
    energy = sum(s.get("energy_score", 0.0) for s in segs) / len(segs)
    pacing_vals = [s.get("pacing_score") for s in segs if s.get("pacing_score") is not None]
    if not pacing_vals:
        return energy
    pacing = sum(pacing_vals) / len(pacing_vals)
    return round(0.7 * energy + 0.3 * pacing, 4)


def _reaction_score(audio_segments: list[dict[str, Any]], win_start: float, win_end: float) -> float:
    """
    Fraction of audio segments in the window flagged with laughter or a crowd.
    reaction.  These cues reliably signal "payoff" moments that hold viewers,
    so a clip containing several rates highly as short-form material.
    """
    segs = _items_in_window(audio_segments, win_start, win_end)
    if not segs:
        return 0.0
    reactions = sum(
        1 for s in segs
        if s.get("laughter_detected") or s.get("crowd_reaction")
    )
    return reactions / len(segs)


def _face_score(face_data: list[dict[str, Any]], win_start: float, win_end: float) -> float:
    """Percentage of sampled frames with face_visible."""
    pts = _face_items_in_window(face_data, win_start, win_end)
    if not pts:
        return 0.0
    return sum(1 for p in pts if p.get("face_visible")) / len(pts)


def _hook_score(
    transcript: dict[str, Any],
    audio_segments: list[dict[str, Any]],
    face_data: list[dict[str, Any]],
    win_start: float,
    hook_window: float = _HOOK_WINDOW_SECONDS,
) -> float:
    """Strength of JUST the opening ≤3 seconds (Part C).

    Viewers on short-form platforms decide in the first 2-3 seconds whether to
    keep watching, so a clip that is great on average but opens weak is a
    liability.  This sub-score looks ONLY at the opening window and blends:
      * audio energy (0.5) — a loud, busy open;
      * immediate speech (0.35) — speech that starts right at the clip open so
        there's no dead air before talking (extends the existing dialogue-bonus
        idea into a dedicated, weighted signal);
      * face visibility (0.15) — a human face present in the opening frames.

    It intentionally runs as its OWN dimension (not folded into the whole-clip
    average) so a clip can score well overall yet still be flagged/deprioritized
    when its specific opening 3 seconds are weak.
    """
    hook_end = win_start + hook_window

    segs = _items_in_window(audio_segments, win_start, hook_end)
    energy = (sum(s.get("energy_score", 0.0) for s in segs) / len(segs)) if segs else 0.0

    speech = 0.0
    open_segs = _items_in_window(transcript.get("segments", []), win_start, hook_end)
    if open_segs:
        try:
            seg_start = min(float(s.get("start", 0)) for s in open_segs)
        except (TypeError, ValueError):
            seg_start = 0.0
        gap = max(seg_start - win_start, 0.0)
        if gap <= 0.5:
            speech = 1.0
        elif gap <= hook_window:
            speech = max(0.0, 1.0 - (gap - 0.5) / 2.5)

    pts = _face_items_in_window(face_data, win_start, hook_end)
    face = (sum(1 for p in pts if p.get("face_visible")) / len(pts)) if pts else 0.0

    return round(0.5 * energy + 0.35 * speech + 0.15 * face, 4)


# ── Pre-computation for normalisation ───────────────────────────────────

def _compute_max_words_in_window(
    transcript: dict[str, Any],
    video_duration: float,
    window_size: float,
    step: float,
) -> int:
    """Scan all windows and return the max word count (for normalisation)."""
    segments = transcript.get("segments", [])
    max_words = 0
    pos = 0.0
    while pos + window_size <= video_duration + 0.01:
        segs = _items_in_window(segments, pos, pos + window_size)
        wc = sum(len(s.get("text", "").split()) for s in segs)
        if wc > max_words:
            max_words = wc
        pos += step
    return max(max_words, 1)


def _compute_max_transitions_in_window(
    scenes: list[dict[str, Any]],
    video_duration: float,
    window_size: float,
    step: float,
) -> int:
    """Scan all windows and return the max transition count."""
    max_count = 0
    pos = 0.0
    while pos + window_size <= video_duration + 0.01:
        count = sum(1 for s in scenes if pos < s["start"] < pos + window_size)
        if count > max_count:
            max_count = count
        pos += step
    return max(max_count, 1)


# ── Topic diversity (Part D) ───────────────────────────────────────────

# Lightweight, code-free stopword list for the topic fingerprint.  We only need
# CONTENT words to tell whether two windows are talking about the same thing.
_STOPWORDS = frozenset({
    "the", "and", "but", "for", "are", "you", "your", "that", "this", "with",
    "have", "has", "had", "was", "were", "will", "would", "could", "should",
    "from", "they", "them", "their", "there", "here", "what", "when", "where",
    "which", "who", "whom", "then", "than", "just", "like", "know", "really",
    "about", "into", "over", "also", "because", "been", "being", "one", "two",
    "get", "got", "can", "cant", "not", "dont", "isnt", "its", "it's", "im",
    "ive", "youre", "get", "does", "did", "do", "going", "gonna", "wanna",
    "let", "lets", "okay", "yeah", "thing", "things", "something", "anything",
})

_DIVERSITY_MAX_SIMILARITY = 0.55  # ≥ this Jaccard overlap → too similar, skip


def _window_text(transcript: dict[str, Any], start: float, end: float) -> str:
    """Concatenate transcript text inside ``[start, end]`` (segments, fallback words)."""
    segs = [
        s for s in transcript.get("segments", [])
        if s.get("start", 0) < end and s.get("end", s.get("start", 0)) > start
    ]
    if segs:
        return " ".join(str(s.get("text", "")) for s in segs)
    words = [
        str(w.get("word", ""))
        for w in transcript.get("words", [])
        if start <= w.get("start", 0) <= end
    ]
    return " ".join(words)


def _topic_keywords(transcript: dict[str, Any], start: float, end: float, top_k: int = 10) -> set[str]:
    """A cheap content-word fingerprint (top frequent non-stopword tokens)."""
    text = _window_text(transcript, start, end).lower()
    freqs: dict[str, int] = {}
    for tok in text.split():
        word = "".join(ch for ch in tok if ch.isalnum())
        if len(word) > 2 and word not in _STOPWORDS:
            freqs[word] = freqs.get(word, 0) + 1
    top = sorted(freqs, key=lambda w: (freqs[w], w), reverse=True)[:top_k]
    return set(top)


def _topic_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of two keyword sets (0.0 = no topic relation, 1.0 = identical)."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


# ── Non-maximum suppression ────────────────────────────────────────────

def _non_maximum_suppression(
    candidates: list[dict[str, Any]],
    max_clips: int,
    min_gap: float,
    transcript: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Select top-scoring clips ensuring a minimum time gap between them.

    Part D: NMS also enforces TOPIC DIVERSITY.  Two windows can be far apart in
    time yet be ~the same moment (a strong segment repeated, or one long topic).
    NMS alone wouldn't catch that.  Using a cheap keyword/n-gram overlap from the
    transcript, a candidate that is topically near-duplicate of an
    already-selected higher-scoring clip is skipped (penalized out), so the final
    set spans more of the video's distinct highlights instead of clustering
    around one moment.  This is v1 — full embedding similarity is a future
    upgrade (the transcript ``words``/semantic boundaries already exist for it).
    """
    # Sort descending by score
    sorted_cands = sorted(candidates, key=lambda c: c["total_score"], reverse=True)
    selected: list[dict[str, Any]] = []
    # Memo of topic fingerprint per (start,end) to avoid recomputing.
    kw_cache: dict[tuple[float, float], set[str]] = {}

    def _kw(cand: dict[str, Any]) -> set[str]:
        key = (cand["start"], cand["end"])
        val = kw_cache.get(key)
        if val is None:
            val = _topic_keywords(transcript, cand["start"], cand["end"]) if transcript else set()
            kw_cache[key] = val
        return val

    for cand in sorted_cands:
        if len(selected) >= max_clips:
            break
        # Check gap to every already-selected clip
        overlaps = False
        for sel in selected:
            # strict non-overlap: if time ranges intersect
            if cand["start"] < sel["end"] and cand["end"] > sel["start"]:
                overlaps = True
                break
            gap = max(
                cand["start"] - sel["end"],
                sel["start"] - cand["end"],
            )
            if gap < min_gap:
                overlaps = True
                break
        if overlaps:
            continue

        # Topic-diversity gate (Part D): skip a candidate that is topically
        # near-duplicate of an already-selected clip, so we surface distinct
        # highlights rather than variations of one strong moment.
        if transcript is not None:
            cand_kw = _kw(cand)
            too_similar = False
            for sel in selected:
                sel_kw = _kw(sel)
                if _topic_similarity(cand_kw, sel_kw) >= _DIVERSITY_MAX_SIMILARITY:
                    too_similar = True
                    break
            if too_similar:
                logger.debug(
                    f"[DIVERSITY] skipping candidate "
                    f"{cand['start']:.1f}s-{cand['end']:.1f}s: topically similar "
                    f"to an already-selected clip"
                )
                continue

        selected.append(cand)

    # Return sorted by start time for intuitive ordering
    return sorted(selected, key=lambda c: c["start"])


def _snap_to_scene_boundaries(
    selected: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    min_duration: float = 30.0,
) -> None:
    """
    Adjust clip boundaries to the nearest scene transitions.

    Both boundaries are snapped to a nearby scene cut where possible, so the
    clip starts and ends on clean visual transitions (no half-cuts) rather
    than arbitrary second boundaries.  A short minimum duration is enforced
    so a boundary snap can never collapse a clip into a useless sliver.
    """
    scene_starts = sorted({s["start"] for s in scenes if s.get("start") is not None})

    def _nearest(target: float, window: float) -> float | None:
        best = None
        best_diff = window
        for ts in scene_starts:
            diff = abs(ts - target)
            if diff <= best_diff:
                best_diff = diff
                best = ts
        return best

    for clip in selected:
        dur = (clip["end"] - clip["start"])
        new_start = _nearest(clip["start"], 5.0)
        new_start = new_start if new_start is not None else clip["start"]
        # Snap the end to a clean cut, but never let the snap extend the clip
        # past its intended window — prefer the nearer of the two boundaries.
        cand_end = _nearest(clip["end"], 5.0)
        if cand_end is not None and cand_end <= clip["end"]:
            new_end = cand_end
        else:
            new_end = clip["end"]

        # Re-anchor so the clip keeps roughly its target length at a clean cut.
        if new_end - new_start < min_duration:
            # Prefer trimming the end back rather than jumping the start.
            if clip["end"] - new_start >= min_duration:
                new_end = clip["end"]
            else:
                new_start = max(0.0, new_end - dur)

        clip["start"] = round(new_start, 3)
        clip["end"] = round(new_end, 3)
        clip["duration"] = round(new_end - new_start, 3)


# ── Narrative / speaker-turn boundary refinement (Part B + E) ──────────

def _transcript_pauses(transcript: dict[str, Any], min_gap: float = 0.4) -> list[float]:
    """Midpoints of silence gaps between consecutive transcript segments.

    A gap between two spoken chunks is a natural place to start or end a
    thought: the previous sentence has resolved and a new idea is beginning.
    We return each gap's MIDPOINT second (the moment of maximum silence), which
    is the cleanest possible in-cut point.
    """
    segments = transcript.get("segments", [])
    out: list[float] = []
    for a, b in zip(segments, segments[1:]):
        try:
            a_end = float(a.get("end", a.get("start", 0)))
            b_start = float(b.get("start", 0))
        except (TypeError, ValueError):
            continue
        gap = b_start - a_end
        if gap >= min_gap:
            out.append(round((a_end + b_start) / 2.0, 3))
    return out


def _transcript_turn_boundaries(transcript: dict[str, Any], min_gap: float = 0.7) -> list[float]:
    """Pause-based speaker-turn proxy for dialogue content (Part E).

    Without full speaker diarization, an UNUSUALLY LONG pause relative to the
    video's typical inter-segment gap is our best non-invasive marker of a
    speaker change.  We only treat a gap as a turn boundary if it both clears a
    floor (``min_gap``) and exceeds the median inter-segment gap by a margin —
    so normal sentence pauses don't get mistaken for turn exchanges.  Real
    diarization can replace this proxy later if it proves insufficient.
    """
    segments = transcript.get("segments", [])
    if not segments:
        return []
    gaps: list[float] = []
    for a, b in zip(segments, segments[1:]):
        try:
            a_end = float(a.get("end", a.get("start", 0)))
            b_start = float(b.get("start", 0))
        except (TypeError, ValueError):
            continue
        gaps.append(b_start - a_end)
    if not gaps:
        return []
    median_gap = sorted(gaps)[len(gaps) // 2]
    out: list[float] = []
    for a, b in zip(segments, segments[1:]):
        try:
            a_end = float(a.get("end", a.get("start", 0)))
            b_start = float(b.get("start", 0))
        except (TypeError, ValueError):
            continue
        gap = b_start - a_end
        if gap >= max(min_gap, median_gap + 0.3):
            out.append(round((a_end + b_start) / 2.0, 3))
    return out


_DIALOGUE_CONTENT_TYPES = {"podcast", "interview", "debate"}


def _refine_narrative_boundaries(
    selected: list[dict[str, Any]],
    transcript: dict[str, Any],
    scenes: list[dict[str, Any]],
    content_type: str | None = None,
    min_duration: float = 30.0,
    window: float = 5.0,
) -> None:
    """Slide each clip's start/end to a more satisfying narrative cut point.

    A premium short clip opens at the start of a NEW idea and closes after a
    thought lands — i.e. at a PAUSE in speech, ideally coinciding with a scene
    cut — rather than slicing mid-sentence.  We draw candidate cut points from
    two cheap, already-computed signals:

      * scene cuts (Stage 2)
      * pause/silence midpoints between transcript segments (Stage 0-1).  For
        podcast / interview / debate (Part E) we use the LONGER, pattern-aware
        pause-based SPEAKER-TURN boundaries instead, so dialogue clips start at
        the beginning of a speaker's turn and end after it resolves.

    Each boundary is slid up to ``window`` (default 5 s) toward the nearest good
    point, preferring positions where BOTH signals agree.  A minimum duration is
    enforced so a slide never collapses a clip into a sliver.
    """
    scene_times = sorted({s["start"] for s in scenes if s.get("start") is not None})
    if content_type in _DIALOGUE_CONTENT_TYPES:
        pauses = _transcript_turn_boundaries(transcript, min_gap=0.7)
    else:
        pauses = _transcript_pauses(transcript, min_gap=0.4)

    if not scene_times and not pauses:
        return

    def _quality(t: float) -> float:
        q = 0.0
        # A pause midpoint is a strong narrative boundary (2 pts).
        if any(abs(t - p) <= 1.2 for p in pauses):
            q += 2.0
        # A scene cut makes the cut visually clean (1 pt).
        if any(abs(t - s) <= 1.2 for s in scene_times):
            q += 1.0
        return q

    def _best_cut(target: float) -> float:
        lo, hi = target - window, target + window
        best = target
        best_q = _quality(target)
        candidates = {round(t, 3) for t in scene_times if lo <= t <= hi}
        candidates |= {round(t, 3) for t in pauses if lo <= t <= hi}
        for t in candidates:
            q = _quality(t)
            # Prefer higher quality, then closer to target (keeps the clip near
            # its intended length).
            if q > best_q or (q == best_q and abs(t - target) < abs(best - target)):
                best, best_q = t, q
        return best

    for clip in selected:
        dur = clip["end"] - clip["start"]
        new_start = _best_cut(clip["start"])
        new_end = _best_cut(clip["end"])
        # Enforce min duration: if the slide collapsed the clip, keep whichever
        # boundary moved least and re-anchor the other to preserve its length.
        if new_end - new_start < min_duration:
            if abs(new_start - clip["start"]) <= abs(new_end - clip["end"]):
                new_end = new_start + dur
            else:
                new_start = max(0.0, new_end - dur)
        clip["start"] = round(max(0.0, new_start), 3)
        clip["end"] = round(new_end, 3)
        clip["duration"] = round(clip["end"] - clip["start"], 3)


# ── Public API ──────────────────────────────────────────────────────────

def _score_window(
    *,
    win_start: float,
    win_end: float,
    dur: float,
    transcript: dict[str, Any],
    scenes: list[dict[str, Any]],
    audio_segments: list[dict[str, Any]],
    face_data: list[dict[str, Any]],
    copyright_segments: list[dict[str, Any]] | None,
    weights: dict[str, float],
    max_words: int,
    max_transitions: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a single candidate window — the SAME logic the sliding sweep uses.

    Extracted so semantic-anchored windows (Phase 2) are scored identically to
    the plain sliding-sweep windows; scoring remains unchanged.
    """
    emo = _emotion_score(audio_segments, win_start, win_end)
    dia = _dialogue_score(transcript, win_start, win_end, max_words)
    sc = _scene_change_score(scenes, win_start, win_end, max_transitions)
    aud = _audio_score(audio_segments, win_start, win_end)
    fac = _face_score(face_data, win_start, win_end)
    reac = _reaction_score(audio_segments, win_start, win_end)
    hook = _hook_score(transcript, audio_segments, face_data, win_start)

    cp_score = 0.0
    if copyright_segments:
        cp_segs = _items_in_window(copyright_segments, win_start, win_end)
        if cp_segs:
            avg_music_score = sum(s.get("music_score", 0.0) for s in cp_segs) / len(cp_segs)
            cp_score = 0.5 * avg_music_score

    # ── Dialogue "hook" quality ───────────────────────────────────────
    # Reward clips whose dialogue starts immediately and ends cleanly
    # (no trailing dead air) and begin on a fresh sentence rather than
    # mid-word (jarring for a viewer landing cold on the clip).
    dialogue_bonus = 0.0
    segs = _items_in_window(transcript.get("segments", []), win_start, win_end)
    if segs:
        first_seg = segs[0]
        seg_start = first_seg["start"]
        gap_start = max(seg_start - win_start, 0.0)
        if gap_start <= 0.5:
            dialogue_bonus += 0.06
        elif gap_start <= 3.0:
            dialogue_bonus += 0.02
        else:
            dialogue_bonus -= min(0.20, gap_start) / 5.0

        last_seg = segs[-1]
        end_time = last_seg.get("end", last_seg["start"])
        if end_time > win_end:
            dialogue_bonus -= 0.05
        elif win_end - end_time <= 3.0:
            dialogue_bonus += 0.03
        else:
            dialogue_bonus -= min(0.20, win_end - end_time) / 8.0

    total = (
        weights["emotion"] * emo
        + weights["dialogue"] * dia
        + weights["scene_change"] * sc
        + weights["audio"] * aud
        + weights["face"] * fac
        + weights.get("reaction", 0.0) * reac
        + weights.get("hook", _HOOK_WEIGHT) * hook
    ) - cp_score + dialogue_bonus

    cand = {
        "start": round(win_start, 3),
        "end": round(win_end, 3),
        "duration": round(dur, 3),
        "total_score": round(total, 4),
        "breakdown": {
            "emotion": round(emo, 4),
            "dialogue": round(dia, 4),
            "scene_change": round(sc, 4),
            "audio": round(aud, 4),
            "face": round(fac, 4),
            "reaction": round(reac, 4),
            "hook": round(hook, 4),
        },
    }
    if extra:
        cand.update(extra)
    return cand


@timed(logger_name="processing")
def score_clips(
    video_duration: float,
    transcript: dict[str, Any],
    scenes: list[dict[str, Any]],
    audio_segments: list[dict[str, Any]],
    face_data: list[dict[str, Any]],
    copyright_segments: list[dict[str, Any]] | None = None,
    clip_durations: list[int] | None = None,
    weights: dict[str, float] | None = None,
    max_clips: int = 10,
    min_gap: float = 60.0,
    snap_to_scenes: bool = True,
    content_type: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Score and select the best candidate clips from a video.

    A sliding window (step = 1 s) is swept across the timeline for each
    requested clip duration.  Per-window scores are computed for five
    dimensions, weighted, and summed.  Non-maximum suppression then picks
    the top non-overlapping clips.

    Args:
        video_duration: Total video length in seconds.
        transcript: Transcript dict as returned by ``transcribe_video()``.
        scenes: Scene list as returned by ``detect_scenes()``.
        audio_segments: Audio analysis list from ``analyze_audio()``.
        face_data: Face tracking list from ``track_faces()``.
        clip_durations: Desired clip lengths in seconds (default from config).
        weights: Scoring-dimension weights dict.
        max_clips: Maximum clips to return.
        min_gap: Minimum gap (seconds) between selected clips.
        snap_to_scenes: Whether to snap clip boundaries to scene transitions.
        content_type: Optional detected content type (podcast/interview/debate/
            ...) — used to pick between generic pause-boundary refinement and
            the speaker-TURN-aware variant (Part E).

    Returns:
        A list of scored clip dicts, sorted by start time::

            [
                {
                    "start": float,
                    "end": float,
                    "duration": float,
                    "total_score": float,
                    "breakdown": {
                        "emotion": float,
                        "dialogue": float,
                        "scene_change": float,
                        "audio": float,
                        "face": float,
                    },
                },
                ...
            ]
    """
    settings = get_settings()
    if clip_durations is None:
        clip_durations = [60, 75, 90]
    if weights is None:
        sw = settings.scoring_weights
        weights = {
            "emotion": sw.emotion,
            "dialogue": sw.dialogue,
            "scene_change": sw.scene_change,
            "audio": sw.audio,
            "face": sw.face,
        }

    # Part C: reserve a slice of the total weight for the dedicated opening-3s
    # "hook" dimension, scaling the others down so the sum still comes to 1.0 and
    # their relative balance is preserved (whether weights came from config or
    # were genre-adjusted upstream).
    if "hook" not in weights:
        scaled = dict(weights)
        for k in list(scaled.keys()):
            scaled[k] = scaled[k] * (1.0 - _HOOK_WEIGHT)
        scaled["hook"] = _HOOK_WEIGHT
        weights = scaled

    step = 1.0  # sliding-window step (seconds)
    all_candidates: list[dict[str, Any]] = []

    # ── Live scoring progress ─────────────────────────────────────────────
    # Scoring is the longest "invisible" stage: it sweeps the whole timeline,
    # once per requested clip length, evaluating ~1 window per second, and
    # previously showed NO percentage at all — so a long video looked frozen on
    # a pulsing bar.  Track how many window positions we must evaluate across
    # all durations, then report a smooth 0.0→1.0 fraction as we go (same live-
    # progress pattern transcription already uses).
    valid_durs = [d for d in clip_durations if d <= video_duration]
    total_positions = 0
    for dur in valid_durs:
        total_positions += int((video_duration - dur) / step) + 1
    scanned = {"n": 0}

    def _report() -> None:
        if progress_callback is None or total_positions <= 0:
            return
        frac = scanned["n"] / total_positions
        try:
            progress_callback(min(1.0, frac))
        except Exception:
            pass  # progress reporting must never break scoring

    for dur in valid_durs:
        # Pre-compute normalisation ceilings for this window size
        max_words = _compute_max_words_in_window(
            transcript, video_duration, float(dur), step
        )
        max_transitions = _compute_max_transitions_in_window(
            scenes, video_duration, float(dur), step
        )

        pos = 0.0
        while pos + dur <= video_duration + 0.01:
            all_candidates.append(_score_window(
                win_start=pos,
                win_end=pos + dur,
                dur=dur,
                transcript=transcript,
                scenes=scenes,
                audio_segments=audio_segments,
                face_data=face_data,
                copyright_segments=copyright_segments,
                weights=weights,
                max_words=max_words,
                max_transitions=max_transitions,
            ))
            pos += step
            scanned["n"] += 1
            _report()

    # ── PHASE 2: semantic-anchored candidate windows ──────────────────────
    # Add well-placed windows that snap to sentence-boundary topic shifts
    # (WhisperX-aligned words + bge embeddings). Scored with the SAME
    # ``_score_window`` logic — plugging in better START/END anchors without
    # altering how clips are judged.
    if get_settings().semantic_boundaries_enabled:
        try:
            from backend.services.semantic_boundaries import semantic_candidate_windows
            sem_windows = semantic_candidate_windows(
                transcript.get("words", []) or [],
                [int(d) for d in clip_durations],
                video_duration,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Semantic-boundary windows skipped ({exc})")
            sem_windows = []

        if sem_windows:
            # Reuse one normalisation pass per duration for the semantic set.
            for dur in clip_durations:
                if dur > video_duration:
                    continue
                max_words = _compute_max_words_in_window(
                    transcript, video_duration, float(dur), step
                )
                max_transitions = _compute_max_transitions_in_window(
                    scenes, video_duration, float(dur), step
                )
                for w in sem_windows:
                    wdur = w["duration"]
                    # Only score semantic windows that match ≈ this duration
                    # bucket to keep normalisation ceilings consistent.
                    if abs(wdur - dur) > 1.5:
                        continue
                    all_candidates.append(_score_window(
                        win_start=w["start"],
                        win_end=w["end"],
                        dur=dur,
                        transcript=transcript,
                        scenes=scenes,
                        audio_segments=audio_segments,
                        face_data=face_data,
                        copyright_segments=copyright_segments,
                        weights=weights,
                        max_words=max_words,
                        max_transitions=max_transitions,
                        extra={
                            "anchor": True,
                            "semantic_boundary": w.get("anchor_boundary"),
                            "semantic_similarity": w.get("semantic_similarity"),
                        },
                    ))
            logger.info(
                f"[SEMANTIC] added {len(sem_windows)} semantic-anchored windows "
                f"to {len(all_candidates)} sliding candidates"
            )

    # Non-maximum suppression (Part D: also enforces topic diversity across
    # the picked clips using a lightweight transcript keyword fingerprint)
    selected = _non_maximum_suppression(
        all_candidates, max_clips, min_gap, transcript=transcript
    )

    # Scene boundary snapping
    if snap_to_scenes:
        _snap_to_scene_boundaries(selected, scenes)

    # Narrative / speaker-turn boundary refinement (Part B + E): after the
    # heuristic picks the best-scoring windows, slide each boundary up to 5 s to
    # a pause / scene-cut (or, for dialogue content, a speaker-turn) boundary so
    # clips open at the start of a new idea and close after it resolves rather
    # than slicing mid-sentence.  Pure heuristic — no LLM needed here; the AI
    # brain adds its own narrative judgment in Stage 5b.
    _refine_narrative_boundaries(selected, transcript, scenes, content_type)

    logger.info(
        f"Clip scoring complete: {len(all_candidates)} candidates evaluated "
        f"({sum(1 for c in all_candidates if c.get('anchor'))} semantic-anchored), "
        f"{len(selected)} clips selected"
    )
    return selected
