"""Gold-standard evaluation cases for the EXPLORER LABELER step.

The labeler receives a list of (role, name) pairs scraped from a page and must
return the SAME pairs, each annotated with a one-line `purpose`. It must NOT
drop any element and must NOT invent new ones (the prompt: "do not invent").

The documented failure mode is silently dropping elements on long lists, so the
cases scale up in size. Structural roles (row/columnheader/cell/listitem) are
excluded because the real pipeline routes those AROUND the labeler.

Answer key = the input pairs themselves:
  fidelity (recall) = input pairs present in output
  invented           = output pairs not in input
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExplorerCase:
    id: str
    pairs: list[tuple[str, str]]


CASES: list[ExplorerCase] = [
    # 1 — small login page (5)
    ExplorerCase(id="login-small", pairs=[
        ("heading", "Login Page"),
        ("textbox", "Username"),
        ("textbox", "Password"),
        ("button", "Login"),
        ("link", "Forgot password?"),
    ]),

    # 2 — dashboard with nav (12)
    ExplorerCase(id="dashboard-medium", pairs=[
        ("heading", "Dashboard"),
        ("link", "Home"), ("link", "Item Master"), ("link", "Invoice Creation"),
        ("link", "Reports"), ("link", "Settings"), ("link", "Logout"),
        ("button", "Create Invoice"), ("button", "Export CSV"),
        ("textbox", "Search"), ("combobox", "Filter by status"),
        ("text", "Welcome back, Gourav"),
    ]),

    # 3 — big form page (36) — stresses the no-drop failure mode
    ExplorerCase(id="big-form", pairs=[
        ("heading", "Create Invoice"),
        ("textbox", "Invoice number"), ("textbox", "Invoice date"),
        ("textbox", "Customer name"), ("textbox", "Customer email"),
        ("textbox", "Billing address"), ("textbox", "Shipping address"),
        ("combobox", "Item name"), ("textbox", "Quantity"), ("textbox", "Unit price"),
        ("textbox", "Discount"), ("textbox", "Tax rate"), ("textbox", "Notes"),
        ("combobox", "Currency"), ("combobox", "Payment terms"),
        ("checkbox", "Apply discount"), ("checkbox", "Send copy to customer"),
        ("checkbox", "Mark as paid"), ("button", "Add line item"),
        ("button", "Remove line item"), ("button", "Save draft"),
        ("button", "Save Invoice"), ("button", "Cancel"), ("button", "Preview"),
        ("link", "Back to list"), ("link", "Help"), ("link", "Item Master"),
        ("heading", "Line items"), ("heading", "Totals"),
        ("text", "Subtotal"), ("text", "Tax"), ("text", "Total due"),
        ("text", "Invoice saved."), ("text", "Please fill all required fields"),
        ("button", "Download PDF"), ("button", "Email invoice"),
    ]),

    # 4 — status/message-heavy page (9), tricky 'text' roles
    ExplorerCase(id="status-messages", pairs=[
        ("heading", "Account Settings"),
        ("textbox", "Display name"), ("button", "Save"),
        ("text", "Profile updated"), ("text", "Changes not saved"),
        ("text", "Email already in use"), ("link", "Change password"),
        ("checkbox", "Enable two-factor auth"), ("button", "Delete account"),
    ]),
]
