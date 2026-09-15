"""Which notifications are worth saying out loud, and how to say them.

An application decides when to notify; only the person at the desk can decide
what is worth being interrupted for. So this is an allow list read from their
own file: a notification matching no rule is not spoken, and the first rule
that matches decides -- which is what makes silencing one noisy app a matter
of putting a rule above another rather than rewriting both.

Nothing here knows about D-Bus and nothing here talks to the daemon. Given a
`Notification` and a `Ruleset` the answer is a `Verdict`, so every judgement
the connector makes is a pure function of two values and is tested as one.

Validation happens at load for the same reason it does in `profiles.py`: a
rule that silently says nothing is an afternoon spent wondering why, and an
unknown placeholder in `say` would otherwise raise `KeyError` deep on the
speech path, where one bad rule takes the whole connector down rather than
just itself.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from string import Formatter

import tomllib

from speakd.clients.notifications.monitor import Notification

# The only placeholders a `say` template may use. Everything else is a load
# error: these three are all a notification carries that is worth speaking.
FIELDS = ("app", "summary", "body")

# The rule file names urgencies; the bus numbers them. Names in the file
# because "critical" is what the person writing it means, and a file full of
# `urgency = 2` is a file nobody can read a year later.
URGENCIES = {"low": 0, "normal": 1, "critical": 2}
_URGENCY_NAMES = {value: name for name, value in URGENCIES.items()}

_WINDOW_SECONDS = 60.0

# Punctuation a template uses to join its fields. Collapsed when a field
# between two of them turns out to be empty -- and only ever in the
# template's own text, never in a notification's.
_SEPARATOR_RUN = re.compile(r"(?:\s*([.,;:!?…])\s*)+")
_SEPARATORS = set(" \t\r\n.,;:!?…-–—")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Rule:
    name: str
    app: str = "*"
    summary: str = "*"
    body: str = "*"
    urgency: str = ""  # "" is any; otherwise one of URGENCIES
    speak: bool = True
    say: str = "{summary}. {body}"
    profile: str = "notification"
    max_chars: int = 220


@dataclass(frozen=True)
class Ruleset:
    rules: tuple[Rule, ...] = ()
    max_per_minute: int = 20


@dataclass(frozen=True)
class Verdict:
    speak: bool
    text: str = ""
    channel: str = ""
    label: str = ""
    profile: str = ""
    rule: str = ""
    reason: str = ""


# What was actually asked for, so the connector does something on the day it
# is installed rather than after an evening of writing TOML. Chrome carries
# WhatsApp Web; Thunderbird and Evolution carry mail.
DEFAULT_RULES = Ruleset(
    rules=(
        # "*chrom*", not "*chrome*": Chromium does not contain "chrome", and a
        # default that covers one of the two browsers and silently misses the
        # other is the kind of thing nobody notices until they switch.
        Rule(name="chrome", app="*chrom*", say="{summary} says. {body}"),
        Rule(name="thunderbird", app="*thunderbird*", say="Email. {summary}. {body}"),
        Rule(name="evolution", app="*evolution*", say="Email. {summary}. {body}"),
    ),
    max_per_minute=20,
)


def load_rules(path: Path) -> Ruleset:
    """Read a ruleset from TOML. `ValueError` names the rule at fault.

    A missing file yields `DEFAULT_RULES`, not an empty ruleset: an empty one
    speaks nothing, which from the outside is indistinguishable from the
    connector being broken. An empty *file* does yield an empty ruleset --
    that one is somebody saying "nothing, thank you".
    """
    if not path.is_file():
        return DEFAULT_RULES
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            # TOMLDecodeError is already a ValueError; re-raised only to put
            # the path in front of a message that otherwise names a line
            # number in a file the caller never mentions.
            raise ValueError(f"{path}: {exc}") from exc
    max_per_minute = data.get("max_per_minute", Ruleset.max_per_minute)
    # bool before int: `isinstance(True, int)` is true, so `max_per_minute =
    # true` would otherwise pass as the number 1 and silence all but the
    # first notification of every minute.
    if isinstance(max_per_minute, bool) or not isinstance(max_per_minute, int):
        raise ValueError(f"max_per_minute must be a whole number, got {max_per_minute!r}")
    # Zero is "no limit", matching `RateLimit`, and is the only way to say so
    # in a file. Rejecting it left the code able to express something the
    # config could not, which is a gap someone finds by trying it and getting
    # an error that reads as though unlimited were unsupported.
    if max_per_minute < 0:
        raise ValueError(
            f"max_per_minute cannot be negative, got {max_per_minute} (use 0 for no limit)"
        )
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        # `[rule]` instead of `[[rule]]` is a single character, and it makes
        # `rule` a table whose keys read as one nameless rule's fields.
        raise ValueError(f"rule must be a list of [[rule]] tables, got {type(raw_rules).__name__}")
    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rules, start=1):
        # The index until the name is known to be usable: an error that cannot
        # say which rule it came from is an error you go looking for, and a
        # rule whose name is the problem has no name to be called by.
        label = f"rule #{index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{label}: must be a table, got {type(raw).__name__}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"{label}: name is required and must be a non-empty string, got {name!r}"
            )
        label = f"rule {name!r}"
        if name in seen:
            # First match wins, so a second rule of the same name is dead
            # code -- and the edit that was meant for it went to the wrong
            # copy, which is invisible from the outside.
            raise ValueError(f"{label}: duplicate name; rule #{index} repeats an earlier one")
        seen.add(name)
        speak = raw.get("speak", Rule.speak)
        if not isinstance(speak, bool):
            # `speak = 1` would work by accident and `speak = "false"` -- a
            # string, and truthy -- would do the opposite of what it says.
            raise ValueError(f"{label}: speak must be true or false, got {speak!r}")
        max_chars = raw.get("max_chars", Rule.max_chars)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError(f"{label}: max_chars must be a positive integer, got {max_chars!r}")
        if "urgency" in raw:
            # Absent means any; present and empty is a line somebody started
            # and did not finish, so it is an error rather than a synonym.
            urgency = _string(raw, "urgency", "", label)
            if urgency not in URGENCIES:
                wanted = ", ".join(URGENCIES)
                raise ValueError(
                    f"{label}: urgency must be one of {wanted}, or absent for any, got {urgency!r}"
                )
        else:
            urgency = ""
        say = _string(raw, "say", Rule.say, label)
        # Validated even when speak is false: a rule flipped to true next week
        # must not fail at runtime for a mistake that was in the file all
        # along, and by then nobody remembers editing it.
        _check_template(say, label)
        rules.append(
            Rule(
                name=name,
                app=_string(raw, "app", Rule.app, label),
                summary=_string(raw, "summary", Rule.summary, label),
                body=_string(raw, "body", Rule.body, label),
                urgency=urgency,
                speak=speak,
                say=say,
                profile=_string(raw, "profile", Rule.profile, label, allow_empty=False),
                max_chars=max_chars,
            )
        )
    return Ruleset(rules=tuple(rules), max_per_minute=max_per_minute)


def decide(note: Notification, rules: Ruleset) -> Verdict:
    """What to do with one notification: the first matching rule decides.

    Every verdict carries the channel and label, spoken or not, because those
    are facts about the notification rather than about the decision -- what
    the history and the GUI need to name a notification they did not speak.
    """
    channel = f"notify:{_slug(note.app)}"
    for rule in rules.rules:
        if not _matches(rule, note):
            continue
        if not rule.speak:
            return Verdict(
                speak=False,
                channel=channel,
                label=note.app,
                rule=rule.name,
                reason=f"rule {rule.name!r} has speak = false",
            )
        text = render(rule.say, note, rule.max_chars)
        if not text:
            # A notification with no summary and no body: there is nothing to
            # say, and an empty utterance still costs a rate-limit token and
            # a line of history claiming it was spoken.
            return Verdict(
                speak=False,
                channel=channel,
                label=note.app,
                rule=rule.name,
                reason=f"rule {rule.name!r} matched, but the notification has no text to say",
            )
        return Verdict(
            speak=True,
            text=text,
            channel=channel,
            label=note.app,
            profile=rule.profile,
            rule=rule.name,
        )
    # Named so that `speakctl notify recent` answers "why did it not read my
    # WhatsApp" by itself: the app name is what a rule has to be written
    # against, and it is the one thing nobody can guess from the outside.
    return Verdict(
        speak=False,
        channel=channel,
        label=note.app,
        reason=(
            f"no rule matched app {note.app!r} at urgency "
            f"{_URGENCY_NAMES.get(note.urgency, note.urgency)}; "
            "the rules are an allow list, so nothing is spoken until one names it"
        ),
    )


def render(template: str, note: Notification, max_chars: int) -> str:
    """Fill a `say` template from one notification, flattened and clamped.

    The template is the format string and the notification supplies the
    values, never the other way round: a WhatsApp message reading "{body}"
    comes out as those six characters, and one reading "{sender}" cannot
    raise. `load_rules` has already rejected a template this cannot fill, so
    the `ValueError` below is reachable only from a `Rule` built in code.
    """
    values = {
        "app": _flatten(note.app),
        "summary": _flatten(note.summary),
        "body": _flatten(note.body),
    }
    return _clamp(_fill(template, values), max_chars)


class RateLimit:
    """A sliding window: at most `per_minute` yeses in any sixty seconds.

    A group chat waking up produces forty notifications in a minute, and a
    queue of forty is not something anyone sits through -- they reach for the
    daemon's off switch, which costs them Claude Code as well.

    `per_minute <= 0` is unlimited, not silence. The other reading turns a
    safety valve into a mute switch nobody went looking for.
    """

    def __init__(self, per_minute: int, *, now: Callable[[], float] = time.time) -> None:
        self._per_minute = per_minute
        self._now = now
        # Only the accepted calls. Recording refusals too would push the
        # window forward on every blocked notification, so a chatty chat
        # would never come back inside the limit.
        self._accepted: deque[float] = deque()

    def allow(self) -> bool:
        """Take a token if there is one. False means: drop this notification."""
        if self._per_minute <= 0:
            return True
        now = self._now()
        while self._accepted and now - self._accepted[0] >= _WINDOW_SECONDS:
            self._accepted.popleft()
        if len(self._accepted) >= self._per_minute:
            return False
        self._accepted.append(now)
        return True


def _string(
    raw: dict[str, object], key: str, default: str, label: str, *, allow_empty: bool = True
) -> str:
    """One string field of a rule, or a `ValueError` naming the rule."""
    value = raw.get(key, default)
    if not isinstance(value, str):
        # str() would turn a list into "['a', 'b']" and a number into a
        # plausible-looking glob -- a rule that matches nothing, reported by
        # nobody, for the rest of the file's life.
        raise ValueError(f"{label}: {key} must be a string, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ValueError(f"{label}: {key} must not be empty")
    return value


def _check_template(template: str, label: str) -> None:
    """Reject at load anything `render` could not fill.

    This is the check the whole module is arranged around. `{sender}` in a
    `say` template is an easy thing to write and impossible to fill, and the
    moment to find out is while reading the file, not while a message from a
    person is waiting to be read out.
    """
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        # An unbalanced brace: "{summary" and the like.
        raise ValueError(f"{label}: say is not a valid template ({exc})") from exc
    known = ", ".join("{" + field + "}" for field in FIELDS)
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in FIELDS:
            raise ValueError(f"{label}: say uses {{{field}}}, which is not one of {known}")
        if spec or conversion is not None:
            # "{body!r}" would quote the message and "{body:>40}" would pad it
            # with spaces nobody can hear. Neither means anything for speech,
            # so neither is quietly ignored.
            raise ValueError(
                f"{label}: say uses a format spec or conversion on {{{field}}}, "
                f"which means nothing spoken aloud"
            )


def _matches(rule: Rule, note: Notification) -> bool:
    if rule.urgency:
        # -1 for a name load_rules would have rejected, so a Rule built in
        # code with a bad urgency matches nothing rather than raising
        # KeyError with a notification already in hand.
        if note.urgency != URGENCIES.get(rule.urgency, -1):
            return False
    return (
        _glob(rule.app, note.app)
        and _glob(rule.summary, note.summary)
        and _glob(rule.body, note.body)
    )


def _glob(pattern: str, value: str) -> bool:
    """Case-insensitive glob, the same way on every machine.

    `fnmatchcase` on two lowered strings, not `fnmatch`: `fnmatch` folds case
    through `os.path.normcase`, which is identity on Linux and `str.lower` on
    Windows and macOS. A rule file would then mean one thing here and another
    thing there, and the difference would only ever show up as notifications
    somebody else does not get.
    """
    return fnmatchcase(value.lower(), pattern.lower())


def _slug(app: str) -> str:
    """An app name as a channel suffix: lowercase, punctuation to dashes."""
    slug = "".join(char if char.isalnum() else "-" for char in app.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if slug:
        return slug
    # An app name of nothing but punctuation still needs a channel of its own:
    # "notify:" for all of them would put one mute and one skip button across
    # apps with nothing to do with each other. sha256 rather than hash(),
    # which is salted per process -- the channel, and any mute set on it,
    # would move on every restart.
    return "app-" + hashlib.sha256(app.encode("utf-8")).hexdigest()[:8]


def _flatten(text: str) -> str:
    """One line, single-spaced. Bodies are multi-line; the segmenter is not."""
    return _WHITESPACE.sub(" ", text).strip()


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute the template's fields, dropping separators left dangling.

    Walked field by field rather than handed to `str.format` so that a field
    with nothing in it takes its separator with it: `"{summary}. {body}"` on a
    notification with no body says "Mamma.", not "Mamma. ." and not "Mamma. ".
    That falls out of the structure rather than being a case about that one
    template, because the separator is template text -- written by whoever
    wrote the rule -- while the values never are.
    """
    out: list[str] = []
    held = ""  # template text since the last field that had something to say
    for literal, field, _spec, _conversion in Formatter().parse(template):
        held += literal
        if field is None:
            continue
        if field not in values:
            raise ValueError(f"say uses {{{field}}}, which is not one of {', '.join(FIELDS)}")
        value = values[field]
        if not value:
            # Keep `held`: its separator was joining this field to the next
            # one, and the next field decides whether it is still needed.
            continue
        connector = _tidy(held)
        if not out and _is_separator(connector):
            connector = ""  # nothing to separate this from yet
        out.append(connector)
        out.append(value)
        held = ""
    tail = _tidy(held)
    # A trailing separator is worth keeping after something was said -- it is
    # the full stop of the last sentence -- and worth dropping after nothing.
    if out or not _is_separator(tail):
        out.append(tail)
    return "".join(out).strip()


def _tidy(literal: str) -> str:
    """Collapse a run of separators in template text down to one.

    Only ever applied to the template's own literal text. "Wait... what?" in a
    body is the sender's punctuation and stays exactly as they typed it; two
    separators meeting in a template are the mark of a field between them that
    had nothing to say.
    """
    return _SEPARATOR_RUN.sub(r"\1 ", literal)


def _is_separator(text: str) -> bool:
    return all(char in _SEPARATORS for char in text)


def _clamp(text: str, max_chars: int) -> str:
    """Cut to `max_chars` at a word boundary, with nothing to mark the cut.

    No ellipsis and no "...": the synthesiser would read it out.
    """
    if max_chars <= 0:
        # Not reachable from a rule file, which requires a positive max_chars.
        # A negative slice below would quietly cut from the far end instead.
        return ""
    if len(text) <= max_chars:
        return text
    if text[max_chars].isspace():
        # The budget ends exactly on a word boundary, so the last word is
        # whole. Cutting back to the previous space here would throw away a
        # word that fits, every time.
        return text[:max_chars].rstrip()
    head, space, _rest = text[:max_chars].rpartition(" ")
    # No space at all means one word longer than the budget -- a URL, usually.
    # Half of it read aloud beats saying nothing.
    return head if space else text[:max_chars]
