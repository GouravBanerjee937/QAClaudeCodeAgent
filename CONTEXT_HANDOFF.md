# QA Agent — Session Context Handoff

Read this first in a new chat. It captures the project's state, the decisions
made in the previous session, and the open work, so a new agent can pick up
without context loss.

---

## 1. What this project is

**QA Autonomous AI Agent** — a Streamlit app that turns a plain-English PRD
into a working pytest-playwright test suite, runs it against a real Chromium
browser, heals failures, and writes a Markdown report.

- Repo: <https://github.com/GouravBanerjee937/QAClaudeCodeAgent>
- Target app under test (when developing locally): a local **Invoice App** on
  `http://localhost:3000` (a single-page React app) whose source is at
  <https://github.com/GouravBanerjee937/AccountingSoftware>.
- Default model: `gpt-4.1-mini` (env `QA_AGENT_MODEL`).

---

## 2. How to run it (quick start)

```bash
cd "/Users/gourav/Work/QA Agent"
uv run streamlit run Home.py
```
- Open <http://localhost:8501>
- Sidebar: paste PRD, set App URL, tick GitHub source enrichment + paste repo URL.
- API keys live in `.env` (gitignored). Required: `OPENAI_API_KEY`. Optional:
  `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` (used by `eval/*.py`).

---

## 3. Pipeline (10 steps in order)

| # | Step (Python module) | LLM? | What it does |
|---|---|---|---|
| 0 | `qa_agent/source.py` `fetch_source` | no | Shallow-clones GitHub repo, scrapes routes/element IDs/API endpoints/JSON field names via regex. |
| 1 | `qa_agent/steps/analyst.py` | yes | Reads PRD → `TestSpec` (app_url, provided_values, acceptance criteria, notes). |
| 2 | `qa_agent/steps/inquirer.py` | yes | Asks user for values still missing. **Has a deterministic post-filter** that drops URL questions already covered by `spec.app_url` or GitHub routes. |
| 3 | `qa_agent/steps/designer.py` | yes | Writes Gherkin `TestPlan` (one case per acceptance criterion, no scope creep, no invented URLs). |
| 4 | `qa_agent/steps/orchestrator.py` `resolve_placeholders` | no | Substitutes `{key}` placeholders in URLs. |
| 5 | `qa_agent/steps/explorer.py` | partly | Live-scrapes pages (Playwright); LLM labels each element. **Now also scrapes every `<select>`'s `<option>` text + value.** |
| 6 | `qa_agent/steps/orchestrator.py` `validate_orchestration` | no | Cross-checks spec ↔ plan ↔ sitemap. |
| 7 | `qa_agent/steps/coder.py` | yes | Writes one pytest-playwright file per TestCase. Followed by `_post_process` deterministic regex auto-correct. |
| 8 | `qa_agent/steps/validator.py` | no | AST-parses each generated test, drops invalid ones, rewrites others to `pytest.fail(...)` if their locators aren't in the SiteMap. |
| 9 | `qa_agent/steps/executor.py` | no | Runs `pytest` with `pytest-playwright`, parses JUnit, attaches screenshots/video/trace. |
| 10 | `qa_agent/steps/healer.py` | yes | On failures: re-scrapes URLs, rewrites tests, re-validates, re-runs. Has the same source-derived hints as the Coder. |
| 11 | `qa_agent/steps/reporter.py` | no | Builds the final `reports/final_qa_report.md` deterministically. |

---

## 4. The "no guessing" guarantees (what's enforced where)

### URLs
- **Designer prompt** (`prompts.py → DESIGNER_SYSTEM`): "if you need a URL not in
  PRD/source/answers, emit a `{placeholder}` — do not invent."
- **Code check `pipeline.guessed_urls`**: after Designer + placeholder resolution,
  scans every page_url against (a) PRD text, (b) `source_insights.routes`,
  (c) `spec.app_url`, (d) answers values. Anything else → pause.
- **UI**: pause screen (`Home.py` → `phase == "verify_urls"`) shows the full GitHub
  route list as copy-friendly options.
- **Inquirer**: source insights passed in; post-filter drops URL questions whose
  tokens overlap GitHub routes (so it won't ask for "Item Master URL" if
  `#/items` is in the source).

### Verbs / Playwright API
- **Coder prompt**: explicit role → verb table; source-ID tag is authoritative
  (`<select>` → `.select_option`, never `.fill`).
- **Deterministic auto-correct** (`coder.py → _post_process`):
  - `combobox.fill(X)` → `.select_option(X)`
  - `checkbox/radio.fill(...)` → `.check()`
  - `button/link.fill(...)` → `.click()`
  - `get_by_role("text", name=X)` → `get_by_text(X, exact=True)`
- **Healer prompt**: maps pytest error strings to fix patterns
  (`"Element is not an <input>..."` → `.select_option`, etc.).

### API field names
- `source.py` now scans `.py`/`.js` files for `"keyname":` patterns and attaches
  the top-30 unique keys to each `ApiEndpoint.response_fields`.
- **Coder/Healer prompts**: "use `response_fields` verbatim — never invent or
  transform (no kebab→snake, no rename)."

### Dropdown options
- Explorer JS now records every `<option>` as
  `role="option", name="<text>   | value=\"<v>\""` scoped to its select's `id`.
- Coder prompt: must use `.select_option(value="<v>")` (stable ID) **or**
  `.select_option(label=re.compile(r"^<base_name>(\s|\(|$)"))` (regex anchored
  to leading stable token). **Never** an exact-text label with volatile suffixes
  like "(available: 10)".

---

## 5. GitHub source enrichment — what gets extracted

`qa_agent/source.py → fetch_source(repo_url)` runs 4 regex scanners:

| Scanner | Reads | Captures |
|---|---|---|
| `_extract_routes` | `.html/.js/.jsx/.ts/.tsx/.vue` | `#/foo` and `<Route path="…">` strings |
| `_extract_ids` | `.html` | every `<tag id="…">` + matching `<label for="…">` text |
| `_extract_python_api` | `.py` | Flask/FastAPI decorators + `path == '/api/…'` stdlib handlers |
| `_extract_node_api` | `.js/.ts` | `app.get/post/put/delete/patch(…)` Express style |
| `_extract_dict_field_names` | `.py/.js/.ts` | every string used as dict key; attached to each endpoint as `response_fields` |

`SourceInsights` is passed to: Analyst, Inquirer, Designer, Coder, Healer
(rendered via `render_hints_for_prompt`). It is **not** passed to the
Explorer scraper (which reads the live DOM) or the Explorer labeler LLM
(skipped to keep the small labeler call cheap).

The UI shows everything extracted in a persistent expandable panel at the top
of every screen (rendered by `_render_source_panel()` in `Home.py`).

---

## 6. Per-step model recommendations (measured on the bundled scenarios)

Measured in `eval/*.py`. For most steps the four models tested
(`gpt-4.1-mini`, `gpt-4.1`, `claude-sonnet-4-6`, `claude-haiku-4-5`) all hit
100% on quality, so the decision collapses to cost:

- **Default everywhere**: `gpt-4.1-mini` (~$0.007 / step, fast).
- **Healer only**: bump to `gpt-4.1` or `claude-haiku` (mini drops to 83% on
  diagnosis — the only place real divergence appeared).
- Gemini wasn't measurable on a free-tier key (quota = 0 for Pro).

Full per-step tables live in
`/Users/gourav/Work/QA Automation Tool/LLM_Calls_Analysis.pdf`.

---

## 7. PRD format research (measured on 10 variants)

Tested 10 PRD shapes on the Pencil/invoice scenario with `gpt-4.1-mini`.
Winner: **sectioned-with-headings (V2)** or **JSON spec (V10)**.

Recommended template:
```markdown
## App URL
http://localhost:3000/#/login

## Credentials
username: <value>
password: <value>

## Sample form data
<kebab-key>: <value>
…

## Acceptance Criteria
- <one specific, testable bullet>
- …
```

Anti-patterns: Gherkin (took 9 min for 0 passes), "As a user / I want / so that"
(inflates criteria count), verbose backstory (pulls business text in as
criteria), single-sentence terse (Analyst flags ambiguity).

Full report: `/Users/gourav/Work/QA Automation Tool/PRD_Format_Experiment_Report.pdf`.

---

## 8. Key files map

```
qa_agent/
├── pipeline.py            ← phase_a / phase_b_explore / phase_b_finish, guessed_urls, unreachable_urls
├── prompts.py             ← all system prompts for the LLM steps
├── source.py              ← GitHub fetch + 4 regex scanners + render_hints_for_prompt
├── models.py              ← Pydantic models passed between steps
├── llm.py                 ← OpenAI client wrapper (structured + free-text)
├── events.py              ← EventBus for per-step live log
├── prd.py                 ← PRD file uploader (PDF/DOCX/TXT/MD)
└── steps/
    ├── analyst.py inquirer.py designer.py
    ├── orchestrator.py validator.py executor.py reporter.py
    ├── explorer.py        ← scraper + labeler; now scrapes <option>s
    ├── coder.py           ← code() + _post_process deterministic regex fixes
    └── healer.py          ← interprets pytest error strings; gets source_insights
Home.py                    ← Streamlit UI (input → questions → verify_urls → run → done)
prompts/                   ← markdown mirrors of each step's system prompt
eval/
├── analyst_eval.py + analyst_cases.py
├── inquirer_eval.py + inquirer_cases.py
├── designer_eval.py + designer_cases.py
├── explorer_eval.py + explorer_cases.py
├── coder_eval.py + coder_cases.py
├── healer_eval.py + healer_cases.py
└── format_experiment.py   ← 10-PRD-variant experiment runner
build_llm_pdf.py           ← LLM-comparison PDF builder (from xlsx)
build_format_pdf.py        ← PRD-format experiment PDF builder
conftest.py                ← pytest snap fixture + ignore_https_errors
CONTEXT.md                 ← original project notes (predates this session)
CONTEXT_HANDOFF.md         ← this file
```

---

## 9. Open issues / next work

1. **Test 3 (table arithmetic)** — the Coder still struggles with the
   "read qty → save invoice → re-read qty → assert decremented by 1" pattern.
   The validator drops it to `pytest.fail` because `get_by_role("cell")` isn't
   in the SiteMap (the Explorer captures rows + columnheaders, not cells).
   The Coder prompt has a worked example (rule 7c) but gpt-4.1-mini doesn't
   apply it reliably. **Likely fix**: include cell-text snippets in the
   Explorer's SiteMap for the row whose name matches the relevant item,
   OR switch the Coder to `gpt-4.1` for table-arithmetic tests only.

2. **Explorer labeler doesn't receive `source_insights`** (intentional —
   labeler is cheap and doesn't need it). If you ever want full consistency,
   it's a 5-line change in `explorer.py`.

3. **Healer hot-reload safety** — fixed once (SiteMap rebuilt via
   `model_validate` to dodge Streamlit class-identity issues). Same pattern
   may need applying elsewhere if other Pydantic class-identity errors appear.

4. **Pencil bug user observed** — Test 2 succeeded after the dropdown fix +
   `.select_option(value=...)`/regex match guidance. Test 2 then failed only
   on the *backend assertion* because the Coder invented field names
   (`invoice_number` vs real `number`). Fixed by adding `response_fields` to
   source extraction + tightening the prompt. Should pass on the next run.

5. **Cell scraping** (real fix for Test 3): extend
   `explorer.py → _scrape_elements` to capture every `<td>` text per row,
   then the Coder can read values directly from the SiteMap.

6. **README in the repo is stale** — it still references `pip install` instead
   of `uv sync`. Low priority cleanup.

---

## 10. Key decisions / lessons learned this session

- **Prompts alone are not reliable for small models.** Every "fix" that lived
  only in the prompt eventually slipped. Pair every behavioural rule with a
  deterministic code check (regex, AST, post-filter).
- **GitHub source is treated as authoritative** in the Coder/Healer prompts —
  routes, HTML tags, and JSON field names are NOT to be transformed.
- **The pause-and-ask pattern is now the default** for URLs the planner can't
  source. The pause shows GitHub routes as quick-copy options.
- **Streamlit hot-reload causes Pydantic class-identity issues.** Round-trip
  via `model_validate({...})` to defuse.
- **PRDs in V2 sectioned format produce the cleanest pipeline behaviour.**
  Don't write PRDs in Gherkin.
- **Per-step model selection isn't wired yet.** Currently one global
  `QA_AGENT_MODEL`. A small change to `llm.py` would let you pick per-step.

---

## 11. Best process for a new chat

1. Open the new chat in this same working directory.
2. Paste this entire file (or attach it as a file) as your first message.
3. Tell the new agent: *"Read CONTEXT_HANDOFF.md first, then ask me what I want
   to work on."*
4. Point at specific files when discussing changes — e.g.
   *"In `qa_agent/steps/healer.py`, the error-interpretation rule…"*.
5. If the change is non-trivial, ask the agent to commit + push after each
   change with a meaningful message.

That's everything. Good luck.
