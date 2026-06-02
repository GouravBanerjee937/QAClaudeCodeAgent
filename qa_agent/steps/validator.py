"""Step 5: validate generated code — syntax + every locator must exist in the SiteMap."""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from ..events import EventBus
from ..models import GeneratedTest, LocatorCheck, LocatorReport, SiteMap


_LOCATOR_METHODS = {
    "get_by_role", "get_by_text", "get_by_label", "get_by_placeholder", "get_by_title",
    "get_by_alt_text", "get_by_test_id",
}
_ACTION_METHODS = {
    "click", "fill", "check", "uncheck", "select_option", "press", "type", "hover",
    "dblclick", "tap", "set_input_files", "focus", "drag_to",
}
_NEEDS_RE = re.compile(r"#\s*NEEDS:\s*(.+)")


def validate(
    generated: list[GeneratedTest],
    sitemap: SiteMap,
    bus: EventBus,
) -> tuple[list[GeneratedTest], list[LocatorReport]]:
    """Returns (survivors, reports). Files with syntax errors are dropped.
    Tests with blocking issues (locator misses, lambdas in assertions, same-locator-
    filled-twice, or no actions/assertions at all) are rewritten to pytest.fail().
    """
    survivors: list[GeneratedTest] = []
    reports: list[LocatorReport] = []
    for gt in generated:
        try:
            tree = ast.parse(gt.code)
        except SyntaxError as exc:
            bus.emit(
                "validator",
                f"{gt.test_case_id}: syntax error at line {exc.lineno} — dropping",
                level="error",
            )
            continue
        report = _check_locators(gt, tree, sitemap)
        issues = _blocking_issues(gt, tree, report)
        if issues:
            new_code = _force_fail_stub(gt, tree, issues)
            Path(gt.file_path).write_text(new_code, encoding="utf-8")
            gt = GeneratedTest(
                test_case_id=gt.test_case_id, file_path=gt.file_path, code=new_code,
            )
            tree = ast.parse(new_code)
            bus.emit(
                "validator",
                f"{gt.test_case_id}: blocked by {len(issues)} issue(s) — rewritten to fail loudly",
                level="warn",
            )
            for i in issues:
                bus.emit("validator", f"    • {i}")
        survivors.append(gt)
        reports.append(report)
        _emit_report(report, bus)
    return survivors, reports


def _blocking_issues(gt: GeneratedTest, tree: ast.AST, report: LocatorReport) -> list[str]:
    issues: list[str] = []
    if _is_incomplete(tree):
        issues.append("no real actions or assertions in the test body")
    misses = [c for c in report.checks if c.status == "miss"]
    for c in misses:
        suggestion = f" → closest: {c.closest!r}" if c.closest else ""
        issues.append(f"locator NOT in SiteMap: {c.method}({c.args}){suggestion}")
    if _has_lambda(tree):
        issues.append("lambda used in a Playwright assertion — use a string or regex")
    for sig in _repeated_fills(tree):
        issues.append(f"same locator filled twice: {sig}")
    for n in report.placeholders:
        issues.append(f"unresolved NEEDS marker — step skipped: {n}")
    return issues


def _has_lambda(tree: ast.AST) -> bool:
    return any(isinstance(node, ast.Lambda) for node in ast.walk(tree))


def _repeated_fills(tree: ast.AST) -> list[str]:
    """Return locator signatures that are filled more than once consecutively."""
    fills: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fill"):
            sig = _locator_signature(node.func.value)
            if sig:
                fills.append(sig)
    duplicates: list[str] = []
    for i in range(1, len(fills)):
        if fills[i] == fills[i - 1] and fills[i] not in duplicates:
            duplicates.append(fills[i])
    return duplicates


def _locator_signature(node: ast.expr) -> str:
    """Render a `page.get_by_role("textbox", name="X")` chain as a stable string."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return ""
    method = node.func.attr
    if method not in _LOCATOR_METHODS:
        return ""
    parts = []
    for a in node.args:
        if isinstance(a, ast.Constant):
            parts.append(repr(a.value))
    for kw in node.keywords:
        if isinstance(kw.value, ast.Constant):
            parts.append(f"{kw.arg}={kw.value.value!r}")
    return f"{method}({', '.join(parts)})"


def _is_incomplete(tree: ast.AST) -> bool:
    """A test is incomplete if it has no element-action method calls and no expect() calls."""
    actions = 0
    asserts = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _ACTION_METHODS:
            actions += 1
        elif isinstance(func, ast.Name) and func.id == "expect":
            asserts += 1
    return actions == 0 and asserts == 0


def _force_fail_stub(gt: GeneratedTest, tree: ast.AST, issues: list[str] | None = None) -> str:
    """Replace the test body with a pytest.fail() that lists the issues."""
    fn_name = f"test_{gt.test_case_id.replace('-', '_')}"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            fn_name = node.name
            break
    needs = _NEEDS_RE.findall(gt.code)
    bullets: list[str] = []
    if issues:
        bullets.extend(issues)
    for n in needs:
        bullets.append(f"NEEDS: {n}")
    summary = "; ".join(bullets) if bullets else "no real actions or assertions"
    return (
        "from playwright.sync_api import Page\n"
        "import pytest\n\n"
        f"def {fn_name}(page: Page, snap):\n"
        f"    pytest.fail({summary!r})\n"
    )


def _check_locators(gt: GeneratedTest, tree: ast.AST, sitemap: SiteMap) -> LocatorReport:
    sitemap_index = _build_sitemap_index(sitemap)
    checks: list[LocatorCheck] = []
    for call in _walk_locator_calls(tree):
        check = _check_one(call, sitemap_index)
        if check is not None:
            checks.append(check)
    placeholders = _NEEDS_RE.findall(gt.code)
    return LocatorReport(
        test_case_id=gt.test_case_id, checks=checks, placeholders=placeholders,
    )


def _build_sitemap_index(sitemap: SiteMap) -> dict[str, set[str]]:
    """role -> set of (case-insensitive) names. Empty 'text' bucket gets text-y names too."""
    index: dict[str, set[str]] = {}
    text_names: set[str] = set()
    for snap in sitemap.pages.values():
        for el in snap.elements:
            index.setdefault(el.role, set()).add(el.name)
            text_names.add(el.name)
    index["__text__"] = text_names
    return index


def _walk_locator_calls(tree: ast.AST) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in _LOCATOR_METHODS:
            out.append(node)
    return out


def _check_one(call: ast.Call, index: dict[str, set[str]]) -> LocatorCheck | None:
    method = call.func.attr  # type: ignore[attr-defined]
    if method == "get_by_role":
        role = _literal_arg(call, 0)
        name = _literal_kwarg(call, "name")
        args_text = f'role={role!r}, name={name!r}' if name else f'role={role!r}'
        if not role:
            return None
        # get_by_role("cell") with no name is always chained from a row locator
        # (e.g. row.get_by_role("cell").nth(N)) — Playwright finds <td>/<gridcell>
        # natively. Never flag it as a miss; the Coder prompt already documents
        # this as an allowed exception.
        if role == "cell" and name is None:
            return LocatorCheck(method=method, args=args_text, status="match")
        return _match("get_by_role", args_text, role, name, index)
    if method in ("get_by_text", "get_by_label", "get_by_placeholder", "get_by_title", "get_by_alt_text"):
        value = _literal_arg(call, 0)
        if value is None:
            return None
        return _match(method, repr(value), "__text__", value, index)
    return None


def _literal_arg(call: ast.Call, idx: int) -> str | None:
    if len(call.args) <= idx:
        return None
    node = call.args[idx]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _match(
    method: str,
    args_text: str,
    role: str,
    name: str | None,
    index: dict[str, set[str]],
) -> LocatorCheck:
    bucket = index.get(role, set())
    if name is None:
        # role-only locator — always matches if the role exists at all
        status = "match" if bucket else "miss"
        return LocatorCheck(method=method, args=args_text, status=status)

    if name in bucket:
        return LocatorCheck(method=method, args=args_text, status="match")

    # Try case-insensitive + fuzzy
    lower_bucket = {b.lower(): b for b in bucket}
    if name.lower() in lower_bucket:
        return LocatorCheck(
            method=method, args=args_text, status="fuzzy",
            closest=lower_bucket[name.lower()],
        )
    candidates = difflib.get_close_matches(name, list(bucket), n=1, cutoff=0.6)
    if candidates:
        return LocatorCheck(
            method=method, args=args_text, status="miss", closest=candidates[0],
        )
    return LocatorCheck(method=method, args=args_text, status="miss")


def _emit_report(report: LocatorReport, bus: EventBus) -> None:
    if report.misses == 0 and not report.placeholders:
        bus.emit(
            "validator",
            f"{report.test_case_id}: all {len(report.checks)} locator(s) match the SiteMap.",
            level="success",
        )
        return
    if report.misses:
        bus.emit(
            "validator",
            f"{report.test_case_id}: {report.misses} locator(s) NOT in SiteMap — test will likely fail",
            level="error",
        )
        for c in report.checks:
            if c.status == "miss":
                suggestion = f" (closest: {c.closest!r})" if c.closest else ""
                bus.emit("validator", f"    ✗ {c.method}({c.args}){suggestion}")
    if report.placeholders:
        bus.emit(
            "validator",
            f"{report.test_case_id}: {len(report.placeholders)} unresolved NEEDS marker(s)",
            level="warn",
        )
        for n in report.placeholders:
            bus.emit("validator", f"    ? {n}")
