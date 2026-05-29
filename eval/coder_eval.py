#!/usr/bin/env python3
"""Measure how different models perform as the CODER step.

The Coder writes a full pytest-playwright file (free text). We score WITHOUT a
browser, reusing the pipeline's OWN validator for grounding:

  syntax   — does the generated code parse (ast)
  grounded — % of cases with ZERO locator misses (every locator is in the SiteMap)
  needs    — correct handling of a missing element: emit `# NEEDS:`, don't invent
  rules    — hard-rule adherence (signature, import, snap(), role-based/no-xpath,
             exact=True, no lambda, select_option for comboboxes)
  lat/tok/$ — cost & latency

SAFETY: free dry-run by default; spends only with --execute.

Usage:
  uv run python eval/coder_eval.py
  uv run --with anthropic python eval/coder_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,claude-sonnet-4-6,claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import ast
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

from coder_cases import CASES, CoderCase  # noqa: E402
from analyst_eval import PRICES, _anthropic, _google, _provider, _temp_kwargs  # noqa: E402

DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
]


# ─────────────────────────── free-text provider calls ────────────────────


def _call_openai(model, system, user):
    from qa_agent.llm import _get_client
    resp = _get_client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **_temp_kwargs(model, 0.1),
    )
    u = resp.usage
    return (resp.choices[0].message.content or ""), (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


def _call_anthropic(model, system, user):
    msg = _anthropic().messages.create(
        model=model, max_tokens=4096, temperature=0.1, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    u = msg.usage
    return text, (u.input_tokens if u else 0), (u.output_tokens if u else 0)


def _call_google(model, system, user):
    from google.genai import types
    resp = _google().models.generate_content(
        model=model, contents=user,
        config=types.GenerateContentConfig(system_instruction=system, temperature=0.1),
    )
    um = resp.usage_metadata
    return (resp.text or ""), (um.prompt_token_count if um else 0) or 0, (um.candidates_token_count if um else 0) or 0


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic, "google": _call_google}


# ─────────────────────────── scoring ─────────────────────────────────────


@dataclass
class CScore:
    case_id: str
    ok: bool = False
    error: str = ""
    syntax_ok: bool = False
    misses: int = 0
    placeholders: int = 0
    needs_ok: bool = False
    rules: float = 0.0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_code(case: CoderCase, code: str) -> CScore:
    from qa_agent.events import EventBus
    from qa_agent.models import GeneratedTest
    from qa_agent.steps import validator

    s = CScore(case_id=case.id, ok=True)

    # syntax
    try:
        ast.parse(code)
        s.syntax_ok = True
    except SyntaxError:
        s.syntax_ok = False

    # grounding — reuse the real validator
    try:
        gt = GeneratedTest(test_case_id=case.test_case.id, file_path="(eval)", code=code)
        _surv, reports = validator.validate([gt], case.sitemap, EventBus())
        rep = reports[0] if reports else None
        s.misses = rep.misses if rep else 0
        s.placeholders = len(rep.placeholders) if rep else 0
    except Exception:
        s.misses, s.placeholders = 0, 0

    # hard-rule checks
    checks = []
    checks.append(bool(re.search(r"def test_\w+\(\s*page:\s*Page,\s*snap\s*\)", code)))  # signature
    checks.append("from playwright.sync_api import" in code and "expect" in code)        # import
    checks.append("snap(" in code)                                                       # screenshots
    checks.append("xpath=" not in code and "css=" not in code)                           # role-based only
    checks.append("lambda" not in code)                                                  # no lambda assertions
    if "get_by_role(" in code and "name=" in code:
        checks.append("exact=True" in code)                                              # exact matching
    if case.must_select_option:
        checks.append("select_option" in code)                                           # combobox verb
    s.rules = sum(1 for c in checks if c) / len(checks) if checks else 1.0

    # NEEDS handling
    if case.expects_needs:
        s.needs_ok = (s.placeholders >= 1 and s.misses == 0)  # flagged, didn't invent
    else:
        s.needs_ok = (s.misses == 0)                          # no invented locators
    return s


def run_one(model: str, case: CoderCase) -> CScore:
    from qa_agent.prompts import CODER_SYSTEM
    from qa_agent.steps.coder import _build_prompt, _post_process, _strip_fences

    user = _build_prompt(case.spec, case.test_case, case.sitemap, case.answers)
    caller = _CALLERS[_provider(model)]
    t0 = time.perf_counter()
    try:
        raw, ptok, ctok = caller(model, CODER_SYSTEM, user)
        latency = time.perf_counter() - t0
        code = _post_process(_strip_fences(raw))
        s = score_code(case, code)
        s.latency_s = latency
        s.prompt_tokens, s.completion_tokens = ptok or 0, ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001
        return CScore(case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
                      latency_s=time.perf_counter() - t0)


# ─────────────────────────── aggregation / output ────────────────────────


@dataclass
class ModelResult:
    model: str
    scores: list[CScore] = field(default_factory=list)

    def _ok(self): return [s for s in self.scores if s.ok]
    @property
    def n(self): return len(self.scores)
    @property
    def syntax_rate(self): return sum(1 for s in self._ok() if s.syntax_ok) / self.n if self.n else 0.0
    @property
    def grounded_rate(self): return sum(1 for s in self._ok() if s.misses == 0) / self.n if self.n else 0.0
    @property
    def needs_rate(self): return sum(1 for s in self._ok() if s.needs_ok) / self.n if self.n else 0.0
    @property
    def total_misses(self): return sum(s.misses for s in self._ok())
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
    print("\n" + "=" * 100)
    print("CODER MODEL COMPARISON  (across cases × repeats)")
    print("=" * 100)
    header = (f"{'model':<28} {'syntax':>7} {'grounded':>9} {'misses':>7} {'needs-ok':>9} "
              f"{'rules':>6} {'lat(s)':>7} {'tok':>7} {'~$':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        c = r.est_cost
        cs = f"{c:.4f}" if c is not None else "n/a"
        print(f"{r.model:<28} {r.syntax_rate*100:>6.0f}% {r.grounded_rate*100:>8.0f}% "
              f"{r.total_misses:>7d} {r.needs_rate*100:>8.0f}% {r.rules*100:>5.0f}% "
              f"{r.avg_latency:>7.2f} {r.total_tokens:>7d} {cs:>8}")
    print("\nLegend: syntax=parses · grounded=% cases with 0 locator misses · "
          "misses=total invented locators · needs-ok=correct missing-element handling · "
          "rules=hard-rule adherence.")
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
                      f"syntax={int(sc.syntax_ok)} misses={sc.misses} ph={sc.placeholders} "
                      f"needs_ok={int(sc.needs_ok)} rules={sc.rules:.2f} {sc.latency_s:.2f}s")
        results.append(mr)
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
