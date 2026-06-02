# Reporter — the "Report Writer"

**What it does:** A prompt for an AI report writer is defined in the code, but
**it is currently not used** — the actual `reporter.py` builds the final
Markdown report deterministically in Python (table, screenshots, failure
messages), without calling any AI. The prompt below is kept in the source as
a reference for a future version that may use it.

**Source:** `qa_agent/prompts.py` → `REPORTER_SYSTEM` (defined but unused)

---

## Prompt (verbatim, currently unused)

```
You write concise QA reports. Given the TestSpec, generated test cases, and run results, produce a Markdown report with: a 2-sentence summary, a results table (case id, title, status, duration), and a 'Findings' section noting any failures with their messages. No fluff.
```
