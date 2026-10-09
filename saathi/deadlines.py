"""Deadlines computed in code, not estimated by a model.

Each escalation step in playbooks.py may carry
    reply_within: {n, unit, basis, source, kind}  - how long that body has to respond (from the day it receives it)
    file_within:  {n, unit, from, basis, source}  - the window the complainant has to approach that step
with unit in hours | days | months | years | working_days and kind "statutory" (a rule in raw/legal/, cited) or
"requested" (a reasonable time the letter asks for where no rule fixes one). The rules were checked against the
indexed legal texts (raw/legal/) on 2026-10-09.

annotate() turns them into dates for one case:
    step["reply_by"]   the date that body must answer by (start + reply_within)
    step["from_date"]  the date you may escalate to this step (the first step's reply_by)
    step["file_by"]    the last date to approach this step (from the incident date or from from_date)
Dates are formatted "12 November 2026"; parse_dates() / amounts() read them back for the critic's consistency check.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | \
         {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}
# National holidays observed everywhere (state holidays vary): used only for working_days.
NATIONAL_HOLIDAYS = {(1, 26), (8, 15), (10, 2)}


def fmt(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_working_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and (d.month, d.day) not in NATIONAL_HOLIDAYS:
            n -= 1
    return d


def add(d: date, rule: dict) -> date:
    n, unit = int(rule["n"]), rule["unit"]
    if unit == "hours":
        return d + timedelta(days=-(-n // 24))  # round up to whole days
    if unit == "days":
        return d + timedelta(days=n)
    if unit == "working_days":
        return add_working_days(d, n)
    if unit == "months":
        return add_months(d, n)
    if unit == "years":
        return add_months(d, 12 * n)
    raise ValueError(f"unknown unit {unit}")


# --------------------------------------------------------------------------- parsing (dates and amounts)

_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|" \
       r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RES = [
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?(?:\s+(\d{{4}}))?\b", re.I), "dmy"),
    (re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b"), "num"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
]


def parse_dates(text: str) -> list[tuple[int | None, int, int, str]]:
    """Every date in the text as (year or None, month, day, matched text). Indian order (day first) for d/m/y."""
    out, taken = [], []
    for rx, kind in DATE_RES:
        for m in rx.finditer(text or ""):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            try:
                if kind == "dmy":
                    d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
                elif kind == "mdy":
                    mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
                elif kind == "num":
                    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
                    y = str(2000 + int(y)) if len(y) == 2 else y
                else:
                    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
                year = int(y) if y else None
                date(year or 2000, mo, d)  # validates
            except (KeyError, ValueError):
                continue
            taken.append((m.start(), m.end()))
            out.append((year, mo, d, m.group(0)))
    return out


def to_date(text: str, today: date | None = None) -> date | None:
    """The first date in text; a date without a year is taken as the most recent such date (not in the future)."""
    today = today or date.today()
    for y, mo, d, _ in parse_dates(text or ""):
        if y:
            return date(y, mo, d)
        guess = date(today.year, mo, d)
        return guess if guess <= today else date(today.year - 1, mo, d)
    return None


AMOUNT_RE = re.compile(r"(?:₹|\brs\.?|\binr\b|\brupees?\b)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore|k)?\b|"
                       r"\b([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore)?\s*(?:rupees?|/-|rs\b)", re.I)
SCALE = {"lakh": 100_000, "lac": 100_000, "crore": 10_000_000, "k": 1_000}


def amounts(text: str) -> list[tuple[int, str]]:
    """Every rupee amount in the text as (value in rupees, matched text)."""
    out = []
    for m in AMOUNT_RE.finditer(text or ""):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            value = float(num.replace(",", "")) * SCALE.get((unit or "").lower(), 1)
        except ValueError:
            continue
        if value > 0:
            out.append((round(value), m.group(0).strip()))
    return out


# --------------------------------------------------------------------------- deadlines for a case

def annotate(steps: list[dict], *, start: date | None = None, incident: date | None = None) -> dict:
    """Add reply_by / from_date / file_by (formatted) to each step in place. Returns the letter's deadline:
    {date, basis, source, kind} for the first step (what the letter asks the recipient to reply by), or {}."""
    start = start or date.today()
    first_reply = None
    for i, s in enumerate(steps):
        for k in ("reply_by", "from_date", "file_by", "ack_by"):
            s.pop(k, None)
        rw = s.get("reply_within")
        if i == 0 and rw:
            first_reply = add(start, rw)
            s["reply_by"] = fmt(first_reply)
            if s.get("ack_within"):
                s["ack_by"] = fmt(add(start, s["ack_within"]))
        if i > 0 and first_reply:
            s["from_date"] = fmt(first_reply)
        fw = s.get("file_within")
        if fw:
            base = incident if fw.get("from") == "incident" else first_reply
            if base:
                s["file_by"] = fmt(add(base, fw))
    rw = (steps[0] if steps else {}).get("reply_within")
    if not (rw and first_reply):
        return {}
    return {"date": fmt(first_reply), "basis": rw["basis"], "source": rw.get("source", ""),
            "kind": rw.get("kind", "statutory")}


def step_dates(s: dict) -> str:
    """One line of dates for an escalation step (for the ladder in the UI, CLI and final markdown)."""
    parts = []
    if s.get("ack_by"):
        parts.append(f"acknowledge by {s['ack_by']}")
    if s.get("reply_by"):
        parts.append(f"reply due by {s['reply_by']}")
    if s.get("from_date"):
        parts.append(f"you can go here from {s['from_date']} if unresolved")
    if s.get("file_by"):
        parts.append(f"last date to file: {s['file_by']}")
    return " · ".join(parts)
