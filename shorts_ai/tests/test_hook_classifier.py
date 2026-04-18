"""Heuristic hook classifier — pure function tests."""

from __future__ import annotations

import pytest

from app.pipeline.hook import HOOK_TYPES, classify_heuristic


@pytest.mark.parametrize("text,expected", [
    ("Did you know dolphins sleep with one eye open?", "question"),
    ("Top 5 things you didn't know about Mars", "list"),
    ("3 reasons your gym routine isn't working", "list"),
    ("Watch this guy fall over", "command"),
    ("Look at what just happened", "command"),
    ("Wait for it...", "command"),
    ("90% of people get this wrong", "statistic"),
    ("So I was at the gym yesterday", "story"),
    ("Yesterday I tried something insane", "story"),
    ("Everyone is wrong about this", "controversy"),
    ("", "visual"),
])
def test_classify_heuristic_examples(text, expected):
    assert classify_heuristic(text) == expected


def test_all_returned_labels_are_known():
    samples = [
        "what?", "watch", "5 ways", "200 dollars", "so today", "nobody knows",
        "", "random sentence with no signal at all here just words",
    ]
    for s in samples:
        assert classify_heuristic(s) in HOOK_TYPES
