#!/usr/bin/env python3
"""Measure how different OpenAI models perform as the ANALYST step.

Runs each model on every gold case in `analyst_cases.py`, scores the TestSpec it
produces against the answer key, and prints a per-model comparison table.

SAFETY: defaults to a FREE dry-run (no API calls). It only spends money when you
pass --execute.

Usage:
  # Free: shows the plan + a rough cost ceiling, makes zero API calls
  uv run python eval/analyst_eval.py

  # Paid: actually calls the models (uses OPENAI_API_KEY from .env)
  uv run python eval/analyst_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,gpt-4o-mini,gpt-4o --repeats 2

Scored criteria (see analyst_cases.py for the answer-key fields):
  C1  provided_values precision / recall  + label-leak count
  C2  structured-output success rate (did .parse() return a valid TestSpec)
  C3  acceptance-criteria recall (fuzzy)
  C4  restraint pass-rate (didn't invent missing values; flagged gaps in notes)
  C5  latency, token usage, estimated cost
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# Make `analyst_cases` and `qa_agent` importable when run from anywhere.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from analyst_cases import CASES, AnalystCase  # noqa: E402

# .env values must win over any empty shell vars (e.g. ANTHROPIC_API_KEY="").
from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=True)

# Cross-vendor default slate. Override with --models.
DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    "gemini-2.5-flash", "gemini-2.5-pro",
]

# Rough USD per 1M tokens (input, output). VERIFY against current vendor pricing —
# these are ballpark figures for cost-estimation only.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1":      (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o":       (2.50, 10.00),
    "gpt-4o-mini":  (0.15, 0.60),
    "claude-opus-4-6":            (15.00, 75.00),
    "claude-sonnet-4-6":          (3.00, 15.00),
    "claude-haiku-4-5-20251001":  (1.00, 5.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro":   (1.25, 10.00),
}

# Reasoning / GPT-5 family reject a custom temperature.
_FIXED_TEMP_PREFIXES = ("gpt-5", "o1", "o3", "o4")


# ─────────────────────────── scoring helpers ─────────────────────────────


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _values_match(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def _fuzzy_covered(gold: str, produced: list[str], threshold: float = 0.45) -> bool:
    """Is this gold criterion covered by any produced criterion?"""
    g = _norm(gold)
    g_words = set(g.split())
    for p in produced:
        pn = _norm(p)
        if SequenceMatcher(None, g, pn).ratio() >= threshold:
            return True
        p_words = set(pn.split())
        if g_words and p_words:
            jacc = len(g_words & p_words) / len(g_words | p_words)
            if jacc >= 0.40:
                return True
    return False


def _looks_like_label_key(key: str) -> bool:
    k = key.lower()
    return any(k.endswith(suf) for suf in ("-label", "-text", "-heading", "-title", "-button"))


@dataclass
class CaseScore:
    case_id: str
    ok: bool = False                 # parse succeeded
    error: str = ""
    pv_recall: float = 0.0
    pv_precision: float = 0.0
    label_leaks: int = 0
    criteria_recall: float = 0.0
    restraint_applicable: bool = False
    restraint_pass: bool = False
    app_url_ok: bool = False
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_spec(case: AnalystCase, spec) -> CaseScore:
    s = CaseScore(case_id=case.id, ok=True)

    extracted: dict[str, str] = {pv.key: pv.value for pv in spec.provided_values}
    gold = case.gold_provided_values

    # C1 — precision / recall matched by VALUE, not key. The prompt never mandates
    # exact key names (it even suggests synonyms like `customer-search-query`), so
    # scoring by key wrongly fails correct extractions. We match each gold value to
    # at most one extracted value (normalized equal, or substring for long strings).
    def _val_match(a: str, b: str) -> bool:
        na, nb = _norm(a), _norm(b)
        if na == nb:
            return True
        return len(na) > 8 and len(nb) > 8 and (na in nb or nb in na)

    gold_vals = list(gold.values())
    ext_vals = list(extracted.values())
    used = [False] * len(ext_vals)
    covered = 0
    for gv in gold_vals:
        for i, ev in enumerate(ext_vals):
            if not used[i] and _val_match(gv, ev):
                used[i] = True
                covered += 1
                break
    s.pv_recall = (covered / len(gold_vals)) if gold_vals else 1.0
    s.pv_precision = (covered / len(ext_vals)) if ext_vals else (1.0 if not gold_vals else 0.0)

    # C1 — label leaks: extracted value equals/contains a forbidden UI label,
    # or the key itself is shaped like a label.
    leaks = 0
    forbid = [_norm(f) for f in case.forbidden_labels]
    for k, v in extracted.items():
        vn = _norm(v)
        if any(vn == f or (f and f in vn) or (vn and vn in f) for f in forbid):
            leaks += 1
        elif _looks_like_label_key(k):
            leaks += 1
    s.label_leaks = leaks

    # C3 — acceptance-criteria recall
    produced = list(spec.acceptance_criteria)
    if case.gold_criteria:
        covered = sum(1 for g in case.gold_criteria if _fuzzy_covered(g, produced))
        s.criteria_recall = covered / len(case.gold_criteria)
    else:
        s.criteria_recall = 1.0

    # C4 — restraint
    if case.has_restraint_check:
        s.restraint_applicable = True
        fabricated = any(
            k.lower() in {m.lower() for m in case.must_not_fabricate}
            for k in extracted
        )
        notes_l = (spec.notes or "").lower()
        flagged = (
            any(t.lower() in notes_l for t in case.must_flag_terms)
            if case.must_flag_terms else True
        )
        s.restraint_pass = (not fabricated) and flagged

    # app URL
    s.app_url_ok = _norm(spec.app_url or "") == _norm(case.gold_app_url or "")
    return s


# ─────────────────────────── model call ──────────────────────────────────


def _temp_kwargs(model: str, temperature: float) -> dict:
    if any(model.startswith(p) for p in _FIXED_TEMP_PREFIXES):
        return {}
    return {"temperature": temperature}


def _provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gemini", "gemma")):
        return "google"
    return "openai"


_anthropic_client = None
_google_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import os
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def _google():
    global _google_client
    if _google_client is None:
        import os
        from google import genai
        _google_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _google_client


def _call_openai(model: str, system: str, user: str):
    """Returns (parsed_TestSpec | None, prompt_tokens, completion_tokens)."""
    from qa_agent.llm import _get_client
    from qa_agent.models import TestSpec
    resp = _get_client().beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format=TestSpec,
        **_temp_kwargs(model, 0.2),
    )
    u = resp.usage
    return (
        resp.choices[0].message.parsed,
        (u.prompt_tokens if u else 0),
        (u.completion_tokens if u else 0),
    )


def _call_anthropic(model: str, system: str, user: str):
    """Claude structured output via a forced tool call whose schema == TestSpec."""
    from qa_agent.models import TestSpec
    tool = {
        "name": "emit_test_spec",
        "description": "Return the structured TestSpec extracted from the PRD.",
        "input_schema": TestSpec.model_json_schema(),
    }
    msg = _anthropic().messages.create(
        model=model,
        max_tokens=2048,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "tool", "name": "emit_test_spec"},
    )
    data = next((b.input for b in msg.content if b.type == "tool_use"), None)
    parsed = TestSpec.model_validate(data) if data is not None else None
    u = msg.usage
    return parsed, (u.input_tokens if u else 0), (u.output_tokens if u else 0)


def _call_google(model: str, system: str, user: str):
    """Gemini structured output via response_schema = TestSpec."""
    from google.genai import types
    from qa_agent.models import TestSpec
    resp = _google().models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=TestSpec,
            temperature=0.2,
        ),
    )
    parsed = resp.parsed  # a TestSpec instance (or None)
    um = resp.usage_metadata
    return (
        parsed,
        (um.prompt_token_count if um else 0) or 0,
        (um.candidates_token_count if um else 0) or 0,
    )


def run_one(model: str, case: AnalystCase) -> CaseScore:
    """Call the model once on a case and score it. Faithful to analyst.analyze()."""
    from qa_agent.prompts import ANALYST_SYSTEM

    user = (
        f"App URL provided by user (use this if PRD omits one): {case.app_url}\n\n"
        f"PRD:\n{case.prd_text}"
    )
    caller = {
        "openai": _call_openai,
        "anthropic": _call_anthropic,
        "google": _call_google,
    }[_provider(model)]

    t0 = time.perf_counter()
    try:
        parsed, ptok, ctok = caller(model, ANALYST_SYSTEM, user)
        latency = time.perf_counter() - t0
        if parsed is None:
            return CaseScore(case_id=case.id, ok=False, error="no parsed object", latency_s=latency)
        if not parsed.app_url:
            parsed.app_url = case.app_url
        s = score_spec(case, parsed)
        s.latency_s = latency
        s.prompt_tokens = ptok or 0
        s.completion_tokens = ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001 — any failure = a failed (C2) run
        return CaseScore(
            case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
            latency_s=time.perf_counter() - t0,
        )


# ─────────────────────────── aggregation / output ─────────────────────────


@dataclass
class ModelResult:
    model: str
    scores: list[CaseScore] = field(default_factory=list)

    def _ok(self) -> list[CaseScore]:
        return [s for s in self.scores if s.ok]

    @property
    def parse_rate(self) -> float:
        return len(self._ok()) / len(self.scores) if self.scores else 0.0

    def _avg(self, attr: str) -> float:
        ok = self._ok()
        return sum(getattr(s, attr) for s in ok) / len(ok) if ok else 0.0

    @property
    def pv_recall(self) -> float: return self._avg("pv_recall")
    @property
    def pv_precision(self) -> float: return self._avg("pv_precision")
    @property
    def criteria_recall(self) -> float: return self._avg("criteria_recall")
    @property
    def label_leaks(self) -> int: return sum(s.label_leaks for s in self._ok())

    @property
    def restraint_rate(self) -> float:
        applicable = [s for s in self._ok() if s.restraint_applicable]
        return sum(1 for s in applicable if s.restraint_pass) / len(applicable) if applicable else 1.0

    @property
    def avg_latency(self) -> float: return self._avg("latency_s")

    @property
    def total_tokens(self) -> int:
        return sum(s.prompt_tokens + s.completion_tokens for s in self._ok())

    @property
    def est_cost(self) -> float | None:
        if self.model not in PRICES:
            return None
        in_price, out_price = PRICES[self.model]
        ok = self._ok()
        cost = sum(
            (s.prompt_tokens / 1e6) * in_price + (s.completion_tokens / 1e6) * out_price
            for s in ok
        )
        return cost


def print_table(results: list[ModelResult]) -> None:
    print("\n" + "=" * 100)
    print("ANALYST MODEL COMPARISON  (averaged across cases × repeats)")
    print("=" * 100)
    header = (
        f"{'model':<16} {'C2 parse':>9} {'C1 prec':>8} {'C1 rec':>7} "
        f"{'leaks':>6} {'C3 crit':>8} {'C4 rstr':>8} {'lat(s)':>7} {'tok':>7} {'~$':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        cost = r.est_cost
        cost_str = f"{cost:.4f}" if cost is not None else "n/a"
        print(
            f"{r.model:<16} {r.parse_rate*100:>8.0f}% {r.pv_precision*100:>7.0f}% "
            f"{r.pv_recall*100:>6.0f}% {r.label_leaks:>6d} {r.criteria_recall*100:>7.0f}% "
            f"{r.restraint_rate*100:>7.0f}% {r.avg_latency:>7.2f} {r.total_tokens:>7d} {cost_str:>8}"
        )
    print("\nLegend: C1=provided_values precision/recall + label leaks · "
          "C2=valid-JSON rate · C3=criteria recall · C4=restraint pass-rate · "
          "lat=avg latency/call · tok=total tokens · ~$=est cost (verify pricing).")
    # Surface any errors for debugging.
    errs = [(r.model, s.case_id, s.error) for r in results for s in r.scores if s.error]
    if errs:
        print("\nErrors:")
        for m, cid, e in errs:
            print(f"  [{m}] {cid}: {e}")


def dry_run(models: list[str], repeats: int) -> None:
    total_calls = len(models) * len(CASES) * repeats
    print("=" * 70)
    print("DRY RUN — no API calls made. Pass --execute to actually run.")
    print("=" * 70)
    print(f"Models ({len(models)}): {', '.join(models)}")
    print(f"Cases ({len(CASES)}): {', '.join(c.id for c in CASES)}")
    print(f"Repeats per (model, case): {repeats}")
    print(f"Planned API calls: {total_calls}")

    # Rough cost ceiling: estimate input tokens ~ chars/4, assume ~450 output tokens.
    rough = 0.0
    unknown = []
    for m in models:
        if m not in PRICES:
            unknown.append(m)
            continue
        in_price, out_price = PRICES[m]
        for c in CASES:
            in_tok = len(ANALYST_PROMPT_CHARS(c)) / 4
            rough += repeats * ((in_tok / 1e6) * in_price + (450 / 1e6) * out_price)
    print(f"\nRough cost CEILING (very approximate): ~${rough:.3f}"
          + (f"  (+ unknown pricing for: {', '.join(unknown)})" if unknown else ""))
    print("\nReview eval/analyst_cases.py (the PRDs + answer keys) before spending.")
    print("Then run:  uv run python eval/analyst_eval.py --execute")


def ANALYST_PROMPT_CHARS(case: AnalystCase) -> str:
    # Approximate the prompt size without importing the prompt at module load.
    from qa_agent.prompts import ANALYST_SYSTEM
    return ANALYST_SYSTEM + case.app_url + case.prd_text


def main() -> int:
    ap = argparse.ArgumentParser(description="Score OpenAI models as the Analyst step.")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="comma-separated OpenAI model ids")
    ap.add_argument("--repeats", type=int, default=2,
                    help="runs per (model, case) — higher = more stable C2/latency")
    ap.add_argument("--execute", action="store_true",
                    help="ACTUALLY call the API (spends money). Default is a dry run.")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if not args.execute:
        dry_run(models, args.repeats)
        return 0

    results: list[ModelResult] = []
    for model in models:
        print(f"\n>>> {model} …")
        mr = ModelResult(model=model)
        for case in CASES:
            for r in range(args.repeats):
                sc = run_one(model, case)
                mr.scores.append(sc)
                flag = "ok" if sc.ok else f"ERR({sc.error[:40]})"
                print(f"    {case.id} [{r+1}/{args.repeats}] {flag} "
                      f"prec={sc.pv_precision:.2f} rec={sc.pv_recall:.2f} "
                      f"crit={sc.criteria_recall:.2f} leaks={sc.label_leaks} "
                      f"{sc.latency_s:.2f}s")
        results.append(mr)

    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
