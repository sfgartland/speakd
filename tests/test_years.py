"""Tests for rewriting years into the words people say."""

import pytest

from speakd.synth.years import speak_years


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1994", "nineteen ninety-four"),
        ("1900", "nineteen hundred"),
        ("1905", "nineteen oh five"),
        ("2000", "two thousand"),
        ("2005", "two thousand five"),
        ("2010", "twenty ten"),
        ("2026", "twenty twenty-six"),
        ("1100", "eleven hundred"),
        ("1990s", "nineteen nineties"),
    ],
)
def test_years_are_spoken_the_way_people_say_them(text: str, expected: str) -> None:
    assert speak_years(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "3.1994",
        "1,994",
        "@Stiegler-1994",
        "1994-95",
        "12:1994",
        "ISBN 1994-123",
        "ISBN 978-1994",
        "v1994",
        "1099",
        "2100",
        "",
    ],
)
def test_things_that_look_like_years_but_are_not_are_left_alone(text: str) -> None:
    assert speak_years(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("In 1994, Habermas", "In nineteen ninety-four, Habermas"),
        ("(1994)", "(nineteen ninety-four)"),
    ],
)
def test_years_inside_sentences_are_rewritten_in_place(text: str, expected: str) -> None:
    assert speak_years(text) == expected
