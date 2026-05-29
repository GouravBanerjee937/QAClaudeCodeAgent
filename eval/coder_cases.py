"""Gold-standard evaluation cases for the CODER step.

Input: a TestSpec + a TestCase + a SiteMap + answers. Output: a complete
pytest-playwright Python file (free text). We score it WITHOUT running a browser:
  - AST validity (does it parse)
  - grounding (every locator name exists in the SiteMap — reuses the real validator)
  - hard-rule adherence (signature, snap(), role-based locators, exact=True, etc.)

Each case is a self-contained (spec, test_case, sitemap, answers) bundle built
from the real qa_agent models, plus a couple of expectations:
  must_select_option → role 'combobox' elements that must use .select_option (not .fill)
  expects_needs      → True if at least one step legitimately has NO SiteMap element
                       (the Coder should emit a `# NEEDS:` marker, not invent)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qa_agent.models import (
    PageElement, PageSnapshot, SiteMap, TestCase, TestSpec, TestStep, UserFlow,
)


@dataclass
class CoderCase:
    id: str
    spec: TestSpec
    test_case: TestCase
    sitemap: SiteMap
    answers: dict[str, str]
    must_select_option: list[str] = field(default_factory=list)
    expects_needs: bool = False


def _el(role, name, purpose="x", container_id=""):
    return PageElement(role=role, name=name, purpose=purpose, container_id=container_id)


def _step(k, t):
    return TestStep(keyword=k, text=t)


# ── Case 1: basic login + status message (role='text') ──────────────────────
_login_spec = TestSpec(
    app_name="App", app_url="https://app.example.com/login",
    user_flows=[UserFlow(name="Login", description="login")],
    acceptance_criteria=["User can log in"], provided_values=[], notes="",
)
_login = CoderCase(
    id="login-basic",
    spec=_login_spec,
    test_case=TestCase(
        id="login-valid", title="User can log in with valid credentials",
        page_urls=["https://app.example.com/login"],
        steps=[
            _step("Given", "the user is on the login page"),
            _step("When", "the user fills {login-username} and {login-password}"),
            _step("And", "the user clicks Login"),
            _step("Then", "a 'Welcome back' message is shown"),
        ],
        expected_outcome="The dashboard greeting 'Welcome back' is visible",
    ),
    sitemap=SiteMap(pages={"https://app.example.com/login": PageSnapshot(
        url="https://app.example.com/login", title="Login",
        elements=[_el("textbox", "Username"), _el("textbox", "Password"),
                  _el("button", "Login"), _el("text", "Welcome back")],
        raw_accessibility_summary="",
    )}),
    answers={"login-username": "qa_user", "login-password": "secret"},
)

# ── Case 2: form with a combobox → must use .select_option, not .fill ────────
_form_spec = TestSpec(
    app_name="Invoices", app_url="https://inv.example.com",
    user_flows=[UserFlow(name="Create", description="create invoice")],
    acceptance_criteria=["User can create an invoice"], provided_values=[], notes="",
)
_form = CoderCase(
    id="invoice-combobox",
    spec=_form_spec,
    test_case=TestCase(
        id="create-invoice", title="Create an invoice and see confirmation",
        page_urls=["https://inv.example.com/new"],
        steps=[
            _step("Given", "on the create-invoice page"),
            _step("When", "the user selects item {invoice-item} from the Item dropdown"),
            _step("And", "fills Quantity {invoice-qty} and clicks Save Invoice"),
            _step("Then", "an 'Invoice saved.' message appears"),
        ],
        expected_outcome="'Invoice saved.' is visible",
    ),
    sitemap=SiteMap(pages={"https://inv.example.com/new": PageSnapshot(
        url="https://inv.example.com/new", title="New Invoice",
        elements=[_el("combobox", "Item"), _el("textbox", "Quantity"),
                  _el("button", "Save Invoice"), _el("text", "Invoice saved.")],
        raw_accessibility_summary="",
    )}),
    answers={"invoice-item": "Pencil", "invoice-qty": "3"},
    must_select_option=["Item"],
)

# ── Case 3: a step with NO matching SiteMap element → expect a # NEEDS: marker ─
_needs_spec = TestSpec(
    app_name="App", app_url="https://app.example.com",
    user_flows=[UserFlow(name="Profile", description="update profile")],
    acceptance_criteria=["User can update profile"], provided_values=[], notes="",
)
_needs = CoderCase(
    id="missing-element",
    spec=_needs_spec,
    test_case=TestCase(
        id="update-avatar", title="User uploads a new avatar",
        page_urls=["https://app.example.com/profile"],
        steps=[
            _step("Given", "on the profile page"),
            _step("When", "the user clicks Edit"),
            _step("And", "uploads an avatar via the 'Upload avatar' button"),
            _step("Then", "a 'Saved' message appears"),
        ],
        expected_outcome="'Saved' is visible",
    ),
    # SiteMap deliberately LACKS an 'Upload avatar' control.
    sitemap=SiteMap(pages={"https://app.example.com/profile": PageSnapshot(
        url="https://app.example.com/profile", title="Profile",
        elements=[_el("button", "Edit"), _el("text", "Saved")],
        raw_accessibility_summary="",
    )}),
    answers={},
    expects_needs=True,
)

CASES: list[CoderCase] = [_login, _form, _needs]
