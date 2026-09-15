"""The rules file `speakctl notify init` writes.

A hand-written file rather than a dump of `DEFAULT_RULES`, because the
comments are most of its value: someone opening it needs to know that the
list is an allow list, that the first match wins, and above all that
`speakctl notify recent` is how you find out what an app actually calls
itself. A serialiser cannot say any of that.

The cost of writing it by hand is that it can drift from the defaults it is
supposed to mirror, so a test speaks a few notifications through both and
holds them to the same answers.
"""

from __future__ import annotations

STARTER_TOML = """\
# Which desktop notifications speakd reads aloud.
#
# Rules are tried in order and the first one that matches decides, so put the
# specific ones above the general ones. A notification matching no rule is not
# spoken: this is an allow list, not a block list.
#
# Match on `app`, `summary` and `body` -- case-insensitive globs, and any you
# leave out matches anything -- and on `urgency`, one of low, normal or
# critical. `say` is a template over {app}, {summary} and {body}. Set
# `speak = false` to silence one app without silencing its neighbours.
#
# You will need `speakctl notify recent` to write good rules here. It shows
# what actually arrived and which rule decided it, which is the only way to
# learn what an app calls itself -- Chrome puts the sender in {summary} and
# their message in {body}, but what it sets {app} to is worth checking rather
# than guessing.

# At most this many spoken notifications a minute; 0 means no limit. A group
# chat waking up can produce forty, and a queue of forty is not something
# anyone sits through.
max_per_minute = 20

# Everything Chrome shows, which includes WhatsApp Web. Narrow it once
# `speakctl notify recent` has told you how to tell WhatsApp from the rest --
# by summary, or by a rule above this one that matches the others and sets
# speak = false.
#
# "*chrom*", not "*chrome*": Chromium does not contain "chrome".
[[rule]]
name = "chrome"
app = "*chrom*"
say = "{summary} says. {body}"

[[rule]]
name = "thunderbird"
app = "*thunderbird*"
say = "Email. {summary}. {body}"

[[rule]]
name = "evolution"
app = "*evolution*"
say = "Email. {summary}. {body}"
"""
