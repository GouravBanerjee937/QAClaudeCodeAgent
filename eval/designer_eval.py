#!/usr/bin/env python3
"""Measure how different models perform as the DESIGNER step.

Input: TestSpec + answers. Output: a TestPlan. Scored on the Designer's criteria:

  coverage   — each acceptance criterion covered by some test case (fuzzy recall)
  ratio      — #test_cases / #criteria (ideal ~1.0; >1.5 = over-splitting)
  scope-v    — count of forbidden scope-creep phrases (lower is better)
  concrete   — fraction of cases with >=3 steps AND a 'Then' assertion (writable)
  url-ok     — fraction of page_urls that are clean (path / placeholder / same-domain)
  leak       — count of answer values typed literally instead of as {key}
  parse      — structured-output success rate
  lat/tok/$  — cost & latency

SAFETY: free dry-run by default; spends only with --execute.

Usage:
  uv run python eval/designer_eval.py                       # free dry run
  uv run --with anthropic python eval/designer_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,claude-sonnet-4-6,claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=True)

from designer_cases import CASES, DesignerCase  # noqa: E402
from analyst_eval import PRICES, _anthropic, _google, _provider, _temp_kwargs  # noqa: E402

DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
]


# ─────────────────────────── faithful Designer payload ───────────────────


def _payload(case: DesignerCase) -> str:
    """Mirror designer.design(): mask password values, JSON-encode spec + answers."""
    return json.dumps(
        {"spec": case.spec.model_dump(),
         "answers": {k: "***" if "pass" in k else v for k, v in case.answers.items()}},
        indent=2,
    )


# ─────────────────────────── provider calls (TestPlan schema) ────────────


def _call_openai(model, system, user):
    from qa_agent.llm import _get_client
    from qa_agent.models import TestPlan
    resp = _get_client().beta.chat.completions.parse(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format=TestPlan, **_temp_kwargs(model, 0.2),
    )
    u = resp.usage
    return resp.choices[0].message.parsed, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


def _call_anthropic(model, system, user):
    from qa_agent.models import TestPlan
    tool = {"name": "emit_test_plan", "description": "Return the structured TestPlan.",
            "input_schema": TestPlan.model_json_schema()}
    msg = _anthropic().messages.create(
        model=model, max_tokens=4096, temperature=0.2, system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool], tool_choice={"type": "tool", "name": "emit_test_plan"},
    )
    data = next((b.input for b in msg.content if b.type == "tool_use"), None)
    parsed = TestPlan.model_validate(data) if data is not None else None
    u = msg.usage
    return parsed, (u.input_tokens if u else 0), (u.output_tokens if u else 0)


def _call_google(model, system, user):
    from google.genai import types
    from qa_agent.models import TestPlan
    resp = _google().models.generate_content(
        model=model, contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_schema=TestPlan, temperature=0.2),
    )
    um = resp.usage_metadata
    return resp.parsed, (um.prompt_token_count if um else 0) or 0, (um.candidates_token_count if um else 0) or 0


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic, "google": _call_google}


# ─────────────────────────── scoring ─────────────────────────────────────


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _case_text(tc) -> str:
    parts = [tc.title, tc.expected_outcome] + [s.text for s in tc.steps]
    return _norm(" ".join(p for p in parts if p))


def _covered(criterion: str, case_texts: list[str]) -> bool:
    cn = _norm(criterion)
    words = {w for w in cn.split() if len(w) > 3}
    for t in case_texts:
        if SequenceMatcher(None, cn, t).ratio() >= 0.4:
            return True
        if words:
            hit = sum(1 for w in words if w in t)
            if hit / len(words) >= 0.5:
                return True
    return False


@dataclass
class DScore:
    case_id: str
    ok: bool = False
    error: str = ""
    coverage: float = 0.0
    ratio: float = 0.0
    scope_viol: int = 0
    concrete: float = 0.0
    url_ok: float = 1.0
    leaks: int = 0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_plan(case: DesignerCase, plan) -> DScore:
    s = DScore(case_id=case.id, ok=True)
    cases = list(plan.test_cases)
    case_texts = [_case_text(tc) for tc in cases]
    full_text = " ".join(case_texts)

    # C1 coverage
    if case.gold_criteria:
        covered = sum(1 for c in case.gold_criteria if _covered(c, case_texts))
        s.coverage = covered / len(case.gold_criteria)
        s.ratio = len(cases) / len(case.gold_criteria)
    else:
        s.coverage, s.ratio = 1.0, 0.0

    # C2 scope discipline — count forbidden phrases
    s.scope_viol = sum(full_text.count(_norm(p)) for p in case.forbidden_scope)

    # C3 concreteness — case has >=3 steps and an assertion step. Gherkin allows
    # the assertion to use 'Then' OR a chained 'And', so accept either.
    if cases:
        good = sum(1 for tc in cases
                   if len(tc.steps) >= 3 and any(st.keyword in ("Then", "And") for st in tc.steps))
        s.concrete = good / len(cases)
    else:
        s.concrete = 0.0

    # C4a URL discipline — page_urls must be path / placeholder / same-domain
    app_net = urlparse(case.spec.app_url).netloc
    urls = [u for tc in cases for u in tc.page_urls]
    if urls:
        clean = 0
        for u in urls:
            if not u or "{" in u:
                clean += 1
                continue
            net = urlparse(u).netloc
            if net == "" or net == app_net:
                clean += 1
        s.url_ok = clean / len(urls)
    else:
        s.url_ok = 1.0

    # C4b placeholder discipline — literal answer values typed into ACTION steps
    # instead of using {key}. Scan step text only: titles/outcomes legitimately
    # describe the scenario (e.g. a criterion that names "Pencil").
    step_text = _norm(" ".join(st.text for tc in cases for st in tc.steps))
    s.leaks = sum(1 for v in case.literal_values if _norm(v) in step_text)
    return s


def run_one(model: str, case: DesignerCase) -> DScore:
    from qa_agent.prompts import DESIGNER_SYSTEM
    user = _payload(case)
    caller = _CALLERS[_provider(model)]
    t0 = time.perf_counter()
    try:
        parsed, ptok, ctok = caller(model, DESIGNER_SYSTEM, user)
        latency = time.perf_counter() - t0
        if parsed is None:
            return DScore(case_id=case.id, ok=False, error="no parsed object", latency_s=latency)
        s = score_plan(case, parsed)
        s.latency_s = latency
        s.prompt_tokens, s.completion_tokens = ptok or 0, ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001
        return DScore(case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
                      latency_s=time.perf_counter() - t0)


# ─────────────────────────── aggregation / output ────────────────────────


@dataclass
class ModelResult:
    model: str
    scores: list[DScore] = field(default_factory=list)

    def _ok(self): return [s for s in self.scores if s.ok]
    @property
    def parse_rate(self): return len(self._ok()) / len(self.scores) if self.scores else 0.0
    def _avg(self, a):
        ok = self._ok()
        return sum(getattr(s, a) for s in ok) / len(ok) if ok else 0.0
    @property
    def coverage(self): return self._avg("coverage")
    @property
    def ratio(self): return self._avg("ratio")
    @property
    def concrete(self): return self._avg("concrete")
    @property
    def url_ok(self): return self._avg("url_ok")
    @property
    def scope_viol(self): return sum(s.scope_viol for s in self._ok())
    @property
    def leaks(self): return sum(s.leaks for s in self._ok())
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
    print("\n" + "=" * 104)
    print("DESIGNER MODEL COMPARISON  (averaged across cases × repeats)")
    print("=" * 104)
    header = (f"{'model':<28} {'parse':>6} {'cover':>6} {'ratio':>6} {'scope-v':>8} "
              f"{'concr':>6} {'url-ok':>7} {'leak':>5} {'lat(s)':>7} {'tok':>7} {'~$':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        c = r.est_cost
        cs = f"{c:.4f}" if c is not None else "n/a"
        print(f"{r.model:<28} {r.parse_rate*100:>5.0f}% {r.coverage*100:>5.0f}% {r.ratio:>6.2f} "
              f"{r.scope_viol:>8d} {r.concrete*100:>5.0f}% {r.url_ok*100:>6.0f}% {r.leaks:>5d} "
              f"{r.avg_latency:>7.2f} {r.total_tokens:>7d} {cs:>8}")
    print("\nLegend: cover=criteria recall · ratio=#cases/#criteria (~1 ideal) · "
          "scope-v=forbidden-phrase count (lower better) · concr=writable cases · "
          "url-ok=clean URLs · leak=literal values not using {key}.")
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
    print("\nReview eval/designer_cases.py (the specs + answer keys) before spending.")


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
                      f"cover={sc.coverage:.2f} ratio={sc.ratio:.1f} scope={sc.scope_viol} "
                      f"concr={sc.concrete:.2f} url={sc.url_ok:.2f} leak={sc.leaks} {sc.latency_s:.2f}s")
        results.append(mr)
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
