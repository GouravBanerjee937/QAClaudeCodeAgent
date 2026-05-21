"""Run the full QA pipeline end-to-end against the live Invoice App, with the
user's Pencil-inventory PRD. No Streamlit — just calls into the library."""
from __future__ import annotations

import sys
from qa_agent import pipeline
from qa_agent.events import EventBus, stdout_sink

PRD = """Test that creating a sales invoice decrements the item quantity in Item Master.
App URL: http://localhost:3000/#/login

1. Log in: username "Gourav", password "1234", click Login.

2. Click the "Item Master" link in the top navigation.
   Read and remember the "Quantity available" value shown in the row for
   the item named "Pencil". Call this number INITIAL_PENCIL_QTY.

3. Click the "Invoice Creation" link in the top navigation, then click
   the "Create Invoice" button.

4. Fill the Create Invoice form:
   - Invoice number: 11
   - Item name (select from dropdown): Pencil
   - Amount: 1
   - Price: 10
   Click "Save Invoice". Verify the "Invoice saved." message is shown.

5. Click the "Item Master" link in the top navigation.
   Verify the "Quantity available" value in the row for "Pencil" now
   equals (INITIAL_PENCIL_QTY - 1).
"""

APP_URL = "http://localhost:3000/#/login"


def main() -> int:
    bus = EventBus()
    bus.subscribe(stdout_sink)
    print("=" * 70)
    print("PHASE A — Analyst + Inquirer")
    print("=" * 70)
    phase_a = pipeline.run_phase_a(PRD, APP_URL, bus)

    # Build answers from provided_values; if Inquirer asked anything we can't
    # answer here, fall back to credentials from PRD.
    answers: dict[str, str] = {pv.key: pv.value for pv in phase_a.spec.provided_values}
    for q in phase_a.questions.items:
        if q.key in answers:
            continue
        if "username" in q.key or "user" in q.key:
            answers[q.key] = "Gourav"
        elif "password" in q.key:
            answers[q.key] = "1234"
        else:
            answers[q.key] = ""
    print(f"\nAnswers: {answers}\n")

    print("=" * 70)
    print("PHASE B — Designer → … → Reporter")
    print("=" * 70)
    phase_b = pipeline.run_phase_b(phase_a.spec, answers, bus)

    final = phase_b.final_results
    print()
    print("=" * 70)
    print(f"RESULT: {final.passed}/{len(final.results)} passed, {final.failed} failed")
    print("=" * 70)
    for r in final.results:
        marker = "PASS" if r.status == "passed" else "FAIL"
        print(f"  [{marker}] {r.test_case_id}")
        if r.status != "passed" and r.failure_message:
            print(f"         {r.failure_message[:200]}")
    return 0 if final.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
