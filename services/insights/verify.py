"""Verification pass (SPEC section 7): every number and date in a generated value must appear in the text it cites.

Matching is on normalised values, so "USD 1,456.00" matches "USD 1,456", "30 days" matches "thirty (30) days" and
"2 November 2021" matches "the 2nd day of November 2021". Clause references ("Clause 8.2.1") are not checked:
they are locations, not facts. A value that fails is shown as unverified, never silently dropped.
"""
from __future__ import annotations

import re
import unicodedata

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"])}
MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
ORD = r"(?:st|nd|rd|th)?"
DATE_PATTERNS = (
    ("dmy", re.compile(rf"\b(\d{{1,2}}){ORD}\s+(?:day\s+of\s+)?{MONTH_RE},?\s+(\d{{4}})\b", re.I)),
    ("mdy", re.compile(rf"\b{MONTH_RE}\s+(\d{{1,2}}){ORD},?\s+(\d{{4}})\b", re.I)),
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("num", re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")),
    ("my", re.compile(rf"\b{MONTH_RE}\s+(\d{{4}})\b", re.I)),
)
REF_RE = re.compile(
    r"\b(?:clauses?|paragraphs?|paras?\.?|schedules?|sections?|parts?|annex(?:es)?|appendix|appendices|articles?)"
    r"\s+\d+(?:\.\d+)*(?:\s*\(\w{1,4}\))*(?:\s*(?:,|and|or|to|-|–)\s*\d+(?:\.\d+)*(?:\s*\(\w{1,4}\))*)*", re.I)
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
WORD_NUMBERS = {w: i for i, w in enumerate(UNITS)} | TENS
WORD_RE = re.compile(rf"\b({'|'.join(TENS)})(?:[\s-]+({'|'.join(UNITS[1:10])}))?\b|\b({'|'.join(UNITS)})\b", re.I)
ORDINALS = {w: i for i, w in enumerate(
    ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
     "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
     "twentieth"], 1)} | {"thirtieth": 30}
ORDINAL_RE = re.compile(rf"\b({'|'.join(ORDINALS)})\b", re.I)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return text.replace("–", "-").replace("—", "-").replace(" ", " ")


def _month(token: str) -> int:
    t = token.lower().rstrip(".")
    return next(n for name, n in MONTHS.items() if name.startswith(t[:3]))


def _num(token: str) -> str:
    """'1,456.00' -> '1456', '99.850' -> '99.85'."""
    t = token.replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


def find_dates(text: str) -> tuple[list[tuple[str, set[tuple]]], list[tuple[int, int]]]:
    """Dates in text as (original, candidate keys) plus their spans. Keys are (y, m, d) or (y, m); a numeric date
    like 02/11/2021 gets both day-month and month-day readings."""
    found, spans = [], []

    def taken(s, e):
        return any(a < e and s < b for a, b in spans)

    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            if taken(*m.span()):
                continue
            try:
                if kind == "dmy":
                    keys = {(int(m[3]), _month(m[2]), int(m[1]))}
                elif kind == "mdy":
                    keys = {(int(m[3]), _month(m[1]), int(m[2]))}
                elif kind == "iso":
                    keys = {(int(m[1]), int(m[2]), int(m[3]))}
                elif kind == "num":
                    a, b, y = int(m[1]), int(m[2]), int(m[3])
                    keys = {(y, b, a), (y, a, b)}
                else:
                    keys = {(int(m[2]), _month(m[1]))}
            except StopIteration:
                continue
            found.append((m[0], keys))
            spans.append(m.span())
    return found, spans


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + " " * (e - s) + text[e:]
    return text


def _word_numbers(text: str, include_one: bool = True) -> list[tuple[str, str]]:
    out = []
    for m in WORD_RE.finditer(text):
        if m[3]:
            if m[3].lower() == "one" and not include_one:
                continue          # "one of the parties": usually not a quantity
            out.append((m[0], str(WORD_NUMBERS[m[3].lower()])))
        else:
            out.append((m[0], str(TENS[m[1].lower()] + (WORD_NUMBERS[m[2].lower()] if m[2] else 0))))
    return out


KEY_NUMBER_RE = re.compile(
    r"(?:\b(?:USD|US\$|EUR|GBP|MGA|INR|\$|€|£)\s?(\d[\d,]*(?:\.\d+)?))"
    r"|(?:(\d[\d,]*(?:\.\d+)?)\s?(?:%|per\s?cent\b|percent\b))"
    r"|(?:\b(\d[\d,]*(?:\.\d+)?)\)?\s+(?:business\s+|calendar\s+|working\s+)?"
    r"(?:days?|weeks?|months?|years?|hours?|minutes?)\b)"
    r"|(?:\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\b)", re.I)


def key_numbers(text: str) -> set[str]:
    """Amounts, percentages, periods and large numbers: the numbers a summary of this text must not lose.
    Clause numbers and list counters are not included."""
    text = _clean(text)
    return {_num(next(g for g in m.groups() if g)) for m in KEY_NUMBER_RE.finditer(text)}


class Evidence:
    """The numbers and dates present in the cited source text."""

    @classmethod
    def merge(cls, parts) -> "Evidence":
        out = cls("")
        for p in parts:
            out.dates |= p.dates
            out.numbers |= p.numbers
        return out

    def __init__(self, text: str):
        text = _clean(text)
        dates, _ = find_dates(text)
        self.dates = set().union(*(k for _, k in dates)) if dates else set()
        self.dates |= {k[:2] for k in self.dates}
        self.numbers = {_num(n) for n in NUM_RE.findall(text)} | {v for _, v in _word_numbers(text)} | \
                       {str(ORDINALS[w.lower()]) for w in ORDINAL_RE.findall(text)}   # "the twelfth Contract Year"


def check(value: str, evidence: Evidence) -> dict:
    """{'status': verified | unverified | no_values, 'checked': [...], 'missing': [...]}"""
    value = _clean(value)
    value = REF_RE.sub(" ", value)
    dates, spans = find_dates(value)
    rest = _blank(value, spans)
    checked, missing = [], []
    for original, keys in dates:
        checked.append(original)
        if not keys & evidence.dates:
            missing.append(original)
    for token in NUM_RE.findall(rest):
        checked.append(token)
        if _num(token) not in evidence.numbers:
            missing.append(token)
    for original, n in _word_numbers(rest, include_one=False):
        checked.append(original)
        if n not in evidence.numbers:
            missing.append(original)
    status = "no_values" if not checked else ("unverified" if missing else "verified")
    return {"status": status, "checked": checked, "missing": missing}
