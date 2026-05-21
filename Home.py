"""Streamlit UI — interactive, per-step transparency."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import streamlit as st

from qa_agent import pipeline, prd
from qa_agent.events import Event, EventBus
from qa_agent.models import Question

st.set_page_config(page_title="QA Autonomous Agent", page_icon="🧪", layout="wide")

LEVEL_ICON = {"info": "•", "success": "✅", "warn": "⚠️", "error": "❌"}

# Persist last-used inputs so users don't re-paste every session.
_INPUTS_PATH = Path(__file__).parent / ".last_inputs.json"


def _load_last_inputs() -> dict[str, str]:
    if not _INPUTS_PATH.exists():
        return {}
    try:
        return json.loads(_INPUTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_inputs(app_url: str, user_story: str) -> None:
    try:
        _INPUTS_PATH.write_text(
            json.dumps({"app_url": app_url, "user_story": user_story}, indent=2),
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


def reset() -> None:
    for k in ("phase", "events", "phase_a", "answers", "phase_b", "error"):
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

        st.session_state.prd_text = prd_text
        st.session_state.app_url = app_url
        st.session_state.user_story = user_story
        _save_last_inputs(app_url, user_story)
        st.session_state.phase = "phase_a"
        st.rerun()


# ─────────────────────────── PHASE: A (analyst + inquirer) ───────────────────────────
if st.session_state.phase == "phase_a":
    st.session_state.events = []
    bus = bus_for_session()
    with st.status("Analyst → reading PRD and identifying missing values…", expanded=True):
        try:
            phase_a = pipeline.run_phase_a(
                st.session_state.prd_text, st.session_state.app_url, bus
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
if st.session_state.phase == "phase_b":
    bus = bus_for_session()

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

    with st.status("🚀 Starting pipeline…", expanded=True) as status:
        # Live mini-feed inside the status box.
        feed = st.empty()
        live_lines: list[str] = []

        def live_sink(event: Event) -> None:
            label = STEP_LABELS.get(event.step, event.step)
            status.update(label=f"Currently: {label}")
            ts = event.timestamp.strftime("%H:%M:%S")
            icon = LEVEL_ICON.get(event.level, "•")
            live_lines.append(f"`{ts}` {icon} **{event.step}** — {event.message}")
            # Show the most recent ~25 lines so the box doesn't grow forever.
            feed.markdown("\n\n".join(live_lines[-25:]))

        bus.subscribe(live_sink)

        try:
            phase_b = pipeline.run_phase_b(
                st.session_state.phase_a.spec, st.session_state.answers, bus,
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
