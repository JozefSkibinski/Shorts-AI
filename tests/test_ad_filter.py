"""Ad classifier — signal coverage + disclosure handling."""

from __future__ import annotations

import pytest

import ad_filter
from ad_filter import classify


class FakePage:
    """Stubs page.evaluate with a fixed DOM probe result."""

    def __init__(self, *, hits=(), text="", href=""):
        self._hits = list(hits)
        self._text = text
        self._href = href
        self.evaluated_with = None

    def evaluate(self, script, arg=None):
        self.evaluated_with = arg
        return {"hits": list(self._hits), "text": self._text, "href": self._href}


def test_clean_short_is_not_ad():
    page = FakePage(hits=[], text="funny pet dog fails #pets", href="https://www.youtube.com/shorts/abc")
    meta = {"duration_seconds": 42.0}
    v = classify(page, meta)
    assert v.is_ad is False
    assert v.has_paid_promotion_disclosure is False
    assert v.reasons == []


def test_dom_hit_flags_ad():
    page = FakePage(hits=["ytd-ad-slot-renderer"], text="", href="")
    v = classify(page, {})
    assert v.is_ad is True
    assert v.reasons == ["dom:ytd-ad-slot-renderer"]


def test_multiple_dom_hits_all_reported():
    page = FakePage(
        hits=["ytd-ad-slot-renderer", ".ytp-ad-player-overlay-layout"],
        text="",
        href="",
    )
    v = classify(page, {})
    assert v.is_ad is True
    assert "dom:ytd-ad-slot-renderer" in v.reasons
    assert "dom:.ytp-ad-player-overlay-layout" in v.reasons


def test_text_sponsored_flags_ad():
    page = FakePage(hits=[], text="sponsored by some brand", href="")
    v = classify(page, {})
    assert v.is_ad is True
    assert any(r.startswith("text:") for r in v.reasons)


def test_disclosure_is_not_ad_and_is_flagged():
    page = FakePage(
        hits=[],
        text="the creator discloses this: includes paid promotion",
        href="",
    )
    v = classify(page, {})
    assert v.is_ad is False
    assert v.has_paid_promotion_disclosure is True


def test_player_ad_placements_flag():
    page = FakePage()
    v = classify(page, {"ad_placements": True})
    assert v.is_ad is True
    assert "player:ad_placements" in v.reasons


def test_player_is_ad_flag():
    page = FakePage()
    v = classify(page, {"is_ad_flag": True})
    assert v.is_ad is True
    assert "player:is_ad_flag" in v.reasons


def test_duration_over_max_flags_ad():
    page = FakePage()
    # Default max_short_duration is 180s (matches YouTube's current Shorts cap).
    v = classify(page, {"duration_seconds": 240.0})
    assert v.is_ad is True
    assert any(r.startswith("duration:over_") for r in v.reasons)


@pytest.mark.parametrize("duration", [None, 0, 15.0, 60.0, 90.0, 180.0])
def test_short_durations_are_not_flagged(duration):
    page = FakePage(text="totally organic content")
    v = classify(page, {"duration_seconds": duration})
    assert v.is_ad is False


def test_duration_threshold_is_overridable():
    page = FakePage()
    # With strict 60s threshold, a 90s Short flags as ad.
    v = classify(page, {"duration_seconds": 90.0}, max_short_duration=60.0)
    assert v.is_ad is True
    assert "duration:over_60s" in v.reasons
    # With the stricter threshold raised, same Short is fine.
    v = classify(page, {"duration_seconds": 90.0}, max_short_duration=200.0)
    assert v.is_ad is False


def test_url_pattern_flag():
    page = FakePage(href="https://www.youtube.com/shorts/abc?&ad_type=video")
    v = classify(page, {})
    assert v.is_ad is True
    assert "url:ad_pattern" in v.reasons


def test_page_evaluate_failure_does_not_crash():
    class BrokenPage:
        def evaluate(self, script, arg=None):
            raise RuntimeError("boom")

    v = classify(BrokenPage(), {})
    assert v.is_ad is False
    assert v.reasons == []


def test_selectors_are_passed_to_page_evaluate():
    page = FakePage()
    classify(page, {})
    assert page.evaluated_with is not None
    assert "ytd-ad-slot-renderer" in page.evaluated_with
    assert ".ytp-ad-player-overlay-layout" in page.evaluated_with


def test_stale_invisible_ad_nodes_do_not_trigger():
    """The fake page only reports visible hits; invisible stale DOM
    nodes from a previous ad shouldn't be returned in 'hits', so
    classify() must not flag a clean Short.

    (The visibility filter itself is enforced in the browser by
    _probe_active_reel's JavaScript; this test documents the contract:
    if the probe returns an empty hits list, classify does not add a
    DOM reason.)
    """
    page = FakePage(hits=[], text="normal organic short content")
    v = classify(page, {"duration_seconds": 45.0})
    assert v.is_ad is False
    assert v.reasons == []
