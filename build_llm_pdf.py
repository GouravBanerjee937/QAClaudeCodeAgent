# -*- coding: utf-8 -*-
"""Build a colourful, manager-ready PDF from the two LLM Calls tabs.
ALL values are transcribed verbatim from the spreadsheet — nothing invented.
Rendered to PDF via Playwright Chromium (prints background colours)."""
import html
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = "/Users/gourav/Work/QA Automation Tool/LLM_Calls_Analysis.pdf"

def esc(s): return html.escape(str(s))

# ---- colour helpers -------------------------------------------------------
def rating_class(v):
    t = str(v).strip()
    if t.startswith("Med-High"): return "r-medhigh"
    if t.startswith("High"): return "r-high"
    if t.startswith("Medium"): return "r-medium"
    if t == "N/A" or t == "—" or t == "": return "r-na"
    return ""

LOWER_BETTER = {"leaks", "over-ask", "misses", "scope-viol", "invented", "leak"}
def metric_class(col, v):
    t = str(v).strip()
    col = col.lower()
    if t in ("", "—"): return ""
    if col in ("lat", "lat.", "latency", "est. cost", "est cost"): return "m-neutral"
    try:
        f = float(t)
    except ValueError:
        return ""
    if col in LOWER_BETTER:
        return "m-good" if f == 0 else "m-bad"
    if col == "ratio":
        return "m-good" if f == 1.0 else "m-warn"
    return "m-good" if f >= 1.0 else "m-warn"

def qual_table(headers, rows):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = ""
    for r in rows:
        cells = ""
        for i, c in enumerate(r):
            cls = "model" if i == 0 else rating_class(c)
            cells += f'<td class="{cls}">{esc(c)}</td>'
        body += f"<tr>{cells}</tr>"
    return f'<table class="grid"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'

def metric_table(headers, rows):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = ""
    for r in rows:
        cells = ""
        for i, c in enumerate(r):
            if i == 0:
                cells += f'<td class="model">{esc(c)}</td>'
            else:
                cells += f'<td class="{metric_class(headers[i], c)}">{esc(c)}</td>'
        body += f"<tr>{cells}</tr>"
    return f'<table class="grid"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'

def crit_list(items):
    lis = "".join(f"<li><span class='cn'>{esc(n)}</span><span class='cw'>{esc(w)}</span></li>" for n, w in items)
    return f"<ul class='crit'>{lis}</ul>"

def callout(text, kind="pick"):
    label = {"pick": "PICK", "verdict": "VERDICT", "bottom": "BOTTOM LINE"}[kind]
    return f"<div class='callout {kind}'><span class='tag'>{label}</span><span>{esc(text)}</span></div>"

# ==========================================================================
# DATA — verbatim from the sheet
# ==========================================================================

# ---- RESULTS: Qualitative summary -----------------------------------------
qual_summary_head = ["Step", "LLM?", "Library/tool it uses", "CHOICE 1", "CHOICE 2", "CHOICE 3"]
qual_summary = [
    ["analyst", "✅ LLM", "—", "GPT 4.1 MINI", "", ""],
    ["inquirer", "✅ LLM", "—", "GPT 4.1 MINI", "CLAUDE HAIKU 4.5", "CLAUDE SONNET 4.6"],
    ["designer", "✅ LLM", "—", "CLAUDE SONNET 4.6", "GPT 4.1", ""],
    ["orchestrator", "❌", "plain Python (regex)", "N/A", "", ""],
    ["explorer", "✅ LLM (labeling only)", "Playwright (sync_playwright, Chromium)", "GPT 4.1 MINI", "CLAUDE HAIKU 4.5", ""],
    ["coder", "✅ LLM", "—", "CLAUDE SONNET 4.6", "GPT 4.1", ""],
    ["validator", "❌", "plain Python (ast)", "N/A", "", ""],
    ["healer", "✅ LLM", "Playwright (sync_playwright, Chromium)", "CLAUDE SONNET 4.6", "GPT 4.1", ""],
    ["reporter", "❌", "plain Python", "N/A", "", ""],
]

# ---- RESULTS: Quantitative summary ----------------------------------------
quant_summary_head = ["Step", "Use", "Why"]
quant_summary = [
    ["Analyst", "gpt-4.1-mini (Sonnet if PRDs messy)", "tied; Sonnet only for recall on hard docs"],
    ["Inquirer", "gpt-4.1-mini", "tied"],
    ["Designer", "gpt-4.1-mini", "tied"],
    ["Explorer-labeler", "gpt-4.1-mini", "tied"],
    ["Coder", "gpt-4.1-mini", "tied (clean cases)"],
    ["Healer", "gpt-4.1 (or claude-haiku)", "only step where mini drops (83% fixed)"],
]

# ---- per-step QUALITATIVE ---------------------------------------------------
analyst_q_crit = [
    ("C1 — Instruction-following & classification precision", "The hardest part of the prompt is the ✅/❌ rule: extract data to type/navigate (provided_values) but never UI labels/button text. Misclassify and you poison the downstream answers dict."),
    ("C2 — Structured-output (schema) reliability", "The code calls .parse() into a Pydantic TestSpec. A malformed/partial object raises and kills the run — so consistent schema conformance is non-negotiable."),
    ("C4 — Restraint / hallucination resistance", "The prompt explicitly says “if you have to guess, do NOT — list it in notes.” Over-eager models invent criteria/values; the whole design philosophy depends on calibrated humility."),
    ("C5 — Cost & latency", "One call per run, no reasoning needed → there's no justification for paying frontier-reasoning prices. Cheap+fast wins unless accuracy demands otherwise."),
]
analyst_q_head = ["LLM", "C1 Instruction/Classification", "C2 Structured output", "C3 Comprehension/Long-ctx", "C4 Restraint", "C5 Cost & latency"]
analyst_q_rows = [
    ["GPT-4.1 mini (repo default)", "High — handles the data-vs-label rule well", "High — native parse, very reliable", "Med-High — solid; can miss subtle items in very long/messy PRDs", "Med-High — usually defers, occasionally over-extracts", "High — cheapest+fastest of the strong options"],
    ["GPT-4.1 (full)", "High — best-in-family rule adherence", "High — native parse", "High — better on long/ambiguous text", "High — better calibrated", "Medium — pricier/slower than mini"],
    ["Claude Sonnet (latest)", "High — excellent at nuanced rules", "High* — strong JSON-schema/tool output, needs adapter", "High — robust on messy docs", "High — strongest “flag, don't guess” behavior", "Medium — mid-tier cost"],
    ["Claude Haiku (latest)", "Med-High — good, less reliable on edge cases", "Med-High* — adapter needed", "Medium — fine for short/clean PRDs", "Med-High — inherits Claude restraint", "High — very cheap/fast"],
    ["Gemini 2.5 Flash", "Med-High — generally follows rules", "High* — supports response schema, adapter needed", "High — huge context, great for big PDFs", "Medium — can be more eager to fill gaps", "High — very cheap, large context"],
]
analyst_bottom = "Keep GPT-4.1 mini unless you observe label/data misclassification or missed criteria in real runs.  |  Upgrade trigger → GPT-4.1 or Claude Sonnet (Sonnet if restraint/ambiguity-flagging is your pain point).  |  Big-document trigger → Gemini 2.5 Flash for cheap long-context."

inquirer_intro = "The Inquirer's job is mostly restraint: ask only for values not already in the spec, never for UI labels, with correct field types. Input is small/structured, so comprehension and long-context barely matter."
inquirer_q_crit = [
    ("C1 — Restraint / no over-asking", "Biggest failure mode is asking for things already in provided_values or asking for button labels. Over-asking nags the user and breaks the “don't guess” UX."),
    ("C2 — Gap coverage", "Must still catch genuinely-missing values (esp. credentials, referenced-but-unspecified URLs) or the run stalls later."),
    ("C3 — Field-typing & key hygiene", "kind ∈ {text,password,url,email}, password for secrets, kebab-case keys, one-line reason. Wrong kind → wrong UI widget (e.g. password shown in clear)."),
    ("C4 — Structured-output reliability", "Emits a Questions object (empty list when complete). Malformed → run dies."),
    ("C5 — Cost & latency", "Tiny task; cheap/fast should win."),
]
inquirer_q_head = ["LLM", "C1 Restraint", "C2 Gap coverage", "C3 Field-typing/keys", "C4 Structured output", "C5 Cost & latency"]
inquirer_q_rows = [
    ["GPT-4.1 mini", "Med-High", "High", "High", "High", "High"],
    ["GPT-4.1", "High", "High", "High", "High", "Medium"],
    ["Claude Sonnet 4.6", "High — least likely to over-ask", "High", "High", "High*", "Medium"],
    ["Claude Haiku 4.5", "Med-High", "Med-High", "Med-High", "Med-High*", "High"],
    ["Gemini 2.5 Flash", "Medium — more eager, tends to over-ask", "High", "Med-High", "High*", "High"],
    ["Gemini 2.5 Pro", "Med-High", "High", "High", "High*", "Medium"],
]
inquirer_pick = "GPT-4.1 mini or Claude Haiku (cheap); upgrade to Claude Sonnet if you see it over-asking."

designer_intro = "The most judgment-heavy step. The dominant risk is scope creep (the prompt has an explicit “forbidden additions” list) and producing steps concrete enough for the Coder."
designer_q_crit = [
    ("C1 — Coverage & criterion-mapping", "One TestCase per acceptance criterion, full end-to-end flow (incl. login). Missed mapping = missing coverage."),
    ("C2 — Scope discipline (restraint)", "Prompt forbids inventing extra checks (“field is required”, “no error shown”, title/meta, a11y). Over-eager models add these and create false failures."),
    ("C3 — Step concreteness & writability", "Gherkin must name buttons/fields/expected text and use {key} refs so the Coder can implement it from steps alone."),
    ("C4 — URL & placeholder discipline", "Never invent domains; absolute /paths; {key} placeholders where a value is needed. Bad URLs trigger the new pause / wasted runs."),
    ("C5 — Cost & latency", "Quality matters most here, so paying a bit more is justified."),
]
designer_q_head = ["LLM", "C1 Coverage/mapping", "C2 Scope discipline", "C3 Step concreteness", "C4 URL/placeholder", "C5 Cost & latency"]
designer_q_rows = [
    ["GPT-4.1 mini", "Med-High", "Medium — sometimes adds “nice-to-have” checks", "Med-High", "Med-High", "High"],
    ["GPT-4.1", "High", "Med-High", "High", "High", "Medium"],
    ["Claude Sonnet 4.6", "High", "High — best at not over-engineering", "High", "High", "Medium"],
    ["Claude Haiku 4.5", "Medium", "Med-High", "Medium", "Med-High", "High"],
    ["Gemini 2.5 Flash", "Med-High", "Medium — eager to add assertions", "Med-High", "Medium", "High"],
    ["Gemini 2.5 Pro", "High", "Med-High", "High", "Med-High", "Medium"],
]
designer_pick = "Claude Sonnet 4.6 or GPT-4.1 — this is a step where the stronger model earns its cost. Sonnet especially if scope-creep is your pain point."

explorer_intro = "A simple, high-frequency call. The code even works around this LLM silently dropping elements (structural roles bypass it). So fidelity and faithfulness matter far more than intelligence."
explorer_q_crit = [
    ("C1 — Fidelity / completeness (no drop)", "It must return every input element. Dropping one hides a real DOM element from the Coder → spurious # NEEDS:. This is the documented failure here."),
    ("C2 — Faithfulness / no-invention", "Must reuse the exact role+name and never add elements; the prompt says “do not invent.”"),
    ("C4 — Structured-output reliability", "Returns a _LabeledElements list."),
    ("C5 — Cost & latency", "Runs per page; cheapest reliable model wins. A reasoning model is pure waste here."),
]
explorer_q_head = ["LLM", "C1 No-drop fidelity", "C2 No-invention", "C3 Label usefulness", "C4 Structured output", "C5 Cost & latency"]
explorer_q_rows = [
    ["GPT-4.1 mini", "Med-High — can drop on big lists", "High", "High", "High", "High"],
    ["GPT-4.1", "High", "High", "High", "High", "Medium"],
    ["Claude Sonnet 4.6", "High", "High", "High", "High*", "Medium"],
    ["Claude Haiku 4.5", "Med-High", "High", "Med-High", "Med-High*", "High"],
    ["Gemini 2.5 Flash", "Medium — likes to summarize/drop", "Med-High", "Med-High", "High*", "High"],
    ["Gemini 2.5 Pro", "Med-High", "High", "High", "High*", "Medium"],
]
explorer_pick = "GPT-4.1 mini or Claude Haiku. Never a reasoning model. (The no-drop weakness is partly mitigated already in code.)"

coder_intro = "Pure code generation (free-text, not structured), with ~14 hard rules. This is the most demanding step and the one where a coding-tuned model most justifies its price."
coder_q_crit = [
    ("C1 — Code correctness & Playwright fluency", "Must emit valid, runnable code: right verb per role (.fill/.select_option/.check/.click), expect() auto-waits, the table-row/.nth() pattern."),
    ("C3 — Rule/format adherence", "Exact signature, snap() after actions, role-based only (no CSS/XPath), exact=True, no markdown fences, no double-.fill(), no lambda. Many rules = many ways to slip."),
    ("C5 — Cost & latency", "Quality dominates; spending more here is defensible."),
]
coder_q_head = ["LLM", "C1 Code correctness", "C2 Grounding discipline", "C3 Rule/format adherence", "C4 Long-context", "C5 Cost & latency"]
coder_q_rows = [
    ["GPT-4.1 mini", "Med-High", "Med-High", "Med-High — occasionally slips a rule (fences/exact)", "Med-High", "High"],
    ["GPT-4.1", "High", "High", "High", "High", "Medium"],
    ["Claude Sonnet 4.6", "High — strong coder", "High", "High", "High", "Medium"],
    ["Claude Haiku 4.5", "Medium", "Med-High", "Medium", "Medium", "High"],
    ["Gemini 2.5 Flash", "Medium", "Medium", "Medium", "High (big context)", "High"],
    ["Gemini 2.5 Pro", "High", "Med-High", "Med-High", "High", "Medium"],
]
coder_pick = "Claude Sonnet 4.6 or GPT-4.1. Avoid the cheapest models here if you want pass-on-first-try and fewer heal cycles."

healer_intro = "Like the Coder, but with a diagnosis front-end: read a traceback, infer the real cause, rewrite. It's the one step where reasoning genuinely helps, and it runs only on failures (fewer calls → a pricier model is more affordable)."
healer_q_crit = [
    ("C1 — Failure diagnosis / reasoning", "Must read the pytest failure and infer the true root cause (wrong locator? timing? bad assertion?) rather than reshuffle code."),
    ("C2 — Grounding discipline (fresh SiteMap)", "Must adopt the fresh SiteMap and not reintroduce the locator that just failed."),
    ("C3 — Code correctness & Playwright fluency", "Same bar as the Coder for the rewrite."),
    ("C4 — Rule/format adherence", "Same hard rules; output pure code, no fences."),
    ("C5 — Cost & latency", "Failure-only → low call volume → a stronger/pricier model is easy to justify."),
]
healer_q_head = ["LLM", "C1 Diagnosis/reasoning", "C2 Grounding (fresh map)", "C3 Code correctness", "C4 Rule/format", "C5 Cost & latency"]
healer_q_rows = [
    ["GPT-4.1 mini", "Medium", "Med-High", "Med-High", "Med-High", "High"],
    ["GPT-4.1", "High", "High", "High", "High", "Medium"],
    ["Claude Sonnet 4.6", "High — strong diagnostic reasoning", "High", "High", "High", "Medium"],
    ["Claude Haiku 4.5", "Medium", "Med-High", "Medium", "Medium", "High"],
    ["Gemini 2.5 Flash", "Medium", "Medium", "Medium", "Medium", "High"],
    ["Gemini 2.5 Pro", "High", "Med-High", "High", "Med-High", "Medium"],
]
healer_pick = "Claude Sonnet 4.6 or GPT-4.1. This is also the one place a dedicated reasoning model (o-series / GPT-5-thinking / Gemini 3 Pro) could be worth a measured trial, since diagnosis rewards reasoning."

crossstep = [
    "Cheap+faithful jobs (Inquirer, Explorer-labeler): GPT-4.1 mini / Claude Haiku.",
    "Judgment & code jobs (Designer, Coder, Healer): Claude Sonnet 4.6 / GPT-4.1 — worth the spend.",
    "A mixed-model pipeline is likely optimal: small model for labeler/inquirer, strong model for designer/coder/healer. The repo already supports per-call model choice via QA_AGENT_MODEL (currently global) — making it per-step would be a small change if you want to act on this.",
]

# ---- per-step QUANTITATIVE --------------------------------------------------
analyst_qn_crit = [
    ("C1 precision", "of the values it pulled out, how many were correct. Why: a wrong value gets typed into the test later."),
    ("C1 recall", "of the values it should have pulled out, how many it got. Why: a missed value means the test is missing data."),
    ("leaks", "did it wrongly grab button/label text as a “value”? Why: button labels come from the live page, not the PRD — grabbing them poisons the data the test types in."),
    ("C3 criteria", "did it capture every acceptance criterion (the things to test)? Why: a missed criterion = a silently missing test."),
    ("C4 restraint", "when something was missing, did it flag it instead of inventing? Why: the whole design is “don't guess.”"),
]
analyst_qn_head = ["Model", "parse", "C1 prec", "C1 rec", "leaks", "C3 criteria", "C4 restraint", "lat", "est. cost"]
analyst_qn_rows = [
    ["gpt-4.1-mini", "1.0", "1.0", "1.0", "0.0", "0.72", "0.83", "4.08s", "$0.0069"],
    ["gpt-4.1", "1.0", "1.0", "1.0", "0.0", "0.83", "1.0", "2.94s", "$0.0357"],
    ["claude-sonnet-4-6", "1.0", "1.0", "1.0", "0.0", "0.93", "1.0", "8.17s", "$0.1234"],
    ["claude-haiku-4-5", "1.0", "1.0", "1.0", "0.0", "0.73", "1.0", "3.11s", "$0.0374"],
]
analyst_verdict = "gpt-4.1-mini default; Sonnet if PRDs are long/messy (its criteria-recall edge is the only real quality gap found here)."

inquirer_qn_crit = [
    ("restraint", "of the questions it asked, how many were actually needed. Why: nagging you for things already known is annoying and pointless."),
    ("over-ask", "count of questions it shouldn't have asked. Why: same — measures wasted questions directly."),
    ("gap-rec (coverage)", "of the genuinely-missing values, how many it asked for. Why: if it doesn't ask, the run stalls later with no data."),
    ("typing", "did it tag each question correctly (password as “password”, URL as “url”) and name keys cleanly? Why: wrong type shows the wrong input box (e.g. a password in plain text)."),
]
inquirer_qn_head = ["Model", "parse", "restraint", "over-ask", "gap-rec", "typing", "lat", "est. cost"]
inquirer_qn_rows = [
    ["gpt-4.1-mini", "1.0", "1.0", "0.0", "1.0", "1.0", "1.85s", "$0.0034"],
    ["gpt-4.1", "1.0", "1.0", "0.0", "1.0", "1.0", "2.10s", "$0.0168"],
    ["claude-sonnet-4-6", "1.0", "1.0", "0.0", "1.0", "1.0", "3.66s", "$0.0651"],
    ["claude-haiku-4-5", "1.0", "1.0", "0.0", "1.0", "1.0", "1.72s", "$0.0196"],
]
inquirer_verdict = "gpt-4.1-mini — all tie on quality; cheapest wins."

designer_qn_crit = [
    ("coverage", "did it create a test for every acceptance criterion? Why: missing a criterion = untested behavior."),
    ("ratio", "number of tests ÷ number of criteria (ideal ≈ 1). Why: catches over-splitting one requirement into many redundant micro-tests."),
    ("scope-viol", "count of “extra” checks it invented that nobody asked for (e.g. “verify field is required”). Why: the prompt forbids scope creep — invented checks cause false failures."),
    ("concrete", "are the steps detailed enough to actually code from (named buttons, ≥3 steps, an assertion)? Why: vague steps can't be turned into a real test."),
    ("url-ok", "are the page URLs real paths, not invented domains? Why: a made-up URL sends the browser nowhere."),
    ("leak", "did it type a literal value (e.g. “Gourav”) instead of a {placeholder}? Why: placeholders keep credentials/data swappable and out of the plan."),
]
designer_qn_head = ["Model", "parse", "coverage", "ratio", "scope-viol", "concrete", "url-ok", "leak", "lat", "est. cost"]
designer_qn_rows = [
    ["gpt-4.1-mini", "1.0", "1.0", "1.0", "0.0", "1.0", "1.0", "0.0", "2.56s", "$0.0081"],
    ["gpt-4.1", "1.0", "1.0", "1.0", "0.0", "1.0", "1.0", "0.0", "2.19s", "$0.0365"],
    ["claude-sonnet-4-6", "1.0", "1.0", "1.0", "0.0", "1.0", "1.0", "0.0", "7.22s", "$0.1310"],
    ["claude-haiku-4-5", "1.0", "1.0", "1.0", "0.0", "1.0", "1.0", "0.0", "3.57s", "$0.0398"],
]
designer_verdict = "gpt-4.1-mini — all tie (even resisted the scope-creep bait)."

explorer_qn_crit = [
    ("fidelity", "did it return every element it was given? Why: this step's known failure is silently dropping elements — a dropped element disappears from the test's view of the page."),
    ("invented", "did it add elements that weren't there? Why: a hallucinated element leads to a locator that doesn't exist."),
    ("purpose", "did it give each element a usable one-line description? Why: that description is the hint the code-writer reads."),
]
explorer_qn_head = ["Model", "parse", "fidelity", "invented", "purpose", "lat", "est. cost"]
explorer_qn_rows = [
    ["gpt-4.1-mini", "1.0", "1.0", "0.0", "1.0", "4.99s", "$0.0052"],
    ["gpt-4.1", "1.0", "1.0", "0.0", "1.0", "2.79s", "$0.0266"],
    ["claude-sonnet-4-6", "1.0", "1.0", "0.0", "1.0", "7.21s", "$0.0855"],
    ["claude-haiku-4-5", "1.0", "1.0", "0.0", "1.0", "3.48s", "$0.0291"],
]
explorer_verdict = "gpt-4.1-mini — perfect fidelity even on the 36-element page; the feared “drop” bug didn't reproduce."

coder_qn_crit = [
    ("syntax", "does the generated Python actually parse? Why: broken code can't run at all."),
    ("grounded", "does every locator it used actually exist on the page (checked by the pipeline's own validator)? Why: a made-up locator fails at runtime — this is the #1 reliability risk."),
    ("misses", "count of invented locators. Why: the raw number behind “grounded.”"),
    ("needs-ok", "when an element was genuinely absent, did it write a # NEEDS: note instead of inventing one? Why: honest “I can't do this” beats a fake that silently fails."),
    ("rules", "did it follow the ~7 hard rules (correct function signature, screenshot calls, role-based locators only/no XPath, exact=True, right verb for dropdowns, no lambda)? Why: each rule prevents a specific, known runtime failure."),
]
coder_qn_head = ["Model", "syntax", "grounded", "misses", "needs-ok", "rules", "lat", "est. cost"]
coder_qn_rows = [
    ["gpt-4.1-mini", "1.0", "1.0", "0.0", "1.0", "1.0", "3.47s", "$0.0071"],
    ["gpt-4.1", "1.0", "1.0", "0.0", "1.0", "1.0", "4.25s", "$0.0357"],
    ["claude-sonnet-4-6", "1.0", "1.0", "0.0", "1.0", "1.0", "5.06s", "$0.0657"],
    ["claude-haiku-4-5", "1.0", "1.0", "0.0", "1.0", "1.0", "2.52s", "$0.0237"],
]
coder_verdict = "gpt-4.1-mini — all tie on these cases (login, combobox, missing-element). Hard patterns (table arithmetic, disambiguation) not yet stressed."

healer_qn_crit = [
    ("syntax", "does the rewritten code parse? Why: same as Coder."),
    ("grounded", "does the fix use real elements from the fresh page scan? Why: the fix must match reality, not the stale page."),
    ("fixed", "did the rewrite actually correct the specific bug (not just shuffle code)? Why: this is the whole point of healing — it's the one criterion that needs real diagnosis (read the error, understand the cause)."),
    ("rules", "same hard-rule adherence as the Coder. Why: the fix must still be valid test code."),
]
healer_qn_head = ["Model", "syntax", "grounded", "fixed", "rules", "lat", "est. cost"]
healer_qn_rows = [
    ["gpt-4.1-mini", "1.0", "0.83", "0.83", "1.0", "3.14s", "$0.0027"],
    ["gpt-4.1", "1.0", "1.0", "1.0", "1.0", "1.91s", "$0.0140"],
    ["claude-sonnet-4-6", "1.0", "1.0", "1.0", "1.0", "2.88s", "$0.0272"],
    ["claude-haiku-4-5", "1.0", "1.0", "1.0", "1.0", "1.59s", "$0.0095"],
]

STEPS = [
    {"name": "1. Analyst", "sub": "PRD → TestSpec",
     "q_intro": "Criteria — and why each matters for the Analyst specifically.",
     "q_crit": analyst_q_crit, "q_head": analyst_q_head, "q_rows": analyst_q_rows, "bottom": analyst_bottom,
     "qn_intro": "Criteria: C1 classification precision (data-vs-label), C2 valid JSON, C3 criteria recall, C4 restraint (flag gaps, don't invent), C5 cost/latency.",
     "qn_crit": analyst_qn_crit, "qn_head": analyst_qn_head, "qn_rows": analyst_qn_rows, "verdict": analyst_verdict},
    {"name": "2. Inquirer", "sub": "TestSpec → only the truly missing values",
     "q_intro": inquirer_intro, "q_crit": inquirer_q_crit, "q_head": inquirer_q_head, "q_rows": inquirer_q_rows, "pick": inquirer_pick,
     "qn_intro": "Criteria: restraint (don't over-ask), gap coverage, field-typing/keys, structured output, cost.",
     "qn_crit": inquirer_qn_crit, "qn_head": inquirer_qn_head, "qn_rows": inquirer_qn_rows, "verdict": inquirer_verdict},
    {"name": "3. Designer", "sub": "TestSpec+answers → Gherkin TestPlan",
     "q_intro": designer_intro, "q_crit": designer_q_crit, "q_head": designer_q_head, "q_rows": designer_q_rows, "pick": designer_pick,
     "qn_intro": "Criteria: coverage, scope discipline (no invented checks), concreteness, URL/placeholder discipline, cost.",
     "qn_crit": designer_qn_crit, "qn_head": designer_qn_head, "qn_rows": designer_qn_rows, "verdict": designer_verdict},
    {"name": "4. Explorer-labeler", "sub": "(role,name) pairs → +purpose",
     "q_intro": explorer_intro, "q_crit": explorer_q_crit, "q_head": explorer_q_head, "q_rows": explorer_q_rows, "pick": explorer_pick,
     "qn_intro": "Criteria: fidelity (no drops), no-invention, purpose quality, structured output, cost.",
     "qn_crit": explorer_qn_crit, "qn_head": explorer_qn_head, "qn_rows": explorer_qn_rows, "verdict": explorer_verdict},
    {"name": "5. Coder", "sub": "TestCase+SiteMap → pytest-playwright code",
     "q_intro": coder_intro, "q_crit": coder_q_crit, "q_head": coder_q_head, "q_rows": coder_q_rows, "pick": coder_pick,
     "qn_intro": "Criteria: syntax, grounding (no invented locators), NEEDS handling, hard-rule adherence, cost. (Grounding scored by the pipeline's own validator.)",
     "qn_crit": coder_qn_crit, "qn_head": coder_qn_head, "qn_rows": coder_qn_rows, "verdict": coder_verdict},
    {"name": "6. Healer", "sub": "broken code + failure → fixed code",
     "q_intro": healer_intro, "q_crit": healer_q_crit, "q_head": healer_q_head, "q_rows": healer_q_rows, "pick": healer_pick,
     "qn_intro": "Criteria: syntax, grounding (vs fresh SiteMap), fixed (actually corrects the bug), rules, cost.",
     "qn_crit": healer_qn_crit, "qn_head": healer_qn_head, "qn_rows": healer_qn_rows, "verdict": None},
]

# ==========================================================================
# Build HTML
# ==========================================================================
def step_html(s):
    parts = [f'<div class="step"><div class="step-head"><span class="step-name">{esc(s["name"])}</span><span class="step-sub">{esc(s["sub"])}</span></div>']
    # Qualitative
    parts.append('<div class="phase phase-q"><span class="phase-tag tag-q">QUALITATIVE</span>'
                 f'<p class="intro">{esc(s["q_intro"])}</p>')
    parts.append("<div class='crit-title'>Criteria &amp; why they matter</div>")
    parts.append(crit_list(s["q_crit"]))
    parts.append(qual_table(s["q_head"], s["q_rows"]))
    if s.get("bottom"): parts.append(callout(s["bottom"], "bottom"))
    if s.get("pick"): parts.append(callout(s["pick"], "pick"))
    parts.append("</div>")
    # Quantitative
    parts.append('<div class="phase phase-n"><span class="phase-tag tag-n">QUANTITATIVE (measured)</span>'
                 f'<p class="intro">{esc(s["qn_intro"])}</p>')
    parts.append("<div class='crit-title'>What each measured column means</div>")
    parts.append(crit_list(s["qn_crit"]))
    parts.append(metric_table(s["qn_head"], s["qn_rows"]))
    if s.get("verdict"): parts.append(callout(s["verdict"], "verdict"))
    parts.append("</div></div>")
    return "".join(parts)

steps_html = "".join(step_html(s) for s in STEPS)
cross_html = "".join(f"<li>{esc(c)}</li>" for c in crossstep)

CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color:#1f2733; margin:0; font-size:10.5px; line-height:1.45; }
.cover { background: linear-gradient(120deg,#4338ca 0%,#6d28d9 45%,#0ea5e9 100%); color:#fff; padding:34px 30px; border-radius:14px; margin-bottom:18px; }
.cover h1 { margin:0 0 6px; font-size:26px; letter-spacing:.3px; }
.cover .subtitle { font-size:12.5px; opacity:.95; }
.cover .meta { margin-top:14px; font-size:10px; opacity:.9; }
.cover .pill { display:inline-block; background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.35); padding:3px 10px; border-radius:20px; margin-right:6px; }
h2.section { font-size:16px; color:#4338ca; border-bottom:3px solid #c7d2fe; padding-bottom:5px; margin:22px 0 10px; }
h3.block { font-size:12.5px; margin:14px 0 6px; color:#0f172a; }
table.grid { width:100%; border-collapse:collapse; margin:8px 0 4px; font-size:9.3px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
table.grid th { background:#312e81; color:#fff; text-align:left; padding:6px 7px; font-weight:600; font-size:9px; vertical-align:top; }
table.grid td { padding:5px 7px; border-bottom:1px solid #e7eaf0; vertical-align:top; }
table.grid tbody tr:nth-child(even) td { background:#f7f8fc; }
td.model { font-weight:700; color:#3730a3; white-space:nowrap; }
/* qualitative ratings */
td.r-high { background:#dcfce7 !important; color:#166534; }
td.r-medhigh { background:#d1fae5 !important; color:#047857; }
td.r-medium { background:#fef3c7 !important; color:#92400e; }
td.r-na { color:#94a3b8; font-style:italic; }
/* quantitative metrics */
td.m-good { background:#dcfce7 !important; color:#166534; font-weight:600; }
td.m-warn { background:#fef3c7 !important; color:#92400e; font-weight:700; }
td.m-bad  { background:#fee2e2 !important; color:#991b1b; font-weight:700; }
td.m-neutral { color:#334155; }
ul.crit { list-style:none; padding:0; margin:4px 0 8px; }
ul.crit li { display:flex; gap:8px; padding:4px 8px; border-left:3px solid #818cf8; background:#f5f6ff; margin-bottom:3px; border-radius:0 6px 6px 0; }
ul.crit .cn { font-weight:700; color:#4338ca; min-width:150px; flex-shrink:0; }
ul.crit .cw { color:#374151; }
.step { border:1px solid #e2e8f0; border-radius:12px; padding:12px 14px; margin:14px 0; page-break-inside:avoid; background:#fff; }
.step-head { display:flex; align-items:baseline; gap:10px; border-bottom:2px solid #ede9fe; padding-bottom:6px; margin-bottom:8px; }
.step-name { font-size:15px; font-weight:800; color:#6d28d9; }
.step-sub { font-size:10px; color:#64748b; }
.phase { margin:8px 0 4px; padding:8px 10px; border-radius:8px; }
.phase-q { background:#faf5ff; border:1px solid #efe2ff; }
.phase-n { background:#eff6ff; border:1px solid #dbeafe; }
.phase-tag { display:inline-block; font-size:9px; font-weight:800; letter-spacing:.6px; padding:2px 9px; border-radius:5px; margin-bottom:5px; }
.tag-q { background:#7c3aed; color:#fff; }
.tag-n { background:#0284c7; color:#fff; }
.intro { font-style:italic; color:#475569; margin:3px 0 6px; }
.crit-title { font-weight:700; color:#334155; font-size:10px; text-transform:uppercase; letter-spacing:.4px; margin:6px 0 2px; }
.callout { display:flex; gap:8px; align-items:flex-start; margin:7px 0; padding:7px 10px; border-radius:8px; font-size:9.6px; }
.callout .tag { font-weight:800; font-size:8.5px; padding:2px 7px; border-radius:5px; white-space:nowrap; }
.callout.verdict { background:#ecfdf5; border:1px solid #a7f3d0; }
.callout.verdict .tag { background:#059669; color:#fff; }
.callout.pick { background:#fffbeb; border:1px solid #fde68a; }
.callout.pick .tag { background:#d97706; color:#fff; }
.callout.bottom { background:#eef2ff; border:1px solid #c7d2fe; }
.callout.bottom .tag { background:#4338ca; color:#fff; }
.cross { background:#1e1b4b; color:#e0e7ff; border-radius:12px; padding:14px 18px; margin-top:6px; }
.cross h3 { margin:0 0 6px; color:#fff; font-size:13px; }
.cross ul { margin:0; padding-left:18px; }
.cross li { margin-bottom:5px; font-size:10px; }
.note { font-size:8.5px; color:#94a3b8; margin-top:10px; }
.legend { font-size:8.6px; color:#475569; margin:4px 0 10px; }
.legend span { display:inline-block; margin-right:10px; }
.sw { display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:middle; margin-right:3px; }
"""

legend = ("<div class='legend'><b>How to read the colours:</b> "
          "<span><span class='sw' style='background:#dcfce7'></span>High / 1.0 (best)</span>"
          "<span><span class='sw' style='background:#d1fae5'></span>Med-High</span>"
          "<span><span class='sw' style='background:#fef3c7'></span>Medium / below-1.0</span>"
          "<span><span class='sw' style='background:#fee2e2'></span>worse on a lower-is-better metric</span>"
          "&nbsp; (qualitative ratings &amp; measured scores transcribed from the sheet)</div>")

HTML = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="cover">
  <h1>QA Automation Tool — LLM Calls Analysis</h1>
  <div class="subtitle">Per-step model selection: Qualitative reasoning &amp; Quantitative (measured) results</div>
  <div class="meta"><span class="pill">Source: QA Automation tool (2).xlsx</span>
  <span class="pill">Tabs: LLM Calls (Qualitative) &amp; (Quantitative)</span>
  <span class="pill">Models: GPT-4.1-mini · GPT-4.1 · Claude Sonnet 4.6 · Claude Haiku 4.5 · Gemini 2.5 Flash/Pro</span></div>
</div>

<h2 class="section">Results — Summary</h2>
{legend}
<h3 class="block">A. Qualitative summary — recommended model choices per step</h3>
{qual_table(qual_summary_head, qual_summary)}
<h3 class="block">B. Quantitative summary — measured verdict per step</h3>
{metric_table(quant_summary_head, quant_summary)}

<h2 class="section">Step-by-step detail</h2>
{steps_html}

<div class="cross">
  <h3>Cross-step takeaway</h3>
  <ul>{cross_html}</ul>
</div>
<div class="note">All ratings, scores, latencies and costs are transcribed verbatim from the spreadsheet's two LLM Calls tabs. Cell colours are presentation only and encode the values shown; no values were added or changed. Asterisks (*) appear as in the source.</div>
</body></html>"""

Path("/tmp/llm_report.html").write_text(HTML, encoding="utf-8")

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto("file:///tmp/llm_report.html")
    pg.pdf(path=OUT, format="A4", print_background=True,
           margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
    b.close()
print("WROTE", OUT)
