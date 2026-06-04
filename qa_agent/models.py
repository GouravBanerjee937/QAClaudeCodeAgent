"""Pydantic contracts that flow between pipeline steps."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

QuestionKind = Literal["text", "password", "url", "email"]


class UserFlow(BaseModel):
    name: str = Field(description="Short name, e.g. 'User login'")
    description: str = Field(description="What the user is trying to accomplish")


class ProvidedValue(BaseModel):
    """A concrete value literally stated in the PRD that the test will need."""
    key: str = Field(description="kebab-case identifier, e.g. 'login-username'")
    value: str = Field(description="the literal value as stated in the PRD")


class TestSpec(BaseModel):
    """Step 1 output — structured understanding of the PRD."""
    app_name: str
    app_url: str = Field(description="Starting URL for the app under test")
    user_flows: list[UserFlow]
    acceptance_criteria: list[str] = Field(
        description="Specific, testable success conditions stated in or implied by the PRD"
    )
    provided_values: list[ProvidedValue] = Field(
        description=(
            "Concrete values literally stated in the PRD (credentials, sample inputs, "
            "specific URLs). Keys are kebab-case (e.g. 'login-username', 'login-password'). "
            "These feed directly into the answers dict so the Inquirer can skip asking and "
            "the Coder still has values to substitute. Return [] if nothing is stated."
        ),
    )
    notes: str = Field(default="", description="Anything the analyst flagged as ambiguous")

    @property
    def provided_values_dict(self) -> dict[str, str]:
        return {pv.key: pv.value for pv in self.provided_values}


class Question(BaseModel):
    """A concrete value the pipeline needs but didn't find in the PRD."""
    key: str = Field(description="Stable kebab-case identifier, e.g. 'login-email'")
    prompt: str = Field(description="Short label shown above the input field")
    hint: str = Field(default="", description="Help text shown below the field")
    kind: QuestionKind = "text"
    reason: str = Field(
        default="",
        description="One sentence — why this value is needed (which test or step)",
    )


class Questions(BaseModel):
    items: list[Question]


class TestStep(BaseModel):
    keyword: Literal["Given", "When", "Then", "And"]
    text: str


class TestCase(BaseModel):
    """Step 2 output — one Gherkin-style test case."""
    id: str = Field(description="kebab-case slug, e.g. 'login-with-valid-credentials'")
    title: str
    page_urls: list[str] = Field(
        description="URLs (relative to app_url or absolute) the test will visit"
    )
    steps: list[TestStep]
    expected_outcome: str


class TestPlan(BaseModel):
    test_cases: list[TestCase]


class PageElement(BaseModel):
    """An interactable thing found on a page."""
    role: str = Field(description="ARIA role, e.g. 'button', 'textbox', 'link'")
    name: str = Field(description="Accessible name, e.g. 'Sign in', 'Email address'")
    purpose: str = Field(description="LLM-inferred purpose, e.g. 'submit login form'")
    container_id: str = Field(
        default="",
        description=(
            "ID of the element's closest <form>, dialog, or section ancestor. "
            "Used to disambiguate same-named fields across multiple forms in "
            "the same DOM (e.g. SPAs that keep all routes' markup mounted)."
        ),
    )
    visible: bool = Field(
        default=True,
        description=(
            "Whether the element was actually visible on the page when scraped. "
            "SPAs keep hidden forms mounted in the DOM; a hidden element exists "
            "but cannot be interacted with until something reveals it (e.g. a "
            "'Create X' button or navigating to that form's URL)."
        ),
    )


class PageSnapshot(BaseModel):
    """Step 3 output for one URL."""
    url: str
    title: str
    elements: list[PageElement]
    raw_accessibility_summary: str = Field(
        description="Trimmed accessibility-tree text, kept for the coder's context"
    )


class SiteMap(BaseModel):
    pages: dict[str, PageSnapshot] = Field(
        description="Keyed by URL (as it appeared in the test case)"
    )


class GeneratedTest(BaseModel):
    """Step 4 output — generated pytest-playwright code for one test case."""
    test_case_id: str
    file_path: str
    code: str


class LocatorCheck(BaseModel):
    """One locator call extracted from generated code, checked against the SiteMap."""
    method: str = Field(description="get_by_role | get_by_text | get_by_label | …")
    args: str = Field(description="Rendered call args, e.g. 'role=\"button\", name=\"Login\"'")
    status: Literal["match", "miss", "fuzzy"]
    closest: str = Field(default="", description="Closest SiteMap match if not exact")


class LocatorReport(BaseModel):
    test_case_id: str
    checks: list[LocatorCheck]
    placeholders: list[str] = Field(
        default_factory=list,
        description="<<NEEDS: ...>> markers the coder emitted because a value was missing",
    )

    @property
    def misses(self) -> int:
        return sum(1 for c in self.checks if c.status == "miss")


class TestArtifacts(BaseModel):
    screenshots: list[str] = Field(
        default_factory=list,
        description="Ordered list of step-by-step screenshots written by snap()",
    )
    trace_path: str = Field(default="", description="trace.zip from pytest-playwright (failures only by default)")
    video_path: str = Field(default="", description="webm video (failures only by default)")


class TestResult(BaseModel):
    test_case_id: str
    status: Literal["passed", "failed", "error"]
    duration_s: float = 0.0
    failure_message: str = ""
    artifacts: TestArtifacts = Field(default_factory=TestArtifacts)


class RunResults(BaseModel):
    results: list[TestResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status in ("failed", "error"))
