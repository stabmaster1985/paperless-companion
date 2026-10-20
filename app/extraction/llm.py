"""Gezielte LLM-Abfragen für Felder, die die Regeln nicht sicher gefunden haben.

Kleine Modelle auf der CPU arbeiten deutlich besser (und schneller), wenn sie
nur wenige konkrete Felder und nur die relevanten Textstellen bekommen.
"""

import re
from datetime import date

from app.extraction.rules import field, normalize, valid_iban
from app.ollama import OllamaClient

SYSTEM = (
    "Du extrahierst Daten aus deutschen Dokumenten (OCR-Text, kann Fehler enthalten). "
    "Antworte ausschließlich mit JSON nach dem vorgegebenen Schema. "
    "Wenn eine Angabe nicht eindeutig im Text steht, gib null zurück. Erfinde nichts. "
    "Datumsangaben im Format JJJJ-MM-TT, Beträge als Zahl mit Punkt als Dezimaltrennzeichen."
)

# Feld -> (JSON-Typ, Beschreibung für das Modell)
FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "invoice": {
        "amount": ("number", "zu zahlender Gesamtbetrag (brutto) in Euro"),
        "invoice_date": ("string", "Rechnungsdatum"),
        "due_date": ("string", "Fälligkeitsdatum / zahlbar bis"),
        "invoice_number": ("string", "Rechnungsnummer"),
        "payment_method": ("string", "'debit' wenn per Lastschrift abgebucht wird, sonst 'transfer'"),
    },
    "contract": {
        "start_date": ("string", "Vertragsbeginn"),
        "initial_term_months": ("integer", "Mindest-/Erstlaufzeit in Monaten"),
        "renewal_mode": ("string", "'renews' wenn sich der Vertrag um einen festen Zeitraum verlängert, "
                                   "'indefinite' wenn er danach unbefristet läuft, 'none' wenn er automatisch endet"),
        "renewal_months": ("integer", "Verlängerungszeitraum in Monaten"),
        "notice_value": ("integer", "Zahl der Kündigungsfrist, z.B. 3 bei 'drei Monate'"),
        "notice_unit": ("string", "Einheit der Kündigungsfrist: 'days', 'weeks' oder 'months'"),
        "notice_to": ("string", "'term_end' (zum Laufzeitende), 'month_end' (zum Monatsende), "
                                "'quarter_end' (zum Quartalsende) oder 'any' (jederzeit)"),
        "end_date": ("string", "festes Vertragsende, falls genannt"),
        "monthly_cost": ("number", "Kosten pro Monat in Euro (Jahresbeiträge durch 12 teilen)"),
        "contract_number": ("string", "Vertrags-, Kunden- oder Versicherungsscheinnummer"),
    },
    "warranty": {
        "purchase_date": ("string", "Kaufdatum"),
        "months": ("integer", "Garantiedauer in Monaten, nur wenn ausdrücklich genannt"),
    },
}

ENUMS = {
    "payment_method": {"debit", "transfer", "other"},
    "renewal_mode": {"renews", "indefinite", "none"},
    "notice_unit": {"days", "weeks", "months"},
    "notice_to": {"term_end", "month_end", "quarter_end", "any"},
}

KEYWORDS = {
    "invoice": r"betrag|summe|gesamt|fällig|zahlbar|rechnung|datum|lastschrift|iban|überweis",
    "contract": r"kündig|laufzeit|verlänger|beginn|vertrag|frist|monatlich|beitrag|jährlich|ende",
    "warranty": r"garantie|gewährleistung|kauf|datum",
}

LLM_CONFIDENCE = 0.6


def select_snippets(text: str, kind: str, max_chars: int) -> str:
    """Kopfbereich plus Zeilen rund um Schlüsselwörter, gekürzt auf max_chars."""
    lines = [line for line in normalize(text).splitlines() if line]
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return "\n".join(lines)
    keep = set(range(min(12, len(lines))))
    pattern = re.compile(KEYWORDS[kind], re.IGNORECASE)
    for i, line in enumerate(lines):
        if pattern.search(line):
            keep.update(range(max(0, i - 1), min(len(lines), i + 3)))
    out, size = [], 0
    for i in sorted(keep):
        if size + len(lines[i]) + 1 > max_chars:
            break
        out.append(lines[i])
        size += len(lines[i]) + 1
    return "\n".join(out)


def build_schema(kind: str, wanted: list[str]) -> dict:
    props = {name: {"type": [FIELDS[kind][name][0], "null"]} for name in wanted}
    return {"type": "object", "properties": props, "required": wanted}


def build_prompt(kind: str, wanted: list[str], snippet: str) -> str:
    lines = [f"- {name}: {FIELDS[kind][name][1]}" for name in wanted]
    return "Gesuchte Felder:\n" + "\n".join(lines) + "\n\nDokumenttext:\n\"\"\"\n" + snippet + "\n\"\"\""


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _compact(s: str) -> str:
    return re.sub(r"\s", "", s).lower()


def validate(kind: str, raw: dict, text: str) -> dict:
    """Prüft und normalisiert die Modellantwort. Werte, die nicht im Text belegbar sind, fallen weg."""
    result: dict = {}
    flat = normalize(text)
    flat_digits = _digits(flat)
    for name, value in raw.items():
        if name not in FIELDS[kind] or value in (None, "", "null"):
            continue
        json_type = FIELDS[kind][name][0]
        try:
            if name in ENUMS:
                value = str(value).strip().lower()
                if value not in ENUMS[name]:
                    continue
            elif json_type == "string" and name.endswith("_date"):
                value = date.fromisoformat(str(value)[:10])
                # Datum muss in irgendeiner Form im Text vorkommen
                if not any(token in flat for token in (
                    value.strftime("%d.%m.%Y"), value.strftime("%d.%m.%y"), f"{value.day}.{value.month}.",
                    value.isoformat(), f"{value.day:02d}.{value.month:02d}.",
                )):
                    continue
            elif json_type == "integer":
                value = int(value)
                if not 0 < value <= 600:
                    continue
            elif json_type == "number":
                value = round(float(value), 2)
                if value <= 0:
                    continue
                if name == "amount" and _digits(f"{value:.2f}") not in flat_digits:
                    continue
            else:
                value = str(value).strip()
                if name == "iban" and not valid_iban(value.replace(" ", "").upper()):
                    continue
                # Nummern müssen wörtlich im Text stehen
                if name.endswith("_number") and _compact(value) not in _compact(flat):
                    continue
        except (TypeError, ValueError):
            continue
        result[name] = field(value, LLM_CONFIDENCE, "llm")
    return result


def extract(client: OllamaClient, kind: str, text: str, wanted: list[str], max_chars: int) -> dict:
    wanted = [name for name in wanted if name in FIELDS.get(kind, {})]
    if not wanted:
        return {}
    snippet = select_snippets(text, kind, max_chars)
    raw = client.extract_json(SYSTEM, build_prompt(kind, wanted, snippet), build_schema(kind, wanted))
    return validate(kind, raw, text)
