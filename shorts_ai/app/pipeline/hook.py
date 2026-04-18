"""Classify the first 3 seconds of a Short into a hook archetype."""

from __future__ import annotations

import logging
import re

from app.config import get_settings


log = logging.getLogger(__name__)


# The seven archetypes match the optimization scorer's expectations.
HOOK_TYPES = (
    "question",      # opens with a question to the viewer
    "statistic",     # opens with a number / shocking stat
    "controversy",   # bold opinion or contrarian claim
    "visual",        # visual cold-open (no spoken hook)
    "command",       # imperative ("watch this", "look")
    "story",         # narrative cold-open ("so I was at...")
    "list",          # "X things you didn't know..."
)


# ---------------------------------------------------------------------------
# Cheap heuristic classifier
# ---------------------------------------------------------------------------


_NUM_RE = re.compile(r"\b\d{1,4}([.,]\d+)?\b")
_LIST_RE = re.compile(r"^(top|the)\s+\d+\b|\b\d+\s+(things|reasons|ways|tips)\b", re.I)
_COMMAND_RE = re.compile(r"^(watch|look|wait|stop|listen|check|see)\b", re.I)
_STORY_RE = re.compile(r"^(so|once|yesterday|today|last|when i)\b", re.I)
_CONTROVERSY_RE = re.compile(
    r"\b(everyone|nobody|never|always|hate|wrong|truth|secret)\b", re.I
)


def classify_heuristic(hook_text: str) -> str:
    text = (hook_text or "").strip()
    if not text:
        return "visual"
    if "?" in text[:80]:
        return "question"
    if _LIST_RE.search(text):
        return "list"
    if _COMMAND_RE.search(text):
        return "command"
    if _NUM_RE.search(text) and len(text.split()) <= 14:
        return "statistic"
    if _STORY_RE.search(text):
        return "story"
    if _CONTROVERSY_RE.search(text):
        return "controversy"
    return "story"  # generic narrative fallback


# ---------------------------------------------------------------------------
# Optional Haiku refinement
# ---------------------------------------------------------------------------


def classify(hook_text: str, *, use_llm: bool = False) -> str:
    """Return one of HOOK_TYPES.

    Heuristic by default — fast and free. Pass ``use_llm=True`` to ask
    Haiku, which is more reliable on edge cases (sarcasm, rhetorical
    questions, etc.).
    """
    base = classify_heuristic(hook_text)
    if not use_llm:
        return base

    settings = get_settings()
    if not settings.anthropic_api_key:
        return base
    try:
        from anthropic import Anthropic
    except ImportError:
        return base

    catalog = ", ".join(HOOK_TYPES)
    prompt = (
        "You are labelling the opening 3 seconds of a YouTube Short. "
        f"Pick exactly one label from this list: {catalog}. "
        "Reply with only the label, nothing else.\n\n"
        f"OPENING: {hook_text!r}"
    )
    try:
        client = Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.niche_model,
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "text", None)).strip().lower()
        token = re.split(r"[^a-z]+", text)[0]
        if token in HOOK_TYPES:
            return token
    except Exception as exc:
        log.debug("Hook LLM refinement failed: %s", exc)
    return base
