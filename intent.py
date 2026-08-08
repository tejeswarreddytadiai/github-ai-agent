"""LLM-backed intent classifier for the chat UI.

Given raw user text, classifies it into one of a small set of intents and
optionally extracts a table reference. If the model is uncertain, the intent
falls back to ``"unknown"`` so the caller can ask a clarifying question
instead of hallucinating.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Optional

from openai import OpenAI

from config import settings

Intent = Literal[
    "start", "interactive", "lookup", "approve", "cancel", "help", "unknown"
]


@dataclass(frozen=True)
class ParsedIntent:
    intent: Intent
    table_ref: Optional[str] = None
    reason: str = ""


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.azure_api_key,
            base_url=settings.azure_endpoint.rstrip("/") + "/openai/v1/",
        )
    return _client


_SYSTEM_PROMPT = """You are an intent classifier for a schema-management chatbot.

Return a JSON object with these fields (no prose, JSON only):
- "intent": one of "start" | "interactive" | "lookup" | "approve" | "cancel" | "help" | "unknown"
- "table_ref": string or null. Only set when intent == "lookup". Copy the
  table reference exactly as the user wrote it (do NOT strip project prefix).
- "reason": short debug string

Intent definitions:
- "start": user wants to run the workflow using the local sample email file
  (e.g. "start", "begin", "process the email", "run the workflow").
- "interactive": user wants to add a column by entering fields step by step
  (e.g. "add a column", "add a new field manually", "I want to add", "interactive add").
- "lookup": user is asking about an existing BigQuery table's details or schema
  (e.g. "show me table X", "what columns does Y have", "give me details of Z",
  "search for T", "info about ..."). Extract the table reference into
  ``table_ref``.
- "approve": user is approving/merging a pending PR (e.g. "approve", "merge",
  "looks good, merge it", "go ahead").
- "cancel": user wants to cancel the current flow.
- "help": user is asking what they can do or for a list of commands.
- "unknown": the message is ambiguous or you cannot confidently classify it.
  Prefer "unknown" over guessing.

If the user mentions a table but the reference is genuinely unclear (e.g.
just "that table" with no antecedent), still return "lookup" but set
``table_ref`` to null; the caller will ask for clarification.
"""


def classify(user_text: str) -> ParsedIntent:
    """Classify a user utterance. Never raises — returns ``"unknown"`` on error."""
    text = (user_text or "").strip()
    if not text:
        return ParsedIntent(intent="unknown", reason="empty input")

    try:
        resp = _get_client().chat.completions.create(
            model=settings.azure_deployment,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw) if raw else {}
    except Exception as exc:  # pragma: no cover - LLM/JSON edge cases
        return ParsedIntent(intent="unknown", reason=f"classifier error: {exc}")

    intent = data.get("intent", "unknown")
    if intent not in {
        "start", "interactive", "lookup", "approve", "cancel", "help", "unknown"
    }:
        intent = "unknown"

    table_ref = data.get("table_ref")
    if not isinstance(table_ref, str) or not table_ref.strip():
        table_ref = None

    return ParsedIntent(
        intent=intent,
        table_ref=table_ref,
        reason=str(data.get("reason", ""))[:200],
    )
