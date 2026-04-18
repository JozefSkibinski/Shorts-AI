"""Niche taxonomy and Haiku-based classifier."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import get_settings


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NicheSpec:
    slug: str
    display_name: str
    description: str


# Tight starter taxonomy. Add slugs here only when there's clear evidence in
# the corpus that an existing slug is too coarse — drift erodes retrieval.
NICHE_TAXONOMY: list[NicheSpec] = [
    NicheSpec("challenge", "Challenge", "Challenge / dare / 'try not to' format"),
    NicheSpec("mrbeast_style", "MrBeast-style", "High-budget stunts, giveaways, big-number framing"),
    NicheSpec("pov_storytime", "POV / Storytime", "First-person scripted stories or POV scenes"),
    NicheSpec("relationship", "Relationships", "Dating, couples, breakup, marriage content"),
    NicheSpec("finance_money", "Finance / Money", "Investing, side hustles, money advice, business"),
    NicheSpec("self_improvement", "Self-improvement", "Mindset, productivity, motivation, habits"),
    NicheSpec("fitness_physique", "Fitness / Physique", "Workouts, transformations, gym content"),
    NicheSpec("food_recipe", "Food / Recipe", "Cooking, recipes, food reviews"),
    NicheSpec("gaming_clips", "Gaming Clips", "Gameplay highlights, gaming reactions"),
    NicheSpec("comedy_skit", "Comedy / Skit", "Scripted comedy bits, sketches, jokes"),
    NicheSpec("reaction", "Reaction", "Reacting to other content, news, clips"),
    NicheSpec("educational_facts", "Educational / Facts", "Explainers, factoids, science, history"),
    NicheSpec("tech_gadgets", "Tech / Gadgets", "Tech reviews, gadgets, apps, AI demos"),
    NicheSpec("pet_animal", "Pets / Animals", "Pet videos, animal facts, wildlife"),
    NicheSpec("dance_trend", "Dance / Trend", "Dance challenges, trending moves"),
    NicheSpec("music_cover", "Music / Cover", "Music performances, covers, original songs"),
    NicheSpec("beauty_skincare", "Beauty / Skincare", "Makeup, skincare routines, product reviews"),
    NicheSpec("fashion_outfit", "Fashion / Outfit", "Outfit ideas, GRWM, style breakdowns"),
    NicheSpec("travel_lifestyle", "Travel / Lifestyle", "Travel vlogs, day-in-the-life, lifestyle"),
    NicheSpec("asmr_satisfying", "ASMR / Satisfying", "ASMR, satisfying visuals, sensory content"),
    NicheSpec("other", "Other", "Does not fit any of the above"),
]


VALID_SLUGS = {n.slug for n in NICHE_TAXONOMY}


def _build_prompt(title: str, transcript: str, hashtags: list[str]) -> str:
    catalog = "\n".join(f"- {n.slug}: {n.description}" for n in NICHE_TAXONOMY)
    tag_blob = " ".join(hashtags or [])
    excerpt = (transcript or "")[:1200]
    return (
        "Classify this YouTube Short into exactly one niche slug from the list. "
        "Respond with JSON only: {\"slug\": \"<one of the slugs>\"}. "
        "If nothing fits, use 'other'.\n\n"
        f"NICHES:\n{catalog}\n\n"
        f"TITLE: {title or '(none)'}\n"
        f"HASHTAGS: {tag_blob or '(none)'}\n"
        f"TRANSCRIPT (first 1200 chars):\n{excerpt or '(none)'}\n"
    )


def classify(title: str, transcript: str, hashtags: list[str]) -> str:
    """Return one of VALID_SLUGS. Falls back to 'other' on any error."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        log.debug("No ANTHROPIC_API_KEY set — niche defaults to 'other'.")
        return "other"

    try:
        from anthropic import Anthropic
    except ImportError:
        log.warning("anthropic SDK not installed; defaulting niche to 'other'.")
        return "other"

    client = Anthropic(api_key=settings.anthropic_api_key)
    prompt = _build_prompt(title, transcript, hashtags or [])

    try:
        resp = client.messages.create(
            model=settings.niche_model,
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("Niche classifier API call failed: %s", exc)
        return "other"

    text = "".join(b.text for b in resp.content if getattr(b, "text", None)).strip()
    slug = _extract_slug(text)
    if slug not in VALID_SLUGS:
        log.warning("Niche classifier returned invalid slug %r, using 'other'", slug)
        return "other"
    return slug


def _extract_slug(text: str) -> str:
    """Best-effort parse of the model's reply. Tries JSON first, then bare token."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return str(obj.get("slug", "")).strip().lower()
    except json.JSONDecodeError:
        pass
    # Fallback: grab the first identifier-looking token
    token = text.split()[0] if text else ""
    return token.strip(' \t\n\r"\'.,').lower()
