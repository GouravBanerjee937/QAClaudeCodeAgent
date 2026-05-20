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


class _LabeledElements(BaseModel):
    elements: list[PageElement]


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

            # Re-snapshot URLs that were initially empty if we've since logged in —
            # they may have been auth-gated.
            if login_succeeded:
                for original, snap in list(pages.items()):
                    if snap.elements:
                        continue
                    absolute = urls[original]
                    bus.emit(
                        "explorer",
                        f"Re-snapshotting {absolute} now that we're logged in…",
                    )
                    try:
                        new_snap, _, discovered = _explore_url(
                            page, original, absolute, None, None, bus,
                        )
                        if new_snap.elements:
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
    initial: list[tuple[str, str]],
    email: str,
    password: str,
    bus: EventBus,
) -> list[tuple[str, str]]:
    """Walk through up to N login states, accumulating every (role, name) we see."""
    union: list[tuple[str, str]] = list(initial)
    seen = set(union)
    filled = {"email": False, "password": False}

    for step in range(_MAX_LOGIN_STEPS):
        current = _scrape_elements(page)
        for pair in current:
            if pair not in seen:
                seen.add(pair)
                union.append(pair)

        email_field = _find_field(current, _EMAIL_HINTS, role_prefer="textbox")
        password_field = _find_field(current, _PASSWORD_HINTS, role_prefer="textbox")
        submit_button = _find_field(current, _SUBMIT_HINTS, role_prefer="button")

        did_something = False
        if email_field and not filled["email"]:
            if _safe_fill(page, *email_field, email):
                bus.emit("explorer", f"     filled email into role={email_field[0]} name={email_field[1]!r}")
                filled["email"] = True
                did_something = True
        if password_field and not filled["password"]:
            if _safe_fill(page, *password_field, password):
                bus.emit("explorer", f"     filled password into role={password_field[0]} name={password_field[1]!r}")
                filled["password"] = True
                did_something = True

        if not submit_button:
            break
        if _safe_click(page, *submit_button):
            bus.emit("explorer", f"     clicked role={submit_button[0]} name={submit_button[1]!r}")
            did_something = True
        else:
            break

        _quiet_wait(page)
        if not did_something:
            break

        # Detect "logged in" — if no email/password fields visible AND URL changed.
        post = _scrape_elements(page)
        for pair in post:
            if pair not in seen:
                seen.add(pair)
                union.append(pair)
        if not _looks_like_login(post):
            break

    return union


def _safe_fill(page: Page, role: str, name: str, value: str) -> bool:
    try:
        loc = page.get_by_role(role, name=name)  # type: ignore[arg-type]
        loc.first.fill(value, timeout=5_000)
        return True
    except Exception:
        return False


def _safe_click(page: Page, role: str, name: str) -> bool:
    try:
        loc = page.get_by_role(role, name=name)  # type: ignore[arg-type]
        loc.first.click(timeout=5_000)
        return True
    except Exception:
        return False


def _find_field(
    elements: list[tuple[str, str]],
    hints: tuple[str, ...],
    *,
    role_prefer: str,
) -> tuple[str, str] | None:
    """Best (role, name) match for any of the given keyword hints."""
    candidates = [(r, n) for r, n in elements if r == role_prefer]
    for r, n in candidates:
        nl = n.lower()
        for h in hints:
            if h in nl:
                return (r, n)
    # Fallback: any role
    for r, n in elements:
        nl = n.lower()
        for h in hints:
            if h in nl:
                return (r, n)
    return None


def _looks_like_login(elements: list[tuple[str, str]]) -> bool:
    has_field = any(
        r == "textbox" and any(h in n.lower() for h in _EMAIL_HINTS + _PASSWORD_HINTS)
        for r, n in elements
    )
    has_button = any(
        r == "button" and any(h in n.lower() for h in _SUBMIT_HINTS)
        for r, n in elements
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


def _scrape_elements(page: Page) -> list[tuple[str, str]]:
    """Pull (role, accessible-name) pairs deterministically via the DOM."""
    js = """
    () => {
      const out = [];
      const seen = new Set();
      const roleAttr = (el) => el.getAttribute('role');
      const implicit = (el) => {
        const t = el.tagName.toLowerCase();
        if (t === 'a' && el.href) return 'link';
        if (t === 'button') return 'button';
        if (t === 'input') {
          const type = (el.type || 'text').toLowerCase();
          if (['text','email','password','search','tel','url','number'].includes(type)) return 'textbox';
          if (type === 'checkbox') return 'checkbox';
          if (type === 'radio') return 'radio';
          if (type === 'submit' || type === 'button') return 'button';
        }
        if (t === 'textarea') return 'textbox';
        if (t === 'select') return 'combobox';
        if (/^h[1-6]$/.test(t)) return 'heading';
        return null;
      };
      const name = (el) => {
        const aria = el.getAttribute('aria-label');
        if (aria) return aria.trim();
        const labelledby = el.getAttribute('aria-labelledby');
        if (labelledby) {
          const ref = document.getElementById(labelledby);
          if (ref) return ref.textContent.trim();
        }
        if (el.tagName.toLowerCase() === 'input' && el.id) {
          const lbl = document.querySelector(`label[for="${el.id}"]`);
          if (lbl) return lbl.textContent.trim();
        }
        if (el.placeholder) return el.placeholder.trim();
        const text = (el.innerText || el.textContent || '').trim();
        return text.length > 0 && text.length < 100 ? text : '';
      };
      document.querySelectorAll('button, a, input, textarea, select, h1, h2, h3, [role]').forEach((el) => {
        const role = roleAttr(el) || implicit(el);
        if (!role) return;
        const n = name(el);
        if (!n) return;
        const key = role + '::' + n;
        if (seen.has(key)) return;
        seen.add(key);
        out.push([role, n]);
      });
      return out;
    }
    """
    pairs = page.evaluate(js)
    return [(r, n) for r, n in pairs if isinstance(r, str) and isinstance(n, str)]


def _label_elements(raw: list[tuple[str, str]]) -> list[PageElement]:
    payload = "\n".join(f"- role={r!r}, name={n!r}" for r, n in raw)
    labeled = structured(EXPLORER_LABELER_SYSTEM, payload, _LabeledElements)
    raw_keys = {(r, n) for r, n in raw}
    return [e for e in labeled.elements if (e.role, e.name) in raw_keys]


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
