"""Zuordnung zwischen Companion-Daten und Paperless-Custom-Fields.

Jede „Rolle“ (z.B. Fälligkeitsdatum) wird über ihren Feldnamen in der .env einem
Paperless-Custom-Field zugeordnet. Der Feldtyp wird aus Paperless gelesen.
"""

from dataclasses import dataclass, field
from datetime import date

# Rolle -> (Einstellung in Settings, Datentyp beim automatischen Anlegen, darf angelegt werden)
ROLES: dict[str, tuple[str, str, bool]] = {
    "amount": ("field_amount", "monetary", True),
    "due_date": ("field_due_date", "date", True),
    "paid_on": ("field_paid_on", "date", True),
    "cancel_by": ("field_cancel_by", "date", True),
    "term_end": ("field_term_end", "date", True),
    "warranty_end": ("field_warranty_end", "date", True),
    "summary": ("field_summary", "string", True),
    # Felder, die der Nutzer selbst pflegt – werden nie automatisch angelegt
    "comment": ("field_comment", "string", False),
    "related": ("field_related", "documentlink", False),
}
# Rollen, die beim automatischen Rückschreiben gesetzt werden
WRITEBACK_ROLES = ("amount", "due_date", "paid_on", "cancel_by", "term_end", "warranty_end")
STRING_LIMIT = 128  # Paperless begrenzt Felder vom Typ „string“ auf 128 Zeichen


@dataclass
class FieldDef:
    id: int
    name: str
    data_type: str
    extra: dict = field(default_factory=dict)

    @property
    def text_limit(self) -> int | None:
        return STRING_LIMIT if self.data_type == "string" else None


def role_names(settings) -> dict[str, str]:
    """Konfigurierte Feldnamen je Rolle (leere Namen = Rolle deaktiviert)."""
    names = {}
    for role, (attr, _, _) in ROLES.items():
        name = (getattr(settings, attr, "") or "").strip()
        if name:
            names[role] = name
    return names


def resolve(definitions: list[dict], settings) -> dict[str, FieldDef]:
    by_name = {d["name"].strip().lower(): d for d in definitions}
    result = {}
    for role, name in role_names(settings).items():
        if d := by_name.get(name.lower()):
            result[role] = FieldDef(d["id"], d["name"], d.get("data_type", "string"), d.get("extra_data") or {})
    return result


def _select_label(fd: FieldDef, value):
    options = fd.extra.get("select_options") or []
    for i, opt in enumerate(options):
        if isinstance(opt, dict) and str(opt.get("id")) == str(value):
            return opt.get("label")
        if isinstance(opt, str) and str(value) == str(i):
            return opt
    return value


def decode(fd: FieldDef, value):
    """Paperless-Wert -> JSON-taugliches Python (Datum als ISO-String)."""
    if value in (None, "", []):
        return None
    try:
        if fd.data_type == "date":
            return str(value)[:10]
        if fd.data_type == "monetary":
            text = str(value).strip()
            text = text[3:] if text[:3].isalpha() else text
            return round(float(text), 2)
        if fd.data_type == "documentlink":
            return [int(v) for v in value]
        if fd.data_type == "integer":
            return int(value)
        if fd.data_type == "float":
            return float(value)
        if fd.data_type == "boolean":
            return bool(value)
        if fd.data_type == "select":
            return _select_label(fd, value)
    except (TypeError, ValueError):
        return None
    return str(value)


def encode(fd: FieldDef, value):
    """Python-Wert -> Paperless-Wert passend zum Feldtyp."""
    if value is None:
        return None
    if fd.data_type == "date":
        return value.isoformat() if isinstance(value, date) else str(value)[:10]
    if fd.data_type == "monetary":
        return f"EUR{float(value):.2f}"
    if fd.data_type == "documentlink":
        return sorted({int(v) for v in value})
    if fd.data_type in ("integer",):
        return int(value)
    if fd.data_type in ("float",):
        return float(value)
    text = value.isoformat() if isinstance(value, date) else str(value)
    if fd.text_limit and len(text) > fd.text_limit:
        text = shorten(text, fd.text_limit)
    return text


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def extract(custom_fields: list[dict] | None, defs: dict[str, FieldDef]) -> dict:
    """Werte der konfigurierten Rollen aus der custom_fields-Liste eines Dokuments."""
    by_id = {fd.id: (role, fd) for role, fd in defs.items()}
    result = {}
    for item in custom_fields or []:
        if item.get("field") in by_id:
            role, fd = by_id[item["field"]]
            decoded = decode(fd, item.get("value"))
            if decoded is not None:
                result[role] = decoded
    return result
