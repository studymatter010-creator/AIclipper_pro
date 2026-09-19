"""
AIClipper Audio Analysis Service

Analyses audio for energy spikes, laughter cues, crowd reactions, and emotion
intensity.  All scores are normalised to the 0-1 range.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from backend.utils.logging import get_logger, timed

logger = get_logger("services.audio_analysis")


def _safe_normalize(values: np.ndarray) -> np.ndarray:
    """Min-max normalise an array to [0, 1].  Returns zeros on empty/constant input."""
    if values.size == 0:
        return values
    vmin, vmax = float(values.min()), float(values.max())
    if vmax - vmin < 1e-9:
        return np.zeros_like(values)
    return (values - vmin) / (vmax - vmin)


@timed(logger_name="processing")
def analyze_audio(
    audio_path: Path,
    segment_duration: float = 5.0,
) -> list[dict[str, Any]]:
    """
    Analyse an audio file and return per-segment energy, laughter, crowd
    reaction, and emotion intensity metrics.

    Args:
        audio_path: Path to a WAV or other librosa-compatible audio file.
        segment_duration: Length (seconds) of each analysis window.

    Returns:
        A list of analysis dicts, one per segment::

            [
                {
                    "start": float,
                    "end": float,
                    "energy_score": float,       # 0-1
                    "laughter_detected": bool,
                    "crowd_reaction": bool,
                    "emotion_intensity": float,  # 0-1
                },
                ...
            ]

        Returns an empty list if *librosa* is not installed or the file
        cannot be loaded.
    """
    # ── 1. Import librosa ───────────────────────────────────────────────
    try:
        import librosa  # type: ignore[import-untyped]
    except ImportError:
        logger.error(
            "librosa is not installed. Install with: pip install librosa"
        )
        return []

    logger.info(
        f"Analysing audio '{audio_path.name}' "
        f"(segment_duration={segment_duration}s)"
    )

    # ── 2. Load audio ──────────────────────────────────────────────────
    try:
        y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    except Exception as exc:
        logger.error(f"Failed to load audio: {exc}", exc_info=True)
        return []

    total_duration: float = float(len(y)) / sr
    if total_duration < 0.1:
        logger.warning("Audio is too short for analysis.")
        return []

    # ── 3. Compute per-sample features ─────────────────────────────────
    hop_length = 512
    frame_length = 2048

    # RMS energy
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]

    # Spectral centroid & bandwidth (used for laughter heuristic)
    spectral_centroid = librosa.feature.spectral_centroid(
        y=y, sr=sr, hop_length=hop_length
    )[0]
    spectral_bandwidth = librosa.feature.spectral_bandwidth(
        y=y, sr=sr, hop_length=hop_length
    )[0]

    # Pitch via pyin for emotion intensity
    try:
        f0, voiced_flag, _ = librosa.pyin(
            y,
            fmin=librosa.note_to_hz("C2"),
            fmax=librosa.note_to_hz("C7"),
            sr=sr,
            hop_length=hop_length,
        )
    except Exception:
        f0 = np.zeros(len(rms))
        voiced_flag = np.zeros(len(rms), dtype=bool)

    # Replace NaN pitch values with 0
    f0 = np.nan_to_num(f0, nan=0.0)

    # Frame timestamps in seconds
    frame_times = librosa.frames_to_time(
        np.arange(len(rms)), sr=sr, hop_length=hop_length
    )

    # ── 3.5. Onset strength (pacing / rhythmic intensity) ─────────────
    # Counts transient "events" per second in the audio.  High onset density
    # (rhythmic music, rapid speech, percussion) reads as energetic, fast
    # pacing — which keeps short-form viewers glued.  This becomes the
    # ``pacing_score`` signal used by the clip scorer.
    try:
        onset_env = librosa.onset.onset_strength(
            y=y, sr=sr, hop_length=hop_length
        )
        on_frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, hop_length=hop_length
        )
        onset_times = librosa.frames_to_time(
            on_frames, sr=sr, hop_length=hop_length
        )
    except Exception:
        onset_times = np.array([])

    # ── 4. Segment-level aggregation ───────────────────────────────────
    n_segments = max(1, int(np.ceil(total_duration / segment_duration)))

    seg_energy = np.zeros(n_segments)
    seg_centroid_mean = np.zeros(n_segments)
    seg_bw_mean = np.zeros(n_segments)
    seg_pitch_std = np.zeros(n_segments)
    seg_energy_std = np.zeros(n_segments)
    seg_onsets = np.zeros(n_segments)

    for i in range(n_segments):
        t_start = i * segment_duration
        t_end = min((i + 1) * segment_duration, total_duration)
        mask = (frame_times >= t_start) & (frame_times < t_end)

        if mask.sum() == 0:
            continue

        seg_energy[i] = float(np.mean(rms[mask]))
        seg_centroid_mean[i] = float(np.mean(spectral_centroid[mask]))
        seg_bw_mean[i] = float(np.mean(spectral_bandwidth[mask]))

        pitch_slice = f0[mask] if len(f0) >= len(rms) else f0[mask[:len(f0)]]
        voiced = pitch_slice[pitch_slice > 0]
        seg_pitch_std[i] = float(np.std(voiced)) if len(voiced) > 2 else 0.0
        seg_energy_std[i] = float(np.std(rms[mask]))

        # Count onset events that fall inside this window.
        onset_mask = (onset_times >= t_start) & (onset_times < t_end)
        seg_onsets[i] = float(onset_mask.sum())

    # ── 5. Normalise scores ────────────────────────────────────────────
    energy_norm = _safe_normalize(seg_energy)
    pitch_std_norm = _safe_normalize(seg_pitch_std)
    energy_std_norm = _safe_normalize(seg_energy_std)
    # Onset density, normalised to events-per-second so segment length doesn't
    # bias the score.  Falls back to energy as a proxy when no onsets found.
    onset_density = np.zeros(n_segments)
    ipa = np.zeros(n_segments)
    for i in range(n_segments):
        t_start = i * segment_duration
        t_end = min((i + 1) * segment_duration, total_duration)
        seg_len = max(t_end - t_start, 0.1)
        ipa[i] = seg_onsets[i] / seg_len
    if float(ipa.max()) > 0:
        onset_density = _safe_normalize(ipa)
    else:
        onset_density = energy_norm.copy()

    # Emotion intensity ≈ combination of pitch variation and energy dynamics
    emotion_raw = 0.6 * pitch_std_norm + 0.4 * energy_std_norm
    emotion_norm = _safe_normalize(emotion_raw)

    # Thresholds
    max_energy = float(seg_energy.max()) if seg_energy.max() > 0 else 1.0
    energy_spike_threshold = 0.65  # fraction of max → "spike"

    # Laughter heuristic: high spectral centroid + wide bandwidth + energy
    centroid_thresh = float(np.percentile(seg_centroid_mean[seg_centroid_mean > 0], 75)) \
        if (seg_centroid_mean > 0).sum() > 0 else 1e9
    bw_thresh = float(np.percentile(seg_bw_mean[seg_bw_mean > 0], 75)) \
        if (seg_bw_mean > 0).sum() > 0 else 1e9

    # ── 6. Build output ────────────────────────────────────────────────
    results: list[dict[str, Any]] = []
    for i in range(n_segments):
        t_start = round(i * segment_duration, 3)
        t_end = round(min((i + 1) * segment_duration, total_duration), 3)

        laughter = bool(
            seg_centroid_mean[i] > centroid_thresh
            and seg_bw_mean[i] > bw_thresh
            and energy_norm[i] > 0.4
        )
        crowd = bool(
            seg_energy[i] > energy_spike_threshold * max_energy
            and seg_bw_mean[i] > bw_thresh
        )

        results.append({
            "start": t_start,
            "end": t_end,
            "energy_score": round(float(energy_norm[i]), 4),
            "laughter_detected": laughter,
            "crowd_reaction": crowd,
            "emotion_intensity": round(float(emotion_norm[i]), 4),
            "pacing_score": round(float(onset_density[i]), 4),
        })

    logger.info(
        f"Audio analysis complete: {n_segments} segments, "
        f"{sum(1 for r in results if r['laughter_detected'])} laughter, "
        f"{sum(1 for r in results if r['crowd_reaction'])} crowd reactions"
    )
    return results


@timed(logger_name="processing")
def extract_energy_envelope(
    video_path: Path,
    window_seconds: float = 0.5,
) -> list[float]:
    """
    Return a per-window RMS energy envelope as a list of floats in [0, 1].

    Used by the auto-editor to make the camera micro-zoom beat-reactive: a
    fallback that does not need a prior audio pass.  Returns a damped, peak
    normalized envelope so quiet driving tracks still register motion without
    blowing out on a single loud spike.
    """
    try:
        import librosa  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        y, sr = librosa.load(str(video_path), sr=None, mono=True)
    except Exception:
        return []

    hop_length = 512
    frame_length = 2048
    rms = librosa.feature.rms(
        y=y, frame_length=frame_length, hop_length=hop_length
    )[0]
    frame_times = librosa.frames_to_time(
        np.arange(len(rms)), sr=sr, hop_length=hop_length
    )
    total_duration = float(len(y)) / sr
    if total_duration < 0.1:
        return []

    n_windows = max(1, int(np.ceil(total_duration / window_seconds)))
    env = np.zeros(n_windows)
    for i in range(n_windows):
        t0 = i * window_seconds
        t1 = min((i + 1) * window_seconds, total_duration)
        mask = (frame_times >= t0) & (frame_times < t1)
        if mask.sum() == 0:
            continue
        env[i] = float(np.mean(rms[mask]))

    norm = _safe_normalize(env)
    # Damp extreme spikes so the zoom reacts to *sections*, not single hits.
    return [round(float(v), 4) for v in np.clip(norm, 0.0, 0.9)]
