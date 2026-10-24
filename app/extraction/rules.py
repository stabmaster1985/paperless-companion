"""Regelbasierte Extraktion für deutschsprachige Dokumente (ohne AI)."""

import re
from datetime import date, timedelta

MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "april": 4, "apr": 4,
    "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
NUM_WORDS = {
    "ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1, "eines": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
    "achtzehn": 18, "vierundzwanzig": 24, "sechsunddreißig": 36,
}

NUM = r"(\d{1,3}|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")"
UNIT = r"(tag(?:e|en)?|woche(?:n)?|monat(?:e|en|s)?|jahr(?:e|en|es)?)"
GAP = r"[\s\S]{0,80}?"  # kurze Lücke, auch über Zeilenumbrüche

_month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    r"(?<!\d)(?P<d>\d{1,2})\.\s?(?P<m>\d{1,2})\.\s?(?P<y>\d{4}|\d{2})(?!\d)"
    r"|(?<!\d)(?P<d2>\d{1,2})\.?\s+(?P<mn>" + _month_names + r")\.?\s+(?P<y2>\d{4})"
    r"|(?<!\d)(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})(?!\d)",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"(?<![\d,.])(?P<int>\d{1,3}(?:\.\d{3})+|\d+),(?P<dec>\d{2})(?![\d])\s*(?:€|eur\b|euro\b)?"
    r"|(?:€|eur)\s?(?P<int2>\d{1,3}(?:\.\d{3})+|\d+),(?P<dec2>\d{2})(?!\d)",
    re.IGNORECASE,
)
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?)\b")
IBAN_LENGTHS = {"DE": 22, "AT": 20, "CH": 21, "NL": 18, "BE": 16, "FR": 27, "LU": 20, "IT": 27, "ES": 24}

AMOUNT_LABELS = [
    (r"zu\s+zahlende[rn]?\s+betrag|zahlbetrag|rechnungsbetrag|endbetrag|gesamtbetrag|bruttobetrag", 0.9),
    (r"gesamtsumme|summe\s+brutto|brutto\s*summe|gesamt\s*\(?brutto\)?|rechnungssumme", 0.85),
    (r"\bgesamt\b|\bsumme\b|\bbetrag\b", 0.6),
]
DUE_LABEL = (
    r"(?:fällig(?:keit)?(?:\s+(?:am|bis|zum))?|zahlbar\s+(?:bis|zum)|zahlungsziel|"
    r"spätestens\s+(?:am|bis)|bitte\s+(?:überweisen|zahlen)\s+sie[^\n]{0,60}?bis(?:\s+zum)?)"
)
DEBIT_RE = re.compile(
    r"lastschrift|bankeinzug|abbuchung|wird\s+(?:[^\n.]{0,60}?)(?:abgebucht|eingezogen)|einzug\s+von\s+ihrem\s+konto",
    re.IGNORECASE,
)
PAID_RE = re.compile(
    r"bereits\s+bezahlt|dankend\s+erhalten|betrag\s+erhalten|zahlung\s+erhalten|bezahlt\s+am|beglichen|"
    r"zahlungsart:?\s*(?:paypal|kreditkarte|visa|mastercard|bar)|bezahlt\s+mit",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    text = text.replace(" ", " ").replace(" ", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())


def to_int(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUM_WORDS.get(token)


def _make_date(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        result = date(y, m, d)
    except ValueError:
        return None
    return result if 1990 <= result.year <= 2100 else None


def find_dates(text: str) -> list[tuple[int, date]]:
    found = []
    for m in DATE_RE.finditer(text):
        if m.group("d"):
            d = _make_date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        elif m.group("d2"):
            d = _make_date(int(m.group("y2")), MONTHS[m.group("mn").lower()], int(m.group("d2")))
        else:
            d = _make_date(int(m.group("iy")), int(m.group("im")), int(m.group("id")))
        if d:
            found.append((m.start(), d))
    return found


def parse_amount(match: re.Match) -> float:
    whole = match.group("int") or match.group("int2")
    dec = match.group("dec") or match.group("dec2")
    return round(float(whole.replace(".", "") + "." + dec), 2)


def find_amounts(text: str) -> list[tuple[int, float]]:
    return [(m.start(), parse_amount(m)) for m in AMOUNT_RE.finditer(text)]


def date_after_label(text: str, label: str, window: int = 80) -> date | None:
    for m in re.finditer(label, text, re.IGNORECASE):
        dates = find_dates(text[m.end(): m.end() + window])
        if dates:
            return dates[0][1]
    return None


def field(value, confidence: float, source: str = "rule") -> dict:
    if isinstance(value, date):
        value = value.isoformat()
    return {"value": value, "confidence": round(confidence, 2), "source": source}


# --------------------------------------------------------------------------- Rechnungen


def extract_amount(text: str) -> dict | None:
    lines = text.splitlines()
    for label, confidence in AMOUNT_LABELS:
        best = None
        for i, line in enumerate(lines):
            if not re.search(label, line, re.IGNORECASE):
                continue
            amounts = find_amounts(line) or (find_amounts(lines[i + 1]) if i + 1 < len(lines) else [])
            if amounts:
                best = amounts[-1][1]  # zuletzt genannter Betrag in der Summenzeile
        if best is not None:
            return field(best, confidence)
    amounts = [a for _, a in find_amounts(text)]
    if amounts:
        return field(max(amounts), 0.4)
    return None


def extract_iban(text: str) -> dict | None:
    for m in IBAN_RE.finditer(text.upper()):
        iban = m.group(1).replace(" ", "")
        expected = IBAN_LENGTHS.get(iban[:2])
        if expected and len(iban) > expected:
            iban = iban[:expected]
        if valid_iban(iban):
            return field(iban, 0.95)
    return None


def valid_iban(iban: str) -> bool:
    if len(iban) < 15 or IBAN_LENGTHS.get(iban[:2], len(iban)) != len(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def extract_number(text: str, label: str) -> str | None:
    m = re.search(label + r"\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/.]{2,30})", text, re.IGNORECASE)
    if m:
        value = m.group(1).rstrip(".-/")
        if re.search(r"\d", value):
            return value
    return None


def extract_invoice(text: str, created: date | None = None) -> dict:
    text = normalize(text)
    result: dict = {}

    amount = extract_amount(text)
    if amount:
        result["amount"] = amount

    invoice_date = date_after_label(text, r"rechnungsdatum|datum\s+der\s+rechnung|belegdatum|ausstellungsdatum")
    if invoice_date:
        result["invoice_date"] = field(invoice_date, 0.9)
    elif created:
        result["invoice_date"] = field(created, 0.7, "paperless")

    number = extract_number(
        text, r"(?:rechnungs?[\s-]*(?:nr|nummer|no)|rechnung\s+nr|beleg[\s-]*(?:nr|nummer))"
    )
    if number:
        result["invoice_number"] = field(number, 0.85)

    iban = extract_iban(text)
    if iban:
        result["iban"] = iban

    if DEBIT_RE.search(text):
        result["payment_method"] = field("debit", 0.8)
    if PAID_RE.search(text):
        result["status"] = field("paid", 0.75)

    due = date_after_label(text, DUE_LABEL)
    if due:
        result["due_date"] = field(due, 0.9)
    else:
        m = re.search(
            r"(?:innerhalb\s+(?:von\s+)?|zahlungsziel:?\s*)(\d{1,3})\s+tage|(\d{1,3})\s+tage\s+netto",
            text,
            re.IGNORECASE,
        )
        base = invoice_date or created
        if m and base:
            days = int(m.group(1) or m.group(2))
            result["due_date"] = field(base + timedelta(days=days), 0.8)

    warranty = extract_warranty_months(text)
    if warranty:
        result["warranty_months"] = warranty
    return result


# --------------------------------------------------------------------------- Verträge


def _unit_to_months(value: int, unit: str) -> int | None:
    unit = unit.lower()
    if unit.startswith("jahr"):
        return value * 12
    if unit.startswith("monat"):
        return value
    return None


def _unit_key(unit: str) -> str:
    unit = unit.lower()
    if unit.startswith("tag"):
        return "days"
    if unit.startswith("woche"):
        return "weeks"
    if unit.startswith("jahr"):
        return "years"
    return "months"


def extract_notice(text: str) -> dict:
    result: dict = {}
    patterns = [
        r"kündigungsfrist\s*(?:von|beträgt|:)?\s*(?:jeweils\s+|mindestens\s+)?" + NUM + r"\s*" + UNIT,
        r"(?:kündig\w*)[^.]{0,120}?frist\s+von\s+(?:jeweils\s+|mindestens\s+)?" + NUM + r"\s*" + UNIT,
        r"(?:kündig\w*)[^.]{0,120}?" + NUM + r"\s*" + UNIT + r"\s+(?:vor|zum)\s+(?:ablauf|ende)",
        r"frist\s+von\s+" + NUM + r"\s*" + UNIT + r"[^.]{0,80}?kündig",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        value = to_int(m.group(1))
        if value is None:
            continue
        unit = _unit_key(m.group(2))
        if unit == "years":
            value, unit = value * 12, "months"
        result["notice_value"] = field(value, 0.85)
        result["notice_unit"] = field(unit, 0.85)

        tail = text[m.end(): m.end() + 120].lower()
        head_tail = text[max(0, m.start() - 40): m.end() + 120].lower()
        if re.search(r"monatsende|ende\s+(?:des|eines)\s+(?:kalender)?monats|zum\s+schluss\s+eines\s+monats", tail):
            result["notice_to"] = field("month_end", 0.8)
        elif re.search(r"quartal", tail):
            result["notice_to"] = field("quarter_end", 0.8)
        elif re.search(r"laufzeit|vertragsende|vertragsjahr|versicherungsjahr|ablauf|ende\s+der", head_tail):
            result["notice_to"] = field("term_end", 0.8)
        elif re.search(r"jederzeit", head_tail):
            result["notice_to"] = field("any", 0.7)
        break
    return result


def extract_contract(text: str, created: date | None = None) -> dict:
    text = normalize(text)
    result: dict = {}
    result.update(extract_notice(text))

    m = re.search(
        r"(?:mindest(?:vertrags)?laufzeit|vertragslaufzeit|erstlaufzeit|laufzeit)\s*(?:von|beträgt|:)?\s*"
        r"(?:zunächst\s+|mindestens\s+)?" + NUM + r"\s*" + UNIT,
        text,
        re.IGNORECASE,
    )
    if m and (value := to_int(m.group(1))) and (months := _unit_to_months(value, m.group(2))):
        result["initial_term_months"] = field(months, 0.85)

    if re.search(r"auf\s+unbestimmte\s+zeit|unbefristet", text, re.IGNORECASE):
        result["renewal_mode"] = field("indefinite", 0.75)
    m = re.search(
        r"verlängert\s+sich" + GAP + r"(?:um\s+)?(?:jeweils\s+)?" + NUM + r"\s*(?:weitere[sn]?\s+)?" + UNIT,
        text,
        re.IGNORECASE,
    )
    if m and (value := to_int(m.group(1))) and (months := _unit_to_months(value, m.group(2))):
        result["renewal_mode"] = field("renews", 0.85)
        result["renewal_months"] = field(months, 0.85)

    start = date_after_label(
        text,
        r"vertragsbeginn|versicherungsbeginn|laufzeitbeginn|beginn\s+(?:der\s+versicherung|des\s+vertrag\w*)|"
        r"\bbeginn\b|beginnt\s+am|gültig\s+ab|vertrag\s+ab",
        window=40,
    )
    if start:
        result["start_date"] = field(start, 0.8)

    end = date_after_label(
        text, r"vertragsende|endet\s+am|läuft\s+bis|gültig\s+bis|befristet\s+bis|ablauf\s+der\s+versicherung", 40
    )
    if end and (not start or end > start):
        result["end_date"] = field(end, 0.75)

    number = extract_number(
        text,
        r"(?:vertrags|kunden|versicherungsschein|police|mitglieds|konto)[\s-]*(?:nr|nummer|no)",
    )
    if number:
        result["contract_number"] = field(number, 0.85)

    cost = extract_monthly_cost(text)
    if cost:
        result["monthly_cost"] = cost
    return result


COST_PERIODS = [
    (r"monatlich|mtl\.?|pro\s+monat|/\s?monat|je\s+monat|monatsbeitrag|monatliche\w*", 1),
    (r"vierteljährlich|pro\s+quartal|/\s?quartal|quartalsbeitrag", 3),
    (r"halbjährlich|pro\s+halbjahr", 6),
    (r"jährlich|pro\s+jahr|/\s?jahr|jahresbeitrag|jahresprämie|jahresbetrag|p\.\s?a\.", 12),
]


def extract_monthly_cost(text: str) -> dict | None:
    for line in text.splitlines():
        amounts = find_amounts(line)
        if not amounts:
            continue
        for pattern, months in COST_PERIODS:
            if re.search(pattern, line, re.IGNORECASE):
                # "jährlich" steckt auch in "vierteljährlich"/"halbjährlich" -> Reihenfolge oben wichtig
                return field(round(amounts[-1][1] / months, 2), 0.7 if months == 1 else 0.6)
    return None


# --------------------------------------------------------------------------- Garantie


def extract_warranty_months(text: str) -> dict | None:
    m = re.search(NUM + r"\s*" + UNIT + r"\s*(?:hersteller)?garantie", text, re.IGNORECASE) or re.search(
        r"garantie\w*\s*(?:von|:|beträgt)?\s*" + NUM + r"\s*" + UNIT, text, re.IGNORECASE
    )
    if m and (value := to_int(m.group(1))) and (months := _unit_to_months(value, m.group(2))):
        return field(months, 0.8)
    return None


def extract_warranty(text: str, created: date | None = None) -> dict:
    text = normalize(text)
    result: dict = {}
    purchase = date_after_label(text, r"kaufdatum|rechnungsdatum|lieferdatum|belegdatum|datum") or created
    if purchase:
        result["purchase_date"] = field(purchase, 0.8 if purchase != created else 0.7, "rule")
    months = extract_warranty_months(text)
    if months:
        result["months"] = months
        result["warranty_type"] = field("garantie", 0.8)
    else:
        result["months"] = field(24, 0.6, "default")
        result["warranty_type"] = field("gewaehrleistung", 0.6, "default")
    amount = extract_amount(text)
    if amount:
        result["price"] = amount
    return result


# --------------------------------------------------------------------------- Klassifizierung


# Abrechnungen, die keine Rechnungen sind (enthalten „…abrechnung“, sind aber nicht zu bezahlen)
NOT_INVOICE = re.compile(
    r"(lohn|gehalts?|entgelt|bezüge|bezuege|verdienst|besoldung|vergütungs?|renten?)\s*-?\s*(und\s+\w+\s*)?abrechnung"
    r"|abrechnung\s+der\s+(brutto|bezüge|bezuege)|brutto\s*/\s*netto|lohnsteuerbescheinigung"
    r"|kontoauszug|kreditkartenabrechnung|depotauszug")


def classify_by_keywords(title: str, text: str) -> str:
    title = title.lower()
    head = text[:2500].lower()
    if NOT_INVOICE.search(title) or NOT_INVOICE.search(head[:600]):
        return "other"
    if re.search(r"rechnung|invoice", title):
        return "invoice"
    if re.search(r"vertrag|police|versicherungsschein", title):
        return "contract"
    if re.search(r"rechnungsnummer|rechnungsbetrag|rechnungsdatum|rechnungs-nr|beitragsrechnung", head):
        return "invoice"
    if re.search(r"versicherungsschein|vertragsbestätigung", head) or (
        "vertrag" in head and re.search(r"laufzeit|kündig", head)
    ):
        return "contract"
    if re.search(r"\brechnung\b", head):
        return "invoice"
    if re.search(r"kassenbon|kaufbeleg|garantieschein|quittung", title + "\n" + head):
        return "warranty"
    return "other"


EXTRACTORS = {
    "invoice": extract_invoice,
    "contract": extract_contract,
    "warranty": extract_warranty,
}
