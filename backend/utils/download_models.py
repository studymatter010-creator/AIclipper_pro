"""
AIClipper Model Download Utility

Downloads required model files for Whisper.cpp and MediaPipe Face Detection.
Run this script on first setup: python -m backend.utils.download_models
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

# ---- Project root ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = PROJECT_ROOT / "models"


# ---- Model definitions ----
MODELS = {
    # MediaPipe Face Detection model
    "blaze_face_short_range.tflite": {
        "url": "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite",
        "size_mb": 0.2,
        "description": "MediaPipe Face Detection (short range)",
    },
    # Whisper.cpp models (GGML format)
    "ggml-tiny.en.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin",
        "size_mb": 75,
        "description": "Whisper Tiny (English only) - Fastest, lowest accuracy",
    },
    "ggml-base.en.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        "size_mb": 142,
        "description": "Whisper Base (English only) - Good balance for constrained hardware",
    },
    "ggml-small.en.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin",
        "size_mb": 466,
        "description": "Whisper Small (English only) - Recommended for CPU",
    },
    "ggml-medium.en.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin",
        "size_mb": 1500,
        "description": "Whisper Medium (English only) - High accuracy, requires 8GB+ RAM",
    },
    # Multilingual models (English + Chinese, language auto-detect)
    "ggml-small.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        "size_mb": 466,
        "description": "Whisper Small (Multilingual) - Light, decent for non-English content",
    },
    "ggml-medium.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        "size_mb": 1500,
        "description": "Whisper Medium (Multilingual) - Precise dialogue detection for English + Chinese, needs 8GB+ RAM (OOMs on 16GB when Ollama qwen3:8b is loaded; use small)",
    },
    "ggml-large-v3-turbo.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        "size_mb": 1600,
        "description": "Whisper Large V3 Turbo (Multilingual) - Near-best accuracy for English + Chinese, faster than full large",
    },
    "ggml-large-v3.bin": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin",
        "size_mb": 3000,
        "description": "Whisper Large V3 (Multilingual) - Maximum accuracy, requires 12GB+ RAM, slowest on CPU",
    },
}

# Default models to download (the active multilingual small model + face detection).
# NOTE: we use ggml-small.bin (466MB) — NOT medium — because Whisper-medium
# OOM-crashed this 16GB machine (~1.5GB file + ~3GB runtime while Ollama
# qwen3:8b is also resident). small still does English+Chinese auto-detect.
DEFAULT_MODELS = ["blaze_face_short_range.tflite", "ggml-small.bin"]


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    """Display download progress bar."""
    downloaded = block_num * block_size
    if total_size > 0:
        percent = min(100, downloaded * 100 / total_size)
        bar_len = 40
        filled = int(bar_len * percent / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        mb_downloaded = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        sys.stdout.write(f"\r  [{bar}] {percent:5.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)")
        sys.stdout.flush()
        if downloaded >= total_size:
            print()
    else:
        mb_downloaded = downloaded / (1024 * 1024)
        sys.stdout.write(f"\r  Downloaded: {mb_downloaded:.1f} MB")
        sys.stdout.flush()


def download_model(name: str, force: bool = False) -> Path:
    """
    Download a single model file.

    Args:
        name: Model filename (key in MODELS dict)
        force: Re-download even if file exists

    Returns:
        Path to the downloaded model file
    """
    if name not in MODELS:
        available = ", ".join(MODELS.keys())
        raise ValueError(f"Unknown model '{name}'. Available: {available}")

    model_info = MODELS[name]
    output_path = MODELS_DIR / name

    if output_path.exists() and not force:
        print(f"  ✓ {name} already exists ({output_path.stat().st_size / (1024*1024):.1f} MB)")
        return output_path

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"  ↓ Downloading {name} ({model_info['size_mb']:.0f} MB)...")
    print(f"    {model_info['description']}")

    try:
        urllib.request.urlretrieve(
            model_info["url"],
            str(output_path),
            reporthook=_progress_hook,
        )
        actual_size = output_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ Downloaded {name} ({actual_size:.1f} MB)")
        return output_path
    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        print(f"  ✗ Failed to download {name}: {e}")
        raise


def download_default_models(force: bool = False) -> list[Path]:
    """Download all default models required for AIClipper."""
    print("=" * 60)
    print("AIClipper Model Downloader")
    print("=" * 60)
    print(f"\nDownloading to: {MODELS_DIR}\n")

    paths = []
    for name in DEFAULT_MODELS:
        try:
            path = download_model(name, force=force)
            paths.append(path)
        except Exception as e:
            print(f"  ⚠ Skipping {name}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Downloaded {len(paths)}/{len(DEFAULT_MODELS)} models.")
    print(f"{'=' * 60}")
    return paths


def list_available_models() -> None:
    """Print all available models with their status."""
    print("\nAvailable Models:")
    print("-" * 70)
    for name, info in MODELS.items():
        status = "✓ Downloaded" if (MODELS_DIR / name).exists() else "✗ Not downloaded"
        default = " [DEFAULT]" if name in DEFAULT_MODELS else ""
        print(f"  {name:40s} {info['size_mb']:>6.0f} MB  {status}{default}")
        print(f"    {info['description']}")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download AI models for AIClipper")
    parser.add_argument("--list", action="store_true", help="List available models")
    parser.add_argument("--model", type=str, help="Download a specific model by name")
    parser.add_argument("--all-defaults", action="store_true", help="Download all default models")
    parser.add_argument("--force", action="store_true", help="Re-download even if exists")

    args = parser.parse_args()

    if args.list:
        list_available_models()
    elif args.model:
        download_model(args.model, force=args.force)
    else:
        download_default_models(force=args.force)
