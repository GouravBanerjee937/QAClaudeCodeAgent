#!/usr/bin/env python3
"""PRD-format experiment v2 — runs the Pencil/invoice scenario against the
local Invoice App on localhost:3000, in 10 different PRD shapes.

Stages scored: Analyst, Inquirer, Designer, End-to-end (first-run + post-heal).
Model: gpt-4.1-mini (per user instruction)."""
from __future__ import annotations

import json
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=True)

from qa_agent import pipeline  # noqa: E402
from qa_agent.events import EventBus  # noqa: E402

# ─────────────────────────── gold for the Pencil scenario ─────────────────
APP_URL = "http://localhost:3000/#/login"
APP_NETLOC = "localhost:3000"

GOLD_PROVIDED = {
    "login-username": "Gourav", "login-password": "1234",
    "invoice-number": "12", "invoice-item": "Pencil",
    "invoice-amount": "1", "invoice-price": "10",
}
GOLD_CRITERIA = [
    "User can log in with username and password",
    "Saving a sales invoice shows the 'Invoice saved.' confirmation",
    "Saving an invoice for Pencil decreases Pencil's Quantity available in Item Master by 1",
]
FORBIDDEN_LABELS = [
    "Login", "Item Master", "Invoice Creation", "Create Invoice",
    "Save Invoice", "Invoice saved.", "Quantity available",
]
FORBIDDEN_SCOPE = [
    "required", "placeholder", "disabled", "accessibility", "keyboard",
    "validation error", "page title", "meta tag", "is masked", "is enabled",
    "aria-", 'type="password"', "type='password'",
]
LITERAL_LEAK_VALUES = ["Gourav"]  # only the unambiguous typed credential

CANONICAL_ANSWERS = {
    "login-username": "Gourav", "login-password": "1234",
    "username": "Gourav", "password": "1234",
    "user-name": "Gourav",
    "invoice-number": "12", "invoice-item": "Pencil",
    "invoice-amount": "1", "invoice-price": "10",
    "item-name": "Pencil", "amount": "1", "price": "10",
}

# ─────────────────────────── 10 PRD variants ──────────────────────────────
V1_FREE_FORM = """Test that creating a sales invoice decrements the item quantity in Item Master.
Go to http://localhost:3000/#/login and log in as Gourav / 1234. Click Item Master and note
Pencil's Quantity available (call it INITIAL_PENCIL_QTY). Then click Invoice Creation, click
Create Invoice, fill Invoice number 12, item Pencil, Amount 1, Price 10 and click Save Invoice.
Verify "Invoice saved." appears. Return to Item Master and verify Pencil's Quantity available
equals INITIAL_PENCIL_QTY minus 1."""

V2_SECTIONED = """## App URL
http://localhost:3000/#/login

## Credentials
username: Gourav
password: 1234

## Sample form data
invoice-number: 12
invoice-item: Pencil
invoice-amount: 1
invoice-price: 10

## Acceptance Criteria
- User can log in with username and password
- Saving a sales invoice shows the 'Invoice saved.' message
- Saving an invoice for Pencil decreases Pencil's Quantity available in Item Master by 1
"""

V3_USER_STORY = """As a sales user
I want to log in at http://localhost:3000/#/login (username Gourav, password 1234), open Item
Master, note Pencil's Quantity available, then go to Invoice Creation and save an invoice with
Invoice number 12, item Pencil, Amount 1, Price 10
So that the 'Invoice saved.' message appears AND when I return to Item Master, Pencil's
Quantity available has decreased by 1.
"""

V4_NUMBERED = """Test the invoice-decrement flow.
URL: http://localhost:3000/#/login

1. Log in: username Gourav, password 1234.
2. Click Item Master. Note Pencil's 'Quantity available'.
3. Click Invoice Creation, then Create Invoice.
4. Fill the form: Invoice number 12, item Pencil, Amount 1, Price 10.
5. Click Save Invoice. Verify 'Invoice saved.' is shown.
6. Click Item Master. Verify Pencil's Quantity available equals the value noted in step 2 minus 1.
"""

V5_VERBOSE = """Our accounting team uses a local web app to manage inventory and invoices.
A common concern raised by sales is whether saving a new invoice actually reflects in the
master inventory list. The flow we want to validate is end-to-end. The QA tester opens
http://localhost:3000/#/login and signs in with username Gourav and password 1234. Once
on the main app, they navigate to the Item Master link in the top navigation. Item Master
shows a table with rows per item, including a column called 'Quantity available'. The
tester focuses on the row for 'Pencil' and notes the current value of that quantity column.
They then go to Invoice Creation, click 'Create Invoice', and fill out the form: Invoice
number 12, item Pencil (select from the dropdown), Amount 1, Price 10. Clicking 'Save
Invoice' should display 'Invoice saved.' on the page. Finally, the tester returns to Item
Master and checks that Pencil's Quantity available is now exactly one less than the noted
baseline value. This single end-to-end test gives confidence the invoice → inventory link
is wired up correctly.
"""

V6_MINIMAL = ("Login at http://localhost:3000/#/login as Gourav/1234. Note Pencil qty in "
              "Item Master. Save an invoice for Pencil (number 12, amount 1, price 10). "
              "Verify 'Invoice saved.' and that Pencil qty decreased by 1.")

V7_GHERKIN = """Feature: Sales invoice decrements item quantity

Scenario: Saving an invoice for Pencil decreases its available quantity by 1
  Given I open http://localhost:3000/#/login
  And I log in with username "Gourav" and password "1234"
  When I open Item Master and note the 'Quantity available' for 'Pencil' as INITIAL_PENCIL_QTY
  And I open Invoice Creation and click 'Create Invoice'
  And I fill: Invoice number 12, Item 'Pencil', Amount 1, Price 10
  And I click 'Save Invoice'
  Then I see the message 'Invoice saved.'
  And when I return to Item Master, Pencil's 'Quantity available' equals INITIAL_PENCIL_QTY - 1
"""

V8_BDD_BLOCK = """Scenario: Creating a Pencil invoice decrements Pencil quantity by 1

Given:
- App URL: http://localhost:3000/#/login
- Logged in as Gourav / 1234
- Pencil's current 'Quantity available' in Item Master recorded as INITIAL_PENCIL_QTY

When:
- Navigate to Invoice Creation, click 'Create Invoice'
- Fill: Invoice number = 12, Item = Pencil, Amount = 1, Price = 10
- Click 'Save Invoice'

Then:
- 'Invoice saved.' message is visible
- In Item Master, Pencil's 'Quantity available' equals INITIAL_PENCIL_QTY - 1
"""

V9_TABLE = """# Test data
| Field | Value |
|---|---|
| App URL | http://localhost:3000/#/login |
| Username | Gourav |
| Password | 1234 |
| Invoice number | 12 |
| Item | Pencil |
| Amount | 1 |
| Price | 10 |

# Steps and expected results
| # | Action | Expected |
|---|---|---|
| 1 | Open URL and log in with the credentials above | Logged in |
| 2 | Open Item Master | Pencil row visible; note Quantity available as INITIAL_PENCIL_QTY |
| 3 | Open Invoice Creation, click Create Invoice | Invoice form visible |
| 4 | Fill the form per Test data, click Save Invoice | 'Invoice saved.' message shown |
| 5 | Open Item Master | Pencil's Quantity available equals INITIAL_PENCIL_QTY - 1 |
"""

V10_JSON = """```json
{
  "app_url": "http://localhost:3000/#/login",
  "credentials": {"username": "Gourav", "password": "1234"},
  "form": {
    "invoice-number": "12",
    "invoice-item": "Pencil",
    "invoice-amount": "1",
    "invoice-price": "10"
  },
  "acceptance_criteria": [
    "User can log in with username and password",
    "Saving a sales invoice shows the 'Invoice saved.' message",
    "Saving an invoice for Pencil decreases Pencil's 'Quantity available' in Item Master by 1"
  ]
}
```"""

VARIANTS = [
    # V1-V4 already completed in the previous run; resume from V5.
    ("V5 verbose w/ backstory", V5_VERBOSE),
    ("V6 minimal/terse",        V6_MINIMAL),
    ("V7 Gherkin",              V7_GHERKIN),
    ("V8 BDD scenario block",   V8_BDD_BLOCK),
    ("V9 table-style",          V9_TABLE),
    ("V10 JSON spec",           V10_JSON),
]

# ─────────────────────────── scoring helpers ──────────────────────────────
def _norm(s): return " ".join(str(s).lower().split())

def _values_match(a, b):
    na, nb = _norm(a), _norm(b)
    if na == nb: return True
    return len(na) > 1 and len(nb) > 1 and (na in nb or nb in na)

def _fuzzy_covered(gold, candidates):
    g = _norm(gold); gw = {w for w in g.split() if len(w) > 3}
    for c in candidates:
        cn = _norm(c)
        if SequenceMatcher(None, g, cn).ratio() >= 0.4: return True
        if gw and sum(1 for w in gw if w in cn) / len(gw) >= 0.5: return True
    return False

def score_analyst(spec):
    extracted = {pv.key: pv.value for pv in spec.provided_values}
    ev = list(extracted.values())
    gold_vals = list(GOLD_PROVIDED.values())
    used = [False] * len(ev)
    covered = 0
    for gv in gold_vals:
        for i, e in enumerate(ev):
            if not used[i] and _values_match(gv, e):
                used[i] = True; covered += 1; break
    recall = covered / len(gold_vals) if gold_vals else 1.0
    precision = covered / len(ev) if ev else (1.0 if not gold_vals else 0.0)
    # label leaks — exclude values that contain a gold value substring
    # (so "SuperSecretPassword!" can't trigger because it contains "password")
    leaks = 0
    forbid = [_norm(x) for x in FORBIDDEN_LABELS]
    gold_set = [_norm(v) for v in gold_vals]
    for k, v in extracted.items():
        vn = _norm(v)
        # Skip if value matches any gold value (it's a legitimate value, not a label)
        if any(vn == gv or vn in gv or gv in vn for gv in gold_set):
            pass
        elif any(vn == f or vn in f or f in vn for f in forbid):
            leaks += 1
        if k.lower().endswith(("-label", "-text", "-heading", "-title", "-button")):
            leaks += 1
    produced = list(spec.acceptance_criteria)
    crit_recall = sum(1 for g in GOLD_CRITERIA if _fuzzy_covered(g, produced)) / len(GOLD_CRITERIA)
    extra = [k for k, v in extracted.items() if not any(_values_match(v, gv) for gv in gold_vals)]
    restraint_pass = (len(extra) == 0)
    return {
        "prec": precision, "rec": recall, "leaks": leaks,
        "crit": crit_recall, "restraint": 1.0 if restraint_pass else 0.0,
        "n_extracted": len(extracted), "n_criteria": len(produced),
        "notes": (spec.notes or "")[:120],
    }

def score_inquirer(questions):
    over = 0
    n = len(questions.items)
    keywords = ("user", "pass", "email", "invoice", "item", "amount", "price")
    for q in questions.items:
        text = (q.key + " " + (q.prompt or "") + " " + (q.hint or "")).lower()
        if any(t in text for t in keywords):
            over += 1
    return {"n_questions": n, "over_ask": over}

def _plan_case_text(tc):
    parts = [tc.title, tc.expected_outcome] + [s.text for s in tc.steps]
    return _norm(" ".join(p for p in parts if p))

def score_designer(plan):
    cases = list(plan.test_cases)
    texts = [_plan_case_text(tc) for tc in cases]
    full = " ".join(texts)
    coverage = sum(1 for g in GOLD_CRITERIA if any(_fuzzy_covered(g, [t]) for t in texts)) / len(GOLD_CRITERIA)
    scope = sum(full.count(_norm(p)) for p in FORBIDDEN_SCOPE)
    urls = [u for tc in cases for u in tc.page_urls]
    if urls:
        clean = 0
        for u in urls:
            if not u or "{" in u or u.startswith("/"): clean += 1; continue
            try:
                net = urlparse(u).netloc
                if net == "" or net == APP_NETLOC: clean += 1
            except Exception:
                pass
        url_ok = clean / len(urls)
    else:
        url_ok = 1.0
    step_text = _norm(" ".join(s.text for tc in cases for s in tc.steps))
    leaks = sum(1 for v in LITERAL_LEAK_VALUES if _norm(v) in step_text)
    return {
        "n_cases": len(cases), "coverage": coverage,
        "scope_viol": scope, "url_ok": url_ok, "leak": leaks,
    }

def score_e2e(results):
    if not results or not results.results:
        return {"passed": 0, "failed": 0, "n": 0, "pass_rate": 0.0}
    n = len(results.results); p = results.passed; f = results.failed
    return {"passed": p, "failed": f, "n": n, "pass_rate": (p / n) if n else 0.0}

# ─────────────────────────── runner ───────────────────────────────────────
def build_answers(questions):
    out = {}
    for q in questions.items:
        k = q.key.lower()
        if k in CANONICAL_ANSWERS:
            out[q.key] = CANONICAL_ANSWERS[k]
        elif "pass" in k:
            out[q.key] = "1234"
        elif "user" in k or "name" in k:
            out[q.key] = "Gourav"
        elif "amount" in k or "qty" in k:
            out[q.key] = "1"
        elif "price" in k:
            out[q.key] = "10"
        elif "item" in k:
            out[q.key] = "Pencil"
        elif "invoice" in k and "number" in k:
            out[q.key] = "12"
        else:
            out[q.key] = ""
    for k, v in GOLD_PROVIDED.items():
        out.setdefault(k, v)
    return out

def run_variant(name, prd_text):
    print("\n" + "=" * 90)
    print(f"VARIANT: {name}")
    print("=" * 90, flush=True)
    bus = EventBus()
    t0 = time.perf_counter()
    try:
        phase_a = pipeline.run_phase_a(prd_text, APP_URL, bus)
    except Exception as e:
        return {"name": name, "error": f"Phase A failed: {e}", "latency_s": time.perf_counter() - t0}
    spec = phase_a.spec
    questions = phase_a.questions
    a_score = score_analyst(spec)
    i_score = score_inquirer(questions)
    answers = build_answers(questions)
    try:
        phase_b = pipeline.run_phase_b(spec, answers, bus, heal_failures=True)
    except Exception as e:
        return {"name": name, "error": f"Phase B failed: {e}", "analyst": a_score,
                "inquirer": i_score, "latency_s": time.perf_counter() - t0}
    d_score = score_designer(phase_b.plan) if phase_b.plan else {}
    e_first = score_e2e(phase_b.initial_results)
    e_final = score_e2e(phase_b.final_results)
    e_score = {**e_final, "first_pass_rate": e_first["pass_rate"],
               "first_passed": e_first["passed"], "first_n": e_first["n"]}
    latency = time.perf_counter() - t0
    print(f"  → analyst={a_score}\n     inquirer={i_score}\n     designer={d_score}\n     e2e={e_score} ({latency:.1f}s)", flush=True)
    return {"name": name, "analyst": a_score, "inquirer": i_score,
            "designer": d_score, "e2e": e_score, "latency_s": latency}

def main():
    results = []
    for name, prd in VARIANTS:
        results.append(run_variant(name, prd))
    print("\n\n" + "=" * 130)
    print("FORMAT EXPERIMENT v2 — SUMMARY  (target: localhost:3000 Pencil invoice scenario, model: gpt-4.1-mini)")
    print("=" * 130)
    cols = [
        ("variant",    lambda r: r["name"], 26),
        ("prec",       lambda r: f"{r.get('analyst',{}).get('prec',0)*100:.0f}%", 5),
        ("rec",        lambda r: f"{r.get('analyst',{}).get('rec',0)*100:.0f}%", 5),
        ("leaks",      lambda r: str(r.get('analyst',{}).get('leaks',0)), 5),
        ("crit",       lambda r: f"{r.get('analyst',{}).get('crit',0)*100:.0f}%", 5),
        ("#crit",      lambda r: str(r.get('analyst',{}).get('n_criteria',0)), 5),
        ("rstrnt",     lambda r: f"{r.get('analyst',{}).get('restraint',0)*100:.0f}%", 6),
        ("over-ask",   lambda r: str(r.get('inquirer',{}).get('over_ask',0)), 8),
        ("#cases",     lambda r: str(r.get('designer',{}).get('n_cases',0)), 6),
        ("cov",        lambda r: f"{r.get('designer',{}).get('coverage',0)*100:.0f}%", 5),
        ("scope",      lambda r: str(r.get('designer',{}).get('scope_viol',0)), 5),
        ("urlok",      lambda r: f"{r.get('designer',{}).get('url_ok',1.0)*100:.0f}%", 5),
        ("leak",       lambda r: str(r.get('designer',{}).get('leak',0)), 4),
        ("1st-pass",   lambda r: f"{r.get('e2e',{}).get('first_passed',0)}/{r.get('e2e',{}).get('first_n',0)}", 8),
        ("post-heal",  lambda r: f"{r.get('e2e',{}).get('passed',0)}/{r.get('e2e',{}).get('n',0)}", 9),
        ("E2E %",      lambda r: f"{r.get('e2e',{}).get('pass_rate',0)*100:.0f}%", 6),
        ("lat",        lambda r: f"{r.get('latency_s',0):.0f}s", 5),
    ]
    fmt = " | ".join("{:<%d}" % c[2] for c in cols)
    print(fmt.format(*[c[0] for c in cols]))
    print("-" * (sum(c[2] for c in cols) + 3 * (len(cols) - 1)))
    for r in results:
        print(fmt.format(*[c[1](r) for c in cols]))
    Path("/tmp/format_experiment_v2_results.json").write_text(
        json.dumps(results, default=str, indent=2)
    )
    print("\n(JSON also saved to /tmp/format_experiment_v2_results.json)")

if __name__ == "__main__":
    main()
