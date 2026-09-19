"""
AIClipper Audio Remix Service.

Handles stem separation (Demucs) and remixing:
    keep_original   — source audio with optional master/voice gain adjustment
    mute_music      — isolated vocals only (background music completely muted)
    replace_music   — isolated vocals + layered replacement music (looped & ducked)
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import struct
import tempfile
import wave
from pathlib import Path
from typing import Any

from backend.utils.config import PROJECT_ROOT, get_settings
from backend.utils.logging import timed

logger = logging.getLogger("services.audio_remix")

MODES = ("keep_original", "mute_music", "replace_music")


# ── Helpers ────────────────────────────────────────────────────────────

def _resolve_path(p: str | Path | None) -> Path | None:
    """Resolve a path against PROJECT_ROOT if it is relative."""
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path if path.exists() else None


def _get_or_create_royalty_free_track() -> Path:
    """Return a path to a built-in royalty-free ambient background track.

    If no external track exists, generates a clean, gentle ambient chord loop
    so auto-swap always has a copyright-free track ready out of the box.
    """
    track_dir = PROJECT_ROOT / "assets" / "music"
    track_dir.mkdir(parents=True, exist_ok=True)
    default_track = track_dir / "royalty_free_ambient.wav"

    if default_track.exists() and default_track.stat().st_size > 1000:
        return default_track

    # Generate a clean 10-second 44.1kHz stereo ambient backing loop
    sample_rate = 44100
    duration_s = 10.0
    num_samples = int(sample_rate * duration_s)
    # Soothing chord frequencies: C4 (261.63), E4 (329.63), G4 (392.00), B4 (493.88)
    freqs = [261.63, 329.63, 392.00, 493.88]

    with wave.open(str(default_track), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            t = float(i) / sample_rate
            # Smooth envelope fade in/out
            env = math.sin(math.pi * (i / num_samples))
            sample_val = 0.0
            for f in freqs:
                sample_val += math.sin(2.0 * math.pi * f * t)
            sample_val = (sample_val / len(freqs)) * env * 0.35
            int_val = max(-32767, min(32767, int(sample_val * 32767)))
            packed = struct.pack("<hh", int_val, int_val)
            frames.extend(packed)
        wf.writeframes(frames)

    return default_track


# ── Demucs Stem Separation ─────────────────────────────────────────────

_DEMUCS = None
_DEMUCS_DEVICE: str | None = None


def _load_demucs() -> bool:
    global _DEMUCS, _DEMUCS_DEVICE
    if _DEMUCS is not None:
        return True
    try:
        settings = get_settings()
        from demucs.api import Separator  # type: ignore[import-untyped]

        _DEMUCS = Separator(model=settings.demucs_model, device=settings.demucs_device)
        _DEMUCS_DEVICE = settings.demucs_device
        logger.info(
            f"Demucs loaded: model={settings.demucs_model} device={settings.demucs_device}"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Demucs unavailable ({exc}); remix will use FFmpeg fallback")
        return False


def demucs_available() -> bool:
    return _load_demucs()


def unload_demucs() -> None:
    global _DEMUCS
    _DEMUCS = None
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    logger.debug("Demucs unloaded")


def _register() -> None:
    try:
        from backend.services import model_team
        model_team.register_process("demucs", ram_mb=2200, unload=unload_demucs)
    except Exception:
        pass


_register()


def _save_tensor(tensor: Any, path: Path, samplerate: int) -> None:
    """Save a Demucs stem tensor to WAV, handling 1D, 2D, and 3D shapes safely."""
    t = tensor.detach().cpu()
    if t.dim() == 3:
        # Demucs [batch, channels, samples] -> squeeze batch dim to [channels, samples]
        t = t.squeeze(0)
    elif t.dim() == 1:
        t = t.unsqueeze(0)

    try:
        import torchaudio  # type: ignore[import-untyped]
        torchaudio.save(str(path), t, samplerate)
    except Exception:
        # Fallback to soundfile if torchaudio has format/backend issues
        import soundfile as sf  # type: ignore[import-untyped]
        arr = t.numpy().T
        sf.write(str(path), arr, samplerate)


@timed(logger_name="processing")
def separate_stems(clip: Any, *, force: bool = False) -> dict[str, Path] | None:
    """Separate a clip's audio into vocals and music stems."""
    raw_path = getattr(clip, "output_path", "") or ""
    clip_path = _resolve_path(raw_path) or Path(raw_path)
    clip_id = getattr(clip, "id", "clip")

    if not clip_path.is_file():
        logger.warning(f"Audio Remix: clip audio missing ({clip_path}); skipping stems")
        return None

    stem_dir = clip_path.parent / "stems"
    cached = {
        "vocals": stem_dir / "vocals.wav",
        "music": stem_dir / "music.wav",
    }
    if not force and all(p.is_file() and p.stat().st_size > 1000 for p in cached.values()):
        return cached

    if not _load_demucs():
        return None

    try:
        from backend.services import model_team
        model_team.acquire_process("demucs")
    except Exception:
        pass

    try:
        settings = get_settings()
        sep = _DEMUCS
        stem_dir.mkdir(parents=True, exist_ok=True)

        origin, sources = sep.separate_audio_file(str(clip_path))
        samplerate = getattr(origin, "sample_rate", settings.demucs_sample_rate or 44100)

        # Build combined non-vocal music bus
        music = None
        for name in ("drums", "bass", "other"):
            if name in sources:
                stem_file = stem_dir / f"{name}.wav"
                _save_tensor(sources[name], stem_file, samplerate)
                music = sources[name] if music is None else music + sources[name]

        vocals = sources.get("vocals")
        if vocals is not None:
            _save_tensor(vocals, stem_dir / "vocals.wav", samplerate)

        if music is not None:
            _save_tensor(music, stem_dir / "music.wav", samplerate)

        logger.info(f"Demucs separated stems for clip {clip_id} into {stem_dir}")
        return cached
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Demucs separation failed ({exc}); remix will skip stems")
        return None


# ── FFmpeg Remix Execution ─────────────────────────────────────────────

def _probe_duration(path: Path, ffmpeg: str) -> float | None:
    import json
    import subprocess

    probe = str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else "ffprobe"
    if not Path(probe).is_file():
        probe = "ffprobe"
    try:
        r = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        d = json.loads(r.stdout or "{}").get("format", {}).get("duration")
        val = float(d) if d else None
        return val if (val and val > 0) else None
    except Exception:
        return None


def _build_duck_filter(threshold: float = 0.03, ratio: float = 8.0, attack: float = 20.0, release: float = 300.0) -> str:
    return (
        f"[mraw][v]sidechaincompress="
        f"threshold={threshold}:ratio={ratio}:attack={attack}:release={release}"
        f"[ducked]"
    )


def _build_vocals_only(input_path: Path, output_path: Path, vocals: Path,
                       vocals_gain: float, ffmpeg: str) -> None:
    """Output video with ONLY the isolated vocals stem (music completely silenced)."""
    import subprocess
    cmd = [
        ffmpeg, "-y",
        "-i", str(input_path),
        "-i", str(vocals),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-af", f"volume={vocals_gain:.3f}",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def _build_remux(input_path: Path, output_path: Path, vocals: Path, music_ref: Path,
                 vocals_gain: float, music_gain: float, auto_duck: bool, ffmpeg: str,
                 loop_music: bool = False) -> None:
    """Mix isolated vocals with replacement background music (with auto-ducking)."""
    import subprocess

    dur = _probe_duration(input_path, ffmpeg)
    fn = [
        "-i", str(input_path),
        "-i", str(vocals),
    ]
    if loop_music:
        fn += ["-stream_loop", "-1"]
    fn += [
        "-i", str(music_ref),
        "-map", "0:v:0",
    ]

    fg = f"[1:a]volume={vocals_gain:.3f}[v];[2:a]volume={music_gain:.3f}[mraw];"
    if auto_duck:
        fg += f"{_build_duck_filter()};[v][ducked]amix=inputs=2:normalize=0[aout]"
    else:
        fg += "[v][mraw]amix=inputs=2:normalize=0[aout]"

    fn += ["-filter_complex", fg, "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]

    if loop_music and dur:
        fn += ["-t", f"{dur:.3f}"]
    else:
        fn += ["-shortest"]

    fn += ["-y", str(output_path)]
    cmd = [ffmpeg, "-y"] + fn
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def _run_gain_copy(input_path: Path, output_path: Path, gain: float, ffmpeg: str) -> None:
    """Adjust master audio volume on the video without re-encoding video frames."""
    import subprocess
    cmd = [
        ffmpeg, "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-af", f"volume={gain:.3f}",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def _build_full_replace(input_path: Path, output_path: Path, repl: Path,
                        music_gain: float, ffmpeg: str) -> None:
    """Replace video audio track with the replacement music file."""
    import subprocess

    dur = _probe_duration(input_path, ffmpeg)
    fn = [
        "-i", str(input_path),
        "-stream_loop", "-1",
        "-i", str(repl),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-af", f"volume={music_gain:.3f}",
        "-c:a", "aac", "-b:a", "192k",
    ]
    if dur:
        fn += ["-t", f"{dur:.3f}"]
    else:
        fn += ["-shortest"]
    fn += ["-y", str(output_path)]
    cmd = [ffmpeg, "-y"] + fn
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


# ── Remix Orchestration ────────────────────────────────────────────────

@timed(logger_name="processing")
def remix_audio(
    input_path: Path,
    output_path: Path,
    *,
    mode: str = "keep_original",
    vocals_gain: float = 1.0,
    music_gain: float = 1.0,
    replacement_track: Path | None = None,
    stems: dict[str, Path] | None = None,
    auto_duck: bool = True,
) -> Path:
    settings = get_settings()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = mode if mode in MODES else "keep_original"
    ffmpeg = settings.ffmpeg_path

    # 1. Keep original audio (with volume adjustments if specified)
    if mode == "keep_original":
        effective_gain = vocals_gain if abs(vocals_gain - 1.0) > 1e-3 else music_gain
        if abs(effective_gain - 1.0) > 1e-3:
            _run_gain_copy(input_path, output_path, effective_gain, ffmpeg)
        else:
            shutil.copyfile(str(input_path), str(output_path))
        logger.info(f"Audio Remix: keep_original (gain={effective_gain}) -> {output_path}")
        return output_path

    # 2. Resolve replacement track if in replace_music mode
    resolved_track = None
    if mode == "replace_music":
        if replacement_track:
            resolved_track = _resolve_path(replacement_track) or (Path(replacement_track) if Path(replacement_track).is_file() else None)
        if not resolved_track:
            # Fallback to built-in copyright-free track
            resolved_track = _get_or_create_royalty_free_track()
            logger.info(f"Audio Remix: using built-in copyright-free track: {resolved_track}")

    # 3. Handle cases where stems are unavailable (Demucs not installed/failed)
    if not stems or not stems.get("vocals") or not stems.get("vocals").exists():
        if mode == "replace_music" and resolved_track:
            logger.warning("Audio Remix: stems unavailable — replacing entire audio track with music")
            _build_full_replace(input_path, output_path, resolved_track, music_gain, ffmpeg)
            return output_path
        logger.warning("Audio Remix: stems unavailable for mute/replace; applying volume gain fallback")
        _run_gain_copy(input_path, output_path, vocals_gain, ffmpeg)
        return output_path

    vocals = stems["vocals"]
    music = stems.get("music")

    try:
        if mode == "mute_music":
            # Completely remove background music; output isolated vocals
            _build_vocals_only(input_path, output_path, vocals, vocals_gain, ffmpeg)
            logger.info(f"Audio Remix: mute_music (isolated vocals) -> {output_path}")
        elif mode == "replace_music" and resolved_track:
            # Mix isolated vocals with the replacement track (looped & ducked)
            _build_remux(input_path, output_path, vocals, resolved_track,
                         vocals_gain, music_gain, auto_duck, ffmpeg, loop_music=True)
            logger.info(f"Audio Remix: replace_music ({resolved_track.name}) -> {output_path}")
        else:
            _build_vocals_only(input_path, output_path, vocals, vocals_gain, ffmpeg)

        if not output_path.is_file() or output_path.stat().st_size < 1000:
            raise RuntimeError("remix output empty")
        return output_path
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Audio Remix failed ({exc}); falling back to volume-adjusted original")
        _run_gain_copy(input_path, output_path, vocals_gain, ffmpeg)
        return output_path
