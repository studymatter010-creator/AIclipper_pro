"""
Thumbnail pipeline — six independent stages with a fallback ladder.

Usage::

    from backend.services.thumbnail import render_thumbnail

    path, tier = render_thumbnail(video_path, hook_sentence, title, output_path)
"""

from backend.services.thumbnail.engine import render_thumbnail

__all__ = ["render_thumbnail"]
