"""Step 2: TestSpec → TestPlan (list of Gherkin TestCases)."""

from __future__ import annotations

import json

from ..events import EventBus
from ..llm import structured
from ..models import TestPlan, TestSpec
from ..prompts import DESIGNER_SYSTEM
from ..source import SourceInsights, render_hints_for_prompt


def design(
    spec: TestSpec,
    answers: dict[str, str],
    bus: EventBus,
    *,
    source_insights: SourceInsights | None = None,
) -> TestPlan:
    bus.emit("designer", "Designing test cases from acceptance criteria…")
    payload = json.dumps(
        {"spec": spec.model_dump(), "answers": {k: "***" if "pass" in k else v
                                                 for k, v in answers.items()}},
        indent=2,
    )
    if source_insights is not None:
        hints = render_hints_for_prompt(source_insights)
        if hints:
            payload = payload + "\n\n" + hints
    plan = structured(DESIGNER_SYSTEM, payload, TestPlan)
    bus.emit(
        "designer",
        f"Designed {len(plan.test_cases)} test case(s).",
        level="success",
    )
    for tc in plan.test_cases:
        bus.emit("designer", f"  • {tc.id}: {tc.title}")
    return plan
