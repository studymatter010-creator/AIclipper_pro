"""
Stage 4 — Copy Selector.

Selects the headline text from the clip's ``hook_sentence`` and ``title``,
truncating to a sane character budget **before** any rendering.  This makes
Stage 5's text-fit problem much easier since it starts from a bounded string.
"""

from __future__ import annotations

_MAX_CHARS = 60


def select_headline(
    hook_sentence: str | None,
    title: str | None,
    max_chars: int = _MAX_CHARS,
) -> str:
    """Pick and bound the headline string.

    Prefer ``hook_sentence`` (punchy, curiosity-driven — the right register
    for a thumbnail).  Fall back to ``title`` if no hook sentence exists.

    Truncation happens **here**, at the source-string level — not by
    truncating already-rendered text later.
    """
    text = (hook_sentence or "").strip()
    if not text:
        text = (title or "").strip()
    if not text:
        text = "Watch this!"

    # Strip common lead-in fillers that add length without hook value.
    for filler in [
        "Check out this amazing",
        "Check out this",
        "Check out",
        "Watch this",
    ]:
        if text.lower().startswith(filler.lower()):
            text = text[len(filler) :].strip()

    # Truncate to budget (ellipsized if mid-phrase).
    if len(text) > max_chars:
        truncated = text[: max_chars - 1].rstrip()
        # Don't cut mid-word — back up to the last space if it's past halfway.
        last_space = truncated.rfind(" ")
        if last_space > max_chars // 3:
            truncated = truncated[:last_space]
        text = truncated + "…"

    # Ensure terminal punctuation for punch.
    if text and not text.endswith(("!", "?", "...", "…")):
        text += "!"

    return text
