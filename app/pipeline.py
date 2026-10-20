"""Verarbeitung: Klassifizieren -> Regeln -> (optional) LLM -> Übernehmen oder Inbox."""

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import deadlines
from app.config import Settings
from app.extraction import llm
from app.extraction.rules import EXTRACTORS, classify_by_keywords, field
from app.models import Contract, Document, Invoice, KeyValue, Warranty
from app.ollama import OllamaClient, OllamaError
from app.paperless import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)

KINDS = {"invoice": "Rechnung", "contract": "Vertrag", "warranty": "Garantie", "other": "Sonstiges"}
AUTO_THRESHOLD = 0.8

# Feldtypen der Datensätze (für Formulare und Übernahme)
FIELD_TYPES: dict[str, dict[str, str]] = {
    "invoice": {
        "issuer": "str", "invoice_number": "str", "invoice_date": "date", "amount": "float", "due_date": "date",
        "iban": "str", "payment_method": "str", "status": "str", "paid_on": "date",
        "warranty_enabled": "bool", "warranty_months": "int",
    },
    "contract": {
        "name": "str", "provider": "str", "contract_number": "str", "start_date": "date",
        "initial_term_months": "int", "renewal_mode": "str", "renewal_months": "int", "notice_value": "int",
        "notice_unit": "str", "notice_to": "str", "end_date": "date", "monthly_cost": "float", "status": "str",
        "cancelled_on": "date", "notes": "str",
    },
    "warranty": {"product": "str", "purchase_date": "date", "months": "int", "warranty_type": "str"},
}


# --------------------------------------------------------------------------- Hilfsfunktionen


def coerce(kind: str, raw):
    if raw is None:
        return None
    if kind == "bool":
        return raw in (True, "1", "on", "true", "yes")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return None
    if kind == "date":
        if isinstance(raw, date):
            return raw
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", raw):
            d, m, y = raw.split(".")
            return date(int(y), int(m), int(d))
        return date.fromisoformat(raw[:10])
    if kind == "float":
        if isinstance(raw, str):
            raw = raw.replace("€", "").replace(" ", "")
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
        return round(float(raw), 2)
    if kind == "int":
        return int(raw)
    return str(raw)


def parse_values(kind: str, raw: dict) -> dict:
    """Wandelt Formular- oder Vorschlagswerte in Python-Typen; ungültige Werte werden None."""
    values = {}
    for name, type_ in FIELD_TYPES[kind].items():
        try:
            values[name] = coerce(type_, raw.get(name))
        except (TypeError, ValueError):
            values[name] = None
    return values


def suggestion_values(doc: Document) -> dict:
    return {k: v.get("value") for k, v in (doc.suggestions or {}).items() if isinstance(v, dict)}


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def kv_get(session: Session, key: str) -> str | None:
    item = session.get(KeyValue, key)
    return item.value if item else None


def kv_set(session: Session, key: str, value: str) -> None:
    item = session.get(KeyValue, key)
    if item:
        item.value = value
    else:
        session.add(KeyValue(key=key, value=value))


# --------------------------------------------------------------------------- Synchronisation


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_document(session: Session, data: dict, correspondents: dict, doc_types: dict, force: bool = False) -> Document:
    doc = session.get(Document, data["id"])
    is_new = doc is None
    if is_new:
        doc = Document(id=data["id"], suggestions={})
        session.add(doc)

    new_type = doc_types.get(data.get("document_type"))
    type_changed = not is_new and new_type != doc.document_type
    doc.title = data.get("title") or ""
    doc.correspondent = correspondents.get(data.get("correspondent"))
    doc.document_type = new_type
    created = data.get("created") or data.get("created_date")
    doc.created = date.fromisoformat(created[:10]) if created else None
    doc.modified = data.get("modified")

    new_hash = content_hash(data.get("content") or "")
    changed = new_hash != doc.content_hash or type_changed
    doc.content_hash = new_hash
    if is_new or force or (changed and doc.review_state != "confirmed"):
        doc.extraction_state = "queued"
        doc.extraction_error = None
    return doc


def sync(session: Session, paperless: PaperlessClient, settings: Settings) -> int:
    since = kv_get(session, "last_modified")
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=settings.initial_sync_days)).isoformat()
    correspondents = paperless.names("correspondents")
    doc_types = paperless.names("document_types")
    newest = _parse_dt(since)
    count = 0
    for data in paperless.documents_modified_after(since):
        upsert_document(session, data, correspondents, doc_types)
        count += 1
        if data.get("modified") and _parse_dt(data["modified"]) > newest:
            newest = _parse_dt(data["modified"])
    kv_set(session, "last_modified", newest.isoformat())
    kv_set(session, "last_sync", datetime.now(timezone.utc).isoformat())
    return count


def import_single(session: Session, paperless: PaperlessClient, doc_id: int) -> Document:
    data = paperless.get_document(doc_id)
    return upsert_document(
        session, data, paperless.names("correspondents"), paperless.names("document_types"), force=True
    )


# --------------------------------------------------------------------------- Analyse


def classify(doc: Document, text: str, settings: Settings) -> tuple[str, str]:
    mapping = settings.doctype_map()
    if doc.document_type and doc.document_type.lower() in mapping:
        return mapping[doc.document_type.lower()], "doctype"
    if settings.keyword_classification:
        return classify_by_keywords(doc.title, text), "keyword"
    return "other", "keyword"


def _fill_defaults(kind: str, doc: Document, sugg: dict) -> None:
    if kind == "invoice":
        if doc.correspondent:
            sugg.setdefault("issuer", field(doc.correspondent, 0.9, "paperless"))
        sugg.setdefault("payment_method", field("transfer", 0.5, "default"))
        sugg.setdefault("status", field("open", 0.5, "default"))
        if "warranty_months" in sugg:
            sugg["warranty_enabled"] = field(True, 0.7, "rule")
    elif kind == "contract":
        sugg.setdefault("name", field(doc.title, 0.9, "paperless"))
        if doc.correspondent:
            sugg.setdefault("provider", field(doc.correspondent, 0.9, "paperless"))
    elif kind == "warranty":
        sugg.setdefault("product", field(doc.title, 0.9, "paperless"))


def _missing(kind: str, sugg: dict) -> list[str]:
    return [
        name for name in llm.FIELDS.get(kind, {})
        if name not in sugg or sugg[name]["confidence"] < AUTO_THRESHOLD
    ]


def _conf(sugg: dict, name: str) -> float:
    return sugg[name]["confidence"] if name in sugg else 0.0


def can_auto_accept(kind: str, sugg: dict, settings: Settings) -> bool:
    if kind == "contract":
        return False  # Verträge immer bestätigen lassen – zu wichtig
    if kind == "invoice":
        required = ["amount"]
        settled = sugg.get("payment_method", {}).get("value") == "debit" or sugg.get("status", {}).get("value") == "paid"
        if not settled:
            required.append("due_date")
    else:
        required = ["purchase_date", "months"]
    for name in required:
        if _conf(sugg, name) < AUTO_THRESHOLD and not (
            settings.llm_auto_accept and sugg.get(name, {}).get("source") == "llm"
        ):
            return False
    return True


def process_document(
    session: Session,
    doc: Document,
    paperless: PaperlessClient,
    ollama: OllamaClient | None,
    settings: Settings,
) -> None:
    data = paperless.get_document(doc.id)
    text = data.get("content") or ""

    kind, source = (doc.kind, "manual") if doc.kind_source == "manual" else classify(doc, text, settings)
    sugg: dict = {}
    if kind != "other":
        sugg = EXTRACTORS[kind](text, doc.created)
        _fill_defaults(kind, doc, sugg)
        wanted = _missing(kind, sugg)
        if ollama is not None and wanted:
            try:
                found = llm.extract(ollama, kind, text, wanted, settings.llm_max_chars)
            except OllamaError as exc:
                log.warning("LLM-Analyse für Dokument %s fehlgeschlagen: %s", doc.id, exc)
                sugg["_llm_error"] = {"value": str(exc), "confidence": 0, "source": "llm"}
            else:
                for name, value in found.items():
                    if _conf(sugg, name) < value["confidence"]:
                        sugg[name] = value

    doc.kind, doc.kind_source = kind, source
    doc.suggestions = sugg
    doc.extraction_state = "done"
    doc.extraction_error = None

    if kind == "other":
        doc.review_state = "none"
    elif can_auto_accept(kind, sugg, settings):
        apply_values(session, doc, kind, parse_values(kind, suggestion_values(doc)))
        doc.review_state = "confirmed"
        doc.auto_confirmed = True
        writeback(paperless, doc, settings)
    else:
        doc.review_state = "pending"
        doc.auto_confirmed = False


def apply_values(session: Session, doc: Document, kind: str, values: dict) -> None:
    """Legt den Datensatz zur Art an bzw. aktualisiert ihn."""
    if kind == "invoice":
        record = doc.invoice or Invoice(document_id=doc.id)
        for name in ("issuer", "invoice_number", "invoice_date", "amount", "due_date", "iban", "paid_on"):
            setattr(record, name, values.get(name))
        record.payment_method = values.get("payment_method") or "transfer"
        record.status = values.get("status") or "open"
        doc.invoice = record
        if values.get("warranty_enabled"):
            warranty = doc.warranty or Warranty(document_id=doc.id)
            warranty.product = doc.title
            warranty.purchase_date = values.get("invoice_date") or doc.created
            warranty.months = values.get("warranty_months") or 24
            warranty.warranty_type = "garantie" if values.get("warranty_months") else "gewaehrleistung"
            doc.warranty = warranty
        elif doc.warranty is not None and doc.kind == "invoice":
            doc.warranty = None
    elif kind == "contract":
        record = doc.contract or Contract(document_id=doc.id)
        for name in FIELD_TYPES["contract"]:
            if name in ("renewal_mode", "notice_unit", "notice_to", "status", "name"):
                continue
            setattr(record, name, values.get(name))
        record.name = values.get("name") or doc.title
        record.renewal_mode = values.get("renewal_mode") or "renews"
        record.notice_unit = values.get("notice_unit") or "months"
        record.notice_to = values.get("notice_to") or "term_end"
        record.status = values.get("status") or "active"
        doc.contract = record
    elif kind == "warranty":
        record = doc.warranty or Warranty(document_id=doc.id)
        record.product = values.get("product") or doc.title
        record.purchase_date = values.get("purchase_date") or doc.created
        record.months = values.get("months") or 24
        record.warranty_type = values.get("warranty_type") or "gewaehrleistung"
        doc.warranty = record
    # Datensätze anderer Arten entfernen, wenn die Art gewechselt wurde
    if kind != "contract":
        doc.contract = None
    if kind != "invoice":
        doc.invoice = None
    if kind not in ("warranty", "invoice"):
        doc.warranty = None
    session.flush()


def writeback(paperless: PaperlessClient, doc: Document, settings: Settings, today: date | None = None) -> None:
    if not settings.writeback_enabled:
        return
    values: dict[str, object] = {}
    if doc.invoice:
        values["Betrag"] = doc.invoice.amount
        values["Fällig am"] = doc.invoice.due_date
    if doc.contract:
        result = deadlines.compute(deadlines.terms_from_contract(doc.contract), today or date.today())
        values["Kündigung bis"] = None if result.anytime else result.cancel_by
        values["Vertragsende"] = result.term_end
    if doc.warranty:
        values["Garantie bis"] = doc.warranty.expires_on
    if not any(v is not None for v in values.values()):
        return
    try:
        paperless.set_custom_fields(doc.id, values)
    except PaperlessError as exc:
        log.warning("Rückschreiben nach Paperless fehlgeschlagen (Dokument %s): %s", doc.id, exc)


def next_queued(session: Session) -> Document | None:
    return session.scalars(
        select(Document).where(Document.extraction_state == "queued").order_by(Document.updated_at).limit(1)
    ).first()
