"""Step 6: run pytest, collect screenshots + traces, parse JUnit XML."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ..events import EventBus
from ..models import GeneratedTest, RunResults, TestArtifacts, TestResult


def execute(
    generated: list[GeneratedTest],
    tests_dir: Path,
    reports_dir: Path,
    bus: EventBus,
) -> RunResults:
    if not generated:
        bus.emit("executor", "No tests to run.", level="warn")
        return RunResults(results=[])

    reports_dir.mkdir(parents=True, exist_ok=True)
    junit = reports_dir / "junit.xml"
    screenshots_dir = reports_dir / "screenshots"
    pw_output_dir = reports_dir / "test-output"

    # Clean prior run's artifacts so the UI doesn't show stale images/traces.
    for path in (junit,):
        if path.exists():
            path.unlink()
    for d in (screenshots_dir, pw_output_dir):
        if d.exists():
            shutil.rmtree(d)

    bus.emit("executor", f"Running pytest on {len(generated)} file(s)…")
    cmd = [
        sys.executable, "-m", "pytest",
        str(tests_dir),
        f"--junitxml={junit}",
        "--browser", "chromium",
        "--tracing", "retain-on-failure",
        "--video", "retain-on-failure",
        f"--output={pw_output_dir}",
        "--tb=short",
        "-q",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    last_line = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
    bus.emit("executor", last_line)
    if proc.returncode not in (0, 1):
        bus.emit(
            "executor",
            f"pytest exited with {proc.returncode}: {proc.stderr[:400]}",
            level="error",
        )

    results = _parse_junit(junit, generated)
    _attach_artifacts(results, generated, screenshots_dir, pw_output_dir)
    bus.emit(
        "executor",
        f"Results: {results.passed} passed, {results.failed} failed.",
        level="success" if results.failed == 0 else "warn",
    )
    return results


def _parse_junit(junit_path: Path, generated: list[GeneratedTest]) -> RunResults:
    if not junit_path.exists():
        return RunResults(results=[
            TestResult(test_case_id=gt.test_case_id, status="error",
                       failure_message="pytest produced no JUnit XML")
            for gt in generated
        ])

    tree = ET.parse(junit_path)
    by_function: dict[str, TestResult] = {}
    for case in tree.iter("testcase"):
        name = case.get("name", "")
        # pytest-playwright sometimes parametrizes function names with browser, e.g.
        # "test_login[chromium]" — strip the suffix to match our function name.
        function_name = name.split("[", 1)[0]
        duration = float(case.get("time", "0") or 0)
        failure = case.find("failure")
        error = case.find("error")
        if failure is not None:
            status, msg = "failed", (failure.get("message") or failure.text or "").strip()
        elif error is not None:
            status, msg = "error", (error.get("message") or error.text or "").strip()
        else:
            status, msg = "passed", ""
        by_function[function_name] = TestResult(
            test_case_id="", status=status, duration_s=duration, failure_message=msg,
        )

    results: list[TestResult] = []
    for gt in generated:
        # Match by the ACTUAL function name written in the generated file, not a
        # name reconstructed from the test_case_id. The Coder occasionally adds a
        # stray word (e.g. "shows_the_invoice" vs id "shows-invoice"), which would
        # otherwise desync the report from a test that actually ran. Fall back to
        # the id-derived name only if we can't parse a def from the code.
        actual_fn = _function_name_in(gt.code)
        candidates = [
            actual_fn,
            f"test_{gt.test_case_id.replace('-', '_')}",
        ]
        r = next((by_function[c] for c in candidates if c and c in by_function), None)
        if r is None:
            results.append(TestResult(
                test_case_id=gt.test_case_id, status="error",
                failure_message=(
                    f"pytest did not report a result for "
                    f"{actual_fn or gt.test_case_id}"
                ),
            ))
        else:
            r.test_case_id = gt.test_case_id
            results.append(r)
    return RunResults(results=results)


_DEF_RE = re.compile(r"^\s*def\s+(test_\w+)\s*\(", re.MULTILINE)


def _function_name_in(code: str) -> str:
    """Return the first `def test_...(` name in the generated file, or '' if none."""
    m = _DEF_RE.search(code or "")
    return m.group(1) if m else ""


def _attach_artifacts(
    results: RunResults,
    generated: list[GeneratedTest],
    screenshots_dir: Path,
    pw_output_dir: Path,
) -> None:
    pw_dirs = list(pw_output_dir.glob("*")) if pw_output_dir.exists() else []

    for r in results.results:
        fn = f"test_{r.test_case_id.replace('-', '_')}"
        art = TestArtifacts()

        # Step-by-step screenshots written by the `snap` fixture.
        # pytest-playwright parametrizes the test name (e.g. `test_foo[chromium]`),
        # which is what request.node.name returns, so the dir on disk may have a
        # `[browser]` suffix. Glob for both the exact match and the parametrized form.
        if screenshots_dir.exists():
            for shots_dir in screenshots_dir.glob(f"{fn}*"):
                if shots_dir.is_dir():
                    art.screenshots = sorted(str(p) for p in shots_dir.glob("*.png"))
                    if art.screenshots:
                        break

        # pytest-playwright's per-test dirs are named after the test path/function.
        # Match by substring against our function name.
        for d in pw_dirs:
            if fn in d.name:
                trace = d / "trace.zip"
                video = next(iter(d.glob("*.webm")), None)
                if trace.exists():
                    art.trace_path = str(trace)
                if video:
                    art.video_path = str(video)
                break

        r.artifacts = art
