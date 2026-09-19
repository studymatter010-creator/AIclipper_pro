"""
AIClipper AI Brain — Enhanced with Virality Framework

A lightweight, local, intelligent "brain" that makes the clipping more
premium.  It runs on a local model (Ollama / ``qwen3:8b`` by default, via
``settings.ollama_model``) and reasons over the *meaning* of what is being
said to refine which segments make the strongest short-form clips.

Heuristic scoring already catches busy audio / faces / scene cuts.  What it
can't judge is *narrative* value: "is this the dramatic payoff?", "does this
moment hook a viewer in the first 3 seconds?".  The brain reads the transcript
for each candidate clip and rates it, then the heuristic score and the brain
score are blended so the best clips bubble to the top.

Enhanced with a structured virality framework (8 ranked signals) ported from
AI-Youtube-Shorts-Generator.  Each candidate now also receives a
``hook_sentence`` and ``virality_reason`` for downstream caption/description
use.

Long videos (>30 min) are chunked into 20-min segments with 60s overlap so
the LLM context stays manageable.

Everything degrades gracefully: if Ollama is unreachable, clip selection
simply falls back to the heuristic scores unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("services.ai_brain")

# How much weight the brain's semantic rating carries vs the heuristics.
_BRAIN_WEIGHT = 0.45

# Within the brain's rating, how much weight goes to overall virality vs the
# new narrative-completeness dimension (Part B).
_VIRALITY_WEIGHT = 0.6
_NARRATIVE_WEIGHT = 0.4


def _as_float(value: Any, default: float = 0.0) -> float:
    """Robustly coerce an LLM JSON field to a float (or ``default``)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# Long-video chunking constants (ported from AI-Youtube-Shorts-Generator)
CHUNK_SIZE_SECONDS = 1200       # 20-min chunks
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60

# ── Virality Framework ────────────────────────────────────────────────
# 8 ranked signals that make short-form content go viral.  Ported from
# AI-Youtube-Shorts-Generator and integrated into the prompt.

VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""

_SYSTEM_PROMPT = (
    "You are the world's best short-form video editor (YouTube Shorts / "
    "TikTok / Reels). A human expert has already flagged candidate segments "
    "of a longer video. For EACH candidate, rate how well it would hold a "
    "scrolling viewer's attention as a standalone 1-minute clip.\n\n"
    "In addition to the virality signals below, judge each candidate's "
    "NARRATIVE COMPLETENESS (0-10): does this window contain a COMPLETE "
    "narrative unit — a setup/tease near the start and a payoff, resolution, "
    "or punchline near the end — or is it an arbitrary slice of a longer "
    "thought?  A clip that starts mid-setup and ends before the payoff feels "
    "unsatisfying.  If shifting the clip's start or END by a few seconds "
    "would better capture the full setup-to-payoff arc, suggest small "
    "shift_start / shift_end values in seconds (positive = move later / "
    "extend the clip, negative = move earlier / trim it; keep them between "
    "-5 and +5; use 0 to leave a boundary unchanged).\n\n"
    f"{VIRALITY_CRITERIA}\n"
    "Reward segments that score high on these signals AND form a complete "
    "narrative arc. Punish slow, rambling, context-dependent, or "
    "emotionally/structurally INCOMPLETE segments (e.g. punchline cut off, "
    "setup missing)."
)

# ── Content-type-aware prompt suffixes ────────────────────────────────
_CONTENT_TYPE_HINTS: dict[str, str] = {
    "podcast": "This is a PODCAST. Prioritize emotional reactions, hot takes, surprising confessions, and quotable one-liners.",
    "interview": "This is an INTERVIEW. Prioritize revealing answers, unexpected opinions, tension between speakers, and quotable moments.",
    "tutorial": "This is a TUTORIAL. Prioritize 'aha' moments, surprising tips, practical takeaways, and clear explanations.",
    "lecture": "This is a LECTURE. Prioritize surprising facts, reframe moments, practical insights, and clear explanations.",
    "vlog": "This is a VLOG. Prioritize emotional peaks, funny moments, surprising reveals, and relatable content.",
    "commentary": "This is COMMENTARY. Prioritize strong opinions, surprising takes, controversy, and quotable statements.",
    "debate": "This is a DEBATE. Prioritize tension, mic-drop moments, surprising arguments, and emotional reactions.",
}


def _clip_window_text(transcript: dict[str, Any], start: float, end: float) -> str:
    """Concatenate transcript text that falls inside [start, end]."""
    words = transcript.get("words") or []
    parts: list[str] = []
    for w in words:
        if start <= w.get("start", 0) <= end:
            parts.append(str(w.get("word", "")))
    if parts:
        return " ".join(parts)
    for seg in transcript.get("segments") or []:
        s = seg.get("start", 0)
        if start <= s <= end:
            parts.append(str(seg.get("text", "")))
    return " ".join(parts).strip()


def _build_prompt(
    candidates: list[dict[str, Any]],
    transcript: dict[str, Any],
    content_type: str | None = None,
) -> str:
    lines = [
        "Number each candidate and respond with a JSON array of objects: "
        '{"id": <candidate number>, "score": <0-10>, "reason": "<one short phrase>", '
        '"hook_sentence": "<best opening line to stop scrolling>", '
        '"virality_reason": "<one sentence explaining why this clip should go viral>", '
        '"narrative_completeness": <0-10>, '
        '"shift_start": <seconds, -5..+5, 0=leave>, '
        '"shift_end": <seconds, -5..+5, 0=leave>}. '
        "Reply with ONLY valid JSON, no markdown, no extra text.\n\nCandidates:"
    ]
    for i, cand in enumerate(candidates, start=1):
        text = _clip_window_text(transcript, cand.get("start", 0), cand.get("end", 0))
        snippet = text[:260] if text else "(no transcript available)"
        lines.append(
            f"{i}. [{cand.get('start', 0):.1f}s-{cand.get('end', 0):.1f}s]: {snippet}"
        )
    return "\n".join(lines)


def _parse_brain_response(text: str) -> dict[int, dict[str, Any]]:
    """
    Parse the LLM JSON response into {index: {score, hook_sentence, virality_reason}}.
    """
    text = text.strip()
    # Strip markdown fences if the model added them.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON array substring as a fallback.
        m = re.search(r"\[[^\]]*\]", text, re.DOTALL)
        if not m:
            logger.warning("AI brain returned unparseable response")
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    result: dict[int, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                idx = item.get("id")
                score = item.get("score")
                try:
                    result[int(idx)] = {
                        "score": float(score),
                        "hook_sentence": str(item.get("hook_sentence", "")).strip(),
                        "virality_reason": str(item.get("virality_reason", "")).strip(),
                        "narrative_completeness": _as_float(item.get("narrative_completeness"), 0.0),
                        "shift_start": _as_float(item.get("shift_start"), 0.0),
                        "shift_end": _as_float(item.get("shift_end"), 0.0),
                    }
                except (TypeError, ValueError):
                    continue
    return result


def _blend_brain(virality_score: float, narrative_score: float) -> float:
    """Combine the virality and narrative-completeness ratings into one 0-1 value.

    0-10 inputs → 0-1 output.  Virality is the dominant factor (0.7) with
    narrative completeness as a meaningful secondary signal (0.3).
    """
    virality_norm = max(0.0, min(_as_float(virality_score, 0.0) / 10.0, 1.0))
    narrative_norm = max(0.0, min(_as_float(narrative_score, 0.0) / 10.0, 1.0))
    return round(_VIRALITY_WEIGHT * virality_norm + _NARRATIVE_WEIGHT * narrative_norm, 4)


def _apply_narrative_shift(
    cand: dict[str, Any],
    shift_start: Any,
    shift_end: Any,
    video_duration: float,
    max_shift: float = 5.0,
    min_duration: float = 30.0,
) -> None:
    """Apply the brain's suggested small boundary shift to a candidate clip.

    Both shifts are clamped to ``±max_shift`` seconds and to the source timeline
    ``[0, video_duration]``.  A minimum duration is enforced so a suggested trim
    can't collapse the clip.  No-op if the brain suggested nothing (0).
    """
    def _clamp(v: Any) -> float:
        f = _as_float(v, 0.0)
        if f == 0.0:
            return 0.0
        return max(-max_shift, min(max_shift, f))

    ss = _clamp(shift_start)
    se = _clamp(shift_end)
    if ss == 0.0 and se == 0.0:
        return

    start = float(cand.get("start", 0.0))
    end = float(cand.get("end", 0.0))
    dur = end - start

    max_end = video_duration if video_duration and video_duration > 0 else end + max_shift
    new_start = max(0.0, min(start + ss, max_end - 0.001))
    new_end = max(new_start + 0.001, min(end + se, max_end))

    # Enforce minimum duration — prefer extending toward the shifted side that
    # wants more length, but never below the floor within the available bounds.
    if new_end - new_start < min_duration:
        if new_start + min_duration <= max_end:
            new_end = new_start + min_duration
        elif new_end - min_duration >= 0.0:
            new_start = new_end - min_duration
        else:
            return  # give up gracefully; keep the existing boundary

    cand["start"] = round(new_start, 3)
    cand["end"] = round(new_end, 3)
    cand["duration"] = round(new_end - new_start, 3)


def _brain_scan(
    candidates: list[dict[str, Any]],
    transcript: dict[str, Any],
    model: str,
    content_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Best-effort synchronous scan of every candidate via Ollama (chunked).

    Returns a list parallel to ``candidates``, each element being:
    ``{"score": float, "hook_sentence": str, "virality_reason": str}``
    """
    empty = {
        "score": 0.0,
        "hook_sentence": "",
        "virality_reason": "",
        "narrative_completeness": 0.0,
        "shift_start": 0.0,
        "shift_end": 0.0,
    }
    scores: list[dict[str, Any]] = [empty.copy() for _ in candidates]
    if not candidates:
        return scores
    if not transcript.get("segments") and not transcript.get("words"):
        logger.info("AI brain skipped: no transcript available")
        return scores

    # Build content-type-aware system prompt
    system_prompt = _SYSTEM_PROMPT
    if content_type and content_type in _CONTENT_TYPE_HINTS:
        system_prompt += f"\n\n{_CONTENT_TYPE_HINTS[content_type]}"

    chunk_size = 12
    for start_idx in range(0, len(candidates), chunk_size):
        chunk = candidates[start_idx : start_idx + chunk_size]
        prompt = _build_prompt(chunk, transcript, content_type)

        # Route through the model TEAM so the call benefits from Ollama's
        # native format:"json" structured output AND the single automatic retry
        # (a transient blip now recovers instead of degrading to heuristics).
        from backend.services.model_team import chat_json

        data = chat_json(
            "brain",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            num_predict=1536,
            retries=1,
            retry_delay=1.5,
        )
        if data is None:
            logger.info("AI brain offline after retry; using heuristic scores only")
            return [empty.copy() for _ in candidates]

        # chat_json returns either the structured list directly, or (if an
        # older Ollama ignores format=json) a dict / raw text — the loose
        # parser is kept as a final safety net for that case.
        if isinstance(data, list):
            parsed = {
                int(i) + 1: row
                for i, row in enumerate(data)
                if isinstance(row, dict)
            }
        else:
            parsed = _parse_brain_response(json.dumps(data) if not isinstance(data, dict) else json.dumps(data))
        for item in chunk:
            idx = candidates.index(item)
            brain_data = parsed.get(idx - start_idx + 1, {})
            scores[idx] = {
                "score": _as_float(brain_data.get("score"), 0.0),
                "hook_sentence": brain_data.get("hook_sentence", ""),
                "virality_reason": brain_data.get("virality_reason", ""),
                "narrative_completeness": _as_float(
                    brain_data.get("narrative_completeness"), 0.0
                ),
                "shift_start": _as_float(brain_data.get("shift_start"), 0.0),
                "shift_end": _as_float(brain_data.get("shift_end"), 0.0),
            }

    return scores


def refine_clip_scores(
    candidates: list[dict[str, Any]],
    transcript: dict[str, Any],
    content_type: str | None = None,
    video_duration: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Blend the AI brain's semantic ratings into the candidate clips' scores.

    For long videos (>30 min), the transcript is chunked into 20-min segments
    with 60s overlap to keep LLM context manageable.

    Modifies each candidate in-place (adds ``brain_score``, ``hook_sentence``,
    ``virality_reason``, and updates ``total_score``) and returns the list,
    preserving order.  When Ollama is unreachable the candidates are returned
    unchanged.

    Args:
        candidates: heuristic-scored clips (each has ``start``, ``end``,
            ``total_score``).
        transcript: transcription dict with ``segments``/``words``.
        content_type: optional content type for genre-aware prompting.
        video_duration: total video duration in seconds.

    Returns:
        The same list with blended scores.
    """
    if not candidates:
        return candidates

    settings = get_settings()

    # ── Cache: re-scoring the SAME video (dev/testing) should not re-run the
    # 8B brain inference.  The cache stores the per-candidate semantic ratings
    # (keyed on a hash of the transcript + content type), then re-applies them
    # to the current candidates.
    from backend.utils.cache import get as cache_get, set as cache_set, transcript_hash
    full_text = transcript.get("full_text") or " ".join(
        str(s.get("text", "")) for s in transcript.get("segments", [])
    )
    cache_key = (
        f"brain:{transcript_hash(full_text)}:{content_type or ''}:"
        f"{'chunked' if (video_duration >= LONG_VIDEO_THRESHOLD and video_duration > 0) else 'full'}"
    )
    cached_ratings = cache_get(cache_key)
    if cached_ratings is not None and isinstance(cached_ratings, list):
        logger.info("AI brain ratings loaded from cache (no 8B inference)")
        for idx, cand in enumerate(candidates):
            if idx >= len(cached_ratings):
                break
            r = cached_ratings[idx]
            score = r.get("score", 0.0)
            narrative = r.get("narrative_completeness", 0.0)
            hook = r.get("hook_sentence", "")
            virality = r.get("virality_reason", "")
            shift_s = r.get("shift_start", 0.0)
            shift_e = r.get("shift_end", 0.0)
            combined = _blend_brain(score, narrative)
            if combined > 0:
                cand["brain_score"] = round(combined, 3)
                cand["hook_sentence"] = hook
                cand["virality_reason"] = virality
                cand["narrative_completeness"] = _as_float(narrative, 0.0)
                _apply_narrative_shift(cand, shift_s, shift_e, video_duration)
                cand["total_score"] = round(
                    cand.get("total_score", 0.0) * (1 - _BRAIN_WEIGHT)
                    + combined * _BRAIN_WEIGHT,
                    3,
                )
        return candidates

    # For long videos, chunk the transcript and process per-chunk
    if video_duration >= LONG_VIDEO_THRESHOLD and video_duration > 0:
        logger.info(f"Long video ({video_duration:.0f}s) — chunking transcript for AI brain")
        brain_scores = _brain_scan_chunked(candidates, transcript, settings.ollama_model, content_type, video_duration)
    else:
        brain_scores = _brain_scan(candidates, transcript, settings.ollama_model, content_type)

    used = 0
    for idx, cand in enumerate(candidates):
        brain_data = (
            brain_scores[idx]
            if idx < len(brain_scores)
            else {
                "score": 0.0,
                "hook_sentence": "",
                "virality_reason": "",
                "narrative_completeness": 0.0,
                "shift_start": 0.0,
                "shift_end": 0.0,
            }
        )
        # Blend virality + narrative-completeness into a single 0-1 rating
        # (narrative completeness is the 9th, structurally-aware dimension).
        brain_norm = _blend_brain(
            brain_data.get("score", 0.0),
            brain_data.get("narrative_completeness", 0.0),
        )
        if brain_norm > 0:
            used += 1
            cand["brain_score"] = round(brain_norm, 3)
            cand["narrative_completeness"] = round(
                max(0.0, min(_as_float(brain_data.get("narrative_completeness"), 0.0) / 10.0, 1.0)),
                3,
            )
            # Apply the brain's suggested small boundary adjustment (clamped to
            # ±5 s) BEFORE finalizing the cut, so a start/end tweak that would
            # better capture the setup→payoff arc actually lands.
            _apply_narrative_shift(
                cand,
                brain_data.get("shift_start", 0.0),
                brain_data.get("shift_end", 0.0),
                video_duration,
            )
            heuristic = cand.get("total_score", 0.0)
            cand["total_score"] = round(
                heuristic * (1 - _BRAIN_WEIGHT) + brain_norm * _BRAIN_WEIGHT, 3
            )
        # Always carry forward hook_sentence and virality_reason
        hook = brain_data.get("hook_sentence", "")
        virality = brain_data.get("virality_reason", "")
        if hook:
            cand["hook_sentence"] = hook
        if virality:
            cand["virality_reason"] = virality

    if used:
        logger.info(
            f"AI brain blended semantic ratings into {used}/{len(candidates)} clips"
        )
        candidates.sort(key=lambda c: c.get("total_score", 0.0), reverse=True)
    else:
        logger.info("AI brain produced no ratings; keeping heuristic order")

    # Persist the brain ratings to the inference cache so re-processing this
    # video skips the 8B inference next time (best-effort).
    try:
        ratings = []
        for cand in candidates:
            ratings.append({
                "score": cand.get("brain_score", 0.0),
                "hook_sentence": cand.get("hook_sentence", ""),
                "virality_reason": cand.get("virality_reason", ""),
                "narrative_completeness": cand.get("narrative_completeness", 0.0),
                "shift_start": 0.0,
                "shift_end": 0.0,
            })
        cache_set(cache_key, ratings)
    except Exception:  # noqa: BLE001 - caching is best-effort
        pass

    # ── Team cooperation: dedicated hook specialist refines the opening line ──
    # The brain scores/selects; then the light "hook" expert crafts the single
    # best stop-scroll line (fast specialist, brain not held resident).  Falls
    # back to the brain's hook_sentence on failure.
    #
    # Change 2 (2026-09-12): the "top clips only" gate was an arbitrary choice —
    # thumbnails consume hook_sentence on EVERY clip (via the AI Editor / poster
    # generator), so every candidate in the final set gets a crafted stop-scroll
    # line, not just the top 3.  Keep a sane bound so a max_clips bump doesn't
    # extend pipeline latency unbounded.
    polished = 0
    try:
        from backend.services.model_team import analyze_hook

        for cand in candidates[:8]:
            text = _clip_window_text(transcript, cand.get("start", 0), cand.get("end", 0))
            if not text:
                continue
            hook_line = analyze_hook(text)
            if hook_line:
                cand["hook_sentence"] = hook_line
                polished += 1
    except Exception as exc:  # noqa: BLE001
        logger.info(f"hook specialist unavailable ({exc}); keeping brain's hooks")
    logger.info(f"hook specialist polished {polished} candidate hook_sentences")

    return candidates


def _brain_scan_chunked(
    candidates: list[dict[str, Any]],
    transcript: dict[str, Any],
    model: str,
    content_type: str | None,
    video_duration: float,
) -> list[dict[str, Any]]:
    """
    For long videos, chunk the transcript and process each chunk separately.

    Each candidate is assigned to the chunk that contains its midpoint.
    Timestamps in the prompt are relative to the chunk, but the results
    are mapped back to the original candidate indices.
    """
    empty = {
        "score": 0.0,
        "hook_sentence": "",
        "virality_reason": "",
        "narrative_completeness": 0.0,
        "shift_start": 0.0,
        "shift_end": 0.0,
    }
    scores: list[dict[str, Any]] = [empty.copy() for _ in candidates]

    segments = transcript.get("segments", [])
    if not segments:
        return scores

    # Build chunks
    chunks: list[dict[str, Any]] = []
    pos = 0.0
    while pos < video_duration:
        chunk_end = min(pos + CHUNK_SIZE_SECONDS, video_duration)
        chunk_segs = [
            s for s in segments
            if s.get("start", 0) >= pos - CHUNK_OVERLAP_SECONDS
            and s.get("end", s.get("start", 0)) <= chunk_end + CHUNK_OVERLAP_SECONDS
        ]
        if chunk_segs:
            chunks.append({
                "segments": chunk_segs,
                "duration": chunk_end - pos,
                "offset": pos,
            })
        pos += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS

    if not chunks:
        return scores

    logger.info(f"AI brain chunking: {len(chunks)} chunks for {video_duration:.0f}s video")

    # Assign each candidate to the chunk containing its midpoint
    for idx, cand in enumerate(candidates):
        midpoint = (cand.get("start", 0) + cand.get("end", 0)) / 2.0

        # Find the best chunk
        best_chunk = None
        for chunk in chunks:
            offset = chunk["offset"]
            chunk_dur = chunk["duration"]
            if offset <= midpoint <= offset + chunk_dur:
                best_chunk = chunk
                break

        if best_chunk is None:
            # Fallback: use the closest chunk
            best_chunk = min(chunks, key=lambda c: abs(c["offset"] - midpoint))

        # Build a local transcript for this candidate within the chunk
        local_transcript = {"segments": best_chunk["segments"], "words": transcript.get("words", [])}

        # Scan just this one candidate
        result = _brain_scan([cand], local_transcript, model, content_type)
        if result:
            scores[idx] = result[0]

    return scores
