"""Unit tests for deterministic {{var}} → parametrize expansion."""

from __future__ import annotations

import ast

from qa_agent.datasets import apply_datasets, referenced_vars, validate

_BASE = '''from playwright.sync_api import Page, expect


def test_login(page: Page, snap):
    page.goto("http://localhost:3000/#/login")
    page.get_by_role("textbox", name="Username", exact=True).fill("{{username}}")
    page.get_by_role("textbox", name="Password", exact=True).fill("{{password}}")
    page.get_by_role("button", name="Login", exact=True).click()
'''


def _parses(src: str) -> ast.Module:
    return ast.parse(src)


def test_referenced_vars_dedup_and_order():
    src = "a {{x}} b {{y}} c {{x}}"
    assert referenced_vars(src) == ["x", "y"]


def test_no_datasets_is_noop():
    assert apply_datasets(_BASE, None) == _BASE
    assert apply_datasets(_BASE, {}) == _BASE


def test_unreferenced_dataset_is_noop():
    # Token in code references a var that isn't defined → leave the file alone.
    assert apply_datasets(_BASE, {"other": ["v"]}) == _BASE


def test_single_var_parametrize():
    src = '''from playwright.sync_api import Page, expect


def test_login(page: Page, snap):
    page.get_by_role("textbox", name="Username", exact=True).fill("{{username}}")
'''
    out = apply_datasets(src, {"username": ["alice", "bob", "carol"]})
    _parses(out)
    assert "import pytest" in out
    assert '@pytest.mark.parametrize("username", [' in out
    assert "'alice', 'bob', 'carol'" in out
    assert "def test_login(page: Page, snap, username):" in out
    # token replaced by f-string interpolation, no raw braces left
    assert "{{username}}" not in out
    assert 'fill(f"{username}")' in out


def test_paired_zip_two_vars():
    out = apply_datasets(
        _BASE,
        {"username": ["alice", "bob"], "password": ["p1", "p2"]},
    )
    _parses(out)
    assert "def test_login(page: Page, snap, username, password):" in out
    assert '@pytest.mark.parametrize("username,password", [' in out
    assert "('alice', 'p1'), ('bob', 'p2')" in out


def test_unequal_lengths_truncate_to_shortest():
    warnings: list[str] = []
    out = apply_datasets(
        _BASE,
        {"username": ["a", "b", "c"], "password": ["p1", "p2"]},
        on_warn=warnings.append,
    )
    _parses(out)
    # zipped to the shorter list (2 rows), and a warning was emitted
    assert "('a', 'p1'), ('b', 'p2')" in out
    assert "'c'" not in out
    assert warnings and "different lengths" in warnings[0]


def test_kebab_var_becomes_valid_identifier():
    src = '''def test_x(page, snap):
    page.get_by_role("textbox", name="Email", exact=True).fill("{{login-email}}")
'''
    out = apply_datasets(src, {"login-email": ["a@b.com"]})
    _parses(out)
    assert '@pytest.mark.parametrize("login_email", [' in out
    assert "def test_x(page, snap, login_email):" in out
    assert 'fill(f"{login_email}")' in out


def test_validate_flags_empty_and_bad_names():
    problems = validate({"": ["x"], "good": [], "1bad": ["y"]})
    blob = " ".join(problems)
    assert "no name" in blob
    assert "'good' has no values" in blob
    assert "1bad" in blob


def test_validate_ok():
    assert validate({"username": ["a", "b"]}) == []
