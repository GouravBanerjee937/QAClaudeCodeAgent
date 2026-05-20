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


def run_phase_b(
    spec: TestSpec,
    answers: dict[str, str],
    bus: EventBus,
    *,
    heal_failures: bool = True,
) -> PhaseB:
    out = PhaseB()
    out.plan = designer.design(spec, answers, bus)
    # Resolve any `{key}` placeholders the Designer baked into URLs, so we don't
    # send literal "{login-url}" strings into the Explorer or Coder.
    orchestrator.resolve_placeholders(out.plan, answers, spec.app_url, bus)
    out.sitemap = explorer.explore(spec, out.plan, answers, bus)
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
    out.generated = coder.code(spec, out.plan, out.sitemap, answers, TESTS_DIR, bus)
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
