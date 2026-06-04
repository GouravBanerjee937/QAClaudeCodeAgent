"""Step 4: TestCase + SiteMap → generated pytest-playwright code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from ..datasets import apply_datasets
from ..events import EventBus
from ..llm import text
from ..models import GeneratedTest, PageSnapshot, SiteMap, TestCase, TestPlan, TestSpec
from ..prompts import CODER_SYSTEM
from ..source import SourceInsights, render_hints_for_prompt


def code(
    spec: TestSpec,
    plan: TestPlan,
    sitemap: SiteMap,
    answers: dict[str, str],
    output_dir: Path,
    bus: EventBus,
    *,
    source_insights: SourceInsights | None = None,
    datasets: dict[str, list[str]] | None = None,
) -> list[GeneratedTest]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir(output_dir)
    source_hints = render_hints_for_prompt(source_insights) if source_insights else ""
    generated: list[GeneratedTest] = []
    for tc in plan.test_cases:
        bus.emit("coder", f"Writing test for '{tc.id}'…")
        prompt = _build_prompt(spec, tc, sitemap, answers, datasets)
        if source_hints:
            prompt = prompt + "\n\n" + source_hints
        raw = text(CODER_SYSTEM, prompt, temperature=0.1)
        source = _post_process(_strip_fences(raw))
        source = _resolve_select_options(
            source, sitemap, answers,
            on_fix=lambda msg: bus.emit("coder", msg, level="warn"),
        )
        source = apply_datasets(
            source, datasets,
            on_warn=lambda msg: bus.emit("coder", msg, level="warn"),
        )
        path = output_dir / f"test_{_sanitize(tc.id)}.py"
        path.write_text(source, encoding="utf-8")
        generated.append(GeneratedTest(test_case_id=tc.id, file_path=str(path), code=source))
        bus.emit("coder", f"  → wrote {path.name}", level="success")
    return generated


# Deterministic role/verb fixes. The Coder prompt has the same rules, but small
# models still slip — these regexes enforce the role-to-verb mapping after the
# fact so a single mistake doesn't cause a runtime failure.
#
# Rule: "text" role doesn't exist; use get_by_text instead.
_BAD_TEXT_ROLE_RE = re.compile(
    r"""get_by_role\(\s*["']text["']\s*,\s*name\s*=\s*(["'][^"']+["'])\s*(?:,\s*exact\s*=\s*True\s*)?\)""",
    re.VERBOSE,
)
# Rule: combobox/<select> uses .select_option, not .fill.
_COMBOBOX_FILL_RE = re.compile(
    r"""(get_by_role\(\s*["']combobox["'][^)]*\))\.fill\(""",
)
# Rule: checkbox/radio uses .check (no args), not .fill(...).
_CHK_RAD_FILL_RE = re.compile(
    r"""(get_by_role\(\s*["'](?:checkbox|radio)["'][^)]*\))\.fill\([^)]*\)""",
)
# Rule: button/link uses .click() (no args), not .fill(...).
_BTN_LNK_FILL_RE = re.compile(
    r"""(get_by_role\(\s*["'](?:button|link)["'][^)]*\))\.fill\([^)]*\)""",
)
# Rule: table rows must be located with .filter(has_text=...) not name=.
# get_by_role("row", name="X") → get_by_role("row").filter(has_text="X")
# because a row's accessible name in the DOM is its full concatenated cell text,
# not just the first cell — so name= never matches at runtime.
_ROW_NAME_RE = re.compile(
    r"""get_by_role\(\s*["']row["']\s*,\s*name\s*=\s*(["'])([^"']+)\1[^)]*\)""",
)
# Rule: comparing a numeric JSON field as a STRING breaks on float vs int
# serialization (e.g. API returns price 10.0, test checks str(price) == "10" →
# "10.0" != "10"). Rewrite `str(<expr>) == "<number>"` to a numeric comparison
# `float(<expr>) == <number>`, which is True for 10.0, 10, and "10" alike.
# Handles ==, !=, and the reversed operand order.
_STR_NUM_CMP_RE = re.compile(
    r"""str\(\s*(?P<expr>.+?)\s*\)\s*(?P<op>==|!=)\s*["'](?P<num>\d+(?:\.\d+)?)["']"""
)
_NUM_STR_CMP_RE = re.compile(
    r"""["'](?P<num>\d+(?:\.\d+)?)["']\s*(?P<op>==|!=)\s*str\(\s*(?P<expr>.+?)\s*\)"""
)


def _post_process(source: str) -> str:
    """Apply deterministic fixes to LLM-generated code before validation.

    Enforces the Playwright role→verb mapping that the Coder/Healer prompts also
    spell out. Belt-and-suspenders: if the model slips, the regex catches it.
    """
    s = _BAD_TEXT_ROLE_RE.sub(lambda m: f"get_by_text({m.group(1)}, exact=True)", source)
    s = _COMBOBOX_FILL_RE.sub(r"\1.select_option(", s)
    s = _CHK_RAD_FILL_RE.sub(r"\1.check()", s)
    s = _BTN_LNK_FILL_RE.sub(r"\1.click()", s)
    s = _ROW_NAME_RE.sub(
        lambda m: f'get_by_role("row").filter(has_text="{m.group(2)}")', s
    )
    s = _STR_NUM_CMP_RE.sub(
        lambda m: f'float({m.group("expr")}) {m.group("op")} {m.group("num")}', s
    )
    s = _NUM_STR_CMP_RE.sub(
        lambda m: f'{m.group("num")} {m.group("op")} float({m.group("expr")})', s
    )
    return s


_OPTION_NAME_RE = re.compile(r'^(?P<text>.*?)\s*\|\s*value="(?P<value>[^"]*)"\s*$')
_SELECT_OPTION_CALL_RE = re.compile(
    r"""\.select_option\(\s*(?:value|label)\s*=\s*["'][^"']*["']\s*\)"""
)


def _parse_option(name: str) -> tuple[str, str]:
    """'Mobile   | value="3"' -> ('Mobile', '3'); plain text -> (text, '')."""
    m = _OPTION_NAME_RE.match(name)
    if m:
        return m.group("text").strip(), m.group("value")
    return name.strip(), ""


def _resolve_select_options(source, sitemap, answers, *, on_fix=None) -> str:
    """Deterministically force `select_option(...)` to the option that matches the
    item the user actually named in the PRD/answers.

    Small models sometimes guess a dropdown value (e.g. value="1"=bottle when the
    PRD said "Mobile"=value 3). The real options live in the SiteMap as
    role='option' entries (name like 'Mobile   | value="3"'). When the page has a
    single <select> and exactly one answer value names one of its options, we
    rewrite every select_option call to that option's value. Conservative: if it's
    ambiguous (0 or >1 matching option, or multiple selects), we leave the code
    untouched rather than risk a wrong "fix".
    """
    # Collect options grouped by their <select> id (the option's container_id).
    opts_by_select: dict[str, list[tuple[str, str]]] = {}
    for snap in sitemap.pages.values():
        for el in snap.elements:
            if el.role != "option":
                continue
            text, value = _parse_option(el.name)
            if not value:
                continue
            bucket = opts_by_select.setdefault(el.container_id, [])
            if (text, value) not in bucket:
                bucket.append((text, value))

    if len(opts_by_select) != 1:
        return source  # zero or multiple selects → don't guess which to touch
    options = next(iter(opts_by_select.values()))

    answer_vals = [str(v).strip() for v in answers.values() if str(v).strip()]

    def _matches(opt_text: str, av: str) -> bool:
        o, a = opt_text.lower(), av.lower()
        return o == a or o.startswith(a + " ") or o.startswith(a + "(")

    intended = []
    for text, value in options:
        if any(_matches(text, av) for av in answer_vals):
            intended.append((text, value))
    # de-dup by value
    uniq = list(dict.fromkeys(v for _, v in intended))
    if len(uniq) != 1:
        return source  # can't determine a single intended item → leave as-is

    correct_value = uniq[0]
    correct_text = next(t for t, v in intended if v == correct_value)

    def _sub(m):
        return f'.select_option(value="{correct_value}")'

    new_source, n = _SELECT_OPTION_CALL_RE.subn(_sub, source)
    if n and new_source != source and on_fix:
        on_fix(
            f"  → forced dropdown selection to '{correct_text}' (value=\"{correct_value}\") "
            f"from the SiteMap options (overrode a guessed value)."
        )
    return new_source


def resolve_url(app_url: str, path: str) -> str:
    """Join app_url + path the way browsers do, even when app_url has its own path."""
    if path.startswith(("http://", "https://", "file://")):
        return path
    parsed = urlparse(app_url)
    if parsed.scheme == "file" or not parsed.netloc:
        # file:// SPAs only support hash routing — preserve the app's file path.
        base_no_frag = urlunparse(parsed._replace(fragment=""))
        fragment = path if path.startswith("#") else "#/" + path.lstrip("/")
        return base_no_frag + fragment
    # Build a clean base = scheme + host (no path) so urljoin handles "/foo" right
    # whether app_url is "https://x.com" or "https://x.com/login".
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    if path.startswith("/"):
        return urljoin(origin + "/", path.lstrip("/"))
    return urljoin(app_url if app_url.endswith("/") else app_url + "/", path)


def _build_prompt(
    spec: TestSpec,
    tc: TestCase,
    sitemap: SiteMap,
    answers: dict[str, str],
    datasets: dict[str, list[str]] | None = None,
) -> str:
    snapshots = _relevant_pages(tc, sitemap)
    resolved = {u: resolve_url(spec.app_url, u) for u in tc.page_urls}
    parts = [
        f"# app_url\n{spec.app_url}",
        "",
        f"# resolved_urls (USE THESE VALUES VERBATIM in page.goto — do NOT modify):\n"
        f"{json.dumps(resolved, indent=2)}",
        "",
        f"# TestCase\n{tc.model_dump_json(indent=2)}",
        "",
        f"# answers (substitute {{key}} references in steps with these)\n"
        f"{json.dumps(answers, indent=2)}",
        "",
    ]
    if datasets:
        parts.extend([
            "# dataset_variables — wherever one of these values is needed, write the "
            "token EXACTLY as {{name}} (double curly braces) inside a normal string "
            "literal, e.g. .fill(\"{{username}}\"). Do NOT substitute a real value or "
            "use single braces; parametrization fills them in afterwards.\n"
            f"{json.dumps(sorted(datasets.keys()), indent=2)}",
            "",
        ])
    parts.append("# SiteMap — allowed elements (use these EXACT role+name strings)")
    parts.append(
        "Each element is tagged VISIBLE or HIDDEN as scraped on that page. A HIDDEN "
        "element exists in the DOM but is not rendered — you CANNOT interact with it "
        "until it is revealed. To use a HIDDEN form field you MUST first either "
        "navigate directly to that field's own page URL (prefer page.goto with the "
        "resolved/source route, e.g. .../#/invoices/new), or click the control that "
        "opens it (e.g. a 'Create Invoice' button). Do NOT fill/click a HIDDEN element "
        "directly — it will time out."
    )
    for snap in snapshots:
        parts.append(f"\n## Page: {snap.url}  (title: {snap.title!r})")
        if not snap.elements:
            parts.append("(no labeled elements captured — likely auth-gated; use # NEEDS: markers)")
        for el in snap.elements:
            vis = "VISIBLE" if getattr(el, "visible", True) else "HIDDEN"
            cid = f' container_id="{el.container_id}"' if el.container_id else ""
            parts.append(f'- [{vis}] role="{el.role}", name="{el.name}"{cid}  — {el.purpose}')
    return "\n".join(parts)


def _relevant_pages(tc: TestCase, sitemap: SiteMap) -> list[PageSnapshot]:
    """Return the entire SiteMap to the Coder.

    Previously this tried to filter to only the URLs listed in `tc.page_urls`,
    but SPA tests routinely touch elements from pages the test doesn't
    explicitly visit — e.g. a nav link in the header, a status message that
    appears on a different route, or a table row that exists on a page the
    test merely passes through. Filtering at this layer was hiding that
    content from the Coder and producing NEEDS markers for things that DID
    exist in the SiteMap. The Coder's prompt instructs it to use only what
    appears in the SiteMap, so handing it the full picture is safe.
    """
    return list(sitemap.pages.values())


_FENCE_RE = re.compile(r"^```(?:python)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(s: str) -> str:
    return _FENCE_RE.sub("", s).strip() + "\n"


def _sanitize(slug: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", slug.lower())


def _clear_dir(directory: Path) -> None:
    for item in directory.glob("test_*.py"):
        item.unlink()
