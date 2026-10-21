"""Gefundene Werte im OCR-Text markieren, damit man sie beim Prüfen schnell wiederfindet."""

import re
from datetime import date

from markupsafe import Markup, escape

from app.extraction.rules import MONTHS

LABELS = {
    "amount": "Betrag", "invoice_date": "Rechnungsdatum", "due_date": "Fällig am", "invoice_number": "Rechnungsnummer",
    "iban": "IBAN", "paid_on": "Bezahlt am", "contract_number": "Vertragsnummer", "start_date": "Vertragsbeginn",
    "end_date": "Laufzeitende", "monthly_cost": "Kosten pro Monat", "purchase_date": "Kaufdatum", "price": "Preis",
}
MONTH_NAMES = {}
for _name, _num in MONTHS.items():
    MONTH_NAMES.setdefault(_num, []).append(_name)


def _amount_pattern(value: float) -> str:
    whole, cents = f"{value:.2f}".split(".")
    grouped = re.sub(r"(\d)(?=(\d{3})+$)", r"\1.", whole)
    variants = {grouped, whole}
    alts = "|".join(re.escape(v).replace(r"\.", r"[.\s]?") for v in sorted(variants, key=len, reverse=True))
    return rf"(?<![\d,.])(?:{alts})[,.]{cents}(?!\d)"


def _date_pattern(value: date) -> str:
    d, m, y = value.day, value.month, value.year
    numeric = rf"(?<!\d)0?{d}\.\s?0?{m}\.\s?(?:{y}|{y % 100:02d})(?!\d)"
    names = "|".join(re.escape(n) for n in MONTH_NAMES.get(m, []))
    textual = rf"(?<!\d)0?{d}\.?\s+(?:{names})\.?\s+{y}" if names else None
    iso = rf"{y}-{m:02d}-{d:02d}"
    return "|".join(p for p in (numeric, textual, iso) if p)


def _flexible(text: str) -> str:
    """Zeichenfolge, bei der Leerzeichen/Bindestriche im OCR-Text abweichen dürfen."""
    chars = [re.escape(c) for c in re.sub(r"[\s\-]", "", text)]
    return r"[\s\-]?".join(chars)


def patterns(suggestions: dict) -> list[tuple[str, str]]:
    """(Feld, Regex) für alle markierbaren Vorschläge."""
    result = []
    for name, item in (suggestions or {}).items():
        if name.startswith("_") or not isinstance(item, dict) or name not in LABELS:
            continue
        value = item.get("value")
        if value in (None, ""):
            continue
        try:
            if name in ("amount", "monthly_cost", "price"):
                result.append((name, _amount_pattern(float(value))))
            elif name.endswith("_date") or name in ("paid_on",):
                result.append((name, _date_pattern(date.fromisoformat(str(value)[:10]))))
            elif len(str(value).strip()) >= 3:
                result.append((name, _flexible(str(value))))
        except (TypeError, ValueError):
            continue
    return result


def highlight(text: str, suggestions: dict) -> tuple[Markup, set[str]]:
    """Text als HTML mit <mark> um gefundene Werte. Gibt auch zurück, welche Felder im Text vorkommen."""
    spans: list[tuple[int, int, str]] = []
    for name, pattern in patterns(suggestions):
        for m in re.finditer(pattern, text, re.IGNORECASE):
            spans.append((m.start(), m.end(), name))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out, pos, found = [], 0, set()
    for start, end, name in spans:
        if start < pos:
            continue  # überlappt mit einer bereits markierten Stelle
        out.append(escape(text[pos:start]))
        out.append(Markup('<mark class="hl hl-{0}" title="{1}">{2}</mark>').format(name, LABELS[name], text[start:end]))
        pos = end
        found.add(name)
    out.append(escape(text[pos:]))
    return Markup("").join(out), found
