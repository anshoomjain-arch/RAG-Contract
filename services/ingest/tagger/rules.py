"""Deterministic tags: defined terms used, extracted values, modality."""
from __future__ import annotations

import re
from collections import Counter

# ---- defined terms ----------------------------------------------------------------------------------------------


class TermMatcher:
    """Finds defined terms (longest first, simple plurals) in text. Case-sensitive for contract text;
    ignore_case=True for user questions, returning the term as defined."""

    def __init__(self, terms: list[str], ignore_case: bool = False):
        terms = sorted({t for t in terms if len(t) > 1}, key=len, reverse=True)
        self.terms = terms
        self.canonical = {t.lower(): t for t in reversed(terms)} if ignore_case else None
        alt = "|".join(re.escape(t) for t in terms)
        self.re = re.compile(rf"(?<![\w-])({alt})(?:s|es)?(?![\w-])", re.I if ignore_case else 0) if terms else None

    def find(self, text: str, exclude: str | None = None) -> list[str]:
        if not self.re:
            return []
        hits = (m.group(1) for m in self.re.finditer(text))   # alternation is longest-first
        if self.canonical is not None:
            hits = (self.canonical.get(h.lower(), h) for h in hits)
        found = dict.fromkeys(hits)
        found.pop(exclude, None)
        return list(found)


# ---- values -----------------------------------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
MONEY_CUR_FIRST = re.compile(rf"\b(USD|US\$|EUR|GBP|MGA)\s?({_NUM})(?![\d,])")
MONEY_AMT_FIRST = re.compile(rf"(?<![\w.])({_NUM})\s?(USD|MGA|EUR|GBP)\b")
PERCENT = re.compile(rf"(?<![\w.])({_NUM})\s?(%|per\s?cent\b)", re.IGNORECASE)
DURATION = re.compile(
    r"(?:\b([a-z]+(?:[\s-][a-z]+){0,3})\s)?\((\d+)\)\s+(Business Days?|calendar days?|days?|weeks?|calendar months?|"
    r"months?|years?|hours?|minutes?)\b"
    r"|(?<![\w.])(\d+)\s+(Business Days?|calendar days?|days?|weeks?|calendar months?|months?|years?|hours?)\b",
    re.IGNORECASE)
MEASURE = re.compile(rf"(?<![\w.])({_NUM})\s?(sqm|square metres|metres|meters|km|kg|kW|kVA|VDC|V)\b")
_UNITS = {"business day": "business_days", "calendar day": "days", "day": "days", "week": "weeks",
          "calendar month": "months", "month": "months", "year": "years", "hour": "hours", "minute": "minutes"}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _unit(u: str) -> str:
    u = u.lower().rstrip("s")
    return _UNITS.get(u, u)


def extract_values(text: str) -> list[dict]:
    out: list[dict] = []
    cur_first = list(MONEY_CUR_FIRST.finditer(text))
    for m in cur_first:
        out.append({"type": "money", "currency": m.group(1).replace("US$", "USD"), "amount": _num(m.group(2)),
                    "text": m.group(0)})
    for m in MONEY_AMT_FIRST.finditer(text):
        # "3.3 USD 1,456": the currency already belongs to "USD 1,456", so "3.3" is not an amount.
        if not any(c.start() <= m.start(2) < c.end() for c in cur_first):
            out.append({"type": "money", "currency": m.group(2), "amount": _num(m.group(1)), "text": m.group(0)})
    for m in PERCENT.finditer(text):
        out.append({"type": "percent", "value": _num(m.group(1)), "text": m.group(0)})
    for m in DURATION.finditer(text):
        if m.group(2):
            out.append({"type": "duration", "value": int(m.group(2)), "unit": _unit(m.group(3)),
                        "text": f"({m.group(2)}) {m.group(3)}"})
        else:
            out.append({"type": "duration", "value": int(m.group(4)), "unit": _unit(m.group(5)), "text": m.group(0)})
    for m in MEASURE.finditer(text):
        out.append({"type": "quantity", "value": _num(m.group(1)), "unit": m.group(2), "text": m.group(0)})
    seen, unique = set(), []
    for v in out:
        key = (v["type"], v.get("currency") or v.get("unit"), v.get("amount", v.get("value")))
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


# ---- modality ---------------------------------------------------------------------------------------------------

PROHIBITION = re.compile(r"\b(?:shall|must|may|will)\s+not\b|\bshall\s+in\s+no\s+event\b|\bshall\s+never\b", re.I)
RIGHT = re.compile(r"\b(?:shall\s+be\s+entitled|is\s+entitled|are\s+entitled|shall\s+have\s+the\s+right|"
                   r"has\s+the\s+right|have\s+the\s+right|may)\b", re.I)
OBLIGATION = re.compile(r"\b(?:shall|must|undertakes?\s+to|agrees?\s+to|is\s+required\s+to)\b", re.I)
CONDITION = re.compile(r"\b(?:subject\s+to|provided\s+that|provided,\s+however|in\s+the\s+event|unless|if)\b", re.I)


def modality(text: str) -> tuple[str | None, dict]:
    """Primary modality and counts. 'shall not' is a prohibition, 'shall be entitled' a right, not obligations."""
    prohib = len(PROHIBITION.findall(text))
    rights = len(RIGHT.findall(text)) - len(re.findall(r"\bmay\s+not\b", text, re.I))
    oblig = len(OBLIGATION.findall(text)) - len(re.findall(r"\b(?:shall|must)\s+not\b", text, re.I)) \
        - len(re.findall(r"\bshall\s+(?:be\s+entitled|have\s+the\s+right|in\s+no\s+event|never)\b", text, re.I))
    cond = len(CONDITION.findall(text))
    counts = {"obligation": max(oblig, 0), "prohibition": prohib, "right": max(rights, 0), "condition": cond}
    order = ["prohibition", "obligation", "right"]   # tie-break: the stronger statement wins
    best = max(order, key=lambda k: (counts[k], -order.index(k)))
    if counts[best] == 0:
        return ("condition" if cond else None), counts
    return best, counts


