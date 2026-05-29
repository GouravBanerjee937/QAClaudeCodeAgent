#!/usr/bin/env python3
"""Measure how different models perform as the INQUIRER step.

The Inquirer reads a TestSpec and emits a `Questions` list — the concrete values
it wants the user to supply. We score it on the Inquirer's own criteria:

  restraint  — of the questions it asked, how many were warranted (precision)
  over-ask   — count of questions it should NOT have asked (already-provided / labels)
  gap-rec    — of the genuinely-missing values, how many it asked for (recall)
  typing     — correct `kind` (password/url/email) + kebab-case key hygiene
  parse      — structured-output success rate
  lat/tok/$  — cost & latency

SAFETY: free dry-run by default; spends only with --execute. Reuses the provider
adapters from analyst_eval.py (OpenAI native parse, Claude tool-call, Gemini schema).

Usage:
  uv run python eval/inquirer_eval.py                       # free dry run
  uv run --with anthropic python eval/inquirer_eval.py --execute \
      --models gpt-4.1-mini,gpt-4.1,claude-sonnet-4-6,claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
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

from inquirer_cases import CASES, InquirerCase  # noqa: E402
from analyst_eval import (  # noqa: E402 — reuse provider plumbing
    PRICES, _anthropic, _google, _provider, _temp_kwargs,
)

DEFAULT_MODELS = [
    "gpt-4.1-mini", "gpt-4.1",
    "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
]

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


# ─────────────────────────── provider calls (Questions schema) ───────────


def _call_openai(model: str, system: str, user: str):
    from qa_agent.llm import _get_client
    from qa_agent.models import Questions
    resp = _get_client().beta.chat.completions.parse(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        response_format=Questions,
        **_temp_kwargs(model, 0.2),
    )
    u = resp.usage
    return resp.choices[0].message.parsed, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


def _call_anthropic(model: str, system: str, user: str):
    from qa_agent.models import Questions
    tool = {
        "name": "emit_questions",
        "description": "Return the list of questions to ask the user.",
        "input_schema": Questions.model_json_schema(),
    }
    msg = _anthropic().messages.create(
        model=model, max_tokens=2048, temperature=0.2, system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool], tool_choice={"type": "tool", "name": "emit_questions"},
    )
    data = next((b.input for b in msg.content if b.type == "tool_use"), None)
    parsed = Questions.model_validate(data) if data is not None else None
    u = msg.usage
    return parsed, (u.input_tokens if u else 0), (u.output_tokens if u else 0)


def _call_google(model: str, system: str, user: str):
    from google.genai import types
    from qa_agent.models import Questions
    resp = _google().models.generate_content(
        model=model, contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_schema=Questions, temperature=0.2,
        ),
    )
    um = resp.usage_metadata
    return resp.parsed, (um.prompt_token_count if um else 0) or 0, (um.candidates_token_count if um else 0) or 0


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic, "google": _call_google}


# ─────────────────────────── scoring ─────────────────────────────────────


def _q_matches(q, concept: str) -> bool:
    """Broad match (key/prompt/hint) — used for GAP coverage (recall)."""
    c = concept.lower()
    return c in (q.key or "").lower() or c in (q.prompt or "").lower() or c in (q.hint or "").lower()


def _forbidden_match(q, concept: str) -> bool:
    """Strict word-boundary match on KEY + PROMPT only — used for over-ask detection.

    The hint is excluded on purpose: a legitimate question's hint naturally mentions
    context words (e.g. dashboard-url's hint "...after login"), which must NOT count
    as re-asking for 'login'. Word boundaries stop 'email' matching inside other text.
    """
    text = ((q.key or "").replace("-", " ") + " " + (q.prompt or "")).lower()
    return re.search(r"\b" + re.escape(concept.lower()) + r"\b", text) is not None


@dataclass
class InqScore:
    case_id: str
    ok: bool = False
    error: str = ""
    restraint: float = 1.0    # precision: warranted / asked
    over_ask: int = 0
    gap_recall: float = 1.0
    typing: float = 1.0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_questions(case: InquirerCase, questions) -> InqScore:
    s = InqScore(case_id=case.id, ok=True)
    items = list(questions.items)

    # Gap coverage (recall): did it ask for each genuinely-missing value?
    if case.expected_ask:
        covered = sum(1 for (concept, _k) in case.expected_ask
                      if any(_q_matches(q, concept) for q in items))
        s.gap_recall = covered / len(case.expected_ask)
    else:
        s.gap_recall = 1.0

    # Restraint: questions matching a forbidden concept are over-asks.
    over = sum(1 for q in items if any(_forbidden_match(q, f) for f in case.forbidden_ask))
    # In a complete spec (nothing expected), ANY question is an over-ask.
    if not case.expected_ask and items:
        over = max(over, len(items))
    s.over_ask = over
    s.restraint = ((len(items) - over) / len(items)) if items else 1.0

    # Field-typing: correct kind on expected-kind questions + kebab key hygiene.
    kind_checks = 0
    kind_ok = 0
    for (concept, kind) in case.expected_ask:
        if not kind:
            continue
        for q in items:
            if _q_matches(q, concept):
                kind_checks += 1
                if (q.kind or "") == kind:
                    kind_ok += 1
                break
    kebab_ok = sum(1 for q in items if _KEBAB_RE.match(q.key or ""))
    kebab_rate = (kebab_ok / len(items)) if items else 1.0
    kind_rate = (kind_ok / kind_checks) if kind_checks else None
    s.typing = kebab_rate if kind_rate is None else (0.5 * kebab_rate + 0.5 * kind_rate)
    return s


def run_one(model: str, case: InquirerCase) -> InqScore:
    from qa_agent.prompts import INQUIRER_SYSTEM
    user = case.spec.model_dump_json(indent=2)
    caller = _CALLERS[_provider(model)]
    t0 = time.perf_counter()
    try:
        parsed, ptok, ctok = caller(model, INQUIRER_SYSTEM, user)
        latency = time.perf_counter() - t0
        if parsed is None:
            return InqScore(case_id=case.id, ok=False, error="no parsed object", latency_s=latency)
        s = score_questions(case, parsed)
        s.latency_s = latency
        s.prompt_tokens, s.completion_tokens = ptok or 0, ctok or 0
        return s
    except Exception as exc:  # noqa: BLE001
        return InqScore(case_id=case.id, ok=False, error=f"{type(exc).__name__}: {exc}",
                        latency_s=time.perf_counter() - t0)


# ─────────────────────────── aggregation / output ────────────────────────


@dataclass
class ModelResult:
    model: str
    scores: list[InqScore] = field(default_factory=list)

    def _ok(self): return [s for s in self.scores if s.ok]
    @property
    def parse_rate(self): return len(self._ok()) / len(self.scores) if self.scores else 0.0
    def _avg(self, a):
        ok = self._ok()
        return sum(getattr(s, a) for s in ok) / len(ok) if ok else 0.0
    @property
    def restraint(self): return self._avg("restraint")
    @property
    def gap_recall(self): return self._avg("gap_recall")
    @property
    def typing(self): return self._avg("typing")
    @property
    def over_ask(self): return sum(s.over_ask for s in self._ok())
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
    print("INQUIRER MODEL COMPARISON  (averaged across cases × repeats)")
    print("=" * 96)
    header = (f"{'model':<28} {'parse':>6} {'restraint':>10} {'over-ask':>9} "
              f"{'gap-rec':>8} {'typing':>7} {'lat(s)':>7} {'tok':>7} {'~$':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        c = r.est_cost
        cs = f"{c:.4f}" if c is not None else "n/a"
        print(f"{r.model:<28} {r.parse_rate*100:>5.0f}% {r.restraint*100:>9.0f}% "
              f"{r.over_ask:>9d} {r.gap_recall*100:>7.0f}% {r.typing*100:>6.0f}% "
              f"{r.avg_latency:>7.2f} {r.total_tokens:>7d} {cs:>8}")
    print("\nLegend: restraint=warranted/asked · over-ask=#unwarranted questions · "
          "gap-rec=recall of missing values · typing=correct kind + kebab keys.")
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
    print("\nReview eval/inquirer_cases.py (the specs + answer keys) before spending.")


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
                      f"restraint={sc.restraint:.2f} over={sc.over_ask} "
                      f"gap={sc.gap_recall:.2f} typing={sc.typing:.2f} {sc.latency_s:.2f}s")
        results.append(mr)
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
