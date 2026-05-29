"""Gold-standard evaluation cases for the HEALER step.

Input: a TestCase + the PREVIOUS (broken) code + the pytest failure message +
a FRESH SiteMap + answers. Output: corrected pytest-playwright code.

Unlike the other steps, the Healer must DIAGNOSE: read the failure, infer the
real cause, and fix it using the fresh SiteMap. Each case encodes a specific bug
and what a correct fix must / must not contain:

  must_contain     → substrings the corrected code MUST have (the actual fix)
  must_not_contain → substrings that signal the bug is still present
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qa_agent.models import (
    GeneratedTest, PageElement, PageSnapshot, SiteMap, TestCase, TestSpec, TestStep, UserFlow,
)


@dataclass
class HealerCase:
    id: str
    spec: TestSpec
    test_case: TestCase
    previous_code: str
    failure_message: str
    sitemap: SiteMap
    answers: dict[str, str]
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)


def _el(role, name, purpose="x", container_id=""):
    return PageElement(role=role, name=name, purpose=purpose, container_id=container_id)


def _step(k, t):
    return TestStep(keyword=k, text=t)


def _spec(url):
    return TestSpec(app_name="App", app_url=url,
                    user_flows=[UserFlow(name="f", description="d")],
                    acceptance_criteria=["x"], provided_values=[], notes="")


# ── Case 1: strict-mode multi-match → fix is exact=True ─────────────────────
_strict = HealerCase(
    id="strict-mode-multimatch",
    spec=_spec("https://app.example.com/login"),
    test_case=TestCase(
        id="login", title="User logs in", page_urls=["https://app.example.com/login"],
        steps=[_step("When", "fill creds and click Login"), _step("Then", "dashboard shown")],
        expected_outcome="dashboard",
    ),
    previous_code=(
        "from playwright.sync_api import Page, expect\n\n"
        "def test_login(page: Page, snap):\n"
        "    page.goto(\"https://app.example.com/login\")\n"
        "    page.get_by_role(\"textbox\", name=\"Username\").fill(\"u\")\n"
        "    page.get_by_role(\"textbox\", name=\"Password\").fill(\"p\")\n"
        "    page.get_by_role(\"button\", name=\"Login\").click()\n"
        "    snap(\"after login\")\n"
    ),
    failure_message=(
        "playwright._impl._errors.Error: strict mode violation: "
        "get_by_role(\"button\", name=\"Login\") resolved to 2 elements:\n"
        "  1) <button>Login</button>\n  2) <button>Login with OTP</button>"
    ),
    sitemap=SiteMap(pages={"https://app.example.com/login": PageSnapshot(
        url="https://app.example.com/login", title="Login",
        elements=[_el("textbox", "Username"), _el("textbox", "Password"),
                  _el("button", "Login"), _el("button", "Login with OTP")],
        raw_accessibility_summary="",
    )}),
    answers={"login-username": "u", "login-password": "p"},
    must_contain=["exact=True"],
    must_not_contain=[],
)

# ── Case 2: wrong locator name → fix is to use the SiteMap's real name ───────
_wrongname = HealerCase(
    id="wrong-locator-name",
    spec=_spec("https://app.example.com/login"),
    test_case=TestCase(
        id="login", title="User logs in", page_urls=["https://app.example.com/login"],
        steps=[_step("When", "click the sign-in button"), _step("Then", "home shown")],
        expected_outcome="home",
    ),
    previous_code=(
        "from playwright.sync_api import Page, expect\n\n"
        "def test_login(page: Page, snap):\n"
        "    page.goto(\"https://app.example.com/login\")\n"
        "    page.get_by_role(\"button\", name=\"Sign In\", exact=True).click()\n"
        "    snap(\"after\")\n"
    ),
    failure_message=(
        "TimeoutError: Locator.click: Timeout 5000ms exceeded.\n"
        "waiting for get_by_role(\"button\", name=\"Sign In\", exact=True)\n"
        "(no element found)"
    ),
    sitemap=SiteMap(pages={"https://app.example.com/login": PageSnapshot(
        url="https://app.example.com/login", title="Login",
        elements=[_el("button", "Log in"), _el("text", "Home")],
        raw_accessibility_summary="",
    )}),
    answers={},
    must_contain=["Log in"],
    must_not_contain=["Sign In"],
)

# ── Case 3: role='text' misuse → fix is get_by_text, not get_by_role(text) ──
_textrole = HealerCase(
    id="text-role-misuse",
    spec=_spec("https://app.example.com"),
    test_case=TestCase(
        id="save", title="Save shows confirmation", page_urls=["https://app.example.com/form"],
        steps=[_step("When", "click Save"), _step("Then", "'Saved successfully' is shown")],
        expected_outcome="saved",
    ),
    previous_code=(
        "from playwright.sync_api import Page, expect\n\n"
        "def test_save(page: Page, snap):\n"
        "    page.goto(\"https://app.example.com/form\")\n"
        "    page.get_by_role(\"button\", name=\"Save\", exact=True).click()\n"
        "    expect(page.get_by_role(\"text\", name=\"Saved successfully\")).to_be_visible()\n"
        "    snap(\"after save\")\n"
    ),
    failure_message=(
        "playwright._impl._errors.Error: Unknown role \"text\".\n"
        "get_by_role(\"text\", name=\"Saved successfully\") found 0 elements."
    ),
    sitemap=SiteMap(pages={"https://app.example.com/form": PageSnapshot(
        url="https://app.example.com/form", title="Form",
        elements=[_el("button", "Save"), _el("text", "Saved successfully")],
        raw_accessibility_summary="",
    )}),
    answers={},
    must_contain=["get_by_text("],
    must_not_contain=['get_by_role("text"'],
)

CASES: list[HealerCase] = [_strict[0] if isinstance(_strict, tuple) else _strict, _wrongname, _textrole]
