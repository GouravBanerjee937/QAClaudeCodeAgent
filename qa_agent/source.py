"""Optional source-code enrichment for the QA pipeline.

When the user supplies a public GitHub URL, this module shallow-clones the repo
into a temp dir and extracts:

  * Stable HTML element IDs and their associated label text (so the Coder has a
    deterministic locator fallback when role-based locators get ambiguous).
  * SPA route paths (so the Explorer knows the URL space upfront instead of
    having to discover routes by clicking).
  * Backend API endpoints (so the Designer can emit API-level assertions for
    business logic — e.g. "after creating an invoice, GET /api/items should
    show qty decreased" — which run ~100× faster than UI flows).

This is purely additive. Nothing in the existing pipeline calls into this
module unless `enhance_with_source=True` is set, so the legacy behaviour is
preserved bit-for-bit when the feature is off.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ──────────────────────────── data shapes ────────────────────────────────


@dataclass
class StableId:
    """One HTML id with a human-readable description if we can derive it."""

    id: str
    tag: str = ""
    label: str = ""  # text from <label for="..."> or nearby heading, if any
    file: str = ""

    def describe(self) -> str:
        parts = [f"#{self.id}"]
        if self.tag:
            parts.append(f"<{self.tag}>")
        if self.label:
            parts.append(f"label={self.label!r}")
        if self.file:
            parts.append(f"in {self.file}")
        return " ".join(parts)


@dataclass
class ApiEndpoint:
    method: str  # GET, POST, PUT, DELETE, …
    path: str    # /api/items
    file: str = ""

    def describe(self) -> str:
        loc = f"  (in {self.file})" if self.file else ""
        return f"{self.method} {self.path}{loc}"


@dataclass
class SourceInsights:
    """Everything the source-reader could extract from a repo."""

    repo_url: str = ""
    stable_ids: list[StableId] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    api_endpoints: list[ApiEndpoint] = field(default_factory=list)
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.stable_ids or self.routes or self.api_endpoints)

    def summary(self) -> str:
        lines = [f"Source insights from {self.repo_url}:"]
        lines.append(f"  - {len(self.stable_ids)} stable HTML id(s)")
        lines.append(f"  - {len(self.routes)} SPA route(s)")
        lines.append(f"  - {len(self.api_endpoints)} API endpoint(s)")
        if self.notes:
            lines.append(f"  - notes: {self.notes}")
        return "\n".join(lines)


# ──────────────────────────── public API ─────────────────────────────────


def fetch_source(repo_url: str, *, timeout_s: int = 60) -> SourceInsights:
    """Shallow-clone the repo, scan it, return SourceInsights.

    Network or git errors return an empty `SourceInsights` with a note rather
    than raising — the pipeline can continue without source enrichment.
    """
    insights = SourceInsights(repo_url=repo_url)
    if not _looks_like_git_url(repo_url):
        insights.notes = "URL does not look like a git/github repo URL — skipped."
        return insights

    tmp = Path(tempfile.mkdtemp(prefix="qa-agent-source-"))
    try:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(tmp)],
                check=True,
                capture_output=True,
                timeout=timeout_s,
            )
        except subprocess.CalledProcessError as exc:
            insights.notes = (
                "git clone failed: " + (exc.stderr.decode("utf-8", "ignore")[:200] or str(exc))
            )
            return insights
        except subprocess.TimeoutExpired:
            insights.notes = "git clone timed out (repo too large or network slow)"
            return insights

        _scan_repo(tmp, insights)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return insights


# ──────────────────────────── scanners ───────────────────────────────────


def _scan_repo(root: Path, out: SourceInsights) -> None:
    html_files = _walk(root, {".html", ".htm"})
    js_files = _walk(root, {".js", ".jsx", ".ts", ".tsx", ".vue", ".html"})
    py_files = _walk(root, {".py"})
    node_files = _walk(root, {".js", ".ts"})

    seen_ids: set[str] = set()
    for path in html_files:
        text = _safe_read(path)
        rel = str(path.relative_to(root))
        _extract_ids(text, rel, seen_ids, out.stable_ids)

    seen_routes: set[str] = set()
    for path in js_files:
        text = _safe_read(path)
        _extract_routes(text, seen_routes, out.routes)

    seen_eps: set[tuple[str, str]] = set()
    for path in py_files:
        text = _safe_read(path)
        rel = str(path.relative_to(root))
        _extract_python_api(text, rel, seen_eps, out.api_endpoints)
    for path in node_files:
        text = _safe_read(path)
        rel = str(path.relative_to(root))
        _extract_node_api(text, rel, seen_eps, out.api_endpoints)


# ──────────────────────────── helpers ────────────────────────────────────


def _walk(root: Path, exts: set[str]) -> Iterable[Path]:
    # Skip vendor / lock dirs that we never want to read.
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        # Bound file size to avoid choking on bundled assets.
        try:
            if p.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        yield p


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _looks_like_git_url(url: str) -> bool:
    u = url.strip().lower()
    return (
        u.startswith("https://github.com/")
        or u.startswith("https://gitlab.com/")
        or u.startswith("https://bitbucket.org/")
        or u.startswith("git@")
        or u.endswith(".git")
    )


_ID_RE = re.compile(r"""<(\w+)\b[^>]*\bid=["']([^"']+)["'][^>]*>""", re.IGNORECASE)
_LABEL_FOR_RE = re.compile(r"""<label\b[^>]*\bfor=["']([^"']+)["'][^>]*>([^<]{1,80})</label>""", re.IGNORECASE)


def _extract_ids(html: str, file: str, seen: set[str], out: list[StableId]) -> None:
    if not html:
        return
    # Map id → label text (if any <label for="id">…</label>)
    label_map: dict[str, str] = {}
    for m in _LABEL_FOR_RE.finditer(html):
        target = m.group(1)
        label = m.group(2).strip()
        if target and label:
            label_map[target] = label
    for m in _ID_RE.finditer(html):
        tag = m.group(1).lower()
        identifier = m.group(2).strip()
        if not identifier or identifier in seen:
            continue
        # Skip obviously generated IDs (random hashes, very long strings)
        if len(identifier) > 60 or _looks_generated(identifier):
            continue
        seen.add(identifier)
        out.append(
            StableId(
                id=identifier,
                tag=tag,
                label=label_map.get(identifier, ""),
                file=file,
            )
        )


_GENERATED_ID_RE = re.compile(r"[0-9a-f]{8,}|--?\w+?-\w+--")


def _looks_generated(s: str) -> bool:
    return bool(_GENERATED_ID_RE.search(s.lower()))


_HASH_ROUTE_RE = re.compile(r"""['"]#/[A-Za-z0-9_\-/]+['"]""")
_REACT_ROUTE_RE = re.compile(
    r"""<Route\b[^>]*\bpath=["']([^"']+)["']""", re.IGNORECASE
)


def _extract_routes(text: str, seen: set[str], out: list[str]) -> None:
    if not text:
        return
    for m in _HASH_ROUTE_RE.finditer(text):
        raw = m.group(0)
        route = raw.strip("'\"")
        if route and route not in seen:
            seen.add(route)
            out.append(route)
    for m in _REACT_ROUTE_RE.finditer(text):
        path = m.group(1).strip()
        if path and path not in seen and len(path) < 80:
            seen.add(path)
            out.append(path)


# Detect plain stdlib BaseHTTPRequestHandler style: `if path == '/api/items':`
_PLAIN_PATH_RE = re.compile(r"""path\s*==\s*["'](/(?:api|v\d+)/[^"']+)["']""")
# Detect Flask / FastAPI decorators.
_FLASK_RE = re.compile(
    r"""@(?:app|router|blueprint|bp)\.(?P<method>get|post|put|delete|patch|route)\s*\(\s*["'](?P<path>/[^"']+)["']""",
    re.IGNORECASE,
)
# Detect FastAPI / aiohttp method names.
_ROUTE_DECO_RE = re.compile(
    r"""@(?:app|router)\.(?P<method>get|post|put|delete|patch)\s*\(\s*["'](?P<path>/[^"']+)["']""",
    re.IGNORECASE,
)


def _extract_python_api(text: str, file: str, seen: set, out: list[ApiEndpoint]) -> None:
    if not text:
        return
    for m in _PLAIN_PATH_RE.finditer(text):
        path = m.group(1)
        # Method inferred from surrounding do_GET / do_POST function — approximate.
        method = _infer_method_in_context(text, m.start())
        k = (method, path)
        if k not in seen:
            seen.add(k)
            out.append(ApiEndpoint(method=method, path=path, file=file))
    for m in _FLASK_RE.finditer(text):
        method = m.group("method").upper()
        if method == "ROUTE":
            method = "GET"
        path = m.group("path")
        k = (method, path)
        if k not in seen:
            seen.add(k)
            out.append(ApiEndpoint(method=method, path=path, file=file))
    for m in _ROUTE_DECO_RE.finditer(text):
        method = m.group("method").upper()
        path = m.group("path")
        k = (method, path)
        if k not in seen:
            seen.add(k)
            out.append(ApiEndpoint(method=method, path=path, file=file))


_DO_METHOD_RE = re.compile(r"def\s+do_(get|post|put|delete|patch)\b", re.IGNORECASE)


def _infer_method_in_context(text: str, pos: int) -> str:
    """Find the nearest preceding `def do_XXX(` and use its method.

    Defaults to GET if nothing precedes (e.g. file isn't an HTTP handler).
    """
    last_match = None
    for m in _DO_METHOD_RE.finditer(text, 0, pos):
        last_match = m
    if last_match:
        return last_match.group(1).upper()
    return "GET"


# Detect Express-style: app.get('/api/items', handler)
_EXPRESS_RE = re.compile(
    r"""\b(?:app|router)\.(?P<method>get|post|put|delete|patch)\s*\(\s*["'](?P<path>/[^"']+)["']""",
    re.IGNORECASE,
)


def _extract_node_api(text: str, file: str, seen: set, out: list[ApiEndpoint]) -> None:
    if not text:
        return
    for m in _EXPRESS_RE.finditer(text):
        method = m.group("method").upper()
        path = m.group("path")
        k = (method, path)
        if k not in seen:
            seen.add(k)
            out.append(ApiEndpoint(method=method, path=path, file=file))


# ──────────────────────────── prompt rendering ────────────────────────────


def render_hints_for_prompt(insights: SourceInsights, max_ids: int = 60) -> str:
    """Format insights as a compact bullet list to drop into Designer/Coder prompts.

    Returns an empty string if insights are empty so callers can just `+= render(...)`
    without checking.
    """
    if insights.is_empty:
        return ""
    lines = [
        "",
        "# Source-derived hints (from the supplied GitHub repo)",
        "These come from a static read of the source. Prefer ROLE-BASED locators",
        "and the SiteMap as your primary signal; use the items below ONLY as a",
        "fallback when role-based locators are ambiguous or missing.",
        "",
    ]
    if insights.routes:
        lines.append("Known routes (the SPA's full URL space):")
        for r in insights.routes[:30]:
            lines.append(f"  - {r}")
        lines.append("")
    if insights.stable_ids:
        lines.append(
            "Stable HTML IDs (use `page.locator('#<id>')` if a role-based locator "
            "fails or is ambiguous — these IDs are hardcoded in source, so they're "
            "reliable):"
        )
        for sid in insights.stable_ids[:max_ids]:
            lines.append(f"  - {sid.describe()}")
        lines.append("")
    if insights.api_endpoints:
        lines.append(
            "Backend API endpoints (for direct API assertions when the test wants "
            "to verify business-logic side-effects faster than going through the "
            "UI — use Python's `requests` module):"
        )
        for ep in insights.api_endpoints[:30]:
            lines.append(f"  - {ep.describe()}")
        lines.append("")
    if insights.notes:
        lines.append(f"Notes: {insights.notes}")
    return "\n".join(lines)
