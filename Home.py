"""Streamlit UI — interactive, per-step transparency."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import streamlit as st

from qa_agent import pipeline, prd
from qa_agent.datasets import validate as dataset_validate
from qa_agent.events import Event, EventBus
from qa_agent.models import Question

st.set_page_config(page_title="QA Autonomous Agent", page_icon="🧪", layout="wide")

LEVEL_ICON = {"info": "•", "success": "✅", "warn": "⚠️", "error": "❌"}

# Human-friendly step labels. The bus emits `step` as the agent name.
STEP_LABELS = {
    "designer": "✍️ Designer — writing the test plan",
    "explorer": "🔎 Explorer — visiting pages and scraping elements",
    "orchestrator": "🧭 Orchestrator — cross-checking plan against reality",
    "coder": "💻 Coder — generating pytest-playwright code",
    "validator": "🧪 Validator — verifying locators",
    "executor": "🚦 Executor — running pytest in a real browser",
    "healer": "🩹 Healer — rewriting failing tests",
    "reporter": "📝 Reporter — assembling the final report",
}

# Persist last-used inputs so users don't re-paste every session.
_INPUTS_PATH = Path(__file__).parent / ".last_inputs.json"


def _load_last_inputs() -> dict:
    if not _INPUTS_PATH.exists():
        return {}
    try:
        return json.loads(_INPUTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_inputs(
    app_url: str,
    user_story: str,
    *,
    github_url: str = "",
    use_source: bool = False,
    enable_human: bool = False,
    datasets: dict[str, list[str]] | None = None,
) -> None:
    try:
        _INPUTS_PATH.write_text(
            json.dumps(
                {
                    "app_url": app_url,
                    "user_story": user_story,
                    "github_url": github_url,
                    "use_source": use_source,
                    "enable_human": enable_human,
                    "datasets": datasets or {},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # persistence is best-effort; never block the pipeline


if "phase" not in st.session_state:
    saved = _load_last_inputs()
    st.session_state.phase = "input"
    st.session_state.events = []
    st.session_state.phase_a = None
    st.session_state.answers = {}
    st.session_state.phase_b = None
    st.session_state.error = None
    st.session_state.app_url = saved.get("app_url", "")
    st.session_state.user_story = saved.get("user_story", "")
    st.session_state.github_url = saved.get("github_url", "")
    st.session_state.use_source = saved.get("use_source", False)
    st.session_state.source_insights = None
    st.session_state.enable_human = saved.get("enable_human", False)
    st.session_state.human_plan = None
    st.session_state.human_sitemap = None
    st.session_state.human_generated = None
    st.session_state.phase_b_partial = None
    st.session_state.url_problems = []
    # Dataset variables: editor rows are [{"name": str, "values": "<newline text>"}].
    saved_ds = saved.get("datasets") or {}
    st.session_state.dataset_rows = [
        {"name": name, "values": "\n".join(vals)} for name, vals in saved_ds.items()
    ] or [{"name": "", "values": ""}]


def _rows_to_datasets(rows: list[dict]) -> dict[str, list[str]]:
    """Editor rows → {name: [values]}, skipping blank names and blank values."""
    out: dict[str, list[str]] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        values = [v.strip() for v in (row.get("values") or "").splitlines() if v.strip()]
        if values:
            out[name] = values
    return out


def reset() -> None:
    for k in (
        "phase", "events", "phase_a", "answers", "phase_b", "error",
        "source_insights", "human_plan", "human_sitemap", "human_generated",
        "phase_b_partial", "url_problems",
    ):
        st.session_state.pop(k, None)


def bus_for_session() -> EventBus:
    bus = EventBus()

    def sink(event: Event) -> None:
        st.session_state.events.append(event)

    bus.subscribe(sink)
    return bus


def render_log() -> None:
    if not st.session_state.events:
        return
    with st.expander("Live log", expanded=False):
        lines = [
            f"`{e.timestamp.strftime('%H:%M:%S')}` "
            f"{LEVEL_ICON.get(e.level, '•')} **{e.step}** — {e.message}"
            for e in st.session_state.events
        ]
        st.markdown("\n\n".join(lines))


st.title("🧪 QA Autonomous Agent")
st.caption("PRD → questions → tests → live diagnostics. Functional, role-based locators, Chromium.")

# Persistent panel: show everything extracted from GitHub source.
def _render_source_panel():
    si = st.session_state.get("source_insights")
    if not si or getattr(si, "is_empty", True):
        return
    header = (f"📂 Extracted from GitHub source ({si.repo_url}): "
              f"{len(si.routes)} route(s) · {len(si.stable_ids)} HTML id(s) · "
              f"{len(si.api_endpoints)} API endpoint(s)")
    with st.expander(header, expanded=False):
        if si.routes:
            st.markdown("**Routes**")
            for r in si.routes:
                st.code(r, language=None)
        if si.stable_ids:
            st.markdown(f"**HTML element IDs ({len(si.stable_ids)})**")
            st.caption("Format: `#id <tag> label='…' [from file]`")
            for sid in si.stable_ids:
                st.code(sid.describe(), language=None)
        if si.api_endpoints:
            st.markdown(f"**API endpoints ({len(si.api_endpoints)})**")
            st.caption("Each shows the JSON field names the endpoint actually uses, "
                       "read from the source code — these are what the Coder must use "
                       "verbatim in backend assertions.")
            for ep in si.api_endpoints:
                st.code(ep.describe(), language=None)
        if si.notes:
            st.info(f"Notes: {si.notes}")

_render_source_panel()

with st.sidebar:
    st.header("Inputs")
    app_url = st.text_input(
        "App URL", placeholder="https://example.com",
        value=st.session_state.get("app_url", ""),
    )
    uploaded = st.file_uploader("PRD (PDF/DOCX/TXT/MD)", type=["pdf", "docx", "txt", "md"])
    user_story = st.text_area(
        "…or paste a user story / PRD here", height=240,
        value=st.session_state.get("user_story", ""),
        placeholder="As a visitor on the homepage, I can click 'Pricing' and land on the pricing page…",
    )

    with st.expander("🔗 Enhance with GitHub source (optional)", expanded=False):
        st.caption(
            "Paste a public GitHub repo URL of the app under test. The pipeline "
            "will read its HTML/server source to use stable element IDs, known "
            "routes, and API endpoints as a cheat-sheet. Leave this off for the "
            "original behaviour."
        )
        github_url = st.text_input(
            "GitHub repo URL",
            placeholder="https://github.com/your-org/your-app",
            value=st.session_state.get("github_url", ""),
        )
        use_source = st.checkbox(
            "Use source code to enhance test quality",
            value=st.session_state.get("use_source", False),
            help=(
                "When ON: shallow-clones the repo and feeds extracted IDs / routes "
                "/ API endpoints to the Designer + Coder. When OFF: pipeline runs "
                "exactly as before, untouched."
            ),
        )
        enable_human = st.checkbox(
            "🧑 Enable human-in-the-loop review",
            value=st.session_state.get("enable_human", False),
            help=(
                "When ON: after the Designer writes the test plan, you approve / "
                "modify / delete each test case. After the Coder writes Python "
                "code, you can edit it before pytest runs. When OFF: the pipeline "
                "runs autonomously, exactly as before."
            ),
        )

    with st.expander("🧮 Dataset variables (optional)", expanded=False):
        st.caption(
            "Define a variable and give it one value per line. Reference it in your "
            "PRD with double braces, e.g. `{{username}}`. If a variable has 3 values, "
            "every test that uses it runs 3 times — once per value. Multiple variables "
            "in the same test are paired row-by-row (value 1 with value 1, …)."
        )
        rows = st.session_state.dataset_rows
        for i, row in enumerate(rows):
            c1, c2 = st.columns([2, 1])
            row["name"] = c1.text_input(
                "Variable name", value=row.get("name", ""),
                key=f"ds_name_{i}", placeholder="username",
            )
            if c2.button("🗑️", key=f"ds_del_{i}", help="Remove this variable"):
                rows.pop(i)
                if not rows:
                    rows.append({"name": "", "values": ""})
                st.rerun()
            row["values"] = st.text_area(
                "Values (one per line)", value=row.get("values", ""),
                key=f"ds_vals_{i}", height=80,
                placeholder="alice\nbob\ncarol",
                label_visibility="collapsed",
            )
        if st.button("➕ Add variable", use_container_width=True):
            rows.append({"name": "", "values": ""})
            st.rerun()

        _ds_preview = _rows_to_datasets(rows)
        _ds_problems = dataset_validate(_ds_preview)
        if _ds_problems:
            for p in _ds_problems:
                st.warning(p)
        elif _ds_preview:
            st.success(
                "Datasets: "
                + ", ".join(f"{k} ({len(v)})" for k, v in _ds_preview.items())
            )

    st.divider()
    if st.button("Reset", use_container_width=True):
        reset()
        st.rerun()


# ─────────────────────────── PHASE: input ───────────────────────────
if st.session_state.phase == "input":
    if st.button("▶ Run pipeline", type="primary"):
        prd_text = None
        if uploaded is not None:
            try:
                prd_text = prd.extract_text(uploaded.name, uploaded.getvalue())
            except Exception as exc:
                st.error(f"Failed to read {uploaded.name}: {exc}")
                st.stop()
        elif user_story.strip():
            prd_text = user_story.strip()

        if not prd_text:
            st.warning("Provide a PRD file or paste a user story.")
            st.stop()
        if not app_url:
            st.warning("Enter the App URL.")
            st.stop()

        datasets = _rows_to_datasets(st.session_state.dataset_rows)
        ds_problems = dataset_validate(datasets)
        if ds_problems:
            st.error("Fix the dataset variables before running:\n\n- " + "\n- ".join(ds_problems))
            st.stop()

        st.session_state.prd_text = prd_text
        st.session_state.app_url = app_url
        st.session_state.user_story = user_story
        st.session_state.github_url = github_url
        st.session_state.use_source = use_source
        st.session_state.enable_human = enable_human
        st.session_state.datasets = datasets
        _save_last_inputs(
            app_url, user_story, github_url=github_url, use_source=use_source,
            enable_human=enable_human, datasets=datasets,
        )

        # Fetch source insights NOW (before Phase A) so they're ready by Phase B.
        # Empty / off → source_insights stays None and the rest of the pipeline
        # behaves exactly as the legacy flow.
        st.session_state.source_insights = None
        if use_source and github_url.strip():
            from qa_agent.source import fetch_source
            with st.spinner(f"Cloning {github_url} and reading source…"):
                ins = fetch_source(github_url.strip())
            if ins.is_empty:
                st.warning(
                    f"Source enrichment OFF for this run — {ins.notes or 'no useful content found in repo'}."
                )
            else:
                st.session_state.source_insights = ins
                st.success(
                    f"Source loaded: {len(ins.stable_ids)} IDs · "
                    f"{len(ins.routes)} routes · "
                    f"{len(ins.api_endpoints)} API endpoint(s)."
                )

        st.session_state.phase = "phase_a"
        st.rerun()


# ─────────────────────────── PHASE: A (analyst + inquirer) ───────────────────────────
if st.session_state.phase == "phase_a":
    st.session_state.events = []
    bus = bus_for_session()
    with st.status("Analyst → reading PRD and identifying missing values…", expanded=True):
        try:
            phase_a = pipeline.run_phase_a(
                st.session_state.prd_text, st.session_state.app_url, bus,
                source_insights=st.session_state.get("source_insights"),
                datasets=st.session_state.get("datasets"),
            )
        except Exception as exc:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            st.session_state.phase_a = phase_a
            st.session_state.phase = "questions"
            st.rerun()


# ─────────────────────────── PHASE: questions ───────────────────────────
if st.session_state.phase == "questions":
    phase_a = st.session_state.phase_a
    spec = phase_a.spec

    st.subheader("📋 Step 1 — TestSpec extracted from PRD")
    c1, c2 = st.columns(2)
    c1.metric("User flows", len(spec.user_flows))
    c2.metric("Acceptance criteria", len(spec.acceptance_criteria))
    with st.expander("Show TestSpec", expanded=False):
        st.write(f"**App:** {spec.app_name}")
        st.write(f"**URL:** {spec.app_url}")
        st.write("**User flows:**")
        for f in spec.user_flows:
            st.markdown(f"- **{f.name}** — {f.description}")
        st.write("**Acceptance criteria:**")
        for c in spec.acceptance_criteria:
            st.markdown(f"- {c}")
        if spec.notes:
            st.info(f"Analyst notes: {spec.notes}")

    questions: list[Question] = phase_a.questions.items

    provided = spec.provided_values_dict

    if not questions:
        st.success("Spec is complete — no values needed from you.")
        if provided:
            with st.expander(f"Using {len(provided)} value(s) extracted from the PRD", expanded=False):
                for k, v in provided.items():
                    display = "•" * len(v) if "password" in k.lower() else v
                    st.markdown(f"- `{k}` = `{display}`")
        if st.button("Continue", type="primary"):
            st.session_state.answers = provided
            st.session_state.phase = "phase_b"
            st.rerun()
    else:
        st.subheader("❓ Step 2 — Please provide these values")
        st.caption("The pipeline won't guess. Fill these in and it'll continue.")
        # Show GitHub source data so the user can copy from it if needed.
        si = st.session_state.get("source_insights")
        if si and not getattr(si, "is_empty", True):
            with st.expander(
                f"📂 From your GitHub source: {len(si.routes)} route(s), "
                f"{len(si.stable_ids)} HTML id(s), {len(si.api_endpoints)} API endpoint(s) — click to view",
                expanded=True,
            ):
                st.caption("If a question asks for a URL or specific value, copy from here.")
                if si.routes:
                    st.markdown("**Routes:**")
                    app_url = phase_a.spec.app_url
                    from qa_agent.steps.coder import resolve_url
                    for r in si.routes:
                        st.code(f"{r}        →  {resolve_url(app_url, r)}", language=None)
                if si.stable_ids:
                    st.markdown("**HTML element IDs (with tag and label):**")
                    for sid in si.stable_ids[:40]:
                        st.code(sid.describe(), language=None)
                if si.api_endpoints:
                    st.markdown("**API endpoints:**")
                    for ep in si.api_endpoints:
                        st.code(ep.describe(), language=None)
        with st.form("answers_form"):
            answers: dict[str, str] = {}
            for q in questions:
                widget_key = f"q::{q.key}"
                default = provided.get(q.key, "")
                if q.kind == "password":
                    val = st.text_input(q.prompt, key=widget_key, type="password", value=default, help=q.hint or None)
                else:
                    val = st.text_input(q.prompt, key=widget_key, value=default, help=q.hint or None)
                if q.reason:
                    st.caption(f"_{q.reason}_")
                answers[q.key] = val
            submitted = st.form_submit_button("▶ Continue", type="primary")
            if submitted:
                missing = [q.key for q in questions if not answers.get(q.key)]
                if missing:
                    st.warning(f"Please fill in: {', '.join(missing)}")
                else:
                    # Merge: PRD-extracted values first, form answers override.
                    st.session_state.answers = {**provided, **answers}
                    st.session_state.phase = "phase_b"
                    st.rerun()

    render_log()


# ─────────────────────────── PHASE: B (designer through reporter) ───────────────────────────
# Branch BEFORE the legacy phase_b runs so a human-mode session is fully isolated.
if st.session_state.phase == "phase_b" and st.session_state.get("enable_human"):
    st.session_state.phase = "human_design"
    st.rerun()

def _attach_live_feed(bus: "EventBus", status) -> None:
    """Wire a status box's label + a rolling mini-feed to bus events."""
    feed = st.empty()
    live_lines: list[str] = []

    def live_sink(event: Event) -> None:
        label = STEP_LABELS.get(event.step, event.step)
        status.update(label=f"Currently: {label}")
        ts = event.timestamp.strftime("%H:%M:%S")
        icon = LEVEL_ICON.get(event.level, "•")
        live_lines.append(f"`{ts}` {icon} **{event.step}** — {event.message}")
        feed.markdown("\n\n".join(live_lines[-25:]))

    bus.subscribe(live_sink)


# Stage B1 — Designer + Explorer, then check whether every planned URL was
# actually reachable. If any weren't, we PAUSE and ask the user — never guess.
if st.session_state.phase == "phase_b":
    st.session_state.events = []
    bus = bus_for_session()
    with st.status("🚀 Designing tests & exploring the site…", expanded=True) as status:
        _attach_live_feed(bus, status)
        try:
            partial = pipeline.run_phase_b_explore(
                st.session_state.phase_a.spec, st.session_state.answers, bus,
                source_insights=st.session_state.get("source_insights"),
            )
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            st.session_state.phase_b_partial = partial
            problems = _collect_url_problems(partial)
            if problems:
                st.session_state.url_problems = problems
                status.update(
                    label=f"⏸ {len(problems)} URL(s) need your confirmation",
                    state="complete",
                )
                st.session_state.phase = "verify_urls"
            else:
                status.update(label="✅ Exploration complete", state="complete")
                st.session_state.phase = "phase_b_finish"
            st.rerun()


def _collect_url_problems(partial) -> list[dict]:
    """Combine two checks: URLs the Designer guessed (not in PRD / source / answers)
    + URLs the Explorer couldn't load. Each entry has a 'reason' so the UI can tell."""
    spec = st.session_state.phase_a.spec
    guessed = pipeline.guessed_urls(
        spec, partial.plan, st.session_state.answers,
        prd_text=st.session_state.get("prd_text", ""),
        source_insights=st.session_state.get("source_insights"),
    )
    unreachable = pipeline.unreachable_urls(spec, partial.plan, partial.sitemap)
    # Deduplicate by (test_case_id, url) — prefer 'guess' reason if both apply
    seen, merged = set(), []
    for p in guessed:
        k = (p["test_case_id"], p["url"])
        if k in seen:
            continue
        seen.add(k)
        merged.append(p)
    for p in unreachable:
        k = (p["test_case_id"], p["url"])
        if k in seen:
            continue
        seen.add(k)
        merged.append({**p, "reason": "unreachable"})
    return merged


# Stage B1.5 — pause: URLs the Designer guessed OR the Explorer couldn't load. Ask the user.
if st.session_state.phase == "verify_urls":
    problems = st.session_state.get("url_problems", [])
    n_guess = sum(1 for p in problems if p.get("reason") == "guess")
    n_unreach = sum(1 for p in problems if p.get("reason") == "unreachable")
    st.subheader("🛑 Confirm these URLs — I won't guess")
    msg = []
    if n_guess:
        msg.append(f"{n_guess} URL(s) the test planner picked but you never gave me "
                   f"(not in your PRD, not in the GitHub source, not in your answers).")
    if n_unreach:
        msg.append(f"{n_unreach} URL(s) that didn't return a usable page.")
    st.caption(" ".join(msg) + " Enter the correct URL for each. My best guess is "
               "pre-filled as a suggestion — change it if it's wrong.")
    # Show all GitHub source routes once at the top so the user can copy from them.
    si = st.session_state.get("source_insights")
    if si and getattr(si, "routes", None):
        with st.expander(f"📂 {len(si.routes)} route(s) found in your GitHub source — click to view", expanded=True):
            st.write("These are the routes the GitHub source reader extracted. "
                     "Copy whichever one is the right page for the test below.")
            app_url = st.session_state.phase_a.spec.app_url
            for r in si.routes:
                # Build the full URL form too, so a copy is one click away
                from qa_agent.steps.coder import resolve_url
                full = resolve_url(app_url, r)
                st.code(f"{r}        →  {full}", language=None)
    with st.form("verify_urls_form"):
        corrections: dict[str, str] = {}
        for i, p in enumerate(problems):
            reason_tag = "🤖 GUESSED" if p.get("reason") == "guess" else "🚫 UNREACHABLE"
            st.markdown(f"**Test `{p['test_case_id']}`** — {reason_tag}")
            st.caption(f"Planned URL: `{p['url']}`")
            corrections[f"{p['test_case_id']}::{p['url']}"] = st.text_input(
                "Correct URL",
                value=p["suggestion"],
                key=f"url_fix_{i}",
                help="Suggestion pre-filled from my best guess — edit if it's wrong. "
                     "If your GitHub source has the right route, copy it from the panel above.",
            )
            st.divider()
        submitted = st.form_submit_button("▶ Use these URLs & continue", type="primary")

    if submitted:
        partial = st.session_state.phase_b_partial
        for tc in partial.plan.test_cases:
            tc.page_urls = [
                corrections.get(f"{tc.id}::{u}", u) for u in tc.page_urls
            ]
        st.session_state.events = []
        bus = bus_for_session()
        with st.status("🔎 Re-exploring with your URLs…", expanded=True) as status:
            _attach_live_feed(bus, status)
            try:
                partial.sitemap = pipeline.explore_only(
                    st.session_state.phase_a.spec, partial.plan,
                    st.session_state.answers, bus,
                )
            except Exception:
                st.session_state.error = traceback.format_exc()
                st.session_state.phase = "error"
                st.rerun()
            else:
                status.update(label="✅ Re-exploration complete", state="complete")
        st.session_state.phase_b_partial = partial
        remaining = _collect_url_problems(partial)
        if remaining:
            st.session_state.url_problems = remaining
            st.warning("Some URLs still need confirmation — please correct them and try again.")
            st.rerun()
        else:
            st.session_state.phase = "phase_b_finish"
            st.rerun()
    render_log()


# Stage B2 — Orchestrator → Coder → Validator → Executor → Healer → Reporter.
if st.session_state.phase == "phase_b_finish":
    bus = bus_for_session()
    with st.status("🚀 Building & running tests…", expanded=True) as status:
        _attach_live_feed(bus, status)
        try:
            phase_b = pipeline.run_phase_b_finish(
                st.session_state.phase_a.spec, st.session_state.answers,
                st.session_state.phase_b_partial, bus,
                source_insights=st.session_state.get("source_insights"),
                datasets=st.session_state.get("datasets"),
            )
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            status.update(label="✅ Pipeline complete", state="complete")
            st.session_state.phase_b = phase_b
            st.session_state.phase = "done"
            st.rerun()


# ─────────────────────────── PHASE: done ───────────────────────────
if st.session_state.phase == "done":
    spec = st.session_state.phase_a.spec
    pb = st.session_state.phase_b

    final = pb.final_results
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Test cases", len(pb.plan.test_cases) if pb.plan else 0)
    c2.metric("URLs explored", len(pb.sitemap.pages) if pb.sitemap else 0)
    c3.metric("Passed", final.passed if final else 0)
    c4.metric("Failed", final.failed if final else 0)

    tabs = st.tabs([
        "1️⃣ TestSpec",
        "2️⃣ Test cases",
        "3️⃣ DOM elements",
        "4️⃣ Generated code",
        "5️⃣ Locator check",
        "6️⃣ Results & why-it-failed",
        "📄 Report",
        "🪵 Log",
    ])

    with tabs[0]:
        st.write(f"**App:** {spec.app_name}")
        st.write(f"**URL:** {spec.app_url}")
        st.write("**User flows**")
        for f in spec.user_flows:
            st.markdown(f"- **{f.name}** — {f.description}")
        st.write("**Acceptance criteria**")
        for c in spec.acceptance_criteria:
            st.markdown(f"- {c}")
        if spec.notes:
            st.info(spec.notes)
        st.write("**Your answers**")
        st.json({k: ("***" if "pass" in k else v) for k, v in st.session_state.answers.items()})

    with tabs[1]:
        if pb.plan:
            for tc in pb.plan.test_cases:
                with st.expander(f"{tc.id} — {tc.title}", expanded=False):
                    st.write(f"**Pages:** {', '.join(tc.page_urls) or '(none)'}")
                    st.write(f"**Expected:** {tc.expected_outcome}")
                    st.markdown(
                        "\n".join(f"- **{s.keyword}** {s.text}" for s in tc.steps)
                    )

    with tabs[2]:
        if pb.sitemap:
            for key, snap in pb.sitemap.pages.items():
                with st.expander(f"{key} — {snap.title}", expanded=False):
                    st.write(f"**Resolved URL:** {snap.url}")
                    if snap.elements:
                        st.table(
                            [{"role": e.role, "name": e.name, "purpose": e.purpose}
                             for e in snap.elements]
                        )
                    else:
                        st.warning("No elements captured. Likely auth-gated or page failed to load.")

    with tabs[3]:
        files = pb.healed_generated or pb.generated
        for gt in files:
            with st.expander(gt.file_path, expanded=False):
                st.code(gt.code, language="python")

    with tabs[4]:
        if not pb.locator_reports:
            st.info("No locator reports.")
        for r in pb.locator_reports:
            icon = "✅" if r.misses == 0 and not r.placeholders else "❌"
            with st.expander(f"{icon} {r.test_case_id}  ({len(r.checks)} locators)", expanded=False):
                if r.checks:
                    rows = []
                    for c in r.checks:
                        rows.append({
                            "method": c.method,
                            "args": c.args,
                            "status": {"match": "✅", "fuzzy": "⚠️", "miss": "❌"}[c.status],
                            "closest match in SiteMap": c.closest,
                        })
                    st.table(rows)
                if r.placeholders:
                    st.warning("Unresolved NEEDS markers:")
                    for n in r.placeholders:
                        st.code(n)

    with tabs[5]:
        if not final:
            st.info("No results.")
        else:
            titles = {tc.id: tc.title for tc in pb.plan.test_cases}
            for res in final.results:
                icon = {"passed": "✅", "failed": "❌", "error": "⚠️"}[res.status]
                with st.expander(
                    f"{icon} {res.test_case_id} — {titles.get(res.test_case_id, '')}  "
                    f"({res.duration_s:.2f}s)",
                    expanded=(res.status != "passed"),
                ):
                    st.write(f"**Status:** {res.status}")
                    if res.failure_message:
                        st.code(res.failure_message, language="text")

                    art = res.artifacts
                    if art.screenshots:
                        st.markdown("**Step-by-step screenshots:**")
                        cols_per_row = 3
                        for i in range(0, len(art.screenshots), cols_per_row):
                            row = art.screenshots[i : i + cols_per_row]
                            cols = st.columns(cols_per_row)
                            for col, shot in zip(cols, row):
                                label = Path(shot).stem  # e.g. "03_after_click_login"
                                col.image(shot, caption=label, use_container_width=True)
                    if art.video_path:
                        st.markdown("**Video (failure):**")
                        try:
                            st.video(art.video_path)
                        except Exception:
                            st.caption(f"Video at `{art.video_path}` — open externally.")
                    if art.trace_path:
                        st.markdown("**Playwright trace (interactive replay):**")
                        st.code(f".venv/bin/playwright show-trace {art.trace_path}", language="bash")
                        with open(art.trace_path, "rb") as f:
                            st.download_button(
                                "⬇ Download trace.zip", f, file_name=Path(art.trace_path).name,
                                mime="application/zip", key=f"trace-{res.test_case_id}",
                            )

                    # Cross-reference: which locator likely caused this?
                    matching = next(
                        (r for r in pb.locator_reports if r.test_case_id == res.test_case_id),
                        None,
                    )
                    if matching and res.status != "passed":
                        misses = [c for c in matching.checks if c.status == "miss"]
                        if misses:
                            st.markdown("**Likely cause — locators not in SiteMap:**")
                            for c in misses:
                                hint = f" → closest match: `{c.closest}`" if c.closest else ""
                                st.markdown(f"- `{c.method}({c.args})`{hint}")
                        if matching.placeholders:
                            st.markdown("**Unresolved values (NEEDS markers):**")
                            for n in matching.placeholders:
                                st.markdown(f"- {n}")

    with tabs[6]:
        # Render the text part of the report (everything before Visual evidence) as markdown,
        # then render screenshots per-test using st.image so Streamlit actually displays them.
        md = pb.report_markdown or ""
        head, _, _ = md.partition("## Visual evidence")
        st.markdown(head or "(no report generated)")

        if final and pb.plan:
            st.markdown("## Visual evidence")
            titles = {tc.id: tc.title for tc in pb.plan.test_cases}
            for r in final.results:
                icon = {"passed": "✅", "failed": "❌", "error": "⚠️"}[r.status]
                st.markdown(
                    f"### {icon} `{r.test_case_id}` — {titles.get(r.test_case_id, '')}"
                )
                if r.artifacts.screenshots:
                    cols_per_row = 3
                    for i in range(0, len(r.artifacts.screenshots), cols_per_row):
                        row = r.artifacts.screenshots[i : i + cols_per_row]
                        cols = st.columns(cols_per_row)
                        for col, shot in zip(cols, row):
                            col.image(shot, caption=Path(shot).stem, use_container_width=True)
                else:
                    st.caption("_No screenshots captured._")
                if r.failure_message:
                    with st.expander("Failure detail"):
                        st.code(r.failure_message, language="text")
                if r.artifacts.trace_path:
                    st.caption(
                        f"Replay: `playwright show-trace {r.artifacts.trace_path}`"
                    )
                st.divider()

        # On-disk markdown download
        report_path = Path("reports/final_qa_report.md")
        if report_path.exists():
            with open(report_path, "rb") as f:
                st.download_button(
                    "⬇ Download report (final_qa_report.md)", f,
                    file_name="final_qa_report.md", mime="text/markdown",
                )

    with tabs[7]:
        if st.session_state.events:
            for e in st.session_state.events:
                st.markdown(
                    f"`{e.timestamp.strftime('%H:%M:%S')}` "
                    f"{LEVEL_ICON.get(e.level, '•')} **{e.step}** — {e.message}"
                )

    st.divider()
    if st.button("🔄 Run again with a new PRD"):
        reset()
        st.rerun()


# ─────────────────────────── PHASE: error ───────────────────────────
if st.session_state.phase == "error":
    st.error("Pipeline crashed.")
    st.code(st.session_state.error or "(no traceback)", language="text")
    render_log()
    if st.button("Reset"):
        reset()
        st.rerun()


# ──────────────────────── HUMAN-IN-THE-LOOP FLOW ────────────────────────
# Everything below only fires when `enable_human` is on. The non-interactive
# flow above doesn't touch any of this. Each phase is a small Streamlit screen
# that pauses for review/edits before continuing.

from qa_agent.steps import (
    coder as _h_coder,
    designer as _h_designer,
    executor as _h_executor,
    explorer as _h_explorer,
    healer as _h_healer,
    orchestrator as _h_orchestrator,
    reporter as _h_reporter,
    validator as _h_validator,
)
from qa_agent.models import TestPlan, TestCase, TestStep


def _human_bus():
    """Reuse the same Streamlit event sink used by the legacy flow."""
    return bus_for_session()


# Stage 1 — run Designer (and placeholder resolution), then hand to review.
if st.session_state.phase == "human_design":
    st.session_state.events = []
    bus = _human_bus()
    with st.status("✍️ Designer — writing the test plan…", expanded=True) as status:
        try:
            plan = _h_designer.design(
                st.session_state.phase_a.spec, st.session_state.answers, bus,
                source_insights=st.session_state.get("source_insights"),
            )
            _h_orchestrator.resolve_placeholders(
                plan,
                st.session_state.answers,
                st.session_state.phase_a.spec.app_url,
                bus,
            )
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            status.update(label=f"Designer drafted {len(plan.test_cases)} test case(s).", state="complete")
            st.session_state.human_plan = plan
            st.session_state.phase = "human_review_plan"
            st.rerun()


# Stage 2 — review / modify / delete each test case.
if st.session_state.phase == "human_review_plan":
    plan: TestPlan = st.session_state.human_plan
    st.subheader("📋 Step 1 of 2 — Review the test plan")
    st.caption(
        "Untick any test case you don't want. Edit the title or expected outcome inline. "
        "Click ▶ Continue when you're happy with the list."
    )

    keep_flags: list[bool] = []
    titles: list[str] = []
    outcomes: list[str] = []
    for i, tc in enumerate(plan.test_cases):
        with st.expander(f"`{tc.id}` — {tc.title}", expanded=False):
            keep = st.checkbox("Keep this test", value=True, key=f"keep_{i}")
            new_title = st.text_input("Title", value=tc.title, key=f"title_{i}")
            new_outcome = st.text_area(
                "Expected outcome", value=tc.expected_outcome, key=f"outcome_{i}", height=70,
            )
            st.markdown("**Steps** (read-only):")
            steps_md = "\n".join([f"- **{s.keyword}** {s.text}" for s in tc.steps])
            st.markdown(steps_md or "_(no steps)_")
            urls_md = ", ".join([f"`{u}`" for u in tc.page_urls]) or "_(none)_"
            st.caption(f"URLs: {urls_md}")
        keep_flags.append(keep)
        titles.append(new_title)
        outcomes.append(new_outcome)

    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("▶ Continue", type="primary"):
            new_cases: list[TestCase] = []
            for i, tc in enumerate(plan.test_cases):
                if not keep_flags[i]:
                    continue
                new_cases.append(
                    TestCase(
                        id=tc.id,
                        title=titles[i].strip() or tc.title,
                        page_urls=tc.page_urls,
                        steps=tc.steps,
                        expected_outcome=outcomes[i].strip() or tc.expected_outcome,
                    )
                )
            if not new_cases:
                st.warning("You can't continue with zero test cases — keep at least one.")
            else:
                st.session_state.human_plan = TestPlan(test_cases=new_cases)
                st.session_state.phase = "human_explore_code"
                st.rerun()
    with col2:
        st.caption(f"Keeping {sum(keep_flags)} of {len(keep_flags)} test case(s).")
    render_log()


# Stage 3 — Explorer (only). Then check URL reachability; pause if any failed.
if st.session_state.phase == "human_explore_code":
    bus = _human_bus()
    spec = st.session_state.phase_a.spec
    plan: TestPlan = st.session_state.human_plan
    answers = st.session_state.answers
    st.session_state.events = []
    with st.status("🔎 Exploring the site…", expanded=True) as status:
        _attach_live_feed(bus, status)
        try:
            sitemap = _h_explorer.explore(spec, plan, answers, bus)
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            st.session_state.human_sitemap = sitemap
            problems = pipeline.unreachable_urls(spec, plan, sitemap)
            if problems:
                st.session_state.url_problems = problems
                status.update(
                    label=f"⏸ {len(problems)} URL(s) need your confirmation",
                    state="complete",
                )
                st.session_state.phase = "human_verify_urls"
            else:
                status.update(label="✅ Exploration complete", state="complete")
                st.session_state.phase = "human_code"
            st.rerun()


# Stage 3.5 — pause: ask the user to confirm URLs the Explorer couldn't reach.
if st.session_state.phase == "human_verify_urls":
    problems = st.session_state.get("url_problems", [])
    st.subheader("🛑 Confirm these URLs — I won't guess")
    st.caption(
        "The Explorer couldn't load a usable page for the URL(s) below (the page was "
        "missing, or returned no interactable elements). Enter the correct URL for each. "
        "My best guess is pre-filled as a suggestion — change it if it's wrong."
    )
    with st.form("human_verify_urls_form"):
        corrections: dict[str, str] = {}
        for i, p in enumerate(problems):
            st.markdown(f"**Test `{p['test_case_id']}`** needs a page it couldn't reach.")
            st.caption(f"Planned URL that failed: `{p['url']}`")
            corrections[f"{p['test_case_id']}::{p['url']}"] = st.text_input(
                "Correct URL",
                value=p["suggestion"],
                key=f"h_url_fix_{i}",
                help="Suggestion pre-filled from my best guess — edit if it's wrong.",
            )
            st.divider()
        submitted = st.form_submit_button("▶ Use these URLs & continue", type="primary")

    if submitted:
        spec = st.session_state.phase_a.spec
        plan = st.session_state.human_plan
        # Patch the failed URLs in the plan with the user's corrections.
        for tc in plan.test_cases:
            tc.page_urls = [
                corrections.get(f"{tc.id}::{u}", u) for u in tc.page_urls
            ]
        st.session_state.human_plan = plan
        st.session_state.events = []
        bus = _human_bus()
        with st.status("🔎 Re-exploring with your URLs…", expanded=True) as status:
            _attach_live_feed(bus, status)
            try:
                sitemap = pipeline.explore_only(
                    spec, plan, st.session_state.answers, bus,
                )
            except Exception:
                st.session_state.error = traceback.format_exc()
                st.session_state.phase = "error"
                st.rerun()
            else:
                status.update(label="✅ Re-exploration complete", state="complete")
        st.session_state.human_sitemap = sitemap
        remaining = pipeline.unreachable_urls(spec, plan, sitemap)
        if remaining:
            st.session_state.url_problems = remaining
            st.warning(
                "Some URLs still couldn't be reached — please correct them and try again."
            )
            st.rerun()
        else:
            st.session_state.phase = "human_code"
            st.rerun()
    render_log()


# Stage 3.6 — Orchestrator validate + Coder, then hand to code review.
if st.session_state.phase == "human_code":
    bus = _human_bus()
    spec = st.session_state.phase_a.spec
    plan: TestPlan = st.session_state.human_plan
    sitemap = st.session_state.human_sitemap
    answers = st.session_state.answers
    st.session_state.events = []
    with st.status("💻 Writing code…", expanded=True) as status:
        _attach_live_feed(bus, status)
        try:
            _h_orchestrator.validate_orchestration(spec, plan, sitemap, bus)
            generated = _h_coder.code(
                spec, plan, sitemap, answers,
                pipeline.TESTS_DIR, bus,
                source_insights=st.session_state.get("source_insights"),
                datasets=st.session_state.get("datasets"),
            )
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            status.update(label="Code drafted — ready for your review.", state="complete")
            st.session_state.human_generated = generated
            st.session_state.phase = "human_review_code"
            st.rerun()


# Stage 4 — review / edit each generated test file. Highlight NEEDS markers.
if st.session_state.phase == "human_review_code":
    st.subheader("📝 Step 2 of 2 — Review the generated code")
    st.caption(
        "Edit any test below before pytest runs. Click 📋 to copy. "
        "If you see a `# NEEDS:` line, that's a spot the Coder couldn't figure out — "
        "you can fix it manually here."
    )
    generated = st.session_state.human_generated
    edited_sources: list[str] = []
    for i, gt in enumerate(generated):
        is_stub = "pytest.fail(" in gt.code or "# NEEDS:" in gt.code
        marker = "⚠️ stub" if is_stub else "✅ ready"
        with st.expander(f"{marker} — `{gt.test_case_id}` ({Path(gt.file_path).name})", expanded=is_stub):
            new_src = st.text_area(
                f"code_{i}",
                value=gt.code,
                height=320,
                key=f"code_edit_{i}",
                label_visibility="collapsed",
            )
            edited_sources.append(new_src)
            st.code(new_src, language="python")  # gives a copy button via Streamlit's UI

    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("▶ Run tests", type="primary"):
            from qa_agent.steps.coder import _post_process  # apply same safety net
            for gt, src in zip(generated, edited_sources):
                final = _post_process(src)
                Path(gt.file_path).write_text(final, encoding="utf-8")
                gt.code = final
            st.session_state.phase = "human_execute"
            st.rerun()
    with col2:
        stubs = sum(1 for s in edited_sources if "pytest.fail(" in s or "# NEEDS:" in s)
        if stubs:
            st.warning(f"{stubs} of {len(edited_sources)} test(s) still contain stubs or NEEDS markers.")
    render_log()


# Stage 5 — Validator + Executor (+ optional Healer) + Reporter.
if st.session_state.phase == "human_execute":
    bus = _human_bus()
    spec = st.session_state.phase_a.spec
    plan: TestPlan = st.session_state.human_plan
    sitemap = st.session_state.human_sitemap
    answers = st.session_state.answers
    generated = st.session_state.human_generated
    with st.status("🚦 Running tests…", expanded=True) as status:
        feed = st.empty()
        lines: list[str] = []

        def _sink2(event: Event) -> None:
            ts = event.timestamp.strftime("%H:%M:%S")
            icon = LEVEL_ICON.get(event.level, "•")
            lines.append(f"`{ts}` {icon} **{event.step}** — {event.message}")
            feed.markdown("\n\n".join(lines[-25:]))
            status.update(label=f"Currently: {event.step}")

        bus.subscribe(_sink2)
        try:
            generated, locator_reports = _h_validator.validate(generated, sitemap, bus)
            initial = _h_executor.execute(generated, pipeline.TESTS_DIR, pipeline.REPORTS_DIR, bus)
            results = initial
            healed = generated
            if initial.failed > 0:
                healed, fresh_sitemap = _h_healer.heal(
                    spec, plan, sitemap, generated, initial, answers, pipeline.TESTS_DIR, bus,
                    source_insights=st.session_state.get("source_insights"),
                )
                sitemap = fresh_sitemap
                healed, locator_reports = _h_validator.validate(healed, sitemap, bus)
                results = _h_executor.execute(healed, pipeline.TESTS_DIR, pipeline.REPORTS_DIR, bus)
            report_md = _h_reporter.report(spec, plan, results, pipeline.REPORTS_DIR, bus)
        except Exception:
            st.session_state.error = traceback.format_exc()
            st.session_state.phase = "error"
            st.rerun()
        else:
            status.update(label="✅ Done.", state="complete")
            # Reuse the existing PhaseB shape so the done-view works without changes.
            pb = pipeline.PhaseB(
                plan=plan,
                sitemap=sitemap,
                generated=generated,
                locator_reports=locator_reports,
                initial_results=initial,
                healed_generated=healed,
                final_results=results,
                report_markdown=report_md,
            )
            st.session_state.phase_b = pb
            st.session_state.phase = "done"
            st.rerun()
