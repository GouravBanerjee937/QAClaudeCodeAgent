"""Step 1.5: TestSpec → Questions the user must answer before we proceed."""

from __future__ import annotations

import re

from ..events import EventBus
from ..llm import structured
from ..models import Questions, TestSpec
from ..prompts import INQUIRER_SYSTEM
from ..source import SourceInsights, render_hints_for_prompt


def inquire(
    spec: TestSpec,
    bus: EventBus,
    *,
    source_insights: SourceInsights | None = None,
    datasets: dict[str, list[str]] | None = None,
) -> Questions:
    bus.emit("inquirer", "Checking what concrete values are missing from the PRD…")
    payload = spec.model_dump_json(indent=2)
    if source_insights is not None:
        hints = render_hints_for_prompt(source_insights)
        if hints:
            payload = payload + "\n\n" + hints
            bus.emit("inquirer", "Source hints attached — won't ask for URLs the source already has.")
    questions = structured(INQUIRER_SYSTEM, payload, Questions)
    # Deterministic post-filter: drop URL questions whose answer we already have.
    # The model still slips on this rule even with the prompt instruction, so we
    # enforce it in code: a URL question is dropped if the spec's app_url covers
    # it, or if the GitHub source's routes list contains a matching route.
    before = len(questions.items)
    questions = _drop_url_questions_covered_by_source(questions, spec, source_insights)
    after = len(questions.items)
    if before > after:
        bus.emit(
            "inquirer",
            f"Dropped {before - after} URL question(s) — already covered by PRD/GitHub.",
            level="info",
        )
    # Deterministic post-filter: never ask for a value the user supplies via a
    # dataset variable ({{name}} tokens). Their values come from the dataset.
    before = len(questions.items)
    questions = _drop_dataset_questions(questions, datasets)
    if before > len(questions.items):
        bus.emit(
            "inquirer",
            f"Dropped {before - len(questions.items)} question(s) — supplied by datasets.",
            level="info",
        )
    if not questions.items:
        bus.emit("inquirer", "Spec is complete. No questions for you.", level="success")
    else:
        bus.emit(
            "inquirer",
            f"Need {len(questions.items)} value(s) from you before continuing.",
            level="warn",
        )
        for q in questions.items:
            bus.emit("inquirer", f"  • {q.key} — {q.prompt}")
    return questions


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _drop_dataset_questions(
    questions: Questions, datasets: dict[str, list[str]] | None,
) -> Questions:
    """Remove questions whose key overlaps a user-defined dataset variable name.

    Uses substring matching so that a dataset variable 'username' covers a
    question keyed 'login-username' (and vice-versa).
    """
    if not datasets:
        return questions
    ds_names = {_norm(n) for n in datasets}

    def _covered(q) -> bool:
        qn = _norm(q.key)
        return any(dn in qn or qn in dn for dn in ds_names)

    return Questions(items=[q for q in questions.items if not _covered(q)])


_APP_URL_PHRASES = (
    "app base", "app url", "application url", "base url", "start url",
    "entry url", "home url", "site url",
)


def _drop_url_questions_covered_by_source(
    questions: Questions,
    spec: TestSpec,
    source_insights: SourceInsights | None,
) -> Questions:
    """Remove questions whose answer is already in the spec.app_url or source routes."""
    if not questions.items:
        return questions

    # Tokenize all known routes into a set of >=3-char tokens we can match against.
    route_tokens: set[str] = set()
    if source_insights and source_insights.routes:
        for r in source_insights.routes:
            for t in re.split(r"[\s/#\-_]+", r.lower()):
                if len(t) >= 3:
                    route_tokens.add(t)

    def is_url_question(q) -> bool:
        return q.kind == "url" or "url" in q.key.lower()

    def covered(q) -> bool:
        if not is_url_question(q):
            return False
        text = (q.key + " " + (q.prompt or "") + " " + (q.hint or "")).lower()
        # Generic "app url" question + we already have spec.app_url → drop
        if spec.app_url and any(p in text for p in _APP_URL_PHRASES):
            return True
        # Tokenize the question's key/prompt and see if the source routes cover it
        q_tokens = {
            t for t in re.split(r"[-_\s]+", q.key.lower())
            if len(t) >= 3 and t not in {"url", "page", "path"}
        }
        # Also include >=4-char words from the prompt
        for w in re.split(r"\W+", (q.prompt or "").lower()):
            if len(w) >= 4 and w not in {"url", "page", "path"}:
                q_tokens.add(w)
        for kt in q_tokens:
            for rt in route_tokens:
                if kt in rt or rt in kt:
                    return True
        return False

    return Questions(items=[q for q in questions.items if not covered(q)])
