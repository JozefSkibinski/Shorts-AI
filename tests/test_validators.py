"""Unit tests for ``bot.utils.validators``."""

from __future__ import annotations

import pytest

from bot.utils import validators
from bot.utils.validators import ValidationError


class TestCleanKeyword:
    def test_strips_and_collapses(self) -> None:
        assert validators.clean_keyword("   lofi    beats  ") == "lofi beats"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValidationError):
            validators.clean_keyword("   ")

    def test_none_raises(self) -> None:
        with pytest.raises(ValidationError):
            validators.clean_keyword(None)  # type: ignore[arg-type]

    def test_too_long_raises(self) -> None:
        with pytest.raises(ValidationError):
            validators.clean_keyword("x" * 101)

    def test_custom_max_len(self) -> None:
        with pytest.raises(ValidationError):
            validators.clean_keyword("abcdef", max_len=3)


class TestParseKeywordList:
    def test_happy_path(self) -> None:
        assert validators.parse_keyword_list("python, rust, go") == [
            "python", "rust", "go",
        ]

    def test_strips_whitespace(self) -> None:
        assert validators.parse_keyword_list("  a  ,  b  ") == ["a", "b"]

    def test_ignores_empty_segments(self) -> None:
        assert validators.parse_keyword_list("a,,b") == ["a", "b"]

    def test_min_items(self) -> None:
        with pytest.raises(ValidationError):
            validators.parse_keyword_list("solo")

    def test_max_items(self) -> None:
        with pytest.raises(ValidationError):
            validators.parse_keyword_list("a,b,c,d,e,f")

    def test_duplicate_rejected_case_insensitive(self) -> None:
        with pytest.raises(ValidationError):
            validators.parse_keyword_list("Python, python")

    def test_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            validators.parse_keyword_list("")


class TestValidateGeo:
    @pytest.mark.parametrize("raw,expected", [
        (None, ""),
        ("", ""),
        ("us", "US"),
        ("  gb  ", "GB"),
        ("IN", "IN"),
    ])
    def test_accepted(self, raw, expected) -> None:
        assert validators.validate_geo(raw) == expected

    @pytest.mark.parametrize("raw", ["USA", "U1", "1", "United States"])
    def test_rejected(self, raw) -> None:
        with pytest.raises(ValidationError):
            validators.validate_geo(raw)


class TestValidateTimeframe:
    def test_default_when_empty(self) -> None:
        assert validators.validate_timeframe(None) == "today 3-m"
        assert validators.validate_timeframe("") == "today 3-m"

    @pytest.mark.parametrize("raw", [
        "now 1-H", "now 4-H", "now 1-d", "now 7-d",
        "today 1-m", "today 3-m", "today 12-m", "today 5-y",
        "2024-01-01 2024-06-30", "all",
    ])
    def test_accepted(self, raw) -> None:
        assert validators.validate_timeframe(raw).lower() == raw.lower()

    @pytest.mark.parametrize("raw", [
        "past week", "yesterday", "now", "today", "1-d",
        "today 3m",             # missing dash sep
        "2024/01/01 2024/06/30",  # wrong separator
    ])
    def test_rejected(self, raw) -> None:
        with pytest.raises(ValidationError):
            validators.validate_timeframe(raw)


class TestClampLimit:
    def test_default(self) -> None:
        assert validators.clamp_limit(None) == 10

    def test_clamp_low(self) -> None:
        assert validators.clamp_limit(-5) == 1

    def test_clamp_high(self) -> None:
        assert validators.clamp_limit(9999) == 25

    def test_custom_bounds(self) -> None:
        assert validators.clamp_limit(100, default=3, lo=1, hi=5) == 5
        assert validators.clamp_limit(None, default=3, lo=1, hi=5) == 3
