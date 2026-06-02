"""Dataset variables: ``{{var}}`` tokens → pytest parametrization.

Users define named variables in the UI, each holding a list of values. Anywhere
the PRD (and so the generated test) references ``{{var}}``, the test is run once
per value. When several variables appear in the same test, their value lists are
zipped (paired rows): ``var_a[i]`` pairs with ``var_b[i]``.

The double-brace token is deliberately distinct from the single-brace ``{key}``
answer placeholders the rest of the pipeline already uses, so the two never
collide. Token expansion is done deterministically here (not by the LLM): the
Coder emits the token verbatim inside a string literal and this module rewrites
the file into a parametrized test.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# A dataset variable name: starts with a letter/underscore, then letters, digits,
# underscores or hyphens (kebab-case allowed). Matched inside double braces.
TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")

# A Python string literal, capturing an optional prefix (f/r/b), the quote, and
# the body. Handles escaped chars but not triple-quoted strings (the Coder never
# puts dataset tokens in docstrings).
_STR_RE = re.compile(r"""([fFrRbB]*)(['"])((?:\\.|(?!\2).)*?)\2""")

# A top-level test function definition with a single-line signature.
_TEST_DEF_RE = re.compile(r"^(def\s+(test_\w+)\s*\()([^)]*)(\)\s*:)", re.MULTILINE)


def referenced_vars(text: str) -> list[str]:
    """Ordered, de-duplicated list of ``{{var}}`` names appearing in ``text``."""
    seen: dict[str, None] = {}
    for name in TOKEN_RE.findall(text or ""):
        seen.setdefault(name, None)
    return list(seen)


def validate(datasets: dict[str, list[str]]) -> list[str]:
    """Return human-readable problems with the dataset definitions (empty = OK)."""
    problems: list[str] = []
    for name, values in datasets.items():
        if not name.strip():
            problems.append("A variable has no name.")
            continue
        if not TOKEN_RE.fullmatch("{{" + name + "}}"):
            problems.append(
                f"'{name}' is not a valid variable name "
                "(use letters, digits, underscore or hyphen; start with a letter)."
            )
        if not [v for v in values if v != ""]:
            problems.append(f"'{name}' has no values.")
    return problems


def apply_datasets(
    source: str,
    datasets: dict[str, list[str]] | None,
    *,
    on_warn: Callable[[str], None] | None = None,
) -> str:
    """Wrap a generated test in ``@pytest.mark.parametrize`` for any ``{{var}}`` it uses.

    Finds the dataset variables referenced via ``{{var}}`` tokens in ``source``,
    replaces the tokens with parametrized f-string interpolation, and injects a
    parametrize decorator plus matching signature parameters on each test
    function. Returns ``source`` unchanged when there are no datasets or no
    referenced tokens, so the legacy (no-dataset) flow is untouched.
    """
    if not datasets:
        return source
    used = [v for v in referenced_vars(source) if _nonempty(datasets.get(v))]
    if not used:
        return source

    params = _dedupe([_ident(v) for v in used])
    columns = [[x for x in datasets[v] if x != ""] for v in used]
    n = min(len(c) for c in columns)
    if any(len(c) != n for c in columns) and on_warn:
        on_warn(
            f"Dataset variables {used} have different lengths; "
            f"zipping to the shortest ({n} run(s))."
        )
    rows = [tuple(c[i] for c in columns) for i in range(n)]

    source = _replace_tokens(source, used, params)
    source = _inject_parametrize(source, params, rows)
    if not re.search(r"^import pytest\b", source, re.MULTILINE):
        source = "import pytest\n" + source
    return source


def _nonempty(values: list[str] | None) -> bool:
    return bool(values) and any(v != "" for v in values)


def _ident(name: str) -> str:
    """Turn a (possibly kebab-case) variable name into a valid Python identifier."""
    s = re.sub(r"\W", "_", name)
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def _dedupe(names: list[str]) -> list[str]:
    """Ensure parameter identifiers are unique (two var names can collapse)."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        candidate = name
        i = 2
        while candidate in seen:
            candidate = f"{name}_{i}"
            i += 1
        seen.add(candidate)
        out.append(candidate)
    return out


def _replace_tokens(source: str, used: list[str], params: list[str]) -> str:
    """Rewrite string literals containing our tokens into f-strings."""
    mapping = dict(zip(used, params))

    def repl(m: re.Match) -> str:
        prefix, quote, body = m.group(1), m.group(2), m.group(3)
        if "{{" not in body:
            return m.group(0)

        def tok(tm: re.Match) -> str:
            name = tm.group(1)
            return "{" + mapping[name] + "}" if name in mapping else tm.group(0)

        new_body = TOKEN_RE.sub(tok, body)
        if new_body == body:
            return m.group(0)
        new_prefix = prefix if "f" in prefix.lower() else prefix + "f"
        return f"{new_prefix}{quote}{new_body}{quote}"

    return _STR_RE.sub(repl, source)


def _inject_parametrize(source: str, params: list[str], rows: list[tuple]) -> str:
    decorator = _build_decorator(params, rows)

    def repl(m: re.Match) -> str:
        head, _name, args, tail = m.groups()
        existing = args.strip()
        extra = ", ".join(params)
        new_args = f"{existing}, {extra}" if existing else extra
        return f"{decorator}{head}{new_args}{tail}"

    return _TEST_DEF_RE.sub(repl, source)


def _build_decorator(params: list[str], rows: list[tuple]) -> str:
    names = ",".join(params)
    if len(params) == 1:
        values = ", ".join(repr(r[0]) for r in rows)
        return f'@pytest.mark.parametrize("{names}", [{values}])\n'
    tuples = ", ".join("(" + ", ".join(repr(x) for x in r) + ")" for r in rows)
    return f'@pytest.mark.parametrize("{names}", [{tuples}])\n'
