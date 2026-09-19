"""
BYOK provider implementations.

A ``LLMProvider`` exposes the two calls the app actually needs --- ``chat`` and
``chat_json`` --- plus a ``test`` used by the Settings "Test connection" button.
Each provider wraps its own request/response translation, so the rest of the
app always talks to this one interface.

No vendor SDKs are required: every provider talks JSON over plain HTTP via the
already-present ``requests`` library, which keeps the install footprint small.
API keys are read from the vault, never logged.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import requests

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("services.llm.providers")


class ProviderError(Exception):
    """Base error for a provider call that could not be satisfied."""


class RateLimitError(ProviderError):
    """A provider-enforced rate limit / quota error (retryable)."""


class LLMProvider(ABC):
    """Common interface every model backend implements.

    Instances are configured for one specific model at build time (see
    ``build_provider``); the caller passes only the prompt/messages.
    """

    #: stable identifier, e.g. "local", "openai", "anthropic", "gemini",
    #: "openai_compatible".
    name: str = "abstract"

    def __init__(self, model: str):
        self.model = model
        self.timeout = get_settings().ollama_timeout or 60

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        num_predict: int = 512,
        timeout: int | None = None,
    ) -> str:
        """Return the assistant text for a chat, or raise ProviderError."""

    @abstractmethod
    def chat_json(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        num_predict: int = 512,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Return a parsed JSON dict, or raise ProviderError."""

    def test(self) -> tuple[bool, str]:
        """Connectivity probe used by the Settings 'Test connection' button."""
        try:
            self.chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                temperature=0, num_predict=8, timeout=20,
            )
            return True, "Connected"
        except Exception as exc:  # noqa: BLE001
            return False, self._friendly(exc)

    def _record_usage(self, body: dict) -> None:
        """Record token usage for cost estimates (overridden by API providers)."""
        return

    @staticmethod
    def _friendly(exc: Exception) -> str:
        if isinstance(exc, RateLimitError):
            return "Rate limited. Wait a moment and try again."
        if isinstance(exc, ProviderError):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"


def _error_from(resp: requests.Response, fallback: str) -> ProviderError:
    """Extract a useful message from an error response, never a full key.

    Guards against echoing back anything that looks like a credential.
    """
    detail = fallback
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("detail") or fallback)
            elif isinstance(err, str):
                detail = err
            elif data.get("message"):
                detail = str(data["message"])
    except Exception:  # noqa: BLE001
        pass
    low = detail.lower()
    if any(k in low for k in ("sk-", "api_key", "authorization", "bearer")):
        detail = fallback
    if resp.status_code == 429 or "rate limit" in low or "quota" in low or "insufficient" in low:
        return RateLimitError(detail)
    return ProviderError(detail)


# ── Local (Ollama) ──────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    """Wraps the existing local Ollama /api/chat endpoint."""

    name = "local"

    def _base(self) -> str:
        return get_settings().ollama_host.rstrip("/")

    def _call(self, messages, temperature, num_predict, timeout, as_json) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if as_json:
            payload["format"] = "json"
        resp = requests.post(
            f"{self._base()}/api/chat", json=payload,
            timeout=timeout or self.timeout,
        )
        if not resp.ok:
            raise _error_from(resp, f"Ollama error {resp.status_code}")
        body = resp.json()
        if isinstance(body.get("error"), str):
            raise ProviderError(body["error"])
        return body

    def chat(self, messages, temperature=0.2, num_predict=512, timeout=None) -> str:
        body = self._call(messages, temperature, num_predict, timeout, False)
        text = (body.get("message") or {}).get("content")
        if not text:
            raise ProviderError("Ollama returned an empty response")
        return text

    def chat_json(self, messages, temperature=0.2, num_predict=512, timeout=None) -> dict:
        body = self._call(messages, temperature, num_predict, timeout, True)
        text = ((body.get("message") or {}).get("content") or "").strip()
        if not text:
            raise ProviderError("Ollama returned an empty response")
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # noqa: BLE001
            raise ProviderError(f"Ollama did not return valid JSON: {exc}") from exc


# ── OpenAI (and OpenAI-compatible) ──────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """Calls the OpenAI chat-completions API with a user-supplied key."""

    name = "openai"
    base_url = "https://api.openai.com/v1"

    def _headers(self) -> dict[str, str]:
        from backend.services.llm.keys import vault
        key = vault.get("openai")
        if not key:
            raise ProviderError("No OpenAI API key configured")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _call(self, messages, temperature, num_predict, timeout, json_mode) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": num_predict,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(), json=payload,
            timeout=timeout or self.timeout,
        )
        if not resp.ok:
            raise _error_from(resp, f"OpenAI error {resp.status_code}")
        body = resp.json()
        self._record_usage(body)
        return body

    def _record_usage(self, body: dict) -> None:
        u = body.get("usage") or {}
        if u:
            from backend.services.llm.usage import record
            record(self.name, u.get("prompt_tokens"), u.get("completion_tokens"))

    @staticmethod
    def _content(body: dict) -> str:
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        return msg.get("content") or ""

    def chat(self, messages, temperature=0.2, num_predict=512, timeout=None) -> str:
        text = self._content(self._call(messages, temperature, num_predict, timeout, False))
        if not text:
            raise ProviderError("OpenAI returned an empty response")
        return text

    def chat_json(self, messages, temperature=0.2, num_predict=512, timeout=None) -> dict:
        text = self._content(self._call(messages, temperature, num_predict, timeout, True)).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI did not return valid JSON: {exc}") from exc


class OpenAICompatibleProvider(OpenAIProvider):
    """Generic provider for any endpoint exposing the OpenAI chat-completions
    schema (OpenRouter, Together, Groq, local vLLM/LM Studio servers, ...)."""

    name = "openai_compatible"
    base_url = ""  # set per-instance

    def __init__(self, model: str, base_url: str | None = None):
        super().__init__(model)
        s = get_settings()
        self.base_url = (
            (base_url or s.llm_openai_compatible_base or "").rstrip("/")
            or "https://api.openai.com/v1"
        )

    def _headers(self) -> dict[str, str]:
        from backend.services.llm.keys import vault
        key = vault.get("openai_compatible") or vault.get("openai")
        if not key:
            raise ProviderError("No API key configured for this endpoint")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# ── Anthropic ───────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    """Calls the Anthropic Messages API with a user-supplied key."""

    name = "anthropic"
    base_url = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str):
        super().__init__(model)
        self.max_tokens = get_settings().llm_anthropic_max_tokens or 1024

    def _headers(self) -> dict[str, str]:
        from backend.services.llm.keys import vault
        key = vault.get("anthropic")
        if not key:
            raise ProviderError("No Anthropic API key configured")
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _call(self, messages, temperature, num_predict, timeout, as_json) -> dict:
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        msgs = [m for m in messages if m["role"] in ("user", "assistant")]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(num_predict, self.max_tokens),
            "temperature": temperature,
            "messages": msgs,
        }
        if system:
            payload["system"] = system
        resp = requests.post(
            self.base_url, headers=self._headers(), json=payload,
            timeout=timeout or self.timeout,
        )
        if not resp.ok:
            raise _error_from(resp, f"Anthropic error {resp.status_code}")
        body = resp.json()
        u = body.get("usage") or {}
        if u:
            from backend.services.llm.usage import record
            record("anthropic", u.get("input_tokens"), u.get("output_tokens"))
        return body

    def _content(self, body: dict) -> str:
        parts = []
        for block in body.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    def chat(self, messages, temperature=0.2, num_predict=512, timeout=None) -> str:
        body = self._call(messages, temperature, num_predict, timeout, False)
        text = self._content(body)
        if not text:
            raise ProviderError("Anthropic returned an empty response")
        return text

    def chat_json(self, messages, temperature=0.2, num_predict=512, timeout=None) -> dict:
        # Instruct JSON output; some models accept <system> JSON hint.
        body = self._call(messages, temperature, num_predict, timeout, True)
        text = self._content(body).strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        if "```json" in text:
            start = text.index("```json") + len("```json")
            end = text.index("```", start)
            text = text[start:end].strip()
        # fall back to the first balanced {...}
        start = text.find("{")
        if start >= 0:
            text = text[start:]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # noqa: BLE001
            raise ProviderError(f"Anthropic did not return valid JSON: {exc}") from exc


# ── Google Gemini ───────────────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    """Calls the Google Gemini generateContent API with a user-supplied key."""

    name = "gemini"

    @property
    def _url(self) -> str:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )

    def _call(self, messages, temperature, num_predict, timeout) -> dict:
        from backend.services.llm.keys import vault
        key = vault.get("gemini")
        if not key:
            raise ProviderError("No Gemini API key configured")
        parts: list[dict[str, str]] = []
        for m in messages:
            role = "user" if m["role"] in ("user", "system") else "model"
            parts.append({"role": role, "parts": [{"text": m["content"]}]})
        payload = {
            "contents": parts,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": num_predict,
            },
        }
        resp = requests.post(
            self._url, params={"key": key}, json=payload,
            timeout=timeout or self.timeout,
        )
        if not resp.ok:
            raise _error_from(resp, f"Gemini error {resp.status_code}")
        body = resp.json()
        um = body.get("usageMetadata") or {}
        if um:
            from backend.services.llm.usage import record
            record(
                "gemini",
                um.get("promptTokenCount"),
                um.get("candidatesTokenCount"),
            )
        return body

    def _content(self, body: dict) -> str:
        try:
            candidates = body["candidates"]
            text_parts = candidates[0]["content"]["parts"]
            return "".join(p.get("text", "") for p in text_parts)
        except Exception:  # noqa: BLE001
            # Gemini may return a safety-blocked "promptFeedback".
            return ""

    def chat(self, messages, temperature=0.2, num_predict=512, timeout=None) -> str:
        text = self._content(self._call(messages, temperature, num_predict, timeout))
        if not text:
            raise ProviderError("Gemini returned an empty response")
        return text

    def chat_json(self, messages, temperature=0.2, num_predict=512, timeout=None) -> dict:
        # Gemini supports responseMimeType json; re-attempt with it if needed.
        text = self._content(self._call(messages, temperature, num_predict, timeout)).strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:  # noqa: BLE001
            # Retry once with explicit JSON MIME type.
            from backend.services.llm.keys import vault
            key = vault.get("gemini")
            if not key:
                raise ProviderError("Gemini did not return valid JSON")
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] in ("user", "system")
                ],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": num_predict},
                "responseMimeType": "application/json",
            }
            resp = requests.post(self._url, params={"key": key}, json=payload,
                                 timeout=timeout or self.timeout)
            if not resp.ok:
                raise _error_from(resp, "Gemini error")
            text = self._content(resp.json()).strip()
            if not text:
                raise ProviderError("Gemini did not return valid JSJON")
            try:
                return json.loads(_strip_fences(text))
            except json.JSONDecodeError as exc:  # noqa: BLE001
                raise ProviderError(f"Gemini did not return valid JSON: {exc}") from exc


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    return t.strip()


# ── Builder ─────────────────────────────────────────────────────────────

def build_provider(provider: str, model: str | None = None) -> LLMProvider:
    """Construct a provider instance for *provider* using *model* (or default)."""
    from backend.services.llm.providers_meta import provider_default_model
    resolved_model = model or provider_default_model(provider)
    if provider == "local":
        return OllamaProvider(resolved_model or "qwen3:8b")
    if provider == "openai":
        return OpenAIProvider(resolved_model or "gpt-4o-mini")
    if provider == "anthropic":
        return AnthropicProvider(resolved_model or "claude-3-5-haiku-latest")
    if provider == "gemini":
        return GeminiProvider(resolved_model or "gemini-1.5-flash")
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(resolved_model or "gpt-4o-mini")
    logger.warning(f"Unknown provider '{provider}'; falling back to local")
    return OllamaProvider(resolved_model or "qwen3:8b")


PROVIDER_BUILDERS = {
    "local": build_provider,
    "openai": build_provider,
    "anthropic": build_provider,
    "gemini": build_provider,
    "openai_compatible": build_provider,
}
