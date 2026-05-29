#!/usr/bin/env python3
"""Measure how different models perform as the HEALER step.

The Healer reads a failure + the broken code + a fresh SiteMap and rewrites the
test. We score WITHOUT a browser:

  syntax    — corrected code parses
  grounded  — % of cases with 0 locator misses vs the FRESH SiteMap
  fixed     — % of cases where the rewrite actually fixes the bug
              (must_contain present AND must_not_contain absent) — THE key metric
  rules     — hard-rule adherence (same as the Coder)
  lat/tok/$ — cost & latency

SAFETY: free dry-run by default; spends only with --execute.

Usage:
  uv run python eval/healer_eval.py
  uv run --with anthropic python eval/healer_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,claude-sonnet-4-6,claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=True)

from healer_cases import CASES, HealerCase  # noqa: E402
# Reuse the Coder eval's free-text provider callers verbatim.
from coder_eval import _CALLERS  # noqa: E402
from analyst_eval import PRICES, _provider  # noqa: E402

DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
]


def _build_prompt(case: HealerCase) -> str:
    """Mirror healer._rewrite()'s prompt construction."""
    pages = [case.sitemap.pages[u] for u in case.test_case.page_urls if u in case.sitemap.pages]
    if not pages:
        pages = list(case.sitemap.pages.values())
    lines = []
    for snap in pages:
        lines.append(f"\n## Page: {snap.url}  (title: {snap.title!r})")
        for el in snap.elements:
            lines.append(f'- role="{el.role}", name="{el.name}"  — {el.purpose}')
    return (
        f"# app_url\n{case.spec.app_url}\n\n"
        f"# TestCase\n{case.test_case.model_dump_json(indent=2)}\n\n"
        f"# Previous generated code\n```python\n{case.previous_code}\n```\n\n"
        f"# Pytest failure\n{case.failure_message[:2000]}\n\n"
        f"# Fresh SiteMap (authoritative)\n" + "\n".join(lines)
        + f"\n\n# answers\n{json.dumps(case.answers, indent=2)}\n"
    )


@dataclass
class HScore:
    case_id: str
    ok: bool = False
    error: str = ""
    syntax_ok: bool = False
    misses: int = 0
    fixed: bool = False
    rules: float = 0.0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_code(case: HealerCase, code: str) -> HScore:
    from qa_agent.events import EventBus
    from qa_agent.models import GeneratedTest
    from qa_agent.steps import validator

    s = HScore(case_id=case.id, ok=True)
    try:
        ast.parse(code)
        s.syntax_ok = True
    except SyntaxError:
        s.syntax_ok = False

    try:
        gt = GeneratedTest(test_case_id=case.test_case.id, file_path="(eval)", code=code)
        _surv, reports = validator.validate([gt], case.sitemap, EventBus())
        rep = reports[0] if reports else None
        s.misses = rep.misses if rep else 0
    except Exception:
        s.misses = 0

    s.fixed = (all(m in code for m in case.must_contain)
               and all(m not in code for m in case.must_not_contain))

    checks = [
        bool(re.search(r"def test_\w+\(\s*page:\s*Page,\s*snap\s*\)", code)),
        "from playwright.sync_api import" in code and "expect" in code,
        "snap(" in code,
        "xpath=" not in code and "css=" not in code,
        "lambda" not in code,
    ]
    if "get_by_role(" in code and "name=" in code:
        checks.append("exact=True" in code)
    s.rules = sum(1 for c in checks if c) / len(checks)
    return s


def run_one(model: str, case: HealerCase) -> HScore:
    from qa_agent.prompts import HEALER_SYSTEM
    from qa_agent.steps.coder import _post_process, _strip_fences

    user = _build_prompt(case)
    caller = _CALLERS[_provider(model)]
    t0 = time.perf_counter()
    try:
        raw, ptok, ctok = caller(model, HEALER_SYSTEM, user)
        latency = time.perf_counter() - t0
        code = _post_process(_strip_fences(raw))
        s = score_code(case, code)
        s.latency_s = latency
        s.prompt_tokens, s.completion_tokens = ptok or 0, ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001
        return HScore(case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
                      latency_s=time.perf_counter() - t0)


@dataclass
class ModelResult:
    model: str
    scores: list[HScore] = field(default_factory=list)

    def _ok(self): return [s for s in self.scores if s.ok]
    @property
    def n(self): return len(self.scores)
    @property
    def syntax_rate(self): return sum(1 for s in self._ok() if s.syntax_ok) / self.n if self.n else 0.0
    @property
    def grounded_rate(self): return sum(1 for s in self._ok() if s.misses == 0) / self.n if self.n else 0.0
    @property
    def fixed_rate(self): return sum(1 for s in self._ok() if s.fixed) / self.n if self.n else 0.0
    def _avg(self, a):
        ok = self._ok()
        return sum(getattr(s, a) for s in ok) / len(ok) if ok else 0.0
    @property
    def rules(self): return self._avg("rules")
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
    print("\n" + "=" * 96)
    print("HEALER MODEL COMPARISON  (across cases × repeats)")
    print("=" * 96)
    header = (f"{'model':<28} {'syntax':>7} {'grounded':>9} {'fixed':>7} "
              f"{'rules':>6} {'lat(s)':>7} {'tok':>7} {'~$':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        c = r.est_cost
        cs = f"{c:.4f}" if c is not None else "n/a"
        print(f"{r.model:<28} {r.syntax_rate*100:>6.0f}% {r.grounded_rate*100:>8.0f}% "
              f"{r.fixed_rate*100:>6.0f}% {r.rules*100:>5.0f}% "
              f"{r.avg_latency:>7.2f} {r.total_tokens:>7d} {cs:>8}")
    print("\nLegend: grounded=% cases with 0 locator misses vs fresh SiteMap · "
          "fixed=% cases where the rewrite actually corrects the bug.")
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
    print(f"Cases ({len(CASES)}): {', '.join(c.id for c in CASES)}")
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
                      f"syntax={int(sc.syntax_ok)} misses={sc.misses} fixed={int(sc.fixed)} "
                      f"rules={sc.rules:.2f} {sc.latency_s:.2f}s")
        results.append(mr)
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
