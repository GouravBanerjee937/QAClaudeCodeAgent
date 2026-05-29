#!/usr/bin/env python3
"""Measure how different models perform as the EXPLORER LABELER step.

The labeler turns scraped (role, name) pairs into the same pairs + a `purpose`.
Scored on:
  fidelity  — % of input elements returned (no silent drops) — THE key metric
  invented  — count of returned pairs that were NOT in the input
  purpose   — % of returned elements with a non-empty purpose
  parse     — structured-output success
  lat/tok/$ — cost & latency

SAFETY: free dry-run by default; spends only with --execute.

Usage:
  uv run python eval/explorer_eval.py
  uv run --with anthropic python eval/explorer_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,claude-sonnet-4-6,claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=True)

from explorer_cases import CASES, ExplorerCase  # noqa: E402
from analyst_eval import PRICES, _anthropic, _google, _provider, _temp_kwargs  # noqa: E402

DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
]


# Schema mirrors explorer.py's private _LabeledElements / _LabeledOnly.
class LabeledOnly(BaseModel):
    role: str
    name: str
    purpose: str


class LabeledElements(BaseModel):
    elements: list[LabeledOnly]


def _payload(case: ExplorerCase) -> str:
    # Exactly mirrors explorer._label_elements: f"- role={r!r}, name={n!r}"
    return "\n".join(f"- role={r!r}, name={n!r}" for r, n in case.pairs)


# ─────────────────────────── provider calls (LabeledElements schema) ─────


def _call_openai(model, system, user):
    from qa_agent.llm import _get_client
    resp = _get_client().beta.chat.completions.parse(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format=LabeledElements, **_temp_kwargs(model, 0.2),
    )
    u = resp.usage
    return resp.choices[0].message.parsed, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


def _call_anthropic(model, system, user):
    tool = {"name": "emit_labeled", "description": "Return every element with a purpose.",
            "input_schema": LabeledElements.model_json_schema()}
    msg = _anthropic().messages.create(
        model=model, max_tokens=4096, temperature=0.2, system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool], tool_choice={"type": "tool", "name": "emit_labeled"},
    )
    data = next((b.input for b in msg.content if b.type == "tool_use"), None)
    parsed = LabeledElements.model_validate(data) if data is not None else None
    u = msg.usage
    return parsed, (u.input_tokens if u else 0), (u.output_tokens if u else 0)


def _call_google(model, system, user):
    from google.genai import types
    resp = _google().models.generate_content(
        model=model, contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_schema=LabeledElements, temperature=0.2),
    )
    um = resp.usage_metadata
    return resp.parsed, (um.prompt_token_count if um else 0) or 0, (um.candidates_token_count if um else 0) or 0


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic, "google": _call_google}


# ─────────────────────────── scoring ─────────────────────────────────────


@dataclass
class EScore:
    case_id: str
    ok: bool = False
    error: str = ""
    fidelity: float = 0.0
    invented: int = 0
    purpose: float = 0.0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_labels(case: ExplorerCase, labeled) -> EScore:
    s = EScore(case_id=case.id, ok=True)
    inp = set(case.pairs)
    out_pairs = [(e.role, e.name) for e in labeled.elements]
    out_set = set(out_pairs)
    s.fidelity = len(inp & out_set) / len(inp) if inp else 1.0
    s.invented = len(out_set - inp)
    s.purpose = (sum(1 for e in labeled.elements if (e.purpose or "").strip()) / len(labeled.elements)
                 if labeled.elements else 0.0)
    return s


def run_one(model: str, case: ExplorerCase) -> EScore:
    from qa_agent.prompts import EXPLORER_LABELER_SYSTEM
    caller = _CALLERS[_provider(model)]
    t0 = time.perf_counter()
    try:
        parsed, ptok, ctok = caller(model, EXPLORER_LABELER_SYSTEM, _payload(case))
        latency = time.perf_counter() - t0
        if parsed is None:
            return EScore(case_id=case.id, ok=False, error="no parsed object", latency_s=latency)
        s = score_labels(case, parsed)
        s.latency_s = latency
        s.prompt_tokens, s.completion_tokens = ptok or 0, ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001
        return EScore(case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
                      latency_s=time.perf_counter() - t0)


# ─────────────────────────── aggregation / output ────────────────────────


@dataclass
class ModelResult:
    model: str
    scores: list[EScore] = field(default_factory=list)

    def _ok(self): return [s for s in self.scores if s.ok]
    @property
    def parse_rate(self): return len(self._ok()) / len(self.scores) if self.scores else 0.0
    def _avg(self, a):
        ok = self._ok()
        return sum(getattr(s, a) for s in ok) / len(ok) if ok else 0.0
    @property
    def fidelity(self): return self._avg("fidelity")
    @property
    def purpose(self): return self._avg("purpose")
    @property
    def invented(self): return sum(s.invented for s in self._ok())
    @property
    def avg_latency(self): return self._avg("latency_s")
    @property
    def total_tokens(self): return sum(s.prompt_tokens + s.completion_tokens for s in self._ok())
    @property
    def est_cost(self):
        if self.model not in PRICES:
            return None
        i, o = PRICES[self.model]
        return sum((s.prompt_tokens / 1e6) * i + (s.completion_tokens / 1e6) * o for s in self._ok())


def print_table(results):
    print("\n" + "=" * 92)
    print("EXPLORER-LABELER MODEL COMPARISON  (averaged across cases × repeats)")
    print("=" * 92)
    header = (f"{'model':<28} {'parse':>6} {'fidelity':>9} {'invented':>9} "
              f"{'purpose':>8} {'lat(s)':>7} {'tok':>7} {'~$':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        c = r.est_cost
        cs = f"{c:.4f}" if c is not None else "n/a"
        print(f"{r.model:<28} {r.parse_rate*100:>5.0f}% {r.fidelity*100:>8.0f}% {r.invented:>9d} "
              f"{r.purpose*100:>7.0f}% {r.avg_latency:>7.2f} {r.total_tokens:>7d} {cs:>8}")
    print("\nLegend: fidelity=% input elements returned (no drops) · "
          "invented=# elements not in input · purpose=% with a non-empty label.")
    errs = [(r.model, s.case_id, s.error) for r in results for s in r.scores if s.error]
    if errs:
        print("\nErrors:")
        for m, cid, e in errs:
            print(f"  [{m}] {cid}: {e[:160]}")


def dry_run(models, repeats):
    print("=" * 70)
    print("DRY RUN — no API calls. Pass --execute to run.")
    print("=" * 70)
    print(f"Models ({len(models)}): {', '.join(models)}")
    print(f"Cases ({len(CASES)}): " + ", ".join(f"{c.id}({len(c.pairs)})" for c in CASES))
    print(f"Repeats: {repeats}  →  planned API calls: {len(models)*len(CASES)*repeats}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if not args.execute:
        dry_run(models, args.repeats)
        return 0

    results = []
    for model in models:
        print(f"\n>>> {model} …")
        mr = ModelResult(model=model)
        for case in CASES:
            for r in range(args.repeats):
                sc = run_one(model, case)
                mr.scores.append(sc)
                flag = "ok" if sc.ok else f"ERR({sc.error[:36]})"
                print(f"    {case.id} [{r+1}/{args.repeats}] {flag} "
                      f"fidelity={sc.fidelity:.2f} invented={sc.invented} "
                      f"purpose={sc.purpose:.2f} {sc.latency_s:.2f}s")
        results.append(mr)
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
