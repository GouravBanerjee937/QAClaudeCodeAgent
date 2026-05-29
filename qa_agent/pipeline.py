"""Two-phase orchestrator.

Phase A: PRD → TestSpec → Questions (pauses for user input)
Phase B: spec + answers → TestPlan → SiteMap → Code → Validate → Run → Heal → Report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .events import EventBus
from .models import (
    GeneratedTest, LocatorReport, Questions, RunResults, SiteMap, TestPlan, TestSpec,
)
from .source import SourceInsights
from .steps import (
    analyst, coder, designer, executor, explorer, healer, inquirer, orchestrator,
    reporter, validator,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests_generated"
REPORTS_DIR = PROJECT_ROOT / "reports"


@dataclass
class PhaseA:
    spec: TestSpec
    questions: Questions


@dataclass
class PhaseB:
    plan: TestPlan | None = None
    sitemap: SiteMap | None = None
    generated: list[GeneratedTest] = field(default_factory=list)
    locator_reports: list[LocatorReport] = field(default_factory=list)
    initial_results: RunResults | None = None
    healed_generated: list[GeneratedTest] = field(default_factory=list)
    final_results: RunResults | None = None
    report_markdown: str = ""


def run_phase_a(prd_text: str, app_url: str, bus: EventBus) -> PhaseA:
    spec = analyst.analyze(prd_text, app_url, bus)
    questions = inquirer.inquire(spec, bus)
    return PhaseA(spec=spec, questions=questions)


def run_phase_b_explore(
    spec: TestSpec,
    answers: dict[str, str],
    bus: EventBus,
    *,
    source_insights: SourceInsights | None = None,
) -> PhaseB:
    """First half of Phase B: design test cases and explore the live site.

    Returns a partially-filled PhaseB (plan + sitemap). The caller can then
    check `unreachable_urls(...)` and, if anything failed, pause to ask the
    user for the correct URL before calling `run_phase_b_finish(...)`.
    """
    out = PhaseB()
    if source_insights and not source_insights.is_empty:
        bus.emit("source", source_insights.summary().replace("\n", " | "), level="info")
    out.plan = designer.design(spec, answers, bus, source_insights=source_insights)
    # Resolve any `{key}` placeholders the Designer baked into URLs, so we don't
    # send literal "{login-url}" strings into the Explorer or Coder.
    orchestrator.resolve_placeholders(out.plan, answers, spec.app_url, bus)
    out.sitemap = explorer.explore(spec, out.plan, answers, bus)
    return out


def explore_only(
    spec: TestSpec, plan: TestPlan, answers: dict[str, str], bus: EventBus,
) -> SiteMap:
    """Re-run JUST the Explorer (no Designer) — used after the user corrects URLs."""
    return explorer.explore(spec, plan, answers, bus)


def unreachable_urls(
    spec: TestSpec, plan: TestPlan, sitemap: SiteMap,
) -> list[dict[str, str]]:
    """Planned URLs the Explorer could NOT turn into a usable snapshot.

    A URL is considered failed if it has no snapshot at all, or the snapshot
    came back with zero elements (page didn't load, 404, or wrong address).
    Each returned entry carries a `suggestion` (the resolved absolute URL) so
    the UI can pre-fill it and let the user confirm or correct — never guess
    silently and proceed.
    """
    from .steps.coder import resolve_url

    problems: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tc in plan.test_cases:
        urls = tc.page_urls or [spec.app_url]
        for url in urls:
            snap = None
            for key, candidate in sitemap.pages.items():
                if key == url or key.endswith(url) or url in key:
                    snap = candidate
                    break
            failed = snap is None or len(snap.elements) == 0
            if failed and (tc.id, url) not in seen:
                seen.add((tc.id, url))
                problems.append({
                    "test_case_id": tc.id,
                    "url": url,
                    "suggestion": resolve_url(spec.app_url, url),
                })
    return problems


def run_phase_b_finish(
    spec: TestSpec,
    answers: dict[str, str],
    partial: PhaseB,
    bus: EventBus,
    *,
    heal_failures: bool = True,
    source_insights: SourceInsights | None = None,
) -> PhaseB:
    """Second half of Phase B: validate, generate code, run, heal, report.

    Takes the partial PhaseB produced by `run_phase_b_explore` (with its plan
    and sitemap possibly already corrected by the user).
    """
    out = partial
    # Cross-stage validation before code generation
    orchestration_ok, orchestration_errors = orchestrator.validate_orchestration(
        spec, out.plan, out.sitemap, bus
    )
    if not orchestration_ok:
        bus.emit(
            "orchestrator",
            "Cannot proceed with code generation due to validation errors",
            level="error",
        )
    out.generated = coder.code(
        spec, out.plan, out.sitemap, answers, TESTS_DIR, bus,
        source_insights=source_insights,
    )
    out.generated, out.locator_reports = validator.validate(out.generated, out.sitemap, bus)
    out.initial_results = executor.execute(out.generated, TESTS_DIR, REPORTS_DIR, bus)

    out.healed_generated = out.generated
    results = out.initial_results
    if heal_failures and out.initial_results.failed > 0:
        out.healed_generated, fresh_sitemap = healer.heal(
            spec, out.plan, out.sitemap, out.generated, out.initial_results,
            answers, TESTS_DIR, bus,
        )
        out.sitemap = fresh_sitemap
        # Re-validate against the FRESH sitemap. The healer's output must pass the
        # same checks as the original Coder output — no NEEDS markers, no ambiguous
        # locators, no lambdas, no double-fills.
        bus.emit("validator", "Re-validating healed code…")
        out.healed_generated, healed_reports = validator.validate(
            out.healed_generated, out.sitemap, bus,
        )
        out.locator_reports = healed_reports
        results = executor.execute(out.healed_generated, TESTS_DIR, REPORTS_DIR, bus)

    out.final_results = results
    out.report_markdown = reporter.report(
        spec, out.plan, out.final_results, REPORTS_DIR, bus,
    )
    return out


def run_phase_b(
    spec: TestSpec,
    answers: dict[str, str],
    bus: EventBus,
    *,
    heal_failures: bool = True,
    source_insights: SourceInsights | None = None,
) -> PhaseB:
    """Run all of Phase B in one shot (explore → finish), without the URL pause.

    Kept for non-interactive callers (e.g. run_pipeline_check.py). The Streamlit
    UI calls run_phase_b_explore + run_phase_b_finish separately so it can pause
    and confirm any URL the Explorer couldn't reach.
    """
    partial = run_phase_b_explore(spec, answers, bus, source_insights=source_insights)
    return run_phase_b_finish(
        spec, answers, partial, bus,
        heal_failures=heal_failures, source_insights=source_insights,
    )
