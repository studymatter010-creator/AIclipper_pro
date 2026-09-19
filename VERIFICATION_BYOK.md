# Bring-Your-Own-Key (BYOK) AI Providers — Verification Guide

This documents the manual checks to run on your Windows machine after the change.
All backend files were `python3 -m py_compile`-verified and all frontend JS files
`node --check`-verified in this session; these steps confirm behaviour end-to-end
on the real runtime.

## What changed

| Area | New capability |
|------|----------------|
| `backend/services/llm/providers.py` | Pluggable `LLMProvider` interface: `chat()` + `chat_json()`. Implementations for Ollama (local), OpenAI, Anthropic, Gemini, and OpenAI-compatible endpoints. Each handles its own request/response translation. |
| `backend/services/llm/keys.py` | OS-level API-key vault via `keyring` (Windows Credential Manager), with an obfuscated fallback file. **Keys are never logged, returned, or written as plaintext.** |
| `backend/services/llm/role_config.py` | In-memory per-role routing cache (`brain/classify/hook/translate` → provider + model), seeded from the DB at startup. Defaults every role to **Local/Ollama**. |
| `backend/services/llm/usage.py` | Session token counters + ≈USD cost estimate per active provider. |
| `backend/services/model_team.py` | `chat()`/`chat_json()` now route each role through its configured provider; on API failure they retry with rate-limit-aware backoff, then **fall back to Local/Ollama** for that call and log it clearly. RAM sequencer estimates updated for the Part-1 A/B candidates (+ over-budget warning). |
| `backend/api/routes/providers.py` | `GET/PUT /api/providers`, `POST /api/providers/test`, `PUT/DELETE /api/providers/key`, `GET /api/providers/usage`. |
| `backend/api/routes/editor.py`, `app.py` | `team_status` exposes active provider per role, key presence, session cost; startup seeds routing from saved settings. |
| `frontend/js/app.js`, `api.js`, `index.html`, `styles.css` | **AI Providers** Settings panel (per-role dropdown + model + Test, per-provider key mgmt) and the **AI Models** sidebar widget now shows provider tag + Free/Paid tier per role. |
| `scripts/benchmark_models.py` | Part-1 A/B harness comparing local model candidates vs current picks. |

## Restart first

Stop the app and relaunch. Every role defaults to **Local/Ollama**, so the app is
fully functional with **zero API keys**.

---

## 1 · Zero-key path still works

1. Do NOT configure any key. Go to **AI Providers** in Settings.
2. All four roles show **Local (Ollama)**, greyed-out model + disabled Test.
3. Generate clips as normal — the AI team runs fully on Ollama. The sidebar
   **AI Models** widget shows four `Local` rows with a green ready dot and no
   "paid" badge.

## 2 · Route one role through an API provider

1. In Settings → **AI Providers**, paste a real API key (e.g. OpenAI) under
   **API Keys** and click **Save**.
2. For one role (e.g. `brain`), switch the dropdown to **OpenAI**, type a model
   (or leave blank for the default), click **Test** → expect **✓ Connected**.
3. Run a video through the pipeline. The sidebar widget's `brain` row now shows
   an **OpenAI** provider tag + a **paid** badge.
4. **AI Providers** panel shows a "Session usage" bar with call/token count and
   ≈ USD estimate.

## 3 · Invalid key falls back to local (no crash)

1. Set a role to an API provider and store a **deliberately wrong** key.
2. Run processing. Expected:
   - The pipeline does **not** fail — that role falls back to Local/Ollama after
     the configured retries with backoff.
   - A clear line is logged: `API provider '<provider>' failed for role '<role>' ... Falling back to local`.
   - The UI keeps working (the fallback result is used, heuristics continue).
3. (Optional) To see the backoff, watch the server logs while it retries rather
   than hammering the key into a rate-limit ban.

## 4 · Keys are not leaked

1. In Settings → API Keys, Save a key, then look at:
   - The Network tab / devtools — the key you type appears only in the `PUT /api/providers/key` request you made; no endpoint **returns** it.
   - Server debug logs — search for the key value: it must **not** appear anywhere.
2. The key input shows masked dots after saving (placeholder "•••••••• (key saved)").

## 5 · Part-1 local model A/B (quality + latency)

Run on your Windows machine with Ollama running:

```
python scripts/benchmark_models.py --repeats 3
```

This benchmarks `gpt-oss-20b`, `deepseek-r1-distill-qwen-14b`, `qwen3.5-4b`
against the current picks (`qwen3:8b` brain, `qwen2.5:3b` specialist) on four
real-roled prompts (rate-clip, classify, hook, translate-zh), reporting latency,
throughput, output length and JSON validity. It writes
`benchmark_results.csv` + `benchmark_report.md`.

**Do not blind-swap.** Keep a candidate only if it matches/beats the current pick
on both quality and acceptable latency. Note the RAM footprints in the report:
on a 16 GB machine a 20B brain cannot coexist with Whisper — raise
`team_ram_budget_mb` only on a bigger-RAM box.
