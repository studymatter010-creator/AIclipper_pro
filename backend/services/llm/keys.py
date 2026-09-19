"""
API-key vault (BYOK).

Per-provider API keys are stored in the OS-level credential store via the
``keyring`` library (Windows Credential Manager on Windows, libsecret/Keychain
elsewhere) so they are never written to settings, ``.env``, or any file that
could be shared or committed.

If ``keyring`` is not installed the vault degrades to a local obfuscated file
under the data dir so the app keeps working, and it logs an explicit warning
that the keys are not OS-encrypted and ``pip install keyring`` is recommended.

Security: keys are read only at call time, never logged, and never echoed in
error messages (the caller redacts anything that looks like a key).
"""
from __future__ import annotations

import base64
import logging

from backend.utils.config import PROJECT_ROOT, get_settings

logger = logging.getLogger("services.llm.keys")

_SERVICE = "AIClipper"

# Provider ids that hold an API key.
ANY_PROVIDERS = ("openai", "anthropic", "gemini", "openai_compatible")


class KeyVault:
    """Per-provider API key storage with an OS credential-store backend."""

    def __init__(self):
        self._backend = self._pick_backend()

    def _pick_backend(self) -> str:
        try:
            import keyring  # noqa: F401
            return "keyring"
        except Exception:  # noqa: BLE001
            logger.warning(
                "keyring not installed - API keys will be stored in an "
                "obfuscated local file instead of the OS credential store. "
                "Install it with 'pip install keyring' for real security."
            )
            return "file"

    # ── public API ──────────────────────────────────────────────────────

    def get(self, provider: str) -> str | None:
        if self._backend == "keyring":
            try:
                import keyring
                return keyring.get_password(_SERVICE, provider)
            except Exception:  # noqa: BLE001
                return None
        return self._file_get(provider)

    def set(self, provider: str, key: str) -> None:
        key = (key or "").strip()
        if not key:
            self.delete(provider)
            return
        if self._backend == "keyring":
            try:
                import keyring
                keyring.set_password(_SERVICE, provider, key)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Could not write key to OS store: {exc}")
        self._file_set(provider, key)

    def delete(self, provider: str) -> None:
        if self._backend == "keyring":
            try:
                import keyring
                keyring.delete_password(_SERVICE, provider)
            except Exception:  # noqa: BLE001
                pass
        self._file_delete(provider)

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def configured(self) -> dict[str, bool]:
        """{provider: True} for each provider with a stored key."""
        return {p: self.has(p) for p in ANY_PROVIDERS}

    # ── obfuscated-file fallback ────────────────────────────────────────

    def _file_path(self):
        s = get_settings()
        target = getattr(s, "data_dir", None) or PROJECT_ROOT / "data"
        return target / "llm_keys.json"

    def _file_get(self, provider: str) -> str | None:
        store = self._load_store()
        raw = store.get(provider)
        if not raw:
            return None
        try:
            return base64.b64decode(raw.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    def _file_set(self, provider: str, key: str) -> None:
        store = self._load_store()
        store[provider] = base64.b64encode(key.encode()).decode()
        self._save_store(store)

    def _file_delete(self, provider: str) -> None:
        store = self._load_store()
        store.pop(provider, None)
        self._save_store(store)

    def _load_store(self) -> dict[str, str]:
        try:
            import json
            p = self._file_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_store(self, store: dict[str, str]) -> None:
        try:
            import json
            p = self._file_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(store), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not persist API keys: {exc}")


# Module-level singleton used across the app.
vault = KeyVault()
