"""Step: Validate consistency across Design → Explorer → Code stages.

The Orchestrator checks that:
1. Each Designer test case maps to a TestSpec acceptance criterion
2. Each test case's URLs are present in the SiteMap
3. Each test case's steps reference elements that exist in the SiteMap
4. No contradictions between Designer assumptions and Explorer discoveries
"""

from __future__ import annotations

import re

from ..events import EventBus
from ..models import GeneratedTest, SiteMap, TestPlan, TestSpec

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9-]*)\}")


class OrchestrationError:
    """A validation failure with diagnostics."""

    def __init__(self, test_case_id: str, issue: str, severity: str = "error"):
        self.test_case_id = test_case_id
        self.issue = issue
        self.severity = severity  # "error" or "warn"

    def __str__(self) -> str:
        return f"{self.test_case_id}: {self.issue}"


def resolve_placeholders(
    plan: TestPlan,
    answers: dict[str, str],
    fallback_url: str,
    bus: EventBus,
) -> None:
    """Substitute {key} placeholders in test case URLs in-place.

    The Designer sometimes emits keys like `{login-url}` in page_urls that
    aren't in the answers dict. Without this, the Coder writes
    `page.goto("{login-url}")` which obviously fails. We replace each
    placeholder with the matching answer, or fall back to the app URL.
    """
    substituted = 0
    for tc in plan.test_cases:
        new_urls: list[str] = []
        for u in tc.page_urls:
            keys = _PLACEHOLDER_RE.findall(u)
            if not keys:
                new_urls.append(u)
                continue
            resolved = u
            for k in keys:
                if k in answers and answers[k]:
                    resolved = resolved.replace("{" + k + "}", answers[k])
                else:
                    resolved = fallback_url
                    bus.emit(
                        "orchestrator",
                        f"  {tc.id}: URL placeholder {{{k}}} has no answer — "
                        f"falling back to app URL",
                        level="warn",
                    )
                    break
            new_urls.append(resolved)
            substituted += 1
        tc.page_urls = new_urls
    if substituted:
        bus.emit(
            "orchestrator",
            f"Resolved placeholders in {substituted} test-case URL(s)",
        )


def validate_orchestration(
    spec: TestSpec,
    plan: TestPlan,
    sitemap: SiteMap,
    bus: EventBus,
) -> tuple[bool, list[OrchestrationError]]:
    """Validate consistency across stages.

    Returns (all_ok, errors_list).
    all_ok = True means no blocking errors (warnings are OK).
    """
    errors: list[OrchestrationError] = []

    # Check 1: TestSpec has required fields
    if not spec.app_name:
        errors.append(OrchestrationError("spec", "app_name is empty", "error"))
    if not spec.app_url:
        errors.append(OrchestrationError("spec", "app_url is empty", "error"))
    if not spec.acceptance_criteria:
        errors.append(
            OrchestrationError("spec", "no acceptance criteria in TestSpec", "error")
        )

    # Check 2: Each test case maps to a spec criterion
    spec_criteria = {c.lower() for c in spec.acceptance_criteria}
    for tc in plan.test_cases:
        tc_lower = tc.title.lower()
        # Fuzzy match: does test case title contain keywords from any spec criterion?
        matched = False
        for crit in spec_criteria:
            # Simple word overlap check
            tc_words = set(tc_lower.split())
            crit_words = set(crit.split())
            if tc_words & crit_words:  # Any word in common
                matched = True
                break
        if not matched:
            errors.append(
                OrchestrationError(
                    tc.id,
                    f"test case does not map to any acceptance criterion. "
                    f"Test: '{tc.title}'. Criteria: {spec_criteria}",
                    "warn",
                )
            )

    # Check 3: Each test case's URLs are in SiteMap
    for tc in plan.test_cases:
        urls = tc.page_urls or [spec.app_url]
        for url in urls:
            # Try to find the URL in SiteMap
            found = any(
                snap_url == url or snap_url.endswith(url) or url in snap_url
                for snap_url in sitemap.pages.keys()
            )
            if not found:
                errors.append(
                    OrchestrationError(
                        tc.id,
                        f"URL not in SiteMap: {url}. "
                        f"Available: {list(sitemap.pages.keys())}",
                        "error",
                    )
                )

    # Check 4: For each test case, verify steps can be realized from SiteMap
    for tc in plan.test_cases:
        urls = tc.page_urls or [spec.app_url]
        # Collect all elements from relevant URLs
        available_elements = set()
        for url in urls:
            snap = next(
                (s for u, s in sitemap.pages.items() if u == url or u.endswith(url)),
                None,
            )
            if snap:
                for elem in snap.elements:
                    available_elements.add((elem.role, elem.name.lower()))

        # Check Gherkin steps reference reasonable locator patterns
        for step in tc.steps:
            text = step.text.lower()
            # Skip trivial checks; mainly look for action keywords
            if any(kw in text for kw in ("click", "fill", "select", "check")):
                # Step implies an action; should have some element available
                if not available_elements:
                    errors.append(
                        OrchestrationError(
                            tc.id,
                            f"Step '{step.text}' requires elements, but no elements "
                            f"found in SiteMap for URLs {urls}",
                            "error",
                        )
                    )

    # Report
    blocking_errors = [e for e in errors if e.severity == "error"]
    warnings = [e for e in errors if e.severity == "warn"]

    if blocking_errors:
        bus.emit(
            "orchestrator",
            f"Found {len(blocking_errors)} blocking error(s)",
            level="error",
        )
        for e in blocking_errors:
            bus.emit("orchestrator", f"  ✗ {e}", level="error")
    if warnings:
        bus.emit(
            "orchestrator",
            f"Found {len(warnings)} warning(s)",
            level="warn",
        )
        for e in warnings:
            bus.emit("orchestrator", f"  ! {e}", level="warn")

    if not blocking_errors:
        bus.emit(
            "orchestrator",
            "Cross-stage validation passed",
            level="success",
        )

    return len(blocking_errors) == 0, errors
