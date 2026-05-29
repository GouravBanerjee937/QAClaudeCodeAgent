"""Gold-standard evaluation cases for the ANALYST step.

Each case is a PRD plus a hand-written "answer key" so we can score a model's
TestSpec output objectively, instead of eyeballing it. The harness in
`analyst_eval.py` runs each model on every case and compares against these keys.

Answer-key fields and which criterion they score:
  gold_app_url          → did it pick the right app URL
  gold_provided_values  → C1 precision/recall of values the test must TYPE/USE
  forbidden_labels      → C1 "label leak": UI text it must NOT extract as a value
  gold_criteria         → C3 recall of acceptance criteria (fuzzy-matched)
  must_not_fabricate    → C4 restraint: keys it must NOT invent (not in the PRD)
  must_flag_terms       → C4 restraint: notes must mention the gap (any term, substring)

Keep PRDs realistic but short. Edit/extend freely before running with --execute.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AnalystCase:
    id: str
    prd_text: str
    app_url: str                                   # URL passed to analyst separately
    gold_app_url: str
    gold_provided_values: dict[str, str] = field(default_factory=dict)
    forbidden_labels: list[str] = field(default_factory=list)
    gold_criteria: list[str] = field(default_factory=list)
    must_not_fabricate: list[str] = field(default_factory=list)
    must_flag_terms: list[str] = field(default_factory=list)

    @property
    def has_restraint_check(self) -> bool:
        return bool(self.must_not_fabricate or self.must_flag_terms)


CASES: list[AnalystCase] = [
    # ── Case 1: complete PRD, many concrete values, no gaps ──────────────────
    AnalystCase(
        id="invoice-decrements-qty",
        app_url="http://localhost:3000/#/login",
        gold_app_url="http://localhost:3000/#/login",
        prd_text="""\
Test that creating a sales invoice decrements the item quantity in Item Master.
App URL: http://localhost:3000/#/login

1. Log in: username "Gourav", password "1234", click Login.
2. Click the "Item Master" link. Note the "Quantity available" for "Pencil".
3. Click "Invoice Creation", then "Create Invoice".
4. Fill the form: Invoice number 12, item "Pencil", Amount 1, Price 10.
   Click "Save Invoice". Verify the "Invoice saved." message appears.
5. Go back to "Item Master" and verify Pencil's quantity decreased by 1.
""",
        gold_provided_values={
            "login-username": "Gourav",
            "login-password": "1234",
            "invoice-number": "12",
            "invoice-item": "Pencil",
            "invoice-amount": "1",
            "invoice-price": "10",
        },
        forbidden_labels=[
            "Login", "Item Master", "Invoice Creation", "Create Invoice",
            "Save Invoice", "Invoice saved.", "Quantity available",
        ],
        gold_criteria=[
            "User can log in with username and password",
            "Creating a sales invoice shows the 'Invoice saved.' confirmation",
            "Saving an invoice for Pencil decreases Pencil's quantity in Item Master by 1",
        ],
    ),

    # ── Case 2: email login + OTP. Email given; OTP/password are NOT. ─────────
    AnalystCase(
        id="email-otp-login",
        app_url="https://app.example.com/login",
        gold_app_url="https://app.example.com/login",
        prd_text="""\
User Story: Email + OTP login.

As a user I enter my email "qa.user@example.com" on the login page and click
"Send code". The app navigates to an OTP verification page where I enter the
6-digit code I receive and click "Verify" to reach the dashboard.
""",
        gold_provided_values={
            "login-email": "qa.user@example.com",
        },
        forbidden_labels=["Send code", "Verify", "dashboard"],
        gold_criteria=[
            "User can enter email and request a login code",
            "User is taken to an OTP verification page",
            "Entering the OTP code and verifying reaches the dashboard",
        ],
        # The OTP is dynamic and no password exists — the model must NOT invent them.
        must_not_fabricate=["otp-code", "otp", "login-password", "password", "verification-code"],
        must_flag_terms=["otp", "code"],
    ),

    # ── Case 3: search; discriminate typed query (data) vs asserted text ──────
    AnalystCase(
        id="product-search",
        app_url="https://shop.example.com",
        gold_app_url="https://shop.example.com",
        prd_text="""\
On https://shop.example.com a shopper types "wireless mouse" into the search box
and presses Search. The results page must show at least one product whose name
contains "Logitech".
""",
        gold_provided_values={
            "search-query": "wireless mouse",
        },
        # "Logitech" is asserted-against (an expected outcome), not typed → must NOT
        # be a provided_value. "Search" is a button label.
        forbidden_labels=["Logitech", "Search"],
        gold_criteria=[
            "Searching for 'wireless mouse' returns results",
            "At least one result name contains 'Logitech'",
        ],
        must_not_fabricate=["expected-result", "logitech", "product-name"],
    ),

    # ── Case 4: no URL anywhere; must flag the gap, not invent one ────────────
    AnalystCase(
        id="missing-url",
        app_url="",  # user didn't supply one either
        gold_app_url="",
        prd_text="""\
As a registered user I want to update my profile. I open the profile settings,
change my display name to "Jordan", and click Save. A "Profile updated" message
should confirm the change.
""",
        gold_provided_values={
            "profile-display-name": "Jordan",
        },
        forbidden_labels=["Save", "Profile updated", "profile settings"],
        gold_criteria=[
            "User can change their display name to 'Jordan' and save",
            "A 'Profile updated' confirmation is shown after saving",
        ],
        # No URL given → should flag in notes, must not invent a domain.
        must_flag_terms=["url"],
    ),

    # ── Case 5: simple complete contact form ─────────────────────────────────
    AnalystCase(
        id="contact-form",
        app_url="https://www.example.com/contact",
        gold_app_url="https://www.example.com/contact",
        prd_text="""\
On the contact page at https://www.example.com/contact, a visitor fills Name
"Alex Rivera", Email "alex@example.com", and Message "Hello, I need help with
billing." then clicks Send. A "Thanks for reaching out" banner confirms it.
""",
        gold_provided_values={
            "contact-name": "Alex Rivera",
            "contact-email": "alex@example.com",
            "contact-message": "Hello, I need help with billing.",
        },
        forbidden_labels=["Send", "Thanks for reaching out", "Name", "Email", "Message"],
        gold_criteria=[
            "Visitor can fill name, email and message and click Send",
            "A 'Thanks for reaching out' confirmation banner is shown",
        ],
    ),
]
