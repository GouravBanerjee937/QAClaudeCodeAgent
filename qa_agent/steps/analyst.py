"""Step 1: PRD → TestSpec."""

from __future__ import annotations

from ..events import EventBus
from ..llm import structured
from ..models import TestSpec
from ..prompts import ANALYST_SYSTEM
from ..source import SourceInsights, render_hints_for_prompt


def analyze(
    prd_text: str,
    app_url: str,
    bus: EventBus,
    *,
    source_insights: SourceInsights | None = None,
) -> TestSpec:
    bus.emit("analyst", "Reading PRD and extracting functional requirements…")
    user = (
        f"App URL provided by user (use this if PRD omits one): {app_url}\n\n"
        f"PRD:\n{prd_text}"
    )
    if source_insights is not None:
        hints = render_hints_for_prompt(source_insights)
        if hints:
            user = user + "\n\n" + hints
    spec = structured(ANALYST_SYSTEM, user, TestSpec)
    if not spec.app_url:
        spec.app_url = app_url
    bus.emit(
        "analyst",
        f"Identified {len(spec.user_flows)} user flow(s) and "
        f"{len(spec.acceptance_criteria)} acceptance criterion/criteria.",
        level="success",
    )
    if spec.notes:
        bus.emit("analyst", f"Ambiguities flagged: {spec.notes}", level="warn")
    return spec
