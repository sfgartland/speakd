"""Tests for the filtering half of the notification connector.

Strings and a fake clock throughout. `decide` and `render` are pure, and
`RateLimit` takes its clock, so every rule in the table below is exercised
without a bus, a daemon or a wall clock -- which is the point of keeping the
config in a module that knows nothing about either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from speakd.clients.notifications.monitor import Notification
from speakd.clients.notifications.rules import (
    DEFAULT_RULES,
    RateLimit,
    Rule,
    Ruleset,
    decide,
    load_rules,
    render,
)


def note(
    app: str = "Google Chrome",
    summary: str = "Mamma",
    body: str = "hei",
    urgency: int = 1,
) -> Notification:
    return Notification(app=app, summary=summary, body=body, urgency=urgency)


class FakeClock:
    """A clock that moves only when a test says so."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- decide -----------------------------------------------------------------


def test_the_first_matching_rule_wins() -> None:
    rules = Ruleset(
        rules=(
            Rule(name="chrome", app="*chrome*", say="first"),
            Rule(name="catch-all", say="second"),
        )
    )
    verdict = decide(note(), rules)
    assert verdict.speak
    assert verdict.rule == "chrome"
    assert verdict.text == "first"


def test_a_silencing_rule_shadows_a_later_permissive_one() -> None:
    # The whole point of first-match-wins: one noisy app is silenced without
    # silencing its neighbours, by putting a `speak = false` rule in front of
    # the permissive one rather than by deleting the permissive one.
    rules = Ruleset(
        rules=(
            Rule(name="noisy", app="*chrome*", speak=False),
            Rule(name="everything", say="{summary}"),
        )
    )
    verdict = decide(note(), rules)
    assert not verdict.speak
    assert verdict.rule == "noisy"
    assert verdict.text == ""
    # The channel and label are facts about the notification, not about the
    # decision, so history and the GUI can show a silenced one by name.
    assert verdict.channel == "notify:google-chrome"
    assert verdict.label == "Google Chrome"


def test_a_notification_matching_no_rule_is_not_spoken_with_an_actionable_reason() -> None:
    verdict = decide(note(app="Slack"), Ruleset(rules=(Rule(name="chrome", app="*chrome*"),)))
    assert not verdict.speak
    assert verdict.rule == ""
    # "no rule matched" alone sends someone to the source; the app name is
    # what they need to write the rule that would have matched.
    assert "Slack" in verdict.reason


def test_a_rule_carries_its_profile_to_the_verdict() -> None:
    rules = Ruleset(rules=(Rule(name="mail", app="*bird*", profile="urgent", say="{summary}"),))
    verdict = decide(note(app="Thunderbird"), rules)
    assert verdict.profile == "urgent"


def test_a_rule_that_renders_to_nothing_is_not_spoken() -> None:
    # A notification with no text at all would otherwise be enqueued as an
    # empty utterance: silence that still costs a rate-limit token and a line
    # of history saying it was spoken.
    rules = Ruleset(rules=(Rule(name="all", say="{summary}. {body}"),))
    verdict = decide(note(summary="", body=""), rules)
    assert not verdict.speak
    assert verdict.rule == "all"
    assert verdict.reason


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        (Rule(name="r", app="*chrome*"), note(app="Google Chrome")),
        (Rule(name="r", app="*CHROME*"), note(app="google chrome")),
        (Rule(name="r", summary="mamma*"), note(summary="Mamma Mia")),
        (Rule(name="r", summary="*MIA"), note(summary="mamma mia")),
        (Rule(name="r", body="*dinner*"), note(body="Are you home for DINNER?")),
        (Rule(name="r", body="*DINNER*"), note(body="are you home for dinner?")),
    ],
)
def test_globs_match_case_insensitively_on_every_text_field(
    rule: Rule, sample: Notification
) -> None:
    assert decide(sample, Ruleset(rules=(rule,))).speak


@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        (Rule(name="r", app="*firefox*"), note(app="Google Chrome")),
        (Rule(name="r", summary="pappa*"), note(summary="Mamma")),
        (Rule(name="r", body="*dinner*"), note(body="hei")),
    ],
)
def test_a_glob_that_does_not_match_does_not_speak(rule: Rule, sample: Notification) -> None:
    assert not decide(sample, Ruleset(rules=(rule,))).speak


def test_matching_does_not_route_through_platform_case_folding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fnmatch.fnmatch` normalises both sides through `os.path.normcase`.

    That is identity on Linux and `str.lower` on Windows and macOS, so a
    matcher built on it agrees with these tests on the developer's machine and
    quietly changes meaning on someone else's. Standing `normcase` on its head
    must change nothing here: with it collapsing every string to the empty
    one, `fnmatch.fnmatch` would say every pattern matches every app.
    """
    monkeypatch.setattr(os.path, "normcase", lambda _s: "")
    rules = Ruleset(
        rules=(
            Rule(name="firefox", app="*firefox*", say="wrong"),
            Rule(name="chrome", app="*chrome*", say="right"),
        )
    )
    verdict = decide(note(app="Google Chrome"), rules)
    assert verdict.rule == "chrome"
    assert verdict.text == "right"


def test_urgency_matches_by_name() -> None:
    rules = Ruleset(rules=(Rule(name="alarm", urgency="critical", say="{summary}"),))
    assert decide(note(urgency=2), rules).speak
    assert not decide(note(urgency=1), rules).speak
    assert not decide(note(urgency=0), rules).speak


def test_an_absent_urgency_matches_any() -> None:
    rules = Ruleset(rules=(Rule(name="all", say="{summary}"),))
    assert all(decide(note(urgency=level), rules).speak for level in (0, 1, 2))


def test_urgency_low_is_zero_not_falsy_nonsense() -> None:
    # `if rule.urgency:` reads naturally and is wrong for "low", whose
    # mapped value is 0 -- the one urgency a truthiness test drops.
    rules = Ruleset(rules=(Rule(name="quiet", urgency="low", say="{summary}"),))
    assert decide(note(urgency=0), rules).speak
    assert not decide(note(urgency=1), rules).speak


# --- channel and label ------------------------------------------------------


@pytest.mark.parametrize(
    ("app", "channel"),
    [
        ("Google Chrome", "notify:google-chrome"),
        ("Thunderbird", "notify:thunderbird"),
        ("org.mozilla.Thunderbird", "notify:org-mozilla-thunderbird"),
        ("  spaces   everywhere  ", "notify:spaces-everywhere"),
        ("Værvarsel — NRK", "notify:værvarsel-nrk"),
    ],
)
def test_the_channel_is_the_slugged_app_name(app: str, channel: str) -> None:
    rules = Ruleset(rules=(Rule(name="all", say="{summary}"),))
    verdict = decide(note(app=app), rules)
    assert verdict.channel == channel
    assert verdict.label == app


def test_an_app_name_that_slugs_to_nothing_still_gets_a_channel() -> None:
    # "notify:" is not a channel: every such app would share one mute and one
    # skip button. Two different punctuation-only names must stay apart.
    rules = Ruleset(rules=(Rule(name="all", say="{summary}"),))
    first = decide(note(app="!!!"), rules).channel
    second = decide(note(app="???"), rules).channel
    assert first.startswith("notify:")
    assert first != "notify:"
    assert first != second
    # Stable across runs: a mute set yesterday must land on the same channel
    # today, which rules out anything built on `hash()`.
    assert decide(note(app="!!!"), rules).channel == first


# --- render -----------------------------------------------------------------


def test_render_fills_the_three_known_fields() -> None:
    text = render("{app}: {summary}. {body}", note(), 220)
    assert text == "Google Chrome: Mamma. hei"


def test_render_collapses_newlines_and_runs_of_whitespace() -> None:
    # Notification bodies are multi-line; the segmenter is happier flat.
    text = render("{body}", note(body="line one\nline two with  a  gap\n\n"), 220)
    assert text == "line one line two with a gap"


def test_render_leaves_no_dangling_separator_when_the_body_is_empty() -> None:
    assert render("{summary}. {body}", note(summary="Mamma", body=""), 220) == "Mamma."


def test_render_leaves_no_dangling_separator_when_the_summary_is_empty() -> None:
    assert render("{summary}. {body}", note(summary="", body="hei"), 220) == "hei"


def test_render_collapses_a_separator_left_by_a_field_in_the_middle() -> None:
    assert render("Email. {summary}. {body}", note(summary="", body="hei"), 220) == "Email. hei"


def test_render_of_an_entirely_empty_notification_is_empty() -> None:
    assert render("{summary}. {body}", note(summary="", body=""), 220) == ""


def test_render_keeps_a_literal_brace_from_the_body_verbatim() -> None:
    # The hazard is formatting the *body* rather than the template: a WhatsApp
    # message reading "{body}" would then expand, and "{sender}" would raise
    # KeyError on the speech path. Values are never format strings.
    body = "look: {body} and a lone { and }"
    assert render("{summary} says. {body}", note(summary="Mamma", body=body), 220) == (
        "Mamma says. look: {body} and a lone { and }"
    )


def test_render_truncates_at_a_word_boundary_without_a_marker() -> None:
    text = render("{body}", note(body="one two three four five"), 11)
    # Not "one two thr", and no ellipsis: the synthesiser would read it.
    assert text == "one two"


def test_render_keeps_a_word_that_ends_exactly_on_the_limit() -> None:
    # The cut lands on the space after "two", so "two" is whole and belongs in
    # the output; dropping it is an off-by-one that costs a word every time.
    assert render("{body}", note(body="one two three"), 7) == "one two"


def test_render_leaves_text_shorter_than_the_limit_alone() -> None:
    assert render("{body}", note(body="exactly"), 7) == "exactly"


def test_render_cuts_a_single_oversized_word_rather_than_saying_nothing() -> None:
    assert render("{body}", note(body="antidisestablishmentarianism"), 6) == "antidi"


def test_render_does_not_strip_punctuation() -> None:
    assert render("{body}", note(body="Hi! Are you there?"), 220) == "Hi! Are you there?"


def test_render_refuses_a_template_it_cannot_fill() -> None:
    # Unreachable from a rules file -- load_rules rejects this template -- but
    # a Rule built in code must fail loudly rather than say a sentence with a
    # hole where the placeholder was.
    with pytest.raises(ValueError, match="sender"):
        render("from {sender}", note(), 220)


@pytest.mark.parametrize("budget", [0, -5])
def test_render_with_no_budget_says_nothing(budget: int) -> None:
    # Also unreachable from a rules file, which requires a positive max_chars.
    # The negative case is the one worth pinning: `text[:-5]` cuts five
    # characters off the far end and says almost the whole notification.
    assert render("{body}", note(body="hello world"), budget) == ""


# --- load_rules -------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "notifications.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_rules_reads_a_ruleset(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
max_per_minute = 5

[[rule]]
name = "whatsapp"
app = "*chrome*"
say = "{summary} says. {body}"

[[rule]]
name = "mail"
app = "thunderbird"
urgency = "normal"
speak = false
profile = "quiet"
max_chars = 80
""",
    )
    ruleset = load_rules(path)
    assert ruleset.max_per_minute == 5
    assert [rule.name for rule in ruleset.rules] == ["whatsapp", "mail"]
    assert ruleset.rules[0].app == "*chrome*"
    assert ruleset.rules[0].summary == "*"
    assert ruleset.rules[0].speak is True
    assert ruleset.rules[1].speak is False
    assert ruleset.rules[1].urgency == "normal"
    assert ruleset.rules[1].profile == "quiet"
    assert ruleset.rules[1].max_chars == 80


def test_load_rules_on_a_missing_file_returns_the_defaults(tmp_path: Path) -> None:
    # Not an empty ruleset: that speaks nothing, which is indistinguishable
    # from the connector being broken on the day it is installed.
    assert load_rules(tmp_path / "absent.toml") == DEFAULT_RULES


def test_load_rules_on_malformed_toml_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "max_per_minute = = 3\n")
    with pytest.raises(ValueError):
        load_rules(path)


@pytest.mark.parametrize(
    "body",
    [
        '[[rule]]\napp = "*chrome*"\n',
        '[[rule]]\nname = ""\n',
        "[[rule]]\nname = 5\n",
    ],
)
def test_load_rules_requires_a_usable_name(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="rule"):
        load_rules(write(tmp_path, body))


def test_load_rules_names_a_nameless_rule_by_its_index(tmp_path: Path) -> None:
    path = write(tmp_path, '[[rule]]\nname = "ok"\n\n[[rule]]\napp = "*chrome*"\n')
    with pytest.raises(ValueError, match="#2"):
        load_rules(path)


def test_load_rules_rejects_duplicate_names(tmp_path: Path) -> None:
    # Two rules called "mail" is an edit that went to the wrong copy; the
    # second is dead, and first-match-wins makes that invisible.
    path = write(tmp_path, '[[rule]]\nname = "mail"\n\n[[rule]]\nname = "mail"\n')
    with pytest.raises(ValueError, match="mail"):
        load_rules(path)


@pytest.mark.parametrize(
    "line",
    ["app = 5", 'app = ["a"]', "summary = true", "body = 1.5", "say = 5", "profile = 5"],
)
def test_load_rules_requires_string_fields(tmp_path: Path, line: str) -> None:
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\n{line}\n'))


def test_load_rules_rejects_an_empty_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, '[[rule]]\nname = "mail"\nprofile = ""\n'))


@pytest.mark.parametrize("value", ["1", '"true"', "1.0"])
def test_load_rules_requires_speak_to_be_a_bool(tmp_path: Path, value: str) -> None:
    # `speak = 1` is truthy and would work by accident; `speak = 0` would work
    # too, and then `speak = "false"` -- a string, truthy -- would not.
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\nspeak = {value}\n'))


@pytest.mark.parametrize("value", ["0", "-1", "true", '"220"', "1.5"])
def test_load_rules_requires_a_positive_integer_max_chars(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\nmax_chars = {value}\n'))


@pytest.mark.parametrize("value", ['"urgent"', '"LOW"', "2", '""'])
def test_load_rules_requires_a_known_urgency_name(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\nurgency = {value}\n'))


def test_load_rules_accepts_the_three_urgency_names(tmp_path: Path) -> None:
    for name in ("low", "normal", "critical"):
        ruleset = load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\nurgency = "{name}"\n'))
        assert ruleset.rules[0].urgency == name


def test_load_rules_rejects_an_unknown_placeholder(tmp_path: Path) -> None:
    # The most important check in the file: `str.format` would raise KeyError
    # deep on the speech path, where one bad rule takes the connector down
    # rather than itself.
    path = write(tmp_path, '[[rule]]\nname = "mail"\nsay = "from {sender}. {body}"\n')
    with pytest.raises(ValueError, match="mail"):
        load_rules(path)
    with pytest.raises(ValueError, match="sender"):
        load_rules(path)


@pytest.mark.parametrize(
    "say",
    [
        '"{summary"',
        '"{body.upper}"',
        '"{body[0]}"',
        '"{}"',
        '"{0}"',
        # "{body!r}" would quote the message and "{body:>40}" would pad it
        # with spaces nobody can hear. Neither is quietly ignored.
        '"{body!r}"',
        '"{body:>40}"',
    ],
)
def test_load_rules_rejects_a_template_that_is_not_plain_placeholders(
    tmp_path: Path, say: str
) -> None:
    with pytest.raises(ValueError, match="mail"):
        load_rules(write(tmp_path, f'[[rule]]\nname = "mail"\nsay = {say}\n'))


def test_load_rules_validates_say_even_on_a_silent_rule(tmp_path: Path) -> None:
    # A rule flipped to `speak = true` next week must not fail at runtime for
    # a mistake that was in the file all along.
    path = write(tmp_path, '[[rule]]\nname = "mail"\nspeak = false\nsay = "{sender}"\n')
    with pytest.raises(ValueError, match="mail"):
        load_rules(path)


@pytest.mark.parametrize("value", ["-1", '"20"', "true", "1.5"])
def test_load_rules_rejects_a_max_per_minute_that_is_not_a_whole_count(
    tmp_path: Path, value: str
) -> None:
    """Zero is excluded deliberately: it is how the file says "no limit"."""
    with pytest.raises(ValueError, match="max_per_minute"):
        load_rules(write(tmp_path, f"max_per_minute = {value}\n"))


def test_load_rules_defaults_max_per_minute_when_absent(tmp_path: Path) -> None:
    ruleset = load_rules(write(tmp_path, '[[rule]]\nname = "mail"\n'))
    assert ruleset.max_per_minute == DEFAULT_RULES.max_per_minute


@pytest.mark.parametrize("text", ['rule = "mail"\n', '[rule]\nname = "mail"\n'])
def test_load_rules_rejects_a_rule_that_is_not_an_array_of_tables(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(ValueError, match="rule"):
        load_rules(write(tmp_path, text))


def test_load_rules_on_an_empty_file_speaks_nothing(tmp_path: Path) -> None:
    # An empty file is a deliberate "nothing, thank you", unlike a missing
    # one, which is only ever the state of a machine nobody has configured.
    assert load_rules(write(tmp_path, "")).rules == ()


# --- DEFAULT_RULES ----------------------------------------------------------


def test_the_default_rules_speak_a_plausible_chrome_notification() -> None:
    verdict = decide(
        note(app="Google Chrome", summary="Mamma", body="Er du hjemme til middag?"),
        DEFAULT_RULES,
    )
    assert verdict.speak
    assert "Mamma" in verdict.text
    assert "Er du hjemme til middag?" in verdict.text
    assert verdict.channel == "notify:google-chrome"
    assert verdict.profile == "notification"


def test_the_default_rules_speak_chromium_too() -> None:
    # "Chromium" does not contain "chrome": a `*chrome*` glob covers the one
    # browser and silently misses the other.
    assert decide(note(app="Chromium"), DEFAULT_RULES).speak


def test_the_default_rules_speak_a_plausible_thunderbird_notification() -> None:
    verdict = decide(
        note(app="Thunderbird", summary="Jan Opsomer", body="Re: seminar Friday"),
        DEFAULT_RULES,
    )
    assert verdict.speak
    assert "Jan Opsomer" in verdict.text
    assert "Re: seminar Friday" in verdict.text


def test_the_default_rules_speak_a_plausible_evolution_notification() -> None:
    assert decide(note(app="Evolution", summary="KU Leuven", body="Timetable"), DEFAULT_RULES).speak


def test_the_default_rules_say_nothing_about_an_app_nobody_asked_for() -> None:
    assert not decide(note(app="Spotify", summary="Now playing"), DEFAULT_RULES).speak


def test_every_default_say_template_survives_a_round_trip(tmp_path: Path) -> None:
    # The same validation the user's file gets, applied to the ruleset that
    # ships: a typo in a built-in template would otherwise only surface as a
    # KeyError on someone's first notification.
    lines = [f"max_per_minute = {DEFAULT_RULES.max_per_minute}"]
    for rule in DEFAULT_RULES.rules:
        lines.append(
            f'\n[[rule]]\nname = "{rule.name}"\napp = "{rule.app}"\n'
            f'say = "{rule.say}"\nprofile = "{rule.profile}"\nmax_chars = {rule.max_chars}'
        )
    assert load_rules(write(tmp_path, "\n".join(lines))) == DEFAULT_RULES


# --- RateLimit --------------------------------------------------------------


def test_the_rate_limit_allows_up_to_the_limit_and_then_blocks() -> None:
    clock = FakeClock()
    limit = RateLimit(3, now=clock)
    assert [limit.allow() for _ in range(4)] == [True, True, True, False]


def test_the_rate_limit_recovers_as_the_window_slides() -> None:
    clock = FakeClock()
    limit = RateLimit(2, now=clock)
    assert limit.allow()
    clock.advance(30)
    assert limit.allow()
    assert not limit.allow()
    # The first hit ages out at sixty seconds; the second has not.
    clock.advance(30)
    assert limit.allow()
    assert not limit.allow()
    clock.advance(30)
    assert limit.allow()


def test_a_blocked_call_does_not_extend_the_window() -> None:
    # A token bucket that records refusals never recovers on a chatty group
    # chat: every blocked notification pushes the window forward.
    clock = FakeClock()
    limit = RateLimit(1, now=clock)
    assert limit.allow()
    clock.advance(59)
    assert not limit.allow()
    clock.advance(1)
    assert limit.allow()


def test_a_rate_limit_of_zero_allows_everything() -> None:
    # Unlimited, not "block everything" -- the reading that turns a safety
    # valve into a mute switch.
    clock = FakeClock()
    limit = RateLimit(0, now=clock)
    assert all(limit.allow() for _ in range(100))


def test_a_negative_rate_limit_allows_everything() -> None:
    limit = RateLimit(-5, now=FakeClock())
    assert all(limit.allow() for _ in range(100))


def test_the_rate_limit_defaults_to_the_wall_clock() -> None:
    # The signature carries `now=time.time`; a limit built without a clock
    # must still work rather than blow up on a missing argument.
    assert RateLimit(2).allow()


def test_a_limit_of_zero_means_no_limit(tmp_path: Path) -> None:
    """The only way to say "unlimited" in a file, and `RateLimit` already means it."""
    path = tmp_path / "notifications.toml"
    path.write_text("max_per_minute = 0\n", encoding="utf-8")
    assert load_rules(path).max_per_minute == 0
    limiter = RateLimit(0)
    assert all(limiter.allow() for _ in range(500))


def test_a_negative_limit_is_rejected_and_points_at_zero(tmp_path: Path) -> None:
    path = tmp_path / "notifications.toml"
    path.write_text("max_per_minute = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="0 for no limit"):
        load_rules(path)
