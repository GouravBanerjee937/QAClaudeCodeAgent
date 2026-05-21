"""Step 7: for each failed test, re-snapshot its URLs, regenerate the test, retry once."""

from __future__ import annotations

import json
from pathlib import Path

from ..events import EventBus
from ..llm import text
from ..models import (
    GeneratedTest, PageSnapshot, RunResults, SiteMap, TestCase, TestPlan, TestSpec,
)
from ..prompts import HEALER_SYSTEM
from .coder import _post_process, _sanitize, _strip_fences
from .explorer import _snapshot_page
from playwright.sync_api import sync_playwright


def heal(
    spec: TestSpec,
    plan: TestPlan,
    sitemap: SiteMap,
    generated: list[GeneratedTest],
    results: RunResults,
    answers: dict[str, str],
    tests_dir: Path,
    bus: EventBus,
) -> tuple[list[GeneratedTest], SiteMap]:
    """Returns (updated_generated_list, updated_sitemap). Mutates files on disk."""
    failed_ids = [r.test_case_id for r in results.results if r.status != "passed"]
    if not failed_ids:
        bus.emit("healer", "Nothing to heal — all tests passed.", level="success")
        return generated, sitemap

    by_id_case = {tc.id: tc for tc in plan.test_cases}
    by_id_gen = {gt.test_case_id: gt for gt in generated}
    by_id_res = {r.test_case_id: r for r in results.results}

    bus.emit("healer", f"Healing {len(failed_ids)} failing test(s)…")

    # Re-snapshot every URL touched by failing tests, once.
    fresh_snaps: dict[str, PageSnapshot] = {}
    urls_to_refresh: dict[str, str] = {}
    for tid in failed_ids:
        tc = by_id_case.get(tid)
        if not tc:
            continue
        for url in tc.page_urls:
            if url not in urls_to_refresh:
                from .coder import resolve_url
                urls_to_refresh[url] = resolve_url(spec.app_url, url)

    if urls_to_refresh:
        bus.emit("healer", f"Re-snapshotting {len(urls_to_refresh)} URL(s) fresh…")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                for key, absolute in urls_to_refresh.items():
                    try:
                        snap = _snapshot_page(page, key, absolute, bus)
                        fresh_snaps[key] = snap
                    except Exception as exc:
                        bus.emit("healer", f"Failed to re-snapshot {absolute}: {exc}", level="error")
                context.close()
            finally:
                browser.close()

    # Merge fresh snapshots into a copy of the sitemap (for the healer prompt).
    merged_pages = dict(sitemap.pages)
    merged_pages.update(fresh_snaps)
    fresh_sitemap = SiteMap(pages=merged_pages)

    # Rewrite each failing test.
    updated = list(generated)
    for idx, gt in enumerate(updated):
        if gt.test_case_id not in failed_ids:
            continue
        tc = by_id_case.get(gt.test_case_id)
        res = by_id_res.get(gt.test_case_id)
        if not tc or not res:
            continue
        bus.emit("healer", f"Rewriting {gt.test_case_id}…")
        new_code = _rewrite(spec, tc, gt, res.failure_message, fresh_sitemap, answers)
        path = tests_dir / f"test_{_sanitize(tc.id)}.py"
        path.write_text(new_code, encoding="utf-8")
        updated[idx] = GeneratedTest(
            test_case_id=tc.id, file_path=str(path), code=new_code,
        )
        bus.emit("healer", f"  → rewrote {path.name}", level="success")

    return updated, fresh_sitemap


def _rewrite(
    spec: TestSpec,
    tc: TestCase,
    previous: GeneratedTest,
    failure: str,
    sitemap: SiteMap,
    answers: dict[str, str],
) -> str:
    relevant_pages = [sitemap.pages[u] for u in tc.page_urls if u in sitemap.pages]
    if not relevant_pages:
        relevant_pages = list(sitemap.pages.values())

    sitemap_text = []
    for snap in relevant_pages:
        sitemap_text.append(f"\n## Page: {snap.url}  (title: {snap.title!r})")
        if not snap.elements:
            sitemap_text.append("(no labeled elements — likely auth-gated)")
        for el in snap.elements:
            sitemap_text.append(f'- role="{el.role}", name="{el.name}"  — {el.purpose}')

    prompt = (
        f"# app_url\n{spec.app_url}\n\n"
        f"# TestCase\n{tc.model_dump_json(indent=2)}\n\n"
        f"# Previous generated code\n```python\n{previous.code}\n```\n\n"
        f"# Pytest failure\n{failure[:2000]}\n\n"
        f"# Fresh SiteMap (authoritative)\n"
        + "\n".join(sitemap_text)
        + f"\n\n# answers\n{json.dumps(answers, indent=2)}\n"
    )
    raw = text(HEALER_SYSTEM, prompt, temperature=0.1)
    return _post_process(_strip_fences(raw))
