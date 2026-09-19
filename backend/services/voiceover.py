"""
AIClipper Multilingual AI Voice-Over (Dubbing) Service

Synthesizes a spoken voice-over for a generated clip in one of four
languages — **English, Korean, Hindi, Japanese** — using the free
`edge-tts <https://github.com/rany2/edge-tts>`_ neural voices, then muxes
the speech onto the clip video:

* ``mode="replace"`` → replaces the original clip audio entirely.
* ``mode="mix"``     → mixes the voice-over on top of the original audio
  (great for podcast commentary / reaction content).

Translation of the clip transcript is delegated to a local Ollama model when
reachable; otherwise the original script is voiced as-is (still a valid
"voice-over", just untranslated).

Every render is persisted as a :class:`backend.database.models.VoiceOver`
row so users can re-download or compare language variants later.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("services.voiceover")


# ---------------------------------------------------------------------------
# Supported languages & voices
# ---------------------------------------------------------------------------

# Locale label shown in the UI (emoji flag + human name).
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"name": "English", "flag": "\U0001F1EC\U0001F1E7"},
    "ko": {"name": "Korean", "flag": "\U0001F1F0\U0001F1F7"},
    "hi": {"name": "Hindi", "flag": "\U0001F1EE\U0001F1F3"},
    "ja": {"name": "Japanese", "flag": "\U0001F1EF\U0001F1F5"},
    "zh": {"name": "Chinese", "flag": "\U0001F1F9\U0001F1FC"},
}

# edge-tts voice tags → friendly label + gender.
VOICES: dict[str, list[dict[str, str]]] = {
    "en": [
        {"id": "en-US-JennyNeural", "label": "Jenny (US Female)", "gender": "female"},
        {"id": "en-US-GuyNeural", "label": "Guy (US Male)", "gender": "male"},
        {"id": "en-US-AriaNeural", "label": "Aria (US Female)", "gender": "female"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia (UK Female)", "gender": "female"},
        {"id": "en-GB-RyanNeural", "label": "Ryan (UK Male)", "gender": "male"},
        {"id": "en-AU-NatashaNeural", "label": "Natasha (AU Female)", "gender": "female"},
    ],
    "ko": [
        {"id": "ko-KR-SunHiNeural", "label": "Sun-Hi (손희, Female)", "gender": "female"},
        {"id": "ko-KR-InJoonNeural", "label": "In-Joon (인준, Male)", "gender": "male"},
    ],
    "hi": [
        {"id": "hi-IN-SwaraNeural", "label": "Swara (Female)", "gender": "female"},
        {"id": "hi-IN-MadhurNeural", "label": "Madhur (Male)", "gender": "male"},
    ],
    "ja": [
        {"id": "ja-JP-NanamiNeural", "label": "Nanami (ななみ, Female)", "gender": "female"},
        {"id": "ja-JP-KeitaNeural", "label": "Keita (けいた, Male)", "gender": "male"},
    ],
    "zh": [
        {"id": "zh-CN-XiaoxiaoNeural", "label": "Xiaoxiao (晓晓, Female)", "gender": "female"},
        {"id": "zh-CN-YunxiNeural", "label": "Yunxi (云希, Male)", "gender": "male"},
        {"id": "zh-CN-YunyangNeural", "label": "Yunyang (云扬, Male, news)", "gender": "male"},
        {"id": "zh-CN-XiaoyiNeural", "label": "Xiaoyi (晓伊, Female)", "gender": "female"},
    ],
}

DEFAULT_VOICE: dict[str, str] = {
    "en": "en-US-JennyNeural",
    "ko": "ko-KR-SunHiNeural",
    "hi": "hi-IN-SwaraNeural",
    "ja": "ja-JP-NanamiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
}


def list_voices() -> list[dict[str, Any]]:
    """Return the catalog of supported languages and voices for the UI."""
    catalog: list[dict[str, Any]] = []
    for code in ["en", "ko", "hi", "ja", "zh"]:
        catalog.append(
            {
                "code": code,
                "name": LANGUAGES[code]["name"],
                "flag": LANGUAGES[code]["flag"],
                "default_voice": DEFAULT_VOICE[code],
                "voices": VOICES[code],
            }
        )
    return catalog


def _get_creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _run_ffmpeg_safe(args: list[str], description: str = "FFmpeg") -> None:
    settings = get_settings()
    cmd = [settings.ffmpeg_path] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
            creationflags=_get_creation_flags(),
        )
    except FileNotFoundError:
        raise RuntimeError(f"FFmpeg not found at '{settings.ffmpeg_path}'.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{description} timed out.")
    if result.returncode != 0:
        lines = [
            l for l in (result.stderr or "").split("\n")
            if any(k in l.lower() for k in ("error", "invalid", "failed", "no such"))
        ]
        msg = lines[0] if lines else result.stderr or "unknown error"
        raise RuntimeError(f"{description}: {msg}")


# ---------------------------------------------------------------------------
# Translation (Ollama-first, graceful fallback)
# ---------------------------------------------------------------------------

_PROMPT = (
    "You are a professional dubbing translator. Translate the video script "
    "below into {lang}. Keep the meaning, tone and enthusiasm. Match the "
    "natural spoken rhythm of {lang}. Reply with ONLY the translated text, "
    "no quotes, no commentary, no numbering."
)


def _translate_with_ollama(text: str, target_lang: str) -> str | None:
    """Translate ``text`` to ``target_lang`` via a local Ollama model."""
    settings = get_settings()
    lang_label = LANGUAGES.get(target_lang, {}).get("name", target_lang)
    system = _PROMPT.format(lang=lang_label)

    # Preferred: the 'ollama' python package.
    try:
        import ollama  # type: ignore[import-untyped]

        resp = ollama.chat(
            model=settings.ollama_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": text}],
        )
        translated = (resp.get("message") or {}).get("content", "").strip()
        if translated:
            return translated
    except Exception as exc:  # noqa: BLE001
        logger.info(f"ollama python client unavailable ({exc}); trying HTTP")

    # Fallback: raw HTTP to the Ollama server.
    try:
        import requests

        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "stream": False,
        }
        resp = requests.post(
            f"{settings.ollama_host.rstrip('/')}/api/chat",
            json=payload,
            timeout=settings.ollama_timeout,
        )
        resp.raise_for_status()
        translated = (resp.json().get("message") or {}).get("content", "").strip()
        if translated:
            return translated
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Ollama translation unavailable: {exc}")

    return None


async def translate_text(text: str, target_lang: str) -> tuple[str, bool]:
    """
    Translate ``text`` into ``target_lang``.

    Returns ``(translated_text, translated_success)``. When no translation
    backend is reachable it returns the original text unchanged with
    ``translated=False`` so callers can still produce a voice-over.
    """
    if not text.strip():
        return "", False
    try:
        translated = await asyncio.to_thread(_translate_with_ollama, text, target_lang)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Translation error: {exc}")
        translated = None
    if translated and translated.strip():
        return translated.strip(), True
    return text, False


# ---------------------------------------------------------------------------
# Speech synthesis (edge-tts)
# ---------------------------------------------------------------------------

async def synthesize_speech(text: str, voice: str, output_path: Path) -> Path:
    """
    Synthesize ``text`` into an MP3 at ``output_path`` using ``edge-tts``.

    Raises:
        RuntimeError: If `edge-tts` is not installed or synthesis fails.
    """
    try:
        import edge_tts  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "The 'edge-tts' package is not installed. Install it with "
            "`pip install edge-tts` to enable AI voice-overs."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))

    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("Speech synthesis produced no audio.")
    logger.info(f"Synthesized {voice}: {output_path.name} ({output_path.stat().st_size} bytes)")
    return output_path


# ---------------------------------------------------------------------------
# Dubbing orchestration
# ---------------------------------------------------------------------------

def _extract_clip_text(clip, transcript) -> str:
    """Pull the transcript text that falls within the clip's time range."""
    text = ""
    words = (
        (transcript.word_timestamps_json if transcript else None) or []
    )
    segments = (
        (transcript.content_json if transcript else None) or []
    )

    for w in words:
        start = w.get("start", 0)
        if clip.start_time <= start <= clip.end_time:
            text += w.get("word", "") + " "
    if text.strip():
        return text.strip()

    for seg in segments:
        start = seg.get("start", seg.get("t0", 0))
        if clip.start_time <= start <= clip.end_time:
            text += seg.get("text", "") + " "
    return text.strip() or (transcript.full_text if transcript else "")[:500]


def probe_duration(path: Path) -> float:
    """Probe a media file's duration in seconds using ffprobe."""
    settings = get_settings()
    try:
        result = subprocess.run(
            [
                settings.ffprobe_path, "-v", "quiet",
                "-print_format", "json", "-show_format",
                str(path.resolve()),
            ],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _mux(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    mode: str,
    mix_volume: float,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    video = video_path.resolve()
    audio = audio_path.resolve()
    out = output_path.resolve()

    if mode == "mix":
        v = f"{mix_volume:.2f}"
        filter_complex = (
            f"[1:a]volume={v}[vo];"
            f"[0:a][vo]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        args = [
            "-i", str(video), "-i", str(audio),
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-y", str(out),
        ]
    else:  # replace
        # NOTE: no "-shortest" here — the voice-over is often shorter than
        # the clip, and "-shortest" would truncate the video early. Instead we
        # honour the video's own length so the clip plays in full.
        args = [
            "-i", str(video), "-i", str(audio),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-y", str(out),
        ]
        vid_dur = probe_duration(video)
        if vid_dur > 0:
            args = [
                "-i", str(video), "-i", str(audio),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-t", f"{vid_dur:.3f}", "-y", str(out),
            ]

    _run_ffmpeg_safe(args, "Voice-over mux")
    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("Mux produced no output.")
    logger.info(f"Muxed voice-over: {output_path.name} ({mode})")
    return output_path


async def dub_clip(
    clip_id: int,
    target_lang: str,
    voice: str | None = None,
    mode: str = "replace",
    mix_volume: float = 1.0,
    translate_enabled: bool = True,
) -> dict[str, Any]:
    """
    Generate a multilingual AI voice-over for ``clip_id``.

    End-to-end: load clip + transcript → translate → synthesize speech →
    mux audio → persist a :class:`~backend.database.models.VoiceOver` row.

    Returns a result dict describing the render (or raising on hard failure).
    """
    from backend.database import crud
    from backend.database.engine import get_session_context
    from backend.database.models import VoiceOverStatus

    settings = get_settings()
    target_lang = target_lang.lower()
    if target_lang not in LANGUAGES:
        raise ValueError(f"Unsupported target language '{target_lang}'.")
    voice = voice or DEFAULT_VOICE[target_lang]
    if mode not in ("replace", "mix"):
        raise ValueError("mode must be 'replace' or 'mix'.")

    # ------------------------------------------------------------------
    # Load clip + transcript
    # ------------------------------------------------------------------
    async with get_session_context() as session:
        clip = await crud.get_clip(session, clip_id)
        if not clip:
            raise ValueError(f"Clip {clip_id} not found")
        if not clip.output_path or not Path(clip.output_path).exists():
            raise ValueError("Clip output file not generated yet.")
        transcript = await crud.get_transcript_for_video(session, clip.video_id)
        source_text = _extract_clip_text(clip, transcript)

        vo = await crud.create_voiceover(
            session,
            clip_id=clip_id,
            language=target_lang,
            voice=voice,
            mode=mode,
            source_text=source_text[:5000],
        )
        vo_id = vo.id

    logger.info(f"Voice-over job {vo_id}: clip={clip_id} lang={target_lang} voice={voice} mode={mode}")

    # Mark processing
    async with get_session_context() as session:
        await crud.update_voiceover(session, vo_id, status=VoiceOverStatus.PROCESSING)

    audio_path: Path | None = None
    output_path: Path | None = None
    translated_text = source_text
    translated = False

    try:
        # ------------------------------------------------------------------
        # Translate
        # ------------------------------------------------------------------
        if translate_enabled and source_text:
            translated_text, translated = await translate_text(source_text, target_lang)

        # ------------------------------------------------------------------
        # Synthesize speech
        # ------------------------------------------------------------------
        audio_path = settings.temp_dir / f"vo_{vo_id}_{target_lang}.mp3"
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        await synthesize_speech(translated_text or " ", voice, audio_path)

        # ------------------------------------------------------------------
        # Mux onto clip
        # ------------------------------------------------------------------
        clip_video = Path(clip.output_path)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = settings.output_dir / f"dubbed_clip_{clip_id}_{target_lang}.mp4"
        await _mux(clip_video, audio_path, output_path, mode, mix_volume)

    except Exception as exc:  # noqa: BLE001
        logger.error(f"Voice-over {vo_id} failed: {exc}", exc_info=True)
        async with get_session_context() as session:
            await crud.update_voiceover(
                session, vo_id,
                status=VoiceOverStatus.FAILED,
                error_message=str(exc),
            )
        if clip_id is not None:
            raise RuntimeError(str(exc)) from exc
        raise

    # ------------------------------------------------------------------
    # Persist success
    # ------------------------------------------------------------------
    async with get_session_context() as session:
        await crud.update_voiceover(
            session, vo_id,
            status=VoiceOverStatus.COMPLETED,
            audio_path=str(audio_path) if audio_path else None,
            output_path=str(output_path) if output_path else None,
            translated_text=translated_text,
            translated=1 if translated else 0,
        )

    return {
        "id": vo_id,
        "clip_id": clip_id,
        "language": target_lang,
        "voice": voice,
        "mode": mode,
        "translated": translated,
        "output_path": str(output_path) if output_path else None,
        "audio_path": str(audio_path) if audio_path else None,
    }