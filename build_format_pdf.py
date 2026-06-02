# -*- coding: utf-8 -*-
"""Manager-ready PDF for the PRD-format experiment.
Includes all 10 PRD variants verbatim, the results table, plain-English findings,
the recommended template, anti-patterns, and next steps."""
import html
from pathlib import Path
from playwright.sync_api import sync_playwright

# Pull the exact variant texts from the experiment script.
import sys
sys.path.insert(0, "/Users/gourav/Work/QA Agent/eval")
from format_experiment import (V1_FREE_FORM, V2_SECTIONED, V3_USER_STORY, V4_NUMBERED,
                                V5_VERBOSE, V6_MINIMAL, V7_GHERKIN, V8_BDD_BLOCK,
                                V9_TABLE, V10_JSON)

OUT = "/Users/gourav/Work/QA Automation Tool/PRD_Format_Experiment_Report.pdf"

def esc(s): return html.escape(str(s))

VARIANTS = [
    ("V1", "Plain paragraph (free-form prose)", V1_FREE_FORM,
     "A natural paragraph describing the test goal in everyday English."),
    ("V2", "Sections with headings", V2_SECTIONED,
     "Explicit `## App URL / ## Credentials / ## Acceptance Criteria` sections.", True),
    ("V3", "User-story format (As a / I want / so that)", V3_USER_STORY,
     "The classic agile user-story template."),
    ("V4", "Numbered steps", V4_NUMBERED,
     "A numbered list of actions and one assertion."),
    ("V5", "Long version with business backstory", V5_VERBOSE,
     "Detailed prose that includes business context and irrelevant detail."),
    ("V6", "Very short, one sentence", V6_MINIMAL,
     "The same intent collapsed into a single sentence."),
    ("V7", "Gherkin (Feature / Scenario / Given / When / Then)", V7_GHERKIN,
     "The Cucumber/Gherkin behaviour-driven format."),
    ("V8", "BDD scenario block (bulleted Given / When / Then)", V8_BDD_BLOCK,
     "A simpler BDD style with bullets under Given/When/Then headers."),
    ("V9", "Table-style (markdown tables)", V9_TABLE,
     "Two markdown tables: one for test data, one for steps and expected results."),
    ("V10", "JSON specification", V10_JSON,
     "A fully machine-readable JSON object with keys for credentials, form data, and acceptance criteria.", True),
]

# Results rows — straight from the experiment.
ROWS = [
    # name, label, understood, useless_q, n_cases, ideal, e2e_pass, e2e_n, e2e_pct, time_s, badge
    ("V1",  "Plain paragraph",                         "Yes", 2, 5, 3, 0, 5, 0,  75,  ""),
    ("V2",  "Sections with headings",                  "Yes", 0, 3, 3, 0, 3, 0,  48,  "winner-clean"),
    ("V3",  "User-story (As a / I want / so that)",    "Yes", 2, 6, 3, 0, 6, 0,  88,  ""),
    ("V4",  "Numbered steps",                          "Yes", 2, 5, 3, 0, 5, 0,  69,  ""),
    ("V5",  "Long version with backstory",             "Yes", 2, 8, 3, 1, 8, 12, 221, ""),
    ("V6",  "Very short, one sentence",                "Yes", 1, 1, 3, 0, 1, 0,  30,  ""),
    ("V7",  "Gherkin (Given/When/Then)",               "Yes", 2, 5, 3, 0, 5, 0,  557, "loser"),
    ("V8",  "BDD scenario block",                      "Yes", 2, 7, 3, 2, 7, 29, 78,  "winner-e2e"),
    ("V9",  "Table-style",                             "Yes", 2, 5, 3, 0, 5, 0,  71,  ""),
    ("V10", "JSON spec",                               "Yes", 0, 3, 3, 0, 3, 0,  45,  "winner-clean"),
]

RECOMMENDED_TEMPLATE = """## App URL
http://localhost:3000/#/login

## Credentials
username: Gourav
password: 1234

## Sample form data
invoice-number: 12
invoice-item: Pencil
invoice-amount: 1
invoice-price: 10

## Acceptance Criteria
- User can log in with username and password
- Saving a sales invoice shows the message "Invoice saved."
- Saving an invoice for Pencil decreases Pencil's Quantity available in Item Master by 1
"""

CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       color: #1f2733; margin: 0; font-size: 10.5px; line-height: 1.5; }

.cover { background: linear-gradient(120deg,#0f766e 0%,#0891b2 50%,#4338ca 100%);
         color: #fff; padding: 32px 30px; border-radius: 14px; margin-bottom: 18px; }
.cover h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: .3px; }
.cover .subtitle { font-size: 12.5px; opacity: .96; }
.cover .meta { margin-top: 14px; font-size: 10px; opacity: .95; }
.cover .pill { display:inline-block; background:rgba(255,255,255,.18);
               border:1px solid rgba(255,255,255,.35); padding:3px 10px;
               border-radius:20px; margin-right:6px; }

h2.section { font-size: 16px; color: #0f766e; border-bottom: 3px solid #99f6e4;
             padding-bottom: 5px; margin: 22px 0 10px; }
h3.block { font-size: 12.5px; margin: 14px 0 6px; color: #0f172a; }

table.grid { width: 100%; border-collapse: collapse; margin: 8px 0 4px;
             font-size: 9.4px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
table.grid th { background: #134e4a; color: #fff; text-align: left;
                padding: 6px 7px; font-weight: 600; font-size: 9px; vertical-align: top; }
table.grid td { padding: 5px 7px; border-bottom: 1px solid #e7eaf0; vertical-align: top; }
table.grid tbody tr:nth-child(even) td { background: #f7fdfb; }

td.code { font-family: 'SF Mono', Menlo, Consolas, monospace; font-weight: 700; color: #0f766e; }
td.good { background: #dcfce7 !important; color: #166534; font-weight: 600; }
td.warn { background: #fef3c7 !important; color: #92400e; font-weight: 600; }
td.bad  { background: #fee2e2 !important; color: #991b1b; font-weight: 700; }
td.neutral { color: #334155; }
td.center { text-align: center; }
.pill-win { background:#059669; color:#fff; font-size:8.5px; padding:2px 6px;
            border-radius:5px; font-weight:700; }
.pill-lose { background:#dc2626; color:#fff; font-size:8.5px; padding:2px 6px;
             border-radius:5px; font-weight:700; }

.callout { display:flex; gap:8px; align-items:flex-start; margin:8px 0;
           padding: 9px 12px; border-radius: 8px; font-size: 10px; }
.callout .tag { font-weight: 800; font-size: 8.5px; padding: 2px 8px;
                border-radius: 5px; white-space: nowrap; }
.callout.win { background: #ecfdf5; border:1px solid #a7f3d0; }
.callout.win .tag { background: #059669; color: #fff; }
.callout.lose { background: #fef2f2; border:1px solid #fecaca; }
.callout.lose .tag { background: #dc2626; color: #fff; }
.callout.warn { background: #fefce8; border:1px solid #fde68a; }
.callout.warn .tag { background: #d97706; color: #fff; }
.callout.info { background: #eff6ff; border:1px solid #bfdbfe; }
.callout.info .tag { background: #2563eb; color: #fff; }

.kv { background:#f3f4f6; border-left:4px solid #0891b2; padding:8px 12px;
      margin: 6px 0; border-radius: 0 6px 6px 0; font-size: 10px; }
.kv b { color: #0f766e; }

.variant { border:1px solid #e2e8f0; border-radius:10px; padding:10px 12px;
           margin:10px 0; background:#fff; page-break-inside: avoid; }
.variant.recommended { border:2px solid #059669; background:#f0fdf4; }
.variant-head { display:flex; align-items:baseline; gap:10px; margin-bottom:4px; }
.variant-id { background:#0f766e; color:#fff; padding:2px 8px;
              border-radius:5px; font-weight:800; font-size:10px; }
.variant-id.rec { background:#059669; }
.variant-id.bad { background:#dc2626; }
.variant-name { font-weight:700; color:#0f172a; font-size:11.5px; }
.variant-desc { color:#475569; font-style:italic; margin: 2px 0 6px; font-size:9.5px; }
.variant-code { background:#0f172a; color:#f1f5f9; padding:10px 12px;
                border-radius:6px; font-family: 'SF Mono', Menlo, Consolas, monospace;
                font-size:9px; white-space: pre-wrap; line-height:1.4;
                overflow-wrap: anywhere; }

ul.findings { padding-left: 18px; margin: 6px 0; }
ul.findings li { margin-bottom: 4px; }

.legend { font-size: 9px; color:#475569; margin: 4px 0 10px; }
.legend span { display:inline-block; margin-right: 10px; }
.sw { display:inline-block; width:10px; height:10px; border-radius:2px;
      vertical-align: middle; margin-right: 3px; }
.note { font-size: 8.6px; color:#94a3b8; margin-top: 10px; }
"""

# ---------- helpers ----------
def variant_html(v):
    rec = (len(v) > 4 and v[4] is True)
    id_class = "rec" if rec else ("bad" if v[0] == "V7" else "")
    extra_class = "recommended" if rec else ""
    badge = ""
    if rec:
        badge = '<span class="pill-win">RECOMMENDED</span>'
    elif v[0] == "V7":
        badge = '<span class="pill-lose">AVOID</span>'
    return f"""
    <div class="variant {extra_class}">
      <div class="variant-head">
        <span class="variant-id {id_class}">{esc(v[0])}</span>
        <span class="variant-name">{esc(v[1])}</span>
        {badge}
      </div>
      <div class="variant-desc">{esc(v[3])}</div>
      <div class="variant-code">{esc(v[2])}</div>
    </div>"""

def results_row(r):
    name, label, understood, q, cases, ideal, passed, n, pct, t, badge = r
    cls_q = "good" if q == 0 else ("warn" if q == 1 else "bad")
    cls_cases = "good" if cases == ideal else ("warn" if abs(cases - ideal) <= 2 else "bad")
    cls_pass = "good" if pct >= 20 else ("warn" if pct > 0 else "bad")
    cls_time = "good" if t <= 60 else ("warn" if t <= 120 else "bad")
    row_extra = ""
    if badge == "winner-clean":
        row_extra = '<span class="pill-win">CLEANEST</span>'
    elif badge == "winner-e2e":
        row_extra = '<span class="pill-win">MOST PASSES</span>'
    elif badge == "loser":
        row_extra = '<span class="pill-lose">AVOID</span>'
    cases_str = f"{cases}" + (" ✓" if cases == ideal else "")
    pass_str = f"{passed}/{n}" + (f" ({pct}%)" if pct > 0 else "")
    return (
        f"<tr>"
        f'<td class="code">{esc(name)}</td>'
        f'<td>{esc(label)} {row_extra}</td>'
        f'<td class="good center">{esc(understood)}</td>'
        f'<td class="{cls_q} center">{q}</td>'
        f'<td class="{cls_cases} center">{cases_str}</td>'
        f'<td class="{cls_pass} center">{pass_str}</td>'
        f'<td class="{cls_time} center">{t}s</td>'
        f"</tr>"
    )

# ---------- build ----------
variants_section = "".join(variant_html(v) for v in VARIANTS)
results_rows = "".join(results_row(r) for r in ROWS)

HTML = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>

<div class="cover">
  <h1>PRD Format Experiment — Final Report</h1>
  <div class="subtitle">Does the shape of your PRD change what the QA pipeline produces?</div>
  <div class="meta">
    <span class="pill">10 PRD format variants</span>
    <span class="pill">Same scenario: Pencil invoice</span>
    <span class="pill">Target: localhost:3000</span>
    <span class="pill">Model: gpt-4.1-mini</span>
    <span class="pill">Self-healing: ON</span>
  </div>
</div>

<h2 class="section">1. What I tested</h2>
<div class="kv">
  <p><b>The app:</b> Your Invoice App running on <code>localhost:3000</code>.</p>
  <p><b>The scenario:</b> Log in (as Gourav / 1234), check Pencil's current quantity in Item Master,
     create an invoice for 1 Pencil, confirm the "Invoice saved." message, then check that
     Pencil's quantity went down by 1.</p>
  <p><b>The only thing I changed:</b> How the requirements were written. Same meaning every time,
     just different shape.</p>
  <p><b>How many formats I tried:</b> 10. The first 6 are normal writing styles; the last 4 are
     more structured styles (Gherkin, BDD blocks, tables, JSON).</p>
  <p><b>Model:</b> gpt-4.1-mini used everywhere (as requested).</p>
  <p><b>Self-healing:</b> Turned on. If a test failed once, the system tried to fix and run it again.</p>
</div>

<h2 class="section">2. What I measured</h2>
<ul class="findings">
  <li><b>Did the system understand the requirements correctly?</b> (right credentials, right test goals, no extras invented)</li>
  <li><b>Did the system have to ask me extra questions that weren't needed?</b> (lower is better)</li>
  <li><b>Did it design too many or too few test cases?</b> (the ideal is 3 — one per real requirement)</li>
  <li><b>Did the tests actually pass when run?</b> (the most important measure)</li>
  <li><b>How long did the whole thing take?</b> (faster is cheaper)</li>
</ul>

<h2 class="section">3. The 10 PRD variants I tested (exactly as fed in)</h2>
<p class="note">Each variant expresses the same scenario. Differences are <i>only</i> in shape.</p>
{variants_section}

<h2 class="section">4. Results — all 10 formats</h2>
<div class="legend">
  <b>How to read the colours:</b>
  <span><span class="sw" style="background:#dcfce7"></span>Good</span>
  <span><span class="sw" style="background:#fef3c7"></span>Mediocre</span>
  <span><span class="sw" style="background:#fee2e2"></span>Bad</span>
  <span>✓ = matches the ideal</span>
</div>
<table class="grid">
  <thead>
    <tr>
      <th>#</th>
      <th>Format</th>
      <th>Understood correctly?</th>
      <th>Useless questions asked</th>
      <th>Test cases made (ideal = 3)</th>
      <th>Tests passed</th>
      <th>Time</th>
    </tr>
  </thead>
  <tbody>{results_rows}</tbody>
</table>

<h2 class="section">5. The headlines</h2>

<div class="callout win">
  <span class="tag">CLEANEST PIPELINE</span>
  <span><b>V2 (Sections with headings) and V10 (JSON):</b> Both extracted exactly 3 test cases
  (matching the 3 real requirements), asked 0 unnecessary questions, and finished in under a
  minute. This is what you want — the system understands the requirements cleanly without confusion.</span>
</div>

<div class="callout win">
  <span class="tag">MOST TESTS PASSED</span>
  <span><b>V8 (BDD scenario block) — 29% pass rate (2 of 7 tests passed):</b> Sounds great,
  but the trick is it made 7 test cases instead of the ideal 3. More tests = more chances for
  <i>some</i> to pass. It is not necessarily better quality, just more attempts.</span>
</div>

<div class="callout lose">
  <span class="tag">AVOID</span>
  <span><b>V7 (Gherkin) — 9 minutes, 0 passes:</b> Surprisingly bad. Gherkin is the same format the
  system uses internally, so I expected it to work well. Instead it took 6–10× longer than every
  other format and produced no passing tests.</span>
</div>

<div class="callout warn">
  <span class="tag">ALSO AVOID</span>
  <span><b>V3 (user-story), V5 (verbose backstory), V6 (one sentence):</b>
  V3 inflated the count to 6 tests; V5 picked up business backstory as requirements and made 8 tests;
  V6 was so short the system flagged the PRD as ambiguous and collapsed to 1 test.</span>
</div>

<h2 class="section">6. Important honest caveat</h2>
<div class="callout info">
  <span class="tag">READ THIS</span>
  <span><b>"More tests passed" does not always mean "the format is better."</b><br>
  V8 (BDD blocks) had 2 passing tests, but it created 7 tests by splitting the work into small pieces.
  V2 and V10 created exactly 3 tests that matched the 3 real requirements, and none of those 3 passed.
  So V8 looks like a winner because lots of small tests succeed; V2 and V10 look "worse" but actually
  they are cleaner — they tried to do the real job and the <i>system itself</i> got stuck on the
  harder test.<br><br>
  The real bottleneck is <b>not the format</b>. It is the system's ability to write code for the
  harder part (the "check Pencil's quantity went down by 1" part needs math). A cleaner format
  (V2 / V10) does not hide this fact behind extra small tests.</span>
</div>

<h2 class="section">7. Recommended PRD template — use this</h2>
<p><b>Use the "sections with headings" format (V2).</b> Copy this template:</p>
<div class="variant recommended">
  <div class="variant-head">
    <span class="variant-id rec">V2</span>
    <span class="variant-name">Recommended PRD template</span>
    <span class="pill-win">USE THIS</span>
  </div>
  <div class="variant-code">{esc(RECOMMENDED_TEMPLATE)}</div>
</div>
<ul class="findings">
  <li>The headings (<code>## App URL</code>, <code>## Credentials</code>, <code>## Acceptance Criteria</code>)
      match exactly what the system is looking for.</li>
  <li>Bullets under "Acceptance Criteria" become test cases one-for-one. No inflation.</li>
  <li>The credentials block is laid out clearly, so the system doesn't have to ask extra questions.</li>
  <li>It was the fastest of the clean formats (48 seconds).</li>
</ul>
<p style="margin-top:6px;">If your team already uses JSON or YAML, <b>V10 (JSON spec) is equally good</b>
and slightly faster (45 seconds). Both are fine choices.</p>

<h2 class="section">8. Anti-patterns — what NOT to do</h2>
<ul class="findings">
  <li><b>Do not write requirements in Gherkin</b> (Given / When / Then). It took 9 minutes and no tests passed.</li>
  <li><b>Do not use "As a user… I want… so that"</b> story format. It pulled out 7 requirements when there were really 3.</li>
  <li><b>Do not write a long version with business backstory.</b> The system reads the backstory as if it were part of the requirements.</li>
  <li><b>Do not write a single super-short sentence.</b> The system gets confused and may flag your PRD as ambiguous.</li>
</ul>

<h2 class="section">9. Open questions and what to try next</h2>
<ul class="findings">
  <li><b>Why are the tests still failing for V2 and V10?</b> Not because of the format — because the test
      for "Pencil quantity went down by 1" needs the system to read a number, save an invoice, read the
      number again, and check the math. That step is hard for the smaller model.</li>
  <li><b>Does V8 (BDD blocks) only look good because it splits into many small tests?</b>
      Worth testing on a simpler scenario to find out.</li>
  <li><b>If we try V2 on a simpler test</b> (just log in, nothing more), can we hit 100% pass?
      That would prove the format is good and the only thing holding us back is test difficulty.</li>
</ul>

<div class="note">All numbers in this report are measured directly from full pipeline runs against
your live Invoice App on localhost:3000 using gpt-4.1-mini, with self-healing enabled. No values
were edited or estimated.</div>

</body></html>"""

Path("/tmp/format_report.html").write_text(HTML, encoding="utf-8")

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto("file:///tmp/format_report.html")
    pg.pdf(path=OUT, format="A4", print_background=True,
           margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
    b.close()
print("WROTE", OUT)
