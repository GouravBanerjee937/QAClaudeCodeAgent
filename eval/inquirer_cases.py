"""Gold-standard evaluation cases for the INQUIRER step.

The Inquirer's input is a TestSpec; its output is a `Questions` list (the
concrete values it wants the user to supply). Each case below is a hand-built
TestSpec plus an answer key describing what the Inquirer SHOULD and SHOULD NOT
ask for.

Answer-key fields and which criterion they score:
  expected_ask    → C "gap coverage": values genuinely missing that it MUST ask.
                    Each entry is (concept_keyword, expected_kind | "").
  forbidden_ask   → C "restraint": concepts it must NOT ask for (already in
                    provided_values, or UI labels). Any question matching these
                    is an over-ask.
  Matching is by keyword: a Question matches a concept if the keyword appears in
  its key, prompt, or hint (case-insensitive) — key names aren't mandated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qa_agent.models import ProvidedValue, TestSpec, UserFlow


@dataclass
class InquirerCase:
    id: str
    spec: TestSpec
    expected_ask: list[tuple[str, str]] = field(default_factory=list)   # (concept, kind|"")
    forbidden_ask: list[str] = field(default_factory=list)              # concepts


def _spec(**kw) -> TestSpec:
    kw.setdefault("notes", "")
    return TestSpec(**kw)


CASES: list[InquirerCase] = [
    # 1 — username provided, password missing → must ask password (kind=password),
    #     must NOT ask username.
    InquirerCase(
        id="login-need-password",
        spec=_spec(
            app_name="Accounting App",
            app_url="http://localhost:3000/#/login",
            user_flows=[UserFlow(name="Login", description="User logs in to reach dashboard")],
            acceptance_criteria=["User can log in with username and password"],
            provided_values=[ProvidedValue(key="login-username", value="Gourav")],
        ),
        expected_ask=[("password", "password")],
        forbidden_ask=["username"],
    ),

    # 2 — spec is complete (both creds present) → must ask NOTHING. Pure restraint.
    InquirerCase(
        id="spec-complete",
        spec=_spec(
            app_name="Accounting App",
            app_url="http://localhost:3000/#/login",
            user_flows=[UserFlow(name="Login", description="User logs in")],
            acceptance_criteria=["User can log in with username and password"],
            provided_values=[
                ProvidedValue(key="login-username", value="Gourav"),
                ProvidedValue(key="login-password", value="1234"),
            ],
        ),
        expected_ask=[],
        forbidden_ask=["username", "password", "login"],
    ),

    # 3 — flow references "the dashboard" with no URL given → must ask a URL
    #     (kind=url). Creds are provided, so must NOT ask those.
    InquirerCase(
        id="missing-dashboard-url",
        spec=_spec(
            app_name="Reporting Portal",
            app_url="https://portal.example.com/login",
            user_flows=[UserFlow(name="View dashboard", description="After login, open the dashboard")],
            acceptance_criteria=["After login the user lands on the dashboard page"],
            provided_values=[
                ProvidedValue(key="login-username", value="analyst1"),
                ProvidedValue(key="login-password", value="pw123"),
            ],
            notes="The PRD mentions 'the dashboard' but never gives its URL.",
        ),
        expected_ask=[("dashboard", "url")],
        forbidden_ask=["username", "password"],
    ),

    # 4 — email provided, password missing → ask password, not email.
    InquirerCase(
        id="email-need-password",
        spec=_spec(
            app_name="SaaS App",
            app_url="https://app.example.com/login",
            user_flows=[UserFlow(name="Login", description="Email + password login")],
            acceptance_criteria=["User logs in with email and password and reaches home"],
            provided_values=[ProvidedValue(key="login-email", value="qa.user@example.com")],
        ),
        expected_ask=[("password", "password")],
        forbidden_ask=["email"],
    ),

    # 5 — "update an existing customer" with nothing provided → must ask WHICH
    #     customer (test-data selector) and the new value. No creds in spec.
    InquirerCase(
        id="update-customer",
        spec=_spec(
            app_name="CRM",
            app_url="https://crm.example.com",
            user_flows=[UserFlow(name="Update customer", description="Edit an existing customer's phone")],
            acceptance_criteria=["User can update an existing customer's phone number and save"],
            provided_values=[],
            notes="Which customer to edit, and the new phone number, are not specified.",
        ),
        expected_ask=[("customer", ""), ("phone", "")],
        forbidden_ask=["save button", "edit button"],
    ),
]
