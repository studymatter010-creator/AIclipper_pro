"""
AIClipper Metadata Generation Service

Multi-provider LLM support for generating video metadata:
  - Ollama (local, default)
  - OpenAI (GPT-4o, GPT-4o-mini)
  - Google Gemini (Gemini Pro, Flash)
  - DeepSeek (DeepSeek V3)

Falls back to heuristic extraction when no LLM provider is available.
"""

from __future__ import annotations

import os
import re
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed

logger = get_logger("services.metadata_generator")

# ── Prompt templates ────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a social-media content expert. Given a video transcript, "
    "generate metadata for a short-form vertical video (YouTube Shorts / "
    "Instagram Reels / TikTok). Be concise, engaging, and SEO-friendly."
)

_USER_PROMPT_TEMPLATE = """Below is the transcript of a video clip. Generate:
1. **Title** — catchy, ≤ 80 characters
2. **Description** — 1-2 sentences, ≤ 200 characters, include a CTA
3. **Hashtags** — 5-8 relevant hashtags, space-separated, each starting with #
4. **Keywords** — 5-10 comma-separated SEO keywords

Respond in **exactly** this format (no extra text):
TITLE: <title>
DESCRIPTION: <description>
HASHTAGS: <hashtags>
KEYWORDS: <keywords>

Transcript:
\"\"\"
{transcript}
\"\"\"
"""


# ── Parsing ─────────────────────────────────────────────────────────────

def _parse_llm_response(text: str) -> dict[str, str]:
    """
    Parse the structured LLM response into a dict.

    Expected format::

        TITLE: ...
        DESCRIPTION: ...
        HASHTAGS: ...
        KEYWORDS: ...
    """
    result: dict[str, str] = {
        "title": "",
        "description": "",
        "hashtags": "",
        "keywords": "",
    }

    for line in text.strip().splitlines():
        line = line.strip()
        lower = line.lower()
        if lower.startswith("title:"):
            result["title"] = line.split(":", 1)[1].strip()
        elif lower.startswith("description:"):
            result["description"] = line.split(":", 1)[1].strip()
        elif lower.startswith("hashtags:"):
            result["hashtags"] = line.split(":", 1)[1].strip()
        elif lower.startswith("keywords:"):
            result["keywords"] = line.split(":", 1)[1].strip()

    return result


# ── Fallback heuristic ──────────────────────────────────────────────────

def _generate_fallback_metadata(transcript_text: str) -> dict[str, str]:
    """
    Build basic metadata from the transcript when no LLM is available.
    """
    words = transcript_text.split()
    title_words = words[:12] if len(words) >= 12 else words
    title = " ".join(title_words).strip(" .,!?;:")
    if len(title) > 80:
        title = title[:77] + "..."

    description = " ".join(words[:30]).strip(" .,!?;:")
    if len(description) > 200:
        description = description[:197] + "..."

    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        clean = re.sub(r"[^a-zA-Z0-9]", "", w).lower()
        if len(clean) > 3 and clean not in seen:
            seen.add(clean)
            keywords.append(clean)
        if len(keywords) >= 8:
            break

    hashtags = " ".join(f"#{kw}" for kw in keywords[:6])
    kw_str = ", ".join(keywords)

    return {
        "title": title or "Untitled Clip",
        "description": description or "Check out this clip!",
        "hashtags": hashtags or "#shorts #video #clip",
        "keywords": kw_str or "video, clip, shorts",
    }


# ── LLM Provider Backends ──────────────────────────────────────────────

def _call_ollama(prompt: str, model: str) -> str | None:
    """Call local Ollama server."""
    try:
        import ollama  # type: ignore[import-untyped]

        logger.info(f"Requesting metadata from Ollama model='{model}'")
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return response["message"]["content"]
    except ImportError:
        logger.debug("ollama package not installed")
    except ConnectionError:
        logger.warning("Cannot connect to Ollama server")
    except Exception as exc:
        logger.warning(f"Ollama call failed: {exc}")
    return None


def _call_openai(prompt: str, model: str) -> str | None:
    """Call OpenAI API (GPT-4o, GPT-4o-mini, etc.)."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.debug("OPENAI_API_KEY not set, skipping OpenAI provider")
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        logger.info(f"Requesting metadata from OpenAI model='{model}'")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except ImportError:
        logger.debug("openai package not installed")
    except Exception as exc:
        logger.warning(f"OpenAI call failed: {exc}")
    return None


def _call_gemini(prompt: str, model: str) -> str | None:
    """Call Google Gemini API."""
    api_key = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.debug("GOOGLE_API_KEY/GEMINI_API_KEY not set, skipping Gemini")
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        gen_model = genai.GenerativeModel(model)
        logger.info(f"Requesting metadata from Gemini model='{model}'")
        response = gen_model.generate_content(
            f"{_SYSTEM_PROMPT}\n\n{prompt}",
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=500,
                temperature=0.7,
            ),
        )
        return response.text
    except ImportError:
        logger.debug("google-generativeai package not installed")
    except Exception as exc:
        logger.warning(f"Gemini call failed: {exc}")
    return None


def _call_deepseek(prompt: str, model: str) -> str | None:
    """Call DeepSeek API (OpenAI-compatible)."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.debug("DEEPSEEK_API_KEY not set, skipping DeepSeek")
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        logger.info(f"Requesting metadata from DeepSeek model='{model}'")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except ImportError:
        logger.debug("openai package not installed (needed for DeepSeek)")
    except Exception as exc:
        logger.warning(f"DeepSeek call failed: {exc}")
    return None


# ── Provider registry ───────────────────────────────────────────────────

_PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {"call": _call_ollama, "default_model": "qwen3:8b"},
    "openai": {"call": _call_openai, "default_model": "gpt-4o-mini"},
    "gemini": {"call": _call_gemini, "default_model": "gemini-1.5-flash"},
    "deepseek": {"call": _call_deepseek, "default_model": "deepseek-chat"},
}


def _detect_provider() -> str:
    """Auto-detect the best available LLM provider."""
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "ollama"  # default to local


# ── Public API ──────────────────────────────────────────────────────────

@timed(logger_name="processing")
def generate_metadata(
    transcript_text: str,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    """
    Generate social-media metadata from a transcript using an LLM.

    Supports multiple providers: ollama, openai, gemini, deepseek.
    Auto-detects provider from environment variables if not specified.

    Args:
        transcript_text: Plain-text transcript of the clip.
        model: LLM model name (provider-specific). Uses provider default if None.
        provider: LLM provider name. Auto-detected if None.

    Returns:
        dict with ``title``, ``description``, ``hashtags``, ``keywords``.
        Falls back to heuristic extraction if all LLM providers fail.
    """
    settings = get_settings()

    # Resolve provider
    if provider is None:
        provider = os.getenv("LLM_PROVIDER", "").lower() or _detect_provider()

    if provider not in _PROVIDERS:
        logger.warning(f"Unknown provider '{provider}', falling back to ollama")
        provider = "ollama"

    # Resolve model
    if provider == "ollama":
        model = model or settings.ollama_model
    else:
        model = _PROVIDERS[provider]["default_model"]

    # Truncate very long transcripts
    max_chars = 3000
    trimmed = transcript_text[:max_chars]
    user_prompt = _USER_PROMPT_TEMPLATE.format(transcript=trimmed)

    # ── Try the selected provider ───────────────────────────────────────
    call_fn = _PROVIDERS[provider]["call"]
    raw_text = call_fn(user_prompt, model)

    if raw_text:
        metadata = _parse_llm_response(raw_text)
        if metadata["title"]:
            logger.info(f"Metadata generated via {provider}: title='{metadata['title'][:50]}…'")
            return metadata
        logger.warning(f"{provider} returned unparseable output")

    # ── Try remaining providers as fallback ──────────────────────────────
    for fallback_name, fallback_info in _PROVIDERS.items():
        if fallback_name == provider:
            continue
        logger.info(f"Trying fallback provider: {fallback_name}")
        fallback_model = _PROVIDERS[fallback_name]["default_model"]
        raw_text = fallback_info["call"](user_prompt, fallback_model)
        if raw_text:
            metadata = _parse_llm_response(raw_text)
            if metadata["title"]:
                logger.info(f"Metadata generated via {fallback_name} (fallback)")
                return metadata

    # ── Final heuristic fallback ────────────────────────────────────────
    logger.info("All LLM providers failed; using heuristic fallback.")
    return _generate_fallback_metadata(transcript_text)
