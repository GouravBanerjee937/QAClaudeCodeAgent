"""Step 4: TestCase + SiteMap → generated pytest-playwright code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

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
) -> list[GeneratedTest]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir(output_dir)
    source_hints = render_hints_for_prompt(source_insights) if source_insights else ""
    generated: list[GeneratedTest] = []
    for tc in plan.test_cases:
        bus.emit("coder", f"Writing test for '{tc.id}'…")
        prompt = _build_prompt(spec, tc, sitemap, answers)
        if source_hints:
            prompt = prompt + "\n\n" + source_hints
        raw = text(CODER_SYSTEM, prompt, temperature=0.1)
        source = _post_process(_strip_fences(raw))
        path = output_dir / f"test_{_sanitize(tc.id)}.py"
        path.write_text(source, encoding="utf-8")
        generated.append(GeneratedTest(test_case_id=tc.id, file_path=str(path), code=source))
        bus.emit("coder", f"  → wrote {path.name}", level="success")
    return generated


# gpt-5 keeps writing `get_by_role("text", name="X", ...)` for status messages
# even though "text" isn't a real ARIA role and Playwright will return nothing.
# The right call is `get_by_text("X", exact=True)`. We auto-fix this here so the
# generated code actually runs.
_BAD_TEXT_ROLE_RE = re.compile(
    r"""get_by_role\(\s*["']text["']\s*,\s*name\s*=\s*(["'][^"']+["'])\s*(?:,\s*exact\s*=\s*True\s*)?\)""",
    re.VERBOSE,
)


def _post_process(source: str) -> str:
    """Apply deterministic fixes to LLM-generated code before validation."""
    return _BAD_TEXT_ROLE_RE.sub(lambda m: f"get_by_text({m.group(1)}, exact=True)", source)


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
    spec: TestSpec, tc: TestCase, sitemap: SiteMap, answers: dict[str, str]
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
        "# SiteMap — allowed elements (use these EXACT role+name strings)",
    ]
    for snap in snapshots:
        parts.append(f"\n## Page: {snap.url}  (title: {snap.title!r})")
        if not snap.elements:
            parts.append("(no labeled elements captured — likely auth-gated; use # NEEDS: markers)")
        for el in snap.elements:
            parts.append(f'- role="{el.role}", name="{el.name}"  — {el.purpose}')
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
