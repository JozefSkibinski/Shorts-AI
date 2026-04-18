"""Multi-signal ad classifier for the YouTube Shorts scraper.

Runs against the active ``ytd-reel-video-renderer`` in a Playwright page and
the metadata the scraper already captured from ``/youtubei/v1/player`` for
that video. Any one of the signals listed in the spec's Part 1.1 table is
enough to classify the Short as an ad.

Paid-promotion disclosures ("Includes paid promotion") are NOT ads — we flag
them separately so the caller can keep them in the corpus.

Sync-only because the rest of the scraper uses ``playwright.sync_api``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page


log = logging.getLogger(__name__)


# DOM selectors, scoped to the active reel element.
#
# We previously matched a broad ``class*='ytp-ad-'`` wildcard here, but
# YouTube leaves those DOM nodes in place (display:none) after an ad
# finishes, so every subsequent organic Short would still match — every
# video after an ad would be misclassified as an ad.
#
# Current rule: only the specific containers that YouTube actually
# injects *and removes* per-ad, plus a visibility check applied in the
# page probe below so hidden leftovers never trigger a false positive.
AD_DOM_SELECTORS: tuple[str, ...] = (
    "ytd-ad-slot-renderer",
    "ytd-promoted-sparkles-text-search-renderer",
    ".ytp-ad-player-overlay-layout",
    ".ytp-ad-preview-container",
    ".badge-style-type-ad",
)

# Visible text. Lowercased match against the active reel's innerText.
# "ad ·" guards against the bare word "ad" appearing in a caption.
AD_TEXT_TOKENS: tuple[str, ...] = ("sponsored", "ad ·", "promoted")
DISCLOSURE_TOKENS: tuple[str, ...] = ("includes paid promotion",)

# URL substrings that only appear on ad endpoints.
AD_URL_SUBSTRINGS: tuple[str, ...] = ("&ad_type=", "/pagead/")

# Max legitimate Short duration. YouTube raised the Shorts cap from 60s to
# 180s in October 2024, so the old "over 60s = promoted long-form" rule is
# no longer a useful signal on its own. We keep a duration check but put
# the threshold above the current Shorts maximum; anything above 180s is
# still a strong "this doesn't belong in the Shorts feed" signal.
MAX_SHORT_DURATION_SECONDS_DEFAULT: float = 180.0


@dataclass
class AdVerdict:
    is_ad: bool
    has_paid_promotion_disclosure: bool
    reasons: list[str] = field(default_factory=list)

    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


def _probe_active_reel(page: Page) -> dict:
    """Collect DOM state from the active Short in one round-trip.

    Ad-indicator DOM hits are only counted when the element is actually
    visible — YouTube leaves hidden ad overlays in the DOM after an ad
    finishes, and matching those would misclassify every following
    organic Short as an ad.
    """
    script = """
    (selectors) => {
        const active = document.querySelector('ytd-reel-video-renderer[is-active]')
            || document.querySelector('ytd-shorts ytd-reel-video-renderer');
        if (!active) return {hits: [], text: '', href: location.href};
        const isVisible = (el) => {
            if (!el) return false;
            if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            const s = getComputedStyle(el);
            if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return false;
            return true;
        };
        const hits = [];
        for (const sel of selectors) {
            try {
                const matches = active.querySelectorAll(sel);
                for (const el of matches) {
                    if (isVisible(el)) { hits.push(sel); break; }
                }
            } catch (_) {}
        }
        return {
            hits,
            text: (active.innerText || '').toLowerCase(),
            href: location.href,
        };
    }
    """
    try:
        return page.evaluate(script, list(AD_DOM_SELECTORS))
    except Exception as exc:
        log.debug("ad_filter DOM probe failed: %s", exc)
        return {"hits": [], "text": "", "href": ""}


def classify(
    page: Page,
    meta: dict[str, Any] | None,
    *,
    max_short_duration: float = MAX_SHORT_DURATION_SECONDS_DEFAULT,
) -> AdVerdict:
    """Classify the currently-visible Short.

    ``meta`` is whatever the scraper has already resolved for this video
    (yt-dlp / html / interceptor output). Extra fields the player
    interceptor captures — ``ad_placements``, ``is_ad_flag`` — are looked
    up opportunistically; when absent we fall back to DOM + text signals
    only.

    ``max_short_duration`` defaults to 180s to match YouTube's current
    Shorts length cap; set higher to disable the duration signal or
    lower if you want to be stricter.
    """
    meta = meta or {}
    reasons: list[str] = []
    disclosure = False

    dom = _probe_active_reel(page)

    for sel in dom.get("hits", []):
        reasons.append(f"dom:{sel}")

    text = dom.get("text", "") or ""
    for tok in AD_TEXT_TOKENS:
        if tok in text:
            reasons.append(f"text:{tok.strip('· ')}")
            break
    if any(tok in text for tok in DISCLOSURE_TOKENS):
        disclosure = True

    # Player-response flags (populated by the interceptor upgrade).
    if meta.get("ad_placements"):
        reasons.append("player:ad_placements")
    if meta.get("is_ad_flag"):
        reasons.append("player:is_ad_flag")

    # Duration anomaly. Legitimate Shorts can now run up to 180s; anything
    # above that is almost certainly a promoted long-form in the reel slot.
    duration = meta.get("duration_seconds")
    try:
        if duration is not None and float(duration) > max_short_duration:
            reasons.append(f"duration:over_{int(max_short_duration)}s")
    except (TypeError, ValueError):
        pass

    # URL pattern check (rare, but cheap).
    href = dom.get("href") or meta.get("url") or ""
    if any(sub in href for sub in AD_URL_SUBSTRINGS):
        reasons.append("url:ad_pattern")

    return AdVerdict(
        is_ad=bool(reasons),
        has_paid_promotion_disclosure=disclosure,
        reasons=reasons,
    )
