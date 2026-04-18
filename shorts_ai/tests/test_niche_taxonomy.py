"""Guardrails on the niche taxonomy."""

from __future__ import annotations

from app.pipeline.niche import NICHE_TAXONOMY, VALID_SLUGS, _extract_slug


def test_taxonomy_is_not_empty():
    assert len(NICHE_TAXONOMY) >= 20


def test_slugs_are_unique():
    slugs = [n.slug for n in NICHE_TAXONOMY]
    assert len(set(slugs)) == len(slugs)


def test_other_slug_exists():
    assert "other" in VALID_SLUGS


def test_spec_starter_niches_present():
    required = {
        "challenge", "mrbeast_style", "pov_storytime", "relationship",
        "finance_money", "self_improvement", "fitness_physique",
        "food_recipe", "gaming_clips", "comedy_skit", "reaction",
        "educational_facts", "tech_gadgets", "pet_animal", "dance_trend",
        "music_cover", "beauty_skincare", "fashion_outfit",
        "travel_lifestyle", "asmr_satisfying",
    }
    missing = required - VALID_SLUGS
    assert not missing, f"missing from taxonomy: {missing}"


def test_extract_slug_from_json():
    assert _extract_slug('{"slug": "comedy_skit"}') == "comedy_skit"


def test_extract_slug_from_bare_word():
    assert _extract_slug("comedy_skit") == "comedy_skit"
    assert _extract_slug('"comedy_skit".') == "comedy_skit"


def test_extract_slug_from_prose():
    assert _extract_slug("comedy_skit because of the sketch format") == "comedy_skit"
