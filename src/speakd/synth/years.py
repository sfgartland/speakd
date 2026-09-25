"""Rewrite years into the words people say, for engines that read them digit by digit.

Piper phonemises with espeak-ng, which reads "1994" as "one thousand nine hundred
ninety four". This pure text rule converts standalone years from 1100 to 2099 into
spoken form -- "nineteen ninety-four" -- before the text reaches the engine.
"""

import re

_YEAR = re.compile(r"(?<![\w@/.,:-])(1[1-9]\d\d|20\d\d)(s?)(?![\w/-]|[.,:]\d)")

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()

_TENS = {
    2: "twenty",
    3: "thirty",
    4: "forty",
    5: "fifty",
    6: "sixty",
    7: "seventy",
    8: "eighty",
    9: "ninety",
}


def _number_words(number: int) -> str:
    if number < 20:
        return _ONES[number]
    tens, rest = divmod(number, 10)
    word = _TENS[tens]
    return f"{word}-{_ONES[rest]}" if rest else word


def _year_words(year: int) -> str:
    century, rest = divmod(year, 100)
    if year == 2000:
        return "two thousand"
    if rest == 0:
        return f"{_number_words(century)} hundred"
    if century == 20:
        if rest < 10:
            return f"two thousand {_number_words(rest)}"
        return f"twenty {_number_words(rest)}"
    if rest < 10:
        return f"{_number_words(century)} oh {_number_words(rest)}"
    return f"{_number_words(century)} {_number_words(rest)}"


def _pluralize(words: str) -> str:
    if words.endswith("y"):
        return f"{words[:-1]}ies"
    return f"{words}s"


def _replace(match: re.Match[str]) -> str:
    words = _year_words(int(match.group(1)))
    if match.group(2):
        words = _pluralize(words)
    return words


def speak_years(text: str) -> str:
    return _YEAR.sub(_replace, text)
