"""Step 3: snapshot each URL in the test plan, with interactive login probing.

Deterministic part: Playwright scrapes the page for (role, accessible-name) pairs.
Interactive part: when credentials are available, the explorer attempts to walk
through multi-step login flows (email → continue → password → login) so the
SiteMap also captures elements that only appear AFTER an action.
LLM part: labels each pair with a one-line purpose. It never invents elements.
"""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright
from pydantic import BaseModel

from ..events import EventBus
from ..llm import structured
from ..models import PageElement, PageSnapshot, SiteMap, TestPlan, TestSpec
from ..prompts import EXPLORER_LABELER_SYSTEM
from .coder import resolve_url


_EMAIL_HINTS = ("email", "mobile", "username", "user name", "user-name", "login id", "phone")
_PASSWORD_HINTS = ("password", "passcode", "secret")
_SUBMIT_HINTS = ("login", "log in", "sign in", "signin", "continue", "next", "submit", "proceed")
_MAX_LOGIN_STEPS = 3


class _LabeledOnly(BaseModel):
    """Slim shape for the labeller LLM call — no container_id (which is
    captured deterministically by the scraper, not asked of the model)."""
    role: str
    name: str
    purpose: str


class _LabeledElements(BaseModel):
    elements: list[_LabeledOnly]


def explore(
    spec: TestSpec, plan: TestPlan, answers: dict[str, str], bus: EventBus,
) -> SiteMap:
    email, password = _detect_credentials(answers)
    urls = _collect_urls(spec, plan)
    bus.emit("explorer", f"Visiting {len(urls)} unique URL(s)…")
    if email and password:
        bus.emit("explorer", "Credentials available — multi-step login probing enabled.")

    pages: dict[str, PageSnapshot] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            login_succeeded = False
            for original, absolute in urls.items():
                try:
                    snapshot, logged_in_here, discovered = _explore_url(
                        page, original, absolute, email, password, bus,
                    )
                    pages[original] = snapshot
                    # Merge any discovered URLs (from redirects) into pages
                    for disc_url, disc_snap in discovered.items():
                        pages[disc_url] = disc_snap
                    login_succeeded = login_succeeded or logged_in_here
                except Exception as exc:
                    bus.emit(
                        "explorer",
                        f"Failed to snapshot {absolute}: {exc}",
                        level="error",
                    )

            # Re-snapshot EVERY visited URL after successful login. Pages can look
            # very different post-auth even when the initial scrape had some elements
            # — e.g. data tables that are empty until the user is authenticated, or
            # nav items that appear only when logged in. The first-pass snapshot can't
            # see any of that, so we redo the whole sweep with the now-authenticated
            # browser context.
            if login_succeeded:
                for original in list(pages.keys()):
                    # `pages` may contain URLs that were discovered post-login (e.g. the
                    # redirect destination), which aren't in the plan's `urls` map.
                    # For those we already have the absolute form == the key.
                    absolute = urls.get(original, original)
                    bus.emit(
                        "explorer",
                        f"Re-snapshotting {absolute} now that we're logged in…",
                    )
                    try:
                        new_snap, _, discovered = _explore_url(
                            page, original, absolute, None, None, bus,
                        )
                        # Replace only if the new snapshot is at least as rich.
                        if len(new_snap.elements) >= len(pages[original].elements):
                            pages[original] = new_snap
                        for disc_url, disc_snap in discovered.items():
                            pages[disc_url] = disc_snap
                    except Exception as exc:
                        bus.emit(
                            "explorer",
                            f"Re-snapshot failed for {absolute}: {exc}",
                            level="warn",
                        )

            context.close()
        finally:
            browser.close()

    return SiteMap(pages=pages)


def _explore_url(
    page: Page,
    key: str,
    absolute: str,
    email: str | None,
    password: str | None,
    bus: EventBus,
) -> tuple[PageSnapshot, bool, dict[str, PageSnapshot]]:
    """Navigate, snapshot, and (if credentials given) interactively probe a login flow.

    Returns (initial_snapshot, logged_in, discovered_snapshots).
    discovered_snapshots maps discovered URLs (from redirects) to their PageSnapshot.
    """
    bus.emit("explorer", f"→ {absolute}")
    page.goto(absolute, wait_until="domcontentloaded", timeout=30_000)
    _quiet_wait(page)

    raw_elements = _scrape_elements(page)
    # SPAs often render async — if first scrape is empty, give the page another
    # moment and retry up to twice before giving up.
    for attempt in range(2):
        if raw_elements:
            break
        bus.emit(
            "explorer",
            f"   empty scrape on attempt {attempt+1} — waiting and retrying…",
            level="warn",
        )
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
        _quiet_wait(page)
        raw_elements = _scrape_elements(page)
    bus.emit("explorer", f"   found {len(raw_elements)} interactable element(s)")

    logged_in = False
    discovered: dict[str, PageSnapshot] = {}
    if email and password and _looks_like_login(raw_elements):
        bus.emit("explorer", "   probing multi-step login…")
        starting_url = page.url
        raw_elements = _probe_login_states(
            page, raw_elements, email, password, bus,
        )
        if page.url != starting_url:
            logged_in = True
            final_url = page.url
            bus.emit("explorer", f"   login appears to have succeeded → {final_url}", level="success")
            # Capture the post-login state as a discovered URL
            final_elements = _scrape_elements(page)
            labeled_final = _label_elements(final_elements) if final_elements else []
            summary_final = _accessibility_summary(page)
            discovered[final_url] = PageSnapshot(
                url=final_url,
                title=page.title(),
                elements=labeled_final,
                raw_accessibility_summary=summary_final,
            )

    labeled = _label_elements(raw_elements) if raw_elements else []
    summary = _accessibility_summary(page)
    return (
        PageSnapshot(
            url=absolute,
            title=page.title(),
            elements=labeled,
            raw_accessibility_summary=summary,
        ),
        logged_in,
        discovered,
    )


def _probe_login_states(
    page: Page,
    initial: list[dict[str, str]],
    email: str,
    password: str,
    bus: EventBus,
) -> list[dict[str, str]]:
    """Walk through up to N login states, accumulating every element we see."""
    def _key(el: dict[str, str]) -> tuple[str, str, str]:
        return (el["role"], el["name"], el["container_id"])

    union: list[dict[str, str]] = list(initial)
    seen: set[tuple[str, str, str]] = {_key(e) for e in union}
    filled = {"email": False, "password": False}

    for step in range(_MAX_LOGIN_STEPS):
        current = _scrape_elements(page)
        for el in current:
            if _key(el) not in seen:
                seen.add(_key(el))
                union.append(el)

        email_field = _find_field(current, _EMAIL_HINTS, role_prefer="textbox")
        password_field = _find_field(current, _PASSWORD_HINTS, role_prefer="textbox")
        submit_button = _find_field(current, _SUBMIT_HINTS, role_prefer="button")

        did_something = False
        if email_field and not filled["email"]:
            if _safe_fill(page, email_field, email):
                bus.emit("explorer", f"     filled email into role={email_field['role']} name={email_field['name']!r}")
                filled["email"] = True
                did_something = True
        if password_field and not filled["password"]:
            if _safe_fill(page, password_field, password):
                bus.emit("explorer", f"     filled password into role={password_field['role']} name={password_field['name']!r}")
                filled["password"] = True
                did_something = True

        if not submit_button:
            break
        if _safe_click(page, submit_button):
            bus.emit("explorer", f"     clicked role={submit_button['role']} name={submit_button['name']!r}")
            did_something = True
        else:
            break

        _quiet_wait(page)
        if not did_something:
            break

        # Detect "logged in" — if no email/password fields visible AND URL changed.
        post = _scrape_elements(page)
        for el in post:
            if _key(el) not in seen:
                seen.add(_key(el))
                union.append(el)
        if not _looks_like_login(post):
            break

    return union


def _safe_fill(page: Page, el: dict[str, str], value: str) -> bool:
    try:
        loc = page.get_by_role(el["role"], name=el["name"])  # type: ignore[arg-type]
        loc.first.fill(value, timeout=5_000)
        return True
    except Exception:
        return False


def _safe_click(page: Page, el: dict[str, str]) -> bool:
    try:
        loc = page.get_by_role(el["role"], name=el["name"])  # type: ignore[arg-type]
        loc.first.click(timeout=5_000)
        return True
    except Exception:
        return False


def _find_field(
    elements: list[dict[str, str]],
    hints: tuple[str, ...],
    *,
    role_prefer: str,
) -> dict[str, str] | None:
    """Best match for any of the given keyword hints."""
    for el in elements:
        if el["role"] != role_prefer:
            continue
        nl = el["name"].lower()
        for h in hints:
            if h in nl:
                return el
    # Fallback: any role
    for el in elements:
        nl = el["name"].lower()
        for h in hints:
            if h in nl:
                return el
    return None


def _looks_like_login(elements: list[dict[str, str]]) -> bool:
    has_field = any(
        el["role"] == "textbox"
        and any(h in el["name"].lower() for h in _EMAIL_HINTS + _PASSWORD_HINTS)
        for el in elements
    )
    has_button = any(
        el["role"] == "button"
        and any(h in el["name"].lower() for h in _SUBMIT_HINTS)
        for el in elements
    )
    return has_field and has_button


def _detect_credentials(answers: dict[str, str]) -> tuple[str | None, str | None]:
    email = password = None
    for k, v in answers.items():
        if not v:
            continue
        kl = k.lower()
        if password is None and "password" in kl:
            password = v
        elif email is None and any(h in kl for h in ("email", "mobile", "username", "user")):
            email = v
    return email, password


def _quiet_wait(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass


def _collect_urls(spec: TestSpec, plan: TestPlan) -> dict[str, str]:
    """Map each URL as written in the test plan to its absolute form."""
    base = spec.app_url
    seen: dict[str, str] = {}
    for tc in plan.test_cases:
        urls = tc.page_urls or [base]
        for u in urls:
            seen.setdefault(u, resolve_url(base, u))
    if not seen:
        seen[base] = base
    return seen


def _scrape_elements(page: Page) -> list[dict[str, str]]:
    """Pull every interactable element with role, accessible name, and the ID
    of its closest <form> ancestor (or nearest [role=dialog] / <section> with
    an id). Deduplication is scoped per container, so SPAs that keep multiple
    forms in the same DOM (toggling visibility) are represented correctly.
    """
    js = r"""
    () => {
      const out = [];
      const seen = new Set();
      const roleAttr = (el) => el.getAttribute('role');
      const STATUS_CLS_RE = /\b(message|alert|toast|notification|snackbar|flash|banner)\b/i;
      const implicit = (el) => {
        // Mapping follows the W3C HTML-ARIA spec
        // (https://www.w3.org/TR/html-aria/) — Playwright's locators query the
        // real accessibility tree, so we must match what it sees.
        const t = el.tagName.toLowerCase();
        if (t === 'a' && el.href) return 'link';
        if (t === 'button') return 'button';
        if (t === 'input') {
          const type = (el.type || 'text').toLowerCase();
          if (['text','email','tel','url','password'].includes(type)) return 'textbox';
          if (type === 'number') return 'spinbutton';
          if (type === 'search') return 'searchbox';
          if (type === 'checkbox') return 'checkbox';
          if (type === 'radio') return 'radio';
          if (type === 'range') return 'slider';
          if (['submit','button','reset','image'].includes(type)) return 'button';
          // date/time/color/file etc. have no clean ARIA role — skip them.
        }
        if (t === 'textarea') return 'textbox';
        if (t === 'select') return 'combobox';
        if (/^h[1-6]$/.test(t)) return 'heading';
        // Status/message text containers — common pattern across UI kits.
        // These usually lack a real ARIA role in the live DOM, so tests should
        // assert on them via get_by_text(...). The scraper marks them with
        // 'text' so the Coder picks the right Playwright locator.
        if (el.getAttribute('aria-live')) return 'text';
        const cls = String(el.className || '');
        if (STATUS_CLS_RE.test(cls)) return 'text';
        return null;
      };
      const LABELABLE = new Set(['input', 'select', 'textarea']);
      const name = (el) => {
        const aria = el.getAttribute('aria-label');
        if (aria) return aria.trim();
        const labelledby = el.getAttribute('aria-labelledby');
        if (labelledby) {
          const ref = document.getElementById(labelledby);
          if (ref) return ref.textContent.trim();
        }
        // <label for="..."> applies to any labelable form control, not just <input>.
        if (LABELABLE.has(el.tagName.toLowerCase()) && el.id) {
          const lbl = document.querySelector(`label[for="${el.id}"]`);
          if (lbl) return lbl.textContent.trim();
        }
        if (el.placeholder) return el.placeholder.trim();
        const text = (el.innerText || el.textContent || '').trim();
        return text.length > 0 && text.length < 100 ? text : '';
      };
      const containerId = (el) => {
        // Closest <form> wins. Falls back to a [role=dialog] or <section>/<aside>
        // with an id, then '' if nothing useful is found.
        let p = el.parentElement;
        while (p) {
          const t = p.tagName.toLowerCase();
          if (t === 'form' && p.id) return p.id;
          if (p.getAttribute && p.getAttribute('role') === 'dialog' && p.id) return p.id;
          if (['section', 'aside', 'dialog'].includes(t) && p.id) return p.id;
          p = p.parentElement;
        }
        return '';
      };
      document.querySelectorAll(
        'button, a, input, textarea, select, h1, h2, h3, [role], [aria-live], ' +
        '[class*="message" i], [class*="alert" i], [class*="toast" i], ' +
        '[class*="notification" i], [class*="snackbar" i], [class*="flash" i]'
      ).forEach((el) => {
        const role = roleAttr(el) || implicit(el);
        if (!role) return;
        const n = name(el);
        if (!n) return;
        const cid = containerId(el);
        const key = role + '::' + n + '::' + cid;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({role: role, name: n, container_id: cid});
      });

      // Tables: emit column headers and each body row keyed by its first cell.
      // Playwright tests query rows via `get_by_role("row").filter(has_text=...)`
      // and then `.get_by_role("cell").nth(N)` — so we expose enough structure for
      // the Coder to know the table exists and which columns it has.
      document.querySelectorAll('table').forEach((table) => {
        const tableId = table.id || (table.closest('[id]') ? table.closest('[id]').id : '');
        // Column headers from <thead><tr><th>...</th></tr></thead>
        table.querySelectorAll('thead th, tr th').forEach((th) => {
          const headerName = (th.innerText || th.textContent || '').trim();
          if (!headerName) return;
          const key = 'columnheader::' + headerName + '::' + tableId;
          if (seen.has(key)) return;
          seen.add(key);
          out.push({role: 'columnheader', name: headerName, container_id: tableId});
        });
        // Body rows + their cells. Row name = first cell's text (the row's natural
        // identifier, e.g. "Pencil"). Cell container_id = "<tableId>:<rowKey>" so
        // each cell is grouped under its specific row.
        const bodyRows = table.querySelectorAll('tbody tr');
        const targetRows = bodyRows.length > 0 ? bodyRows : table.querySelectorAll('tr');
        targetRows.forEach((tr) => {
          const firstCell = tr.querySelector('td, th');
          if (!firstCell) return;
          const rowKey = (firstCell.innerText || firstCell.textContent || '').trim();
          if (!rowKey || rowKey.length > 100) return;
          const rowKeyClean = rowKey;
          const rowKeyId = 'row::' + rowKeyClean + '::' + tableId;
          if (!seen.has(rowKeyId)) {
            seen.add(rowKeyId);
            out.push({role: 'row', name: rowKeyClean, container_id: tableId});
          }
          const cellContainer = tableId + ':' + rowKeyClean;
          tr.querySelectorAll('td').forEach((td) => {
            const cellText = (td.innerText || td.textContent || '').trim();
            if (!cellText || cellText.length > 100) return;
            const ck = 'cell::' + cellText + '::' + cellContainer;
            if (seen.has(ck)) return;
            seen.add(ck);
            out.push({role: 'cell', name: cellText, container_id: cellContainer});
          });
        });
      });
      return out;
    }
    """
    items = page.evaluate(js)
    return [
        {"role": it["role"], "name": it["name"], "container_id": it.get("container_id", "")}
        for it in items
        if isinstance(it, dict) and isinstance(it.get("role"), str) and isinstance(it.get("name"), str)
    ]


def _label_elements(raw: list[dict[str, str]]) -> list[PageElement]:
    # Self-explanatory structural elements (table rows, column headers, list items)
    # don't need an LLM-written "purpose" — passing them through the labeller is
    # wasteful and risky (the labeller has been seen to silently drop them).
    # We emit those directly with a deterministic purpose string.
    PASSTHROUGH_ROLES = {"row", "columnheader", "rowheader", "cell", "listitem"}
    PASSTHROUGH_PURPOSES = {
        "row": "table row",
        "columnheader": "table column header",
        "rowheader": "table row header",
        "cell": "table cell",
        "listitem": "list item",
    }

    # Split: structural elements bypass the labeller; everything else goes through.
    labellable_raw: list[dict[str, str]] = []
    structural_raw: list[dict[str, str]] = []
    for el in raw:
        if el["role"] in PASSTHROUGH_ROLES:
            structural_raw.append(el)
        else:
            labellable_raw.append(el)

    # Label each unique (role, name) pair once; re-attach container_id afterwards
    # so a "Price" field in two different forms produces two PageElements with
    # the same purpose label but distinct container ids.
    purpose_map: dict[tuple[str, str], str] = {}
    if labellable_raw:
        unique_pairs: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for el in labellable_raw:
            k = (el["role"], el["name"])
            if k not in seen_pairs:
                seen_pairs.add(k)
                unique_pairs.append(k)
        payload = "\n".join(f"- role={r!r}, name={n!r}" for r, n in unique_pairs)
        labeled = structured(EXPLORER_LABELER_SYSTEM, payload, _LabeledElements)
        purpose_map = {(le.role, le.name): le.purpose for le in labeled.elements}

    out: list[PageElement] = []
    emitted: set[tuple[str, str, str]] = set()
    # Emit structural rows/headers/cells/listitems first — never filter these out.
    for el in structural_raw:
        k = (el["role"], el["name"], el["container_id"])
        if k in emitted:
            continue
        emitted.add(k)
        out.append(
            PageElement(
                role=el["role"],
                name=el["name"],
                purpose=PASSTHROUGH_PURPOSES.get(el["role"], "structural"),
                container_id=el["container_id"],
            )
        )
    # Then emit everything else, honouring the labeller's purposes.
    for el in labellable_raw:
        k = (el["role"], el["name"], el["container_id"])
        if k in emitted:
            continue
        emitted.add(k)
        purpose = purpose_map.get((el["role"], el["name"]), "")
        if not purpose:
            continue  # labeller dropped this one; honour that
        out.append(
            PageElement(
                role=el["role"],
                name=el["name"],
                purpose=purpose,
                container_id=el["container_id"],
            )
        )
    return out


def _accessibility_summary(page: Page, max_chars: int = 4000) -> str:
    try:
        snapshot = page.accessibility.snapshot()
    except Exception:
        return ""
    text = _flatten_accessibility(snapshot)
    return text[:max_chars]


def _flatten_accessibility(node: dict | None, depth: int = 0) -> str:
    if not node:
        return ""
    lines = []
    role = node.get("role", "")
    name = node.get("name", "")
    if role and (name or role == "heading"):
        lines.append(f"{'  ' * depth}{role}: {name}")
    for child in node.get("children", []) or []:
        lines.append(_flatten_accessibility(child, depth + 1))
    return "\n".join(line for line in lines if line)


# Kept as a public helper for the Healer.
def _snapshot_page(page: Page, key: str, absolute: str, bus: EventBus) -> PageSnapshot:
    snap, _, _ = _explore_url(page, key, absolute, None, None, bus)
    return snap
