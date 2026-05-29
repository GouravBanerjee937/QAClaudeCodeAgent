"""Gold-standard evaluation cases for the DESIGNER step.

Input: a TestSpec + answers dict. Output: a TestPlan (list of Gherkin TestCases).
Each case carries an answer key for the Designer's five criteria:

  gold_criteria   → C1 coverage: each must be covered by some test case (fuzzy).
  forbidden_scope → C2 scope discipline: phrases that signal invented extra checks
                    the PRD never asked for (the prompt explicitly forbids these).
  literal_values  → C4 placeholder discipline: answer values that must appear as
                    `{key}` references, NOT typed literally (password values are
                    masked by the Designer, so they're never in this list).

URL discipline (C4) and concreteness (C3) are derived structurally in the scorer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qa_agent.models import ProvidedValue, TestSpec, UserFlow

# Scope-creep markers the Designer prompt explicitly forbids inventing.
DEFAULT_FORBIDDEN_SCOPE = [
    "required", "placeholder", "disabled", "accessibility", "keyboard",
    "validation error", "page title", "meta tag", "is masked", "is enabled",
    "aria-", "type='password'", 'type="password"',
]


@dataclass
class DesignerCase:
    id: str
    spec: TestSpec
    answers: dict[str, str]
    gold_criteria: list[str] = field(default_factory=list)
    forbidden_scope: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_SCOPE))
    literal_values: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.gold_criteria:
            self.gold_criteria = list(self.spec.acceptance_criteria)


def _spec(**kw) -> TestSpec:
    kw.setdefault("notes", "")
    return TestSpec(**kw)


CASES: list[DesignerCase] = [
    # 1 — multi-criterion flow with many concrete values (tests coverage + placeholder use)
    DesignerCase(
        id="login-invoice",
        spec=_spec(
            app_name="Accounting App",
            app_url="http://localhost:3000/#/login",
            user_flows=[UserFlow(name="Invoice", description="Create an invoice and check stock")],
            acceptance_criteria=[
                "User can log in with username and password",
                "Saving a sales invoice shows the 'Invoice saved.' confirmation",
                "Saving an invoice for Pencil decreases Pencil's quantity in Item Master by 1",
            ],
            provided_values=[],
        ),
        answers={
            "login-username": "Gourav", "login-password": "1234",
            "invoice-number": "12", "invoice-item": "Pencil",
            "invoice-amount": "1", "invoice-price": "10",
        },
        literal_values=["Gourav"],  # username must be {login-username}, never typed literally
    ),

    # 2 — single simple login criterion
    DesignerCase(
        id="simple-login",
        spec=_spec(
            app_name="SaaS App",
            app_url="https://app.example.com/login",
            user_flows=[UserFlow(name="Login", description="Email + password login")],
            acceptance_criteria=["User logs in with email and password and reaches the home page"],
            provided_values=[],
        ),
        answers={"login-email": "qa.user@example.com", "login-password": "secret"},
        literal_values=["qa.user@example.com"],
    ),

    # 3 — search with an asserted result (Logitech is expected text, not an input)
    DesignerCase(
        id="product-search",
        spec=_spec(
            app_name="Shop",
            app_url="https://shop.example.com",
            user_flows=[UserFlow(name="Search", description="Search products")],
            acceptance_criteria=["Searching for 'wireless mouse' shows a result whose name contains 'Logitech'"],
            provided_values=[],
        ),
        answers={"search-query": "wireless mouse"},
        literal_values=["wireless mouse"],  # should be {search-query}
    ),

    # 4 — minimal criterion designed to BAIT scope creep (extra validation checks)
    DesignerCase(
        id="scope-bait",
        spec=_spec(
            app_name="Marketing Site",
            app_url="https://www.example.com/contact",
            user_flows=[UserFlow(name="Contact", description="Submit the contact form")],
            acceptance_criteria=["Submitting the contact form shows a confirmation message"],
            provided_values=[],
        ),
        answers={"contact-name": "Alex Rivera", "contact-email": "alex@example.com",
                 "contact-message": "Hello, I need help with billing."},
        literal_values=["Alex Rivera", "alex@example.com"],
    ),

    # 5 — navigation with no full URL given → must use a path, never invent a domain
    DesignerCase(
        id="url-discipline",
        spec=_spec(
            app_name="Corp Site",
            app_url="https://site.example.com",
            user_flows=[UserFlow(name="Pricing", description="Navigate to pricing")],
            acceptance_criteria=["From the home page, clicking 'Pricing' navigates to the pricing page"],
            provided_values=[],
        ),
        answers={},
        literal_values=[],
    ),
]
