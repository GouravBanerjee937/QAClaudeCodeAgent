"""Thin OpenAI wrapper that returns Pydantic models via structured outputs."""

from __future__ import annotations

import os
from typing import TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

_MODEL = os.getenv("QA_AGENT_MODEL", "gpt-4.1-mini")
_client: OpenAI | None = None

# Reasoning / GPT-5 family only accept the default temperature (1.0); passing
# any other value 400s. For these models we omit the kwarg entirely.
_FIXED_TEMP_PREFIXES = ("gpt-5", "o1", "o3", "o4")

T = TypeVar("T", bound=BaseModel)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key or api_key.startswith("your-"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Edit .env in the project root."
            )
        _client = OpenAI(api_key=api_key)
    return _client


def _temp_kwargs(temperature: float) -> dict:
    if any(_MODEL.startswith(p) for p in _FIXED_TEMP_PREFIXES):
        return {}
    return {"temperature": temperature}


def structured(
    system: str,
    user: str,
    schema: type[T],
    *,
    temperature: float = 0.2,
) -> T:
    """Call the model and parse the response as `schema`."""
    response = _get_client().beta.chat.completions.parse(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format=schema,
        **_temp_kwargs(temperature),
    )
    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError(f"Model returned no parseable {schema.__name__}")
    return parsed


def text(system: str, user: str, *, temperature: float = 0.2) -> str:
    """Free-form text completion (for code generation)."""
    response = _get_client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **_temp_kwargs(temperature),
    )
    return response.choices[0].message.content or ""
