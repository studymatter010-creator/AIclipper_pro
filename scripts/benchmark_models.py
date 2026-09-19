#!/usr/bin/env python3
"""
AIClipper Model Team A/B Benchmark
===================================

Compare local Ollama model candidates on real-clip prompts and report
quality + latency before adopting a new default.

Usage (run on your Windows machine with Ollama running):

    python scripts/benchmark_models.py --clips ./test_clips/ --repeats 3

By default it benchmarks the three Part-1 candidates against the current
picks.  Pass ``--models`` to override.

    python scripts/benchmark_models.py --models qwen3:8b,gpt-oss-20b,deepseek-r1-distill-qwen-14b

Output:  benchmark_results.csv  +  benchmark_report.md   in the project root.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── Ollama base URL ──────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

import requests  # noqa: E402  (stdlib-ish, always available)

# ── Default models ───────────────────────────────────────────────────────
CURRENT_BRAIN    = "qwen3:8b"
CURRENT_SPECIALIST = "qwen2.5:3b"
CANDIDATE_BRAINS = ["gpt-oss-20b", "deepseek-r1-distill-qwen-14b"]
CANDIDATE_SPECIALIST = "qwen3.5-4b"

# ── RAM footprints (MB) — same dict as model_team.py ─────────────────────
_RAM_MB = {
    "qwen3:8b": 5400, "qwen2.5:3b": 2600, "qwen2.5:7b": 5200,
    "qwen3:4b": 3200, "qwen3:1.7b": 1600, "llama3.2:3b": 2500,
    "gpt-oss-20b": 13000, "deepseek-r1-distill-qwen-14b": 9200,
    "qwen3.5-4b": 3200,
}

# ── Representative prompts per role ──────────────────────────────────────
# These mirror the production prompts faithfully.

SAMPLE_TRANSCRIPT = (
    "00:00 [Speaker 1] Welcome to the show today. We're going to talk about "
    "the most important things in life. You know, people always ask me what's "
    "the one thing you'd change about yourself if you could. And I always say "
    "the same thing: discipline. Without discipline, talent is just potential. "
    "00:12 [Speaker 2] That's interesting because I've been thinking about "
    "that exact topic. When I started my company, the hardest part wasn't the "
    "idea — it was showing up every single day when nobody believed in it.\n"
    "00:25 [Speaker 1] Right. And here's what people don't understand: "
    "discipline isn't about being perfect. It's about being consistent. You "
    "don't need to train for five hours. You just need to train for thirty "
    "minutes every day. That compounds into something massive over a year.\n"
    "00:40 [Speaker 2] I love that. Consistency over intensity. That's actually "
    "exactly how we built our product. Small improvements every week. Ships "
    "that nobody noticed at first, but after six months, the whole thing was "
    "completely different. The compounding effect is real.\n"
    "00:55 [Speaker 1] And that's the thing about talent — it's overrated. "
    "What actually moves the needle is showing up when you don't feel like it. "
    "And I think the modern world has confused that message. People want the "
    "shortcut. They want the life hack. But there is no hack. There's just work."
)

BRAIN_MESSAGES = [
    {"role": "system", "content": (
        "You are a viral-shorts expert. Rate this clip for short-form video potential "
        "(1-10) and explain why in one sentence. Reply with JSON: "
        '{"score": <int>, "reason": "<string>"}'
    )},
    {"role": "user", "content": f"Transcript:\n{SAMPLE_TRANSCRIPT}"},
]
BRAIN_NUM_PREDICT = 128

CLASSIFY_MESSAGES = [
    {"role": "user", "content": (
        f"Analyze this video transcript sample and classify the content type.\n"
        f"Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.\n"
        f"Also estimate content density: low, medium, or high.\n"
        f"Respond with JSON only: {{\"content_type\": \"...\", \"density\": \"...\"}}\n\n"
        f"Transcript sample:\n{SAMPLE_TRANSCRIPT[:3000]}"
    )},
]
CLASSIFY_NUM_PREDICT = 128

HOOK_MESSAGES = [
    {"role": "user", "content": (
        "You are a viral-shorts hook expert. Read the clip transcript and write ONE "
        "attention-grabbing opening line (<= 40 characters, no quotes, no hashtags) that "
        "would stop a scrolling viewer in the first 3 seconds. Reply with only the line.\n\n"
        f"Transcript:\n{SAMPLE_TRANSCRIPT}"
    )},
]
HOOK_NUM_PREDICT = 64

TRANSLATE_MESSAGES = [
    {"role": "system", "content": (
        "You are a professional subtitle translator for short-form videos. "
        "Translate the following subtitle segments into Simplified Chinese. "
        "Keep translations concise (suitable for on-screen subtitles). "
        "Maintain the meaning and energy. Reply with ONLY the translated text, "
        "one segment per line, no numbering, no commentary."
    )},
    {"role": "user", "content": (
        "Discipline without talent is just potential.\n"
        "Consistency over intensity.\n"
        "There is no hack. There's just work."
    )},
]
TRANSLATE_NUM_PREDICT = 256

# All 4 roles, each paired with the model-role it maps to.
ROLE_BENCHMARKS = [
    {"role": "brain",       "role_key": "brain",        "messages": BRAIN_MESSAGES,       "num_predict": BRAIN_NUM_PREDICT,       "as_json": True,  "prompt_label": "rate-clip"},
    {"role": "classify",    "role_key": "specialist",    "messages": CLASSIFY_MESSAGES,     "num_predict": CLASSIFY_NUM_PREDICT,    "as_json": True,  "prompt_label": "classify"},
    {"role": "hook",        "role_key": "specialist",    "messages": HOOK_MESSAGES,         "num_predict": HOOK_NUM_PREDICT,        "as_json": False, "prompt_label": "hook"},
    {"role": "translate",   "role_key": "specialist",    "messages": TRANSLATE_MESSAGES,    "num_predict": TRANSLATE_NUM_PREDICT,   "as_json": False, "prompt_label": "translate-zh"},
]


@dataclass
class BenchResult:
    role: str
    role_key: str
    prompt_label: str
    model: str
    latency_s: float = 0.0
    eval_count: int = 0
    prompt_eval_count: int = 0
    output_len: int = 0
    output_text: str = ""
    success: bool = False
    json_valid: bool = False
    tokens_per_s: float = 0.0
    error: str = ""


def _ollama_chat(model, messages, temperature, num_predict, as_json) -> dict:
    """Single /api/chat round-trip; returns the full response body."""
    payload = {
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if as_json:
        payload["format"] = "json"
    resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _parse_chat(body: dict) -> str:
    return (body.get("message") or {}).get("content") or ""


def _validate_json(text: str) -> bool:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`").removeprefix("json").strip()
    try:
        json.loads(clean)
        return True
    except Exception:
        return False


def _bench_one(model: str, bench: dict, *, temperature: float = 0.2) -> BenchResult:
    res = BenchResult(
        role=bench["role"], role_key=bench["role_key"],
        prompt_label=bench["prompt_label"], model=model,
    )
    t0 = time.perf_counter()
    try:
        body = _ollama_chat(
            model=model,
            messages=bench["messages"],
            temperature=temperature,
            num_predict=bench["num_predict"],
            as_json=bench["as_json"],
        )
        text = _parse_chat(body)
        res.latency_s = round(time.perf_counter() - t0, 3)
        res.output_len = len(text)
        res.output_text = text[:500]
        res.success = bool(text.strip())
        res.json_valid = _validate_json(text) if bench["as_json"] else True
        u = body.get("prompt_eval_count") or 0
        d = body.get("eval_count") or 0
        res.prompt_eval_count = u
        res.eval_count = d
        dur_s = max((body.get("eval_duration") or 0) / 1e9, 0.001)
        res.tokens_per_s = round(d / dur_s, 1) if d else 0.0
    except Exception as exc:
        res.latency_s = round(time.perf_counter() - t0, 3)
        res.error = str(exc)[:200]
        res.success = False
    return res


def _fmt_lat(lat: float) -> str:
    return f"{lat:.2f}s"


def _fmt_tps(tps: float) -> str:
    return f"{tps:.1f}t/s" if tps else "—"


def run_benchmark(models: list[str], repeats: int = 3) -> list[BenchResult]:
    all_res: list[BenchResult] = []
    total = len(models) * len(ROLE_BENCHMARKS) * repeats
    done = 0
    for model in models:
        print(f"\n{'='*60}\n  MODEL: {model}  ({_RAM_MB.get(model, '?')} MB)\n{'='*60}")
        for bench in ROLE_BENCHMARKS:
            lats, tps_list = [], []
            best: BenchResult | None = None
            for i in range(repeats):
                res = _bench_one(model, bench)
                all_res.append(res)
                done += 1
                if res.success:
                    lats.append(res.latency_s)
                    tps_list.append(res.tokens_per_s)
                if best is None or (res.success and (not best.success or res.output_len > best.output_len)):
                    best = res
                tag = "✓" if res.success else f"✗ {res.error[:40]}"
                print(f"  [{done}/{total}] {bench['prompt_label']} r{i+1}: {_fmt_lat(res.latency_s)} {_fmt_tps(res.tokens_per_s)} {tag}")
            avg_lat = round(sum(lats)/len(lats), 2) if lats else None
            avg_tps = round(sum(tps_list)/len(tps_list), 1) if tps_list else None
            if best:
                best = BenchResult(
                    role=best.role, role_key=best.role_key,
                    prompt_label=best.prompt_label, model=model,
                    latency_s=avg_lat or best.latency_s,
                    tokens_per_s=avg_tps or best.tokens_per_s,
                    output_len=best.output_len,
                    output_text=best.output_text,
                    success=best.success, json_valid=best.json_valid,
                )
                all_res.append(best)  # <-- avg record
    return all_res


def _write_csv(results: list[BenchResult], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model","role","prompt","latency_s","tokens_per_s",
                     "output_len","success","json_valid","output_preview"])
        for r in results:
            w.writerow([r.model, r.role, r.prompt_label, r.latency_s,
                        r.tokens_per_s, r.output_len, r.success, r.json_valid,
                        r.output_text[:200].replace("\n"," ")])


def _write_report(results: list[BenchResult], models: list[str], repeats: int, path: Path) -> None:
    lines = [
        "# Model Team A/B Benchmark Report\n",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}\n",
        f"**Repeats per combo:** {repeats}\n",
        f"**Models tested:** {', '.join(models)}\n",
        "## RAM Footprints\n",
    ]
    for m in models:
        lines.append(f"- **{m}**: ~{_RAM_MB.get(m, '?')} MB")
    lines.append("")
    lines.append("## Results by Role\n")
    seen = set()
    for r in results:
        key = (r.role, r.model, r.prompt_label)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"### {r.role} (`{r.prompt_label}`) — {r.model}")
        lines.append(f"- Latency: {_fmt_lat(r.latency_s)}")
        lines.append(f"- Throughput: {_fmt_tps(r.tokens_per_s)}")
        lines.append(f"- Output len: {r.output_len} chars")
        lines.append(f"- Success: {'✓' if r.success else '✗'}  |  JSON valid: {'✓' if r.json_valid else '✗'}")
        lines.append(f"- Preview: `{r.output_text[:160]}`")
        lines.append("")

    # ── Recommendation ──
    lines.append("## Recommendation\n")
    lines.append("Compare latency + quality (output length, JSON validity, preview text) across models.\n")
    lines.append("**Do NOT blindly swap defaults.** Keep the current picks unless a candidate\n")
    lines.append("strictly matches or beats them on both quality AND acceptable latency.\n")
    lines.append("On a 16 GB machine, the 20B brains cannot coexist with Whisper; raise\n")
    lines.append("`team_ram_budget_mb` (or use a 32 GB box) if you adopt one.\n")

    # Per-role comparison (fastest overall successful candidate wins the
    # latency row; quality is in the previews above).
    for role_name, current in [
        ("brain", CURRENT_BRAIN),
        ("classify", CURRENT_SPECIALIST),
        ("hook", CURRENT_SPECIALIST),
        ("translate", CURRENT_SPECIALIST),
    ]:
        lines.append(f"### Weighted summary for `{role_name}`\n")
        lines.append("| model | vs default latency | output len | success |")
        lines.append("|-------|--------------------|-----------|---------|")
        recs = [r for r in results if r.role == role_name]
        for r in sorted(recs, key=lambda x: x.model):
            vs = ""
            base = [x for x in recs if x.model == current]
            if base and r.model != current and base[0].latency_s:
                delta = r.latency_s - base[0].latency_s
                vs = f"{delta:+.2f}s" if delta else "≈"
            lines.append(f"| {r.model} | {vs or _fmt_lat(r.latency_s)} | {r.output_len} | {'✓' if r.success else '✗'} |")
        lines.append("")
        if role_name == "brain":
            lines.append(f"**Note:** the 20B brain candidates cost more RAM than the whole 9000 MB")
            lines.append(f"budget on their own — adopt them only on a bigger-RAM box.\n")

    path.write_text("\n".join(lines), encoding="utf-8")

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="AIClipper model A/B benchmark")
    parser.add_argument("--models", type=str, default=None,
                        help="Comma-separated models to benchmark")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Repeats per model×role combo (default 3)")
    parser.add_argument("--host", type=str, default=None,
                        help="Ollama host URL override")
    args = parser.parse_args()

    global OLLAMA_HOST
    if args.host:
        OLLAMA_HOST = args.host

    # Check Ollama is reachable
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        installed = {m.get("name","").split(":")[0] for m in r.json().get("models",[])}
    except Exception as e:
        print(f"ERROR: Cannot reach Ollama at {OLLAMA_HOST}: {e}")
        sys.exit(1)

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = [CURRENT_BRAIN, *CANDIDATE_BRAINS, CURRENT_SPECIALIST, CANDIDATE_SPECIALIST]

    missing = [m for m in models if m.split(":")[0] not in installed and m not in installed]
    if missing:
        print(f"WARNING: these models are not pulled in Ollama and may fail: {missing}")
        print(f"  Pull them first: ollama pull {'  ollama pull '.join(missing)}\n")

    print(f"Benchmarking {len(models)} models × {len(ROLE_BENCHMARKS)} roles × {args.repeats} repeats")
    print(f"Ollama: {OLLAMA_HOST}\n")

    results = run_benchmark(models, repeats)

    root = Path(__file__).resolve().parent.parent
    csv_path = root / "benchmark_results.csv"
    report_path = root / "benchmark_report.md"
    _write_csv(results, csv_path)
    _write_report(results, models, args.repeats, report_path)
    print(f"\n{'='*60}")
    print(f"  Results written to:\n    {csv_path}\n    {report_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
