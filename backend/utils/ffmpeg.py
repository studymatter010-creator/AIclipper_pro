"""
AIClipper FFmpeg Utilities

Reusable FFmpeg operations: probing, cutting, cropping, subtitle burning,
frame extraction, and format conversion. All operations use subprocess
for maximum cross-platform reliability.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed

logger = get_logger("ffmpeg")


def _get_creation_flags() -> int:
    """Return subprocess creation flags to suppress console windows on Windows."""
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _run_ffmpeg(args: list[str], description: str = "FFmpeg operation") -> subprocess.CompletedProcess:
    """
    Run an FFmpeg command with standard error handling.

    Returns the CompletedProcess result.
    Raises RuntimeError on failure.
    """
    settings = get_settings()
    cmd = [settings.ffmpeg_path] + args
    logger.debug(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
            creationflags=_get_creation_flags(),
        )
        if result.returncode != 0:
            logger.error(f"{description} failed: {result.stderr[:500]}")
            raise RuntimeError(f"{description} failed: {result.stderr[:500]}")
        return result
    except FileNotFoundError:
        raise RuntimeError(
            f"FFmpeg not found at '{settings.ffmpeg_path}'. "
            "Please install FFmpeg and ensure it's in your PATH."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{description} timed out after 1 hour.")


def check_ffmpeg_installed() -> bool:
    """Check if FFmpeg is available on the system."""
    settings = get_settings()
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_get_creation_flags(),
        )
        if result.returncode == 0:
            version_line = result.stdout.split("\n")[0]
            logger.info(f"FFmpeg found: {version_line}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    logger.error("FFmpeg not found on system")
    return False


@timed(logger_name="processing")
def extract_audio(
    video_path: Path,
    output_path: Path,
    sample_rate: int = 16000,
    mono: bool = True,
) -> Path:
    """
    Extract audio from video as WAV file.

    Args:
        video_path: Input video file path
        output_path: Output WAV file path
        sample_rate: Audio sample rate (default 16kHz for Whisper)
        mono: Convert to mono (default True for Whisper)

    Returns:
        Path to the extracted audio file
    """
    args = [
        "-i", str(video_path),
        "-vn",                          # No video
        "-acodec", "pcm_s16le",         # 16-bit PCM WAV
        "-ar", str(sample_rate),        # Sample rate
    ]
    if mono:
        args.extend(["-ac", "1"])       # Mono channel
    args.extend([
        "-y",                           # Overwrite
        str(output_path),
    ])

    _run_ffmpeg(args, f"Audio extraction from {video_path.name}")
    logger.info(f"Extracted audio: {output_path.name} ({sample_rate}Hz, {'mono' if mono else 'stereo'})")
    return output_path


@timed(logger_name="processing")
def cut_clip(
    input_path: Path,
    output_path: Path,
    start_time: float,
    duration: float,
    reencode: bool = False,
) -> Path:
    """
    Cut a clip from a video file.

    Args:
        input_path: Source video file
        output_path: Output clip file
        start_time: Start time in seconds
        duration: Clip duration in seconds
        reencode: If True, re-encode (slower but more precise cuts)

    Returns:
        Path to the generated clip
    """
    args = [
        "-ss", f"{start_time:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
    ]
    if reencode:
        settings = get_settings()
        args.extend([
            "-c:v", settings.output_settings.codec,
            "-crf", str(settings.output_settings.crf),
            "-preset", settings.output_settings.preset,
            "-c:a", settings.output_settings.audio_codec,
            "-b:a", settings.output_settings.audio_bitrate,
        ])
    else:
        args.extend(["-c", "copy"])

    args.extend(["-avoid_negative_ts", "1", "-y", str(output_path)])

    _run_ffmpeg(args, f"Clip cut {start_time:.1f}s-{start_time+duration:.1f}s")
    return output_path


@timed(logger_name="processing")
def convert_vertical(
    input_path: Path,
    output_path: Path,
    crop_x: int | None = None,
    crop_y: int | None = None,
    crop_w: int | None = None,
    crop_h: int | None = None,
) -> Path:
    """
    Convert a video to vertical (1080x1920) format.

    If crop coordinates are provided, crops to those coordinates first.
    Otherwise, scales and pads to fit 1080x1920.

    Returns:
        Path to the converted video
    """
    settings = get_settings()
    w = settings.output_settings.width
    h = settings.output_settings.height

    if crop_x is not None and crop_w is not None:
        # Crop then scale to output resolution
        vf = (
            f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
        )
    else:
        # Scale maintaining aspect ratio, pad to fill
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
        )

    args = [
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", settings.output_settings.codec,
        "-crf", str(settings.output_settings.crf),
        "-preset", settings.output_settings.preset,
        "-c:a", settings.output_settings.audio_codec,
        "-ac", "2",  # Downmix to stereo (AAC encoder can't handle 5.1)
        "-b:a", settings.output_settings.audio_bitrate,
        "-r", str(settings.output_settings.fps),
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Vertical conversion of {input_path.name}")
    return output_path


@timed(logger_name="processing")
def reencode_clip(
    input_path: Path,
    output_path: Path,
) -> Path:
    """
    Re-encode a clip keeping the original aspect ratio.

    Applies H.264/AAC encoding with stereo downmix but preserves
    the source video dimensions and aspect ratio.

    Returns:
        Path to the re-encoded video
    """
    settings = get_settings()

    args = [
        "-i", str(input_path),
        "-c:v", settings.output_settings.codec,
        "-crf", str(settings.output_settings.crf),
        "-preset", settings.output_settings.preset,
        "-c:a", settings.output_settings.audio_codec,
        "-ac", "2",  # Downmix to stereo (AAC encoder can't handle 5.1)
        "-b:a", settings.output_settings.audio_bitrate,
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Re-encode of {input_path.name}")
    return output_path


@timed(logger_name="processing")
def convert_shorts_format(
    input_path: Path,
    output_path: Path,
) -> Path:
    """
    Convert a clip to the configured vertical canvas (default 1080x1920, 9:16)
    using a "fit with blurred background" layout.

    The ENTIRE source frame is always kept visible (never cropped):
      * background = the source scaled up to fill the canvas, then blurred
      * foreground = the source scaled DOWN to fit within the canvas (whole frame)
      * overlay centers the foreground; any leftover bars are filled by the blur

    Handles every source aspect ratio gracefully:
      * 16:9 / landscape  -> letterboxed top & bottom with blurred fill
      * 9:16 / portrait   -> fills the canvas exactly (no bars)
      * square / other    -> blurred fill in both axes
    """
    from backend.utils.validators import probe_video
    settings = get_settings()

    # Probe validates the input decodes before we build the filter graph.
    probe_video(input_path)

    w = settings.output_settings.width
    h = settings.output_settings.height

    # The background is the source scaled up to fill the canvas; on landscape
    # sources the upscale factor is large, so a mild boxblur still leaves a
    # RECOGNIZABLE (duplicate) face in the bars — the "blurred backdrop reads as
    # a face" bug.  Fix: blur in two strong passes (the second uses a bigger
    # window to smear large features), then desaturate + darken so it recedes
    # behind the sharp foreground instead of competing with it.
    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"boxblur=lr=55:lp=2,boxblur=lr=35:lp=2,"
        f"eq=saturation=0.35:brightness=-0.18[bg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )

    args = [
        "-i", str(input_path),
        "-filter_complex", vf,
        "-c:v", settings.output_settings.codec,
        "-crf", str(settings.output_settings.crf),
        "-preset", settings.output_settings.preset,
        "-c:a", settings.output_settings.audio_codec,
        "-ac", "2",
        "-b:a", settings.output_settings.audio_bitrate,
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Vertical fit+blur conversion of {input_path.name}")
    return output_path


@timed(logger_name="processing")
def apply_dynamic_crop(
    input_path: Path,
    output_path: Path,
    crop_timeline: list[dict[str, Any]],
) -> Path:
    """
    Apply dynamic face-following crop using FFmpeg's sendcmd/crop filters.

    For CPU efficiency, we generate a crop filter with keyframed positions.

    Args:
        input_path: Source video
        output_path: Output video
        crop_timeline: List of {"time": float, "crop_x": int, "crop_y": int, "crop_w": int, "crop_h": int}
    """
    settings = get_settings()
    w = settings.output_settings.width
    h = settings.output_settings.height

    if not crop_timeline:
        return convert_vertical(input_path, output_path)

    # Use the median crop position for a static crop (simplest reliable approach)
    # Dynamic per-frame cropping via sendcmd is complex; this gives good results
    xs = [c["crop_x"] for c in crop_timeline]
    ys = [c["crop_y"] for c in crop_timeline]
    ws = [c["crop_w"] for c in crop_timeline]
    hs = [c["crop_h"] for c in crop_timeline]

    crop_x = sorted(xs)[len(xs) // 2]
    crop_y = sorted(ys)[len(ys) // 2]
    crop_w = sorted(ws)[len(ws) // 2]
    crop_h = sorted(hs)[len(hs) // 2]

    vf = (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    args = [
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", settings.output_settings.codec,
        "-crf", str(settings.output_settings.crf),
        "-preset", settings.output_settings.preset,
        "-c:a", settings.output_settings.audio_codec,
        "-ac", "2",  # Downmix to stereo (AAC encoder can't handle 5.1)
        "-b:a", settings.output_settings.audio_bitrate,
        "-r", str(settings.output_settings.fps),
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Dynamic crop of {input_path.name}")
    return output_path


@timed(logger_name="processing")
def burn_subtitles(
    input_path: Path,
    output_path: Path,
    subtitle_path: Path,
    font_family: str = "Arial",
    font_size: int = 24,
    font_color: str = "&HFFFFFF",
    outline_color: str = "&H000000",
    outline_width: int = 2,
) -> Path:
    """
    Burn subtitles into a video using the ASS subtitle filter.

    Returns:
        Path to the video with burned-in subtitles
    """
    settings = get_settings()

    # Escape special characters in path for FFmpeg filter
    sub_path_str = str(subtitle_path).replace("\\", "/").replace(":", "\\:")

    force_style = (
        f"FontName={font_family},"
        f"FontSize={font_size},"
        f"PrimaryColour={font_color},"
        f"OutlineColour={outline_color},"
        f"Outline={outline_width},"
        f"Shadow=1,"
        f"MarginV=40"
    )

    vf = f"subtitles='{sub_path_str}':force_style='{force_style}'"

    args = [
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", settings.output_settings.codec,
        "-crf", str(settings.output_settings.crf),
        "-preset", settings.output_settings.preset,
        "-c:a", "copy",
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Subtitle burn for {input_path.name}")
    return output_path


@timed(logger_name="processing")
def extract_frame(
    video_path: Path,
    output_path: Path,
    timestamp: float,
) -> Path:
    """
    Extract a single frame from a video at the given timestamp.

    Returns:
        Path to the extracted frame image
    """
    args = [
        "-ss", f"{timestamp:.3f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",  # High quality JPEG
        "-y", str(output_path),
    ]

    _run_ffmpeg(args, f"Frame extraction at {timestamp:.1f}s")
    return output_path


@timed(logger_name="processing")
def extract_frames_batch(
    video_path: Path,
    output_dir: Path,
    fps: float = 1.0,
    prefix: str = "frame",
) -> list[Path]:
    """
    Extract frames from a video at the given FPS rate.

    Returns:
        List of paths to extracted frames
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / f"{prefix}_%06d.jpg")

    args = [
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-q:v", "2",
        "-y", pattern,
    ]

    _run_ffmpeg(args, f"Batch frame extraction at {fps} fps")

    frames = sorted(output_dir.glob(f"{prefix}_*.jpg"))
    logger.info(f"Extracted {len(frames)} frames from {video_path.name}")
    return frames


def probe_achieved_start(
    video_path: Path,
    requested_start: float,
    lookback_seconds: float = 60.0,
) -> float:
    """Return the ACTUAL start time (source seconds) a stream-copy cut at
    ``requested_start`` will land on.

    ``cut_clip(..., reencode=False)`` uses ``-ss <t>`` *input* seeking with
    ``-c copy``.  FFmpeg satisfies that seek by starting at the LAST KEYFRAME at
    or before ``t``, so the rendered clip begins a little EARLIER than requested
    (the "timing lead" that used to push captions early).  This helper probes the
    source's keyframe timestamps in the window just before ``requested_start``
    and returns the snap point, so callers can rebase subtitles against the
    clip's REAL first frame instead of the requested (wrong) value.

    Falls back to ``requested_start`` if probing fails or finds no keyframe.
    """
    settings = get_settings()
    lo = max(0.0, requested_start - lookback_seconds)
    interval = f"{lo:.3f}%{requested_start:.3f}"
    cmd = [
        settings.ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=best_effort_timestamp_time,pts_time",
        "-read_intervals", interval,
        "-of", "json",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_get_creation_flags(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return requested_start
        import json as _json
        data = _json.loads(result.stdout)
        times: list[float] = []
        for frame in data.get("frames") or []:
            ts = frame.get("best_effort_timestamp_time")
            if ts is None:
                ts = frame.get("pts_time")
            if ts is None:
                continue
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if t <= requested_start + 0.01:
                times.append(t)
        if times:
            achieved = max(times)
            logger.debug(
                f"probe_achieved_start: req={requested_start:.3f} -> {achieved:.3f} "
                f"(snapped to prior keyframe)"
            )
            return round(achieved, 3)
    except Exception:  # noqa: BLE001
        logger.warning(
            f"Could not probe achieved start for {video_path.name}; "
            f"using requested {requested_start:.3f}"
        )
    return requested_start


def get_video_duration(video_path: Path) -> float:
    """Quick duration probe using FFprobe."""
    settings = get_settings()
    cmd = [
        settings.ffprobe_path,
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_get_creation_flags(),
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0


def _probe_streams(path: Path) -> dict[str, dict[str, Any]]:
    """Best-effort ffprobe of the video/audio stream properties for a file."""
    settings = get_settings()
    try:
        result = subprocess.run(
            [
                settings.ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_get_creation_flags(),
        )
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    streams = data.get("streams", [])
    return {
        "video": next((s for s in streams if s.get("codec_type") == "video"), None) or {},
        "audio": next((s for s in streams if s.get("codec_type") == "audio"), None) or {},
    }


@timed(logger_name="processing")
def add_thumbnail_intro(
    video_path: Path,
    thumbnail_path: Path,
    duration: float = 0.8,
) -> Path:
    """
    Prepend a motionless thumbnail frame as an intro to a clip.

    Returns a path the caller should keep using (normally ``video_path`` itself),
    transparently pointing at the clip-with-intro. The caller's DB file path
    (e.g. ``clips.output_path`` / the ClipEdit file) does NOT need to change.

    The input clip is never the persistent source of truth we overwrite blindly —
    we build the intro-prefixed clip in a temp file and atomically replace the
    given path so downstream references stay valid. If anything can't be done
    (missing inputs, FFmpeg error, non-atomic replace), we log and return the
    ORIGINAL ``video_path`` unchanged, so callers can safely use the return value
    without an error branch.

    How it works (matching the clip exactly so a concat stays glitch-free):
      1) Render a looped still of ``thumbnail_path`` at the clip's resolution +
         fps, scaled to fill the frame.
      2) Add a silent audio track matching the clip's sample rate + channel layout
         (avoids "audio pop" and makes the streams compatible).
      3) Concatenate [intro, main] via the concat demuxer with CONSISTENT
         re-encode settings (libx264 / AAC) so streams are guaranteed compatible.

    ``duration`` is clamped to [0.1, 10.0] seconds (default 0.8).
    """
    video_path = Path(video_path)
    thumbnail_path = Path(thumbnail_path)

    if not video_path.exists():
        logger.warning(f"[INTRO] Skip: clip missing ({video_path})")
        return video_path
    if not thumbnail_path.exists():
        logger.warning(f"[INTRO] Skip: thumbnail missing ({thumbnail_path})")
        return video_path

    duration = max(0.1, min(float(duration), 10.0))
    settings = get_settings()
    oc = settings.output_settings

    # 1) Probe the clip for the video/audio properties we must mirror.
    probe = _probe_streams(video_path)
    v = probe.get("video") or {}
    a = probe.get("audio") or {}
    width = int(v.get("width") or 0) or oc.width
    height = int(v.get("height") or 0) or oc.height

    fps = 0.0
    r_frame_rate = v.get("r_frame_rate", "0/1")
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 0.0
    if fps <= 0:
        fps = float(oc.fps or 30)

    sample_rate = a.get("sample_rate") or "44100"
    channel_layout = a.get("channel_layout") or "stereo"

    parent = video_path.parent
    stem = video_path.stem
    intro_file = parent / f"{stem}._intro.mp4"
    concat_list = parent / f"{stem}._intro_list.txt"
    tmp_final = parent / f"{stem}._introtmp.mp4"

    try:
        # 2) Build the intro segment: looped thumbnail + matching silent audio.
        intro_args = [
            "-loop", "1",
            "-framerate", f"{fps:g}",
            "-i", str(thumbnail_path.resolve()),
            "-f", "lavfi",
            "-i", f"anullsrc=r={sample_rate}:cl={channel_layout}",
            "-vf",
            (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"),
            "-c:v", oc.codec,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:g}",
            "-crf", str(oc.crf),
            "-preset", oc.preset,
            "-c:a", oc.audio_codec,
            "-b:a", str(oc.audio_bitrate),
            "-t", f"{duration:.3f}",
            "-shortest",
            "-y", str(intro_file),
        ]
        _run_ffmpeg(intro_args, f"Thumbnail intro segment for {video_path.name}")
        if not intro_file.exists():
            raise RuntimeError("intro segment was not produced")

        # 3) Concatenate [intro, main] with consistent re-encode settings so the
        #    streams are guaranteed compatible (concat demuxer + transcode).
        # Must use POSIX separators in the list file: on Windows the concat
        # demuxer mangles "C:\..." (backslash escaping), so write forward slashes
        # (mirrors concat_videos in auto_editor.py).
        concat_list.write_text(
            f"file '{intro_file.resolve().as_posix()}'\nfile '{video_path.resolve().as_posix()}'\n",
            encoding="utf-8",
        )
        concat_args = [
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", oc.codec,
            "-pix_fmt", "yuv420p",
            "-crf", str(oc.crf),
            "-preset", oc.preset,
            "-c:a", oc.audio_codec,
            "-b:a", str(oc.audio_bitrate),
            "-movflags", "+faststart",
            "-y", str(tmp_final),
        ]
        _run_ffmpeg(concat_args, f"Intro concat for {video_path.name}")

        if not tmp_final.exists() or tmp_final.stat().st_size == 0:
            raise RuntimeError("intro concat produced an empty/absent file")

        # 4) Atomically replace the caller's clip path with the intro version.
        os.replace(str(tmp_final), str(video_path))
        logger.info(
            f"[INTRO] Prepended {duration:.2f}s thumbnail intro to {video_path.name} "
            f"(fps={fps:g}, {width}x{height}, {sample_rate}Hz/{channel_layout})"
        )
    except Exception as e:  # noqa: BLE001 — intro is best-effort, never fatal
        logger.warning(
            f"[INTRO] Could not add thumbnail intro to {video_path.name}; "
            f"keeping original clip. Reason: {e}"
        )
        for p in (intro_file, concat_list, tmp_final):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        # Best-effort cleanup of temp artifacts (current intro version already
        # replaced video_path, so the intro/concat temp files are no longer needed).
        for p in (intro_file, concat_list, tmp_final):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    return video_path
