"""Vertragsangaben als lesbarer Text in einem Paperless-Feld – und zurück.

Beispiel: „Laufzeit 24 Monate · verlängert sich um 12 Monate · Kündigungsfrist 3 Monate zum Laufzeitende“
So steht das Wichtigste auch in Paperless, und bei einer Neuinstallation baut der Companion den Vertrag daraus wieder auf.
"""

import re
from datetime import date

UNITS = {"months": ("Monat", "Monate"), "weeks": ("Woche", "Wochen"), "days": ("Tag", "Tage")}
TO = {"term_end": "Laufzeitende", "month_end": "Monatsende", "quarter_end": "Quartalsende", "any": "beliebigen Tag"}


def _plural(n: int, unit: str) -> str:
    one, many = UNITS[unit]
    return f"{n} {one if n == 1 else many}"


def to_text(c) -> str | None:
    parts = []
    if c.initial_term_months:
        parts.append(f"Laufzeit {_plural(c.initial_term_months, 'months')}")
    if c.end_date and (c.renewal_mode == "none" or not c.initial_term_months):
        parts.append(f"endet am {c.end_date:%d.%m.%Y}")
    if c.renewal_mode == "renews" and c.renewal_months:
        parts.append(f"verlängert sich um {_plural(c.renewal_months, 'months')}")
    elif c.renewal_mode == "indefinite":
        parts.append("danach unbefristet")
    elif c.renewal_mode == "none":
        parts.append("endet automatisch")
    if c.notice_value:
        parts.append(f"Kündigungsfrist {_plural(c.notice_value, c.notice_unit or 'months')} zum {TO.get(c.notice_to or 'term_end')}")
    return " · ".join(parts) or None


def from_text(text: str | None) -> dict:
    """Gegenstück zu to_text (verträgt auch kleine Abweichungen beim Abtippen)."""
    text = (text or "").strip()
    values: dict = {}
    if m := re.search(r"laufzeit\s+(\d+)\s*monat", text, re.IGNORECASE):
        values["initial_term_months"] = int(m.group(1))
    if m := re.search(r"endet\s+am\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text, re.IGNORECASE):
        values["end_date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if m := re.search(r"verlängert\s+sich\s+um\s+(\d+)\s*monat", text, re.IGNORECASE):
        values["renewal_mode"], values["renewal_months"] = "renews", int(m.group(1))
    elif re.search(r"unbefristet", text, re.IGNORECASE):
        values["renewal_mode"] = "indefinite"
    elif re.search(r"endet\s+automatisch", text, re.IGNORECASE):
        values["renewal_mode"] = "none"
    if m := re.search(r"kündigungsfrist\s+(\d+)\s*(monat|woche|tag)\w*(?:\s+zum\s+(\w+(?:\s+tag)?))?", text, re.IGNORECASE):
        values["notice_value"] = int(m.group(1))
        values["notice_unit"] = {"monat": "months", "woche": "weeks", "tag": "days"}[m.group(2).lower()]
        to = (m.group(3) or "").lower()
        values["notice_to"] = next((k for k, v in TO.items() if v.lower() == to), "term_end")
    return values
