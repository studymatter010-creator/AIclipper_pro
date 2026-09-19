"""
AIClipper URL Download Service

Downloads a video directly from a shareable URL (YouTube, Facebook, and
any other platform supported by yt-dlp) so the user can paste a link and have
the app automatically pull the source video and run it through the clipping
pipeline — no manual download needed.

Uses `yt-dlp` <https://github.com/yt-dlp/yt-dlp>, the most capable, actively
maintained youtube_dl fork. If `yt-dlp` is not installed a helpful error is
raised so the user knows to `pip install yt-dlp`.
"""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed

logger = get_logger("services.downloader")

# Rough matchers used just to pick sensible yt-dlp output format options.
_YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
_FACEBOOK_RE = re.compile(r"facebook\.com", re.IGNORECASE)

# YouTube video-ID matchers (v=, /shorts/, /live/, youtu.be/<id>) used to
# derive a stable, comparable source ID from a URL *before* any download so an
# import can be de-duplicated without spending bandwidth.  Matches the same
# 11-char base64-ish id yt-dlp reports via info["id"].
_YT_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/|/watch/)([A-Za-z0-9_-]{11})")


class DownloadError(Exception):
    """Raised when a URL download fails."""


def _get_creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _import_ytdlp():
    """Lazily import the ``yt_dlp`` module or raise a clear error."""
    try:
        import yt_dlp  # type: ignore[import-untyped]
        return yt_dlp
    except ImportError as exc:  # noqa: BLE001
        raise DownloadError(
            "The 'yt-dlp' package is not installed. Install it with "
            "`pip install yt-dlp` to enable URL downloads."
        ) from exc


def detect_platform(url: str) -> str:
    """Return 'youtube', 'facebook', or 'other' for a given URL."""
    if _YOUTUBE_RE.search(url):
        return "youtube"
    if _FACEBOOK_RE.search(url):
        return "facebook"
    return "other"


def extract_source_id(url: str) -> str | None:
    """Derive a stable, comparable platform source-ID from *url* if possible.

    Used BEFORE downloading so a URL import can be de-duplicated (no
    re-download) against an earlier import of the same source.  Matches the
    id yt-dlp reports for the same URLs.  Returns ``None`` when the URL form
    isn't one we can cheaply identify (callers then simply skip dedup).
    """
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


@timed(logger_name="processing")
def download_video(url: str, dest_dir: Path | None = None) -> dict[str, Any]:
    """
    Download the video at ``url`` into ``dest_dir`` (default: uploads dir).

    Returns a dict with the local file path plus useful metadata about the
    source (original title, uploader, duration, etc.).

    Raises:
        DownloadError: If yt-dlp is missing or the download fails.
    """
    settings = get_settings()
    dest_dir = dest_dir or settings.upload_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    yt_dlp = _import_ytdlp()

    # Output template: uuid-based safe filename, extension filled by yt-dlp.
    run_id = uuid.uuid4().hex
    outtmpl = str((dest_dir / f"{run_id}.%(ext)s").resolve())

    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "noprogress": True,
        # Prefer mp4 so our FFmpeg pipeline can process it directly.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "ignoreerrors": False,
    }

    platform = detect_platform(url)
    logger.info(f"Downloading {platform} video from URL: {url[:80]}...")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"Download failed: {exc}") from exc

    if info is None:
        raise DownloadError("No video information could be extracted.")

    # Locate the actual file on disk (yt-dlp standardizes the extension).
    final_path: Path | None = None
    if info.get("requested_downloads"):
        filepath = info["requested_downloads"][0].get("filepath")
        if filepath:
            final_path = Path(filepath)
    if final_path is None or not final_path.exists():
        # Fall back to scanning the destination for our unique prefix.
        matches = list(dest_dir.glob(f"{run_id}.*"))
        final_path = matches[0] if matches else None

    if final_path is None or not final_path.exists():
        # Clean up partial files then bail.
        for leftover in dest_dir.glob(f"{run_id}.*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise DownloadError("Download completed but no output file was found.")

    duration = info.get("duration") or 0
    title = info.get("title") or final_path.stem

    logger.info(
        f"Downloaded '{title}' ({duration / 60:.1f} min) -> {final_path.name}"
    )

    return {
        "file_path": str(final_path),
        "filename": final_path.name,
        "title": title,
        "uploader": info.get("uploader") or "",
        "duration": float(duration),
        "webpage_url": info.get("webpage_url") or url,
        "platform": platform,
        "id": info.get("id"),
        "source_video_id": extract_source_id(url) or info.get("id"),
    }