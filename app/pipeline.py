"""Verarbeitung: Klassifizieren -> Regeln -> (optional) LLM -> Übernehmen oder Inbox."""

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import deadlines, fields
from app.config import Settings
from app.extraction import llm
from app.extraction.rules import EXTRACTORS, classify_by_keywords, field
from app.models import Contract, Document, Invoice, KeyValue, Warranty
from app.ollama import OllamaClient, OllamaError
from app.paperless import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)

# Bei Erweiterungen der Erkennung erhöhen – die Systemseite bietet dann an, betroffene Dokumente neu zu analysieren
ANALYSIS_VERSION = 2

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


SOURCE_LABELS = {
    "rule": "Regel", "llm": "AI", "paperless": "Paperless", "firefly": "Firefly", "default": "Standard", "manual": "manuell",
}


def set_source(doc: Document, names, source: str, confidence: float | None = None, confirmed: bool = False) -> None:
    """Herkunft für Felder festhalten (überschreibt frühere Angaben dieser Felder)."""
    sources = dict(doc.field_sources or {})
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    for name in ([names] if isinstance(names, str) else names):
        sources[name] = {"source": source, "confidence": confidence, "confirmed": confirmed, "at": stamp}
    doc.field_sources = sources


def clear_source(doc: Document, *names: str) -> None:
    sources = {k: v for k, v in (doc.field_sources or {}).items() if k not in names}
    doc.field_sources = sources or None


def sources_from_suggestions(doc: Document, kind: str, values: dict) -> None:
    """Nach automatischer Übernahme: Herkunft aus den Vorschlägen übernehmen."""
    sugg = doc.suggestions or {}
    sources: dict = {}
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    for name in FIELD_TYPES[kind]:
        if values.get(name) is not None and isinstance(sugg.get(name), dict):
            sources[name] = {"source": sugg[name].get("source"), "confidence": sugg[name].get("confidence"),
                             "confirmed": False, "at": stamp}
    doc.field_sources = sources or None


def record_confirmation(doc: Document, kind: str, values: dict, before: dict, before_sources: dict) -> None:
    """Beim Bestätigen: unveränderte Vorschläge als „bestätigt“, alles Geänderte als „manuell“ merken."""
    sugg = doc.suggestions or {}
    types = FIELD_TYPES[kind]
    sources: dict = {}
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    for name, value in values.items():
        if value is None or value is False or name not in types:
            continue
        s = sugg.get(name) if isinstance(sugg.get(name), dict) else None
        try:
            suggested = coerce(types[name], s.get("value")) if s else None
        except (TypeError, ValueError):
            suggested = None
        if s and suggested == value:
            sources[name] = {"source": s.get("source"), "confidence": s.get("confidence"), "confirmed": True, "at": stamp}
        elif name in before_sources and before.get(name) == value:
            sources[name] = before_sources[name]  # unverändert gegenüber dem bisherigen Stand
        else:
            sources[name] = {"source": "manual", "confidence": None, "confirmed": True, "at": stamp}
    doc.field_sources = sources or None


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


DOCTYPE_KINDS = ("invoice", "contract", "warranty")


def doctype_choices(settings: Settings) -> dict:
    """Aktuelle Zuordnung Dokumenttyp -> Art und Steuer-Typen (kleingeschrieben)."""
    return {"kinds": settings.doctype_map(), "tax": settings.tax_doctype_list}


def apply_doctype_settings(session: Session, settings: Settings) -> bool:
    """In der App gespeicherte Dokumenttyp-Zuordnung übernehmen; sie hat Vorrang vor der .env."""
    raw = kv_get(session, "doctype_settings")
    if not raw:
        return False
    stored = json.loads(raw)
    kinds: dict = stored.get("kinds", {})
    settings.doctypes_invoice, settings.doctypes_contract, settings.doctypes_warranty = (
        ",".join(name for name, kind in kinds.items() if kind == k) for k in DOCTYPE_KINDS
    )
    settings.tax_doctypes = ",".join(stored.get("tax", []))
    return True


def save_doctype_settings(session: Session, settings: Settings, kinds: dict[str, str], tax: list[str]) -> list[str]:
    """Zuordnung speichern und übernehmen. Gibt die Typen zurück, deren Art sich geändert hat."""
    before = settings.doctype_map()
    kinds = {name.lower(): kind for name, kind in kinds.items() if kind in DOCTYPE_KINDS}
    kv_set(session, "doctype_settings", json.dumps({"kinds": kinds, "tax": sorted({t.lower() for t in tax})}))
    apply_doctype_settings(session, settings)
    return sorted(n for n in set(before) | set(kinds) if before.get(n) != kinds.get(n))


def requeue_doctypes(session: Session, names: list[str]) -> int:
    """Analysierte, unbestätigte Dokumente dieser Typen neu einreihen (manuell gesetzte Art bleibt)."""
    if not names:
        return 0
    count = 0
    for doc in session.scalars(select(Document).where(Document.extraction_state == "done", Document.review_state != "confirmed")):
        if (doc.document_type or "").lower() in names and doc.kind_source not in ("manual", "fields"):
            doc.extraction_state = "queued"
            count += 1
    return count


# --------------------------------------------------------------------------- Synchronisation


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_names(paperless: PaperlessClient, settings: Settings | None = None) -> dict:
    names: dict = {endpoint: paperless.names(endpoint) for endpoint in ("correspondents", "document_types", "tags")}
    names["fields"] = fields.resolve(paperless.custom_field_definitions(), settings) if settings else {}
    names["open_max_age_days"] = settings.fields_open_max_age_days if settings else 90
    return names


def _iso_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def apply_paperless_fields(doc: Document, open_max_age_days: int = 90, today: date | None = None) -> None:
    """Übernimmt in Paperless gepflegte Werte (Fälligkeit, bezahlt am, Betrag).

    Dokumente mit Fälligkeits- oder Bezahlt-Datum gelten als Rechnung – auch ohne Analyse.
    """
    if doc.writeback_pending:
        return  # eigene, noch nicht zurückgeschriebene Änderungen haben Vorrang
    pf = doc.paperless_fields or {}
    due, paid, amount = _iso_date(pf.get("due_date")), _iso_date(pf.get("paid_on")), pf.get("amount")
    if doc.invoice is not None:
        inv = doc.invoice
        if paid and (inv.status != "paid" or inv.paid_on != paid):
            inv.status, inv.paid_on = "paid", paid
            set_source(doc, ["status", "paid_on"], "paperless")
        if due and inv.due_date != due:
            inv.due_date = due
            set_source(doc, "due_date", "paperless")
        if amount and inv.amount is None:
            inv.amount = amount
            set_source(doc, "amount", "paperless")
        return
    if not (due or paid) or doc.contract is not None or doc.kind_source == "manual" or doc.kind not in ("other", "invoice"):
        return
    # Alte Rechnungen ohne Bezahlt-Datum wurden früher nicht nachgepflegt -> als erledigt annehmen
    stale = not paid and due and due < (today or date.today()) - timedelta(days=open_max_age_days)
    doc.invoice = Invoice(
        document_id=doc.id, issuer=doc.correspondent, invoice_date=doc.created, due_date=due, amount=amount,
        status="paid" if paid or stale else "open", paid_on=paid,
    )
    doc.kind, doc.kind_source, doc.review_state = "invoice", "fields", "confirmed"
    set_source(doc, [n for n, v in (("due_date", due), ("paid_on", paid), ("amount", amount)) if v], "paperless")
    set_source(doc, "issuer", "paperless")
    set_source(doc, "invoice_date", "paperless")
    if stale:
        set_source(doc, "status", "default")  # angenommen erledigt (alt, ohne Bezahlt-Datum)
    elif paid:
        set_source(doc, "status", "paperless")
    if not paid and not stale and amount is None and doc.extraction_state == "skipped":
        doc.extraction_state = "queued"  # Betrag für offene Rechnung per Analyse ermitteln


def upsert_document(
    session: Session, data: dict, names: dict[str, dict[int, str]], force: bool = False, analyze: bool = True
) -> Document:
    """Übernimmt Metadaten aus Paperless. Mit analyze=False (Archiv-Index) wird nichts zur Analyse eingereiht."""
    doc = session.get(Document, data["id"])
    is_new = doc is None
    if is_new:
        doc = Document(
            id=data["id"], suggestions={}, tags=[], kind="other", kind_source="keyword", review_state="none",
            extraction_state="queued" if analyze else "skipped",
        )
        session.add(doc)

    new_type = names["document_types"].get(data.get("document_type"))
    type_changed = not is_new and new_type != doc.document_type
    doc.title = data.get("title") or ""
    doc.correspondent = names["correspondents"].get(data.get("correspondent"))
    doc.document_type = new_type
    doc.tags = sorted(names["tags"].get(t, str(t)) for t in data.get("tags") or [])
    created = data.get("created") or data.get("created_date")
    doc.created = date.fromisoformat(created[:10]) if created else None
    doc.modified = data.get("modified")
    if "page_count" in data:
        doc.page_count = data.get("page_count")
    if "original_file_name" in data:
        doc.original_file_name = data.get("original_file_name")
    if "custom_fields" in data and names.get("fields"):
        doc.paperless_fields = fields.extract(data.get("custom_fields"), names["fields"])
        apply_paperless_fields(doc, names.get("open_max_age_days", 90))

    if not analyze:
        return doc

    new_hash = content_hash(data.get("content") or "")
    changed = new_hash != doc.content_hash or type_changed
    doc.content_hash = new_hash
    if is_new or force or doc.extraction_state == "skipped" or (changed and doc.review_state != "confirmed"):
        doc.extraction_state = "queued"
        doc.extraction_error = None
    return doc


def sync(session: Session, paperless: PaperlessClient, settings: Settings) -> int:
    since = kv_get(session, "last_modified")
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=settings.initial_sync_days)).isoformat()
    names = load_names(paperless, settings)
    newest = _parse_dt(since)
    count = 0
    for data in paperless.documents_modified_after(since):
        upsert_document(session, data, names)
        count += 1
        if data.get("modified") and _parse_dt(data["modified"]) > newest:
            newest = _parse_dt(data["modified"])
    kv_set(session, "last_modified", newest.isoformat())
    kv_set(session, "last_sync", datetime.now(timezone.utc).isoformat())
    return count


def index_sync(session: Session, paperless: PaperlessClient, settings: Settings | None = None) -> tuple[int, int]:
    """Gleicht die Metadaten des gesamten Archivs ab. Gibt (Anzahl, entfernt) zurück.

    Dokumente, die es in Paperless nicht mehr gibt, werden samt Datensätzen entfernt –
    aber nur, wenn der Abgleich vollständig war.
    """
    names = load_names(paperless, settings)
    total, documents = paperless.all_documents_metadata()
    seen: set[int] = set()
    for data in documents:
        upsert_document(session, data, names, analyze=False)
        seen.add(data["id"])
    removed = 0
    if len(seen) >= total:
        for doc in session.scalars(select(Document).where(Document.id.not_in(seen))):
            session.delete(doc)
            removed += 1
    kv_set(session, "last_index_sync", datetime.now(timezone.utc).isoformat())
    return len(seen), removed


def import_single(session: Session, paperless: PaperlessClient, doc_id: int, settings: Settings | None = None) -> Document:
    data = paperless.get_document(doc_id)
    return upsert_document(session, data, load_names(paperless, settings), force=True)


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


def _inject_paperless_fields(doc: Document, sugg: dict) -> None:
    """In Paperless gepflegte Werte sind sicherer als jede Erkennung."""
    pf = doc.paperless_fields or {}
    for role in ("due_date", "paid_on", "amount"):
        if pf.get(role) is not None:
            sugg[role] = field(pf[role], 0.95, "paperless")
    if pf.get("paid_on"):
        sugg["status"] = field("paid", 0.95, "paperless")


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
    llm_allowed: bool = True,
) -> dict:
    data = paperless.get_document(doc.id)
    text = data.get("content") or ""

    if doc.kind_source in ("manual", "fields"):
        kind, source = doc.kind, doc.kind_source
    else:
        kind, source = classify(doc, text, settings)
    sugg: dict = {}
    # Was ist bei dieser Analyse passiert? (für die Aktivitätsanzeige)
    info: dict = {"kind": kind, "ai": "not_needed", "ai_seconds": None, "ai_fields": [], "wanted": []}
    if kind != "other":
        sugg = EXTRACTORS[kind](text, doc.created)
        _fill_defaults(kind, doc, sugg)
        if kind == "invoice":
            _inject_paperless_fields(doc, sugg)
        wanted = _missing(kind, sugg)
        info["wanted"] = wanted
        if wanted and ollama is None:
            info["ai"] = "disabled"
        if ollama is not None and wanted and not llm_allowed:
            info["ai"] = "deferred"
            # Außerhalb des AI-Zeitfensters: nur Regeln, AI wird im nächsten Fenster nachgeholt
            sugg["_llm_error"] = {
                "value": f"AI pausiert außerhalb der AI-Zeiten ({settings.llm_active_hours} Uhr)", "confidence": 0,
                "source": "llm", "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                "attempts": 0, "deferred": True,
            }
        elif ollama is not None and wanted:
            started = time.monotonic()
            try:
                found = llm.extract(ollama, kind, text, wanted, settings.llm_max_chars)
            except OllamaError as exc:
                info["ai"], info["ai_seconds"] = "failed", round(time.monotonic() - started, 1)
                log.warning("LLM-Analyse für Dokument %s fehlgeschlagen: %s", doc.id, exc)
                previous = (doc.suggestions or {}).get("_llm_error") or {}
                sugg["_llm_error"] = {
                    "value": str(exc), "confidence": 0, "source": "llm",
                    "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                    "attempts": int(previous.get("attempts", 0)) + 1,
                }
            else:
                info["ai"], info["ai_seconds"] = "used", round(time.monotonic() - started, 1)
                for name, value in found.items():
                    if _conf(sugg, name) < value["confidence"]:
                        sugg[name] = value
                        info["ai_fields"].append(name)

    doc.kind, doc.kind_source = kind, source
    doc.suggestions = sugg
    doc.extraction_state = "done"
    doc.extraction_error = None
    doc.analyzed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.analysis_version = ANALYSIS_VERSION

    if settings.summary_mode == "new" and settings.llm_enabled and doc.summary_state is None \
            and not (doc.paperless_fields or {}).get("summary"):
        doc.summary_state = "queued"
    if settings.meta_mode == "new" and doc.meta_state is None:
        doc.meta_state = "queued"

    if kind == "other":
        doc.review_state = "none"
    elif can_auto_accept(kind, sugg, settings):
        values = parse_values(kind, suggestion_values(doc))
        apply_values(session, doc, kind, values)
        sources_from_suggestions(doc, kind, values)
        doc.review_state = "confirmed"
        doc.auto_confirmed = True
        mark_changed(doc)
    else:
        doc.review_state = "pending"
        doc.auto_confirmed = False
    info["review"] = doc.review_state
    return info


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


def ensure_fields(paperless: PaperlessClient, settings: Settings, roles) -> dict[str, fields.FieldDef]:
    """Löst Rollen zu Paperless-Feldern auf und legt fehlende (erlaubte) Felder an."""
    definitions = paperless.custom_field_definitions()
    defs = fields.resolve(definitions, settings)
    names = fields.role_names(settings)
    for role in roles:
        if role in defs or role not in names:
            continue
        _, data_type, creatable = fields.ROLES[role]
        if creatable and settings.writeback_create_fields:
            created = paperless.create_custom_field(names[role], data_type)
            defs[role] = fields.FieldDef(created["id"], created["name"], created.get("data_type", data_type))
    return defs


def write_fields(paperless: PaperlessClient, doc: Document, settings: Settings, values: dict[str, object]) -> list[str]:
    """Schreibt Rollenwerte (None = Feld leeren) nach Paperless. Gibt nicht zuordenbare Rollen zurück."""
    defs = ensure_fields(paperless, settings, values.keys())
    payload = {defs[role].id: fields.encode(defs[role], value) for role, value in values.items() if role in defs}
    if payload:
        paperless.set_custom_fields(doc.id, payload)
        current = dict(doc.paperless_fields or {})
        for role, value in values.items():
            if role not in defs:
                continue
            if value is None:
                current.pop(role, None)
            else:
                current[role] = fields.decode(defs[role], fields.encode(defs[role], value))
        doc.paperless_fields = current
    return [role for role in values if role not in defs]


def _norm(value):
    """Vergleichbare, JSON-taugliche Form eines Feldwerts."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (list, set, tuple)):
        return sorted(int(v) for v in value)
    if isinstance(value, str):
        return value.strip() or None
    return value


def companion_values(doc: Document, today: date) -> dict[str, object]:
    """Werte, die der Companion für die Paperless-Felder vorschlagen würde."""
    values: dict[str, object] = {}
    pf = doc.paperless_fields or {}
    if doc.review_state == "confirmed":
        if doc.invoice:
            values["amount"] = doc.invoice.amount
            values["due_date"] = doc.invoice.due_date
            if doc.invoice.status == "paid":
                values["paid_on"] = doc.invoice.paid_on
            elif pf.get("paid_on"):
                values["paid_on"] = None  # wieder offen: Feld leeren
        if doc.contract:
            result = deadlines.compute(deadlines.terms_from_contract(doc.contract), today)
            values["cancel_by"] = None if result.anytime else result.cancel_by
            values["term_end"] = result.term_end
        if doc.warranty:
            values["warranty_end"] = doc.warranty.expires_on
        # Unbekannte Werte nie vorschlagen (würde Eingaben in Paperless löschen) – außer bewusstem Leeren
        values = {role: v for role, v in values.items() if v is not None or (role == "paid_on" and pf.get("paid_on"))}
    values.update(doc.proposed_fields or {})
    return values


def pending_changes(doc: Document, settings: Settings, today: date) -> list[tuple[str, object, object]]:
    """Unterschiede Companion -> Paperless als (Rolle, Paperless-Wert, Vorschlag)."""
    configured = fields.role_names(settings)
    pf = doc.paperless_fields or {}
    dismissed = doc.dismissed_fields or {}
    changes = []
    for role, value in companion_values(doc, today).items():
        if role not in configured:
            continue
        new, current = _norm(value), _norm(pf.get(role))
        if new == current or (role in dismissed and dismissed[role] == new):
            continue
        changes.append((role, current, new))
    return changes


def mark_changed(doc: Document) -> None:
    doc.writeback_pending = True


def propose(doc: Document, role: str, value, source: str = "manual") -> None:
    """Eigene Eingabe für ein Paperless-Feld vormerken (wird erst nach Zustimmung geschrieben)."""
    proposed = dict(doc.proposed_fields or {})
    proposed[role] = _norm(value)
    doc.proposed_fields = proposed
    set_source(doc, f"proposal:{role}", source)  # Zeitpunkt für „Für Paperless“
    dismissed = dict(doc.dismissed_fields or {})
    dismissed.pop(role, None)
    doc.dismissed_fields = dismissed
    doc.writeback_pending = True


def approve_changes(
    paperless: PaperlessClient, doc: Document, settings: Settings, roles: list[str], today: date
) -> list[str]:
    """Schreibt die ausgewählten Vorschläge nach Paperless. Gibt nicht zuordenbare Rollen zurück."""
    wanted = {role: new for role, _, new in pending_changes(doc, settings, today) if role in roles}
    if not wanted:
        return []
    missing = write_fields(paperless, doc, settings, wanted)
    proposed = {k: v for k, v in (doc.proposed_fields or {}).items() if k not in wanted or k in missing}
    doc.proposed_fields = proposed or None
    doc.writeback_pending = bool(pending_changes(doc, settings, today))
    return missing


def dismiss_changes(doc: Document, settings: Settings, roles: list[str], today: date) -> None:
    """Vorschläge verwerfen; sie erscheinen erst wieder, wenn sich der Wert im Companion ändert."""
    dismissed = dict(doc.dismissed_fields or {})
    proposed = dict(doc.proposed_fields or {})
    for role, _, new in pending_changes(doc, settings, today):
        if role in roles:
            if role in proposed:
                proposed.pop(role)  # eigene Eingabe zurücknehmen
            else:
                dismissed[role] = new
    doc.proposed_fields = proposed or None
    doc.dismissed_fields = dismissed or None
    doc.writeback_pending = bool(pending_changes(doc, settings, today))
    if not doc.writeback_pending:
        apply_paperless_fields(doc)  # Paperless-Stand wieder maßgeblich


# Aus welchen erfassten Feldern ergibt sich der Wert einer Paperless-Rolle?
ROLE_FIELDS = {
    "amount": ("amount",),
    "due_date": ("due_date",),
    "paid_on": ("paid_on", "status"),
    "cancel_by": ("start_date", "initial_term_months", "end_date", "renewal_mode", "renewal_months",
                  "notice_value", "notice_unit", "notice_to", "status", "cancelled_on"),
    "term_end": ("start_date", "initial_term_months", "end_date", "renewal_mode", "renewal_months", "status"),
    "warranty_end": ("purchase_date", "months", "invoice_date", "warranty_months"),
}


def change_info(doc: Document, role: str) -> dict:
    """Wann (UTC, ISO) und woher stammt der vorgeschlagene Wert einer Rolle? Rückfall: letzte Analyse."""
    sources = doc.field_sources or {}
    if role in (doc.proposed_fields or {}) and f"proposal:{role}" in sources:
        entry = sources[f"proposal:{role}"]
        return {"at": entry.get("at"), "source": entry.get("source")}
    entries = [sources[f] for f in ROLE_FIELDS.get(role, ()) if sources.get(f, {}).get("at")]
    if entries:
        newest = max(entries, key=lambda e: e["at"])
        return {"at": newest["at"], "source": newest.get("source")}
    return {"at": doc.analyzed_at.isoformat(timespec="minutes") if doc.analyzed_at else None, "source": None}


def documents_with_changes(session: Session, settings: Settings, today: date) -> list[tuple[Document, list]]:
    result = []
    for doc in session.scalars(select(Document).where(Document.writeback_pending.is_(True)).order_by(Document.id)):
        changes = pending_changes(doc, settings, today)
        if changes:
            result.append((doc, changes))
        else:
            doc.writeback_pending = False
    return result


def related_ids(session: Session, doc_id: int) -> set[int]:
    """Alle über „gehört zu“ verbundenen Dokumente (in beide Richtungen, transitiv), ohne doc_id selbst."""
    graph: dict[int, set[int]] = {}
    for doc in session.scalars(select(Document).where(Document.paperless_fields.is_not(None))):
        for other in (doc.paperless_fields or {}).get("related") or []:
            graph.setdefault(doc.id, set()).add(other)
            graph.setdefault(other, set()).add(doc.id)
    seen, todo = {doc_id}, [doc_id]
    while todo:
        for nxt in graph.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen - {doc_id}


def process_summary(
    session: Session, doc: Document, paperless: PaperlessClient, ollama: OllamaClient, settings: Settings
) -> None:
    from app import summary  # lokal, um Importzyklen zu vermeiden

    # Nur lesen – fehlende Felder werden hier nicht angelegt
    defs = fields.resolve(paperless.custom_field_definitions(), settings)
    limit = (defs["summary"].text_limit if "summary" in defs else None) or summary.DEFAULT_LIMIT
    data = paperless.get_document(doc.id)
    text = summary.generate(
        ollama, doc.title, doc.correspondent, data.get("content") or "", limit, settings.summary_max_input_chars
    )
    doc.summary = text
    propose(doc, "summary", text, source="llm")  # erst nach Zustimmung in Paperless
    doc.summary_state = "done"


METHODS = {
    "rules_ai": "Regeln + AI",
    "rules": "Regeln",
    "rules_ai_failed": "nur Regeln (AI nicht erreichbar)",
    "rules_ai_deferred": "nur Regeln (AI folgt im AI-Zeitfenster)",
    "fields": "aus Paperless-Feldern",
    "none": "nicht analysiert",
}


def analysis_method(doc: Document) -> str:
    """Womit wurden die Werte des Dokuments ermittelt?"""
    if doc.extraction_state != "done" and not doc.analyzed_at:
        return "fields" if doc.kind_source == "fields" else "none"
    sugg = doc.suggestions or {}
    if "_llm_error" in sugg:
        return "rules_ai_deferred" if sugg["_llm_error"].get("deferred") else "rules_ai_failed"
    if any(isinstance(v, dict) and v.get("source") == "llm" for k, v in sugg.items() if not k.startswith("_")):
        return "rules_ai"
    if doc.kind_source == "fields" and not doc.analyzed_at:
        return "fields"
    return "rules"


def index_documents_with_known_type(session: Session, settings: Settings) -> list[Document]:
    """Nur indexierte Dokumente, deren Paperless-Dokumenttyp einer Art zugeordnet ist (Rechnung, Vertrag …)."""
    mapping = settings.doctype_map()
    return [
        d for d in session.scalars(select(Document).where(Document.extraction_state == "skipped"))
        if d.kind_source != "fields" and (d.document_type or "").lower() in mapping
    ]


def analysis_overview(session: Session, settings: Settings) -> dict:
    from sqlalchemy import func

    by_kind = dict(session.execute(
        select(Document.kind, func.count()).where(
            (Document.extraction_state != "skipped") | (Document.kind_source == "fields")
        ).group_by(Document.kind)
    ).all())
    index_only = session.scalar(
        select(func.count()).select_from(Document).where(Document.extraction_state == "skipped", Document.kind_source != "fields")
    )
    return {
        "by_kind": by_kind,
        "processed": sum(by_kind.values()),
        "index_only": index_only,
        "index_known_type": len(index_documents_with_known_type(session, settings)),
        "doctypes": ", ".join(sorted({name for name in settings.doctype_map()})),
    }


def outdated_documents(session: Session) -> list[Document]:
    """Analysierte, nicht bestätigte Dokumente mit älterem Erkennungsstand."""
    return list(session.scalars(
        select(Document).where(
            Document.extraction_state == "done",
            Document.review_state != "confirmed",
            (Document.analysis_version.is_(None)) | (Document.analysis_version < ANALYSIS_VERSION),
        )
    ))


LLM_RETRY_LIMIT = 3


def documents_with_llm_error(session: Session, include_exhausted: bool = False) -> list[Document]:
    """Zu prüfende Dokumente, deren Analyse ohne AI lief, weil Ollama nicht erreichbar war."""
    result = []
    for doc in session.scalars(select(Document).where(Document.review_state == "pending")):
        error = (doc.suggestions or {}).get("_llm_error")
        if error and doc.extraction_state == "done" and (
            include_exhausted or int(error.get("attempts", 1)) < LLM_RETRY_LIMIT
        ):
            result.append(doc)
    return result


def clear_queue(session: Session) -> int:
    """Wartende Analysen und Zusammenfassungen zurücknehmen (nichts geht verloren)."""
    count = 0
    for doc in session.scalars(select(Document).where(Document.extraction_state == "queued")):
        doc.extraction_state = "done" if doc.analyzed_at or doc.suggestions else "skipped"
        count += 1
    for doc in session.scalars(select(Document).where(Document.summary_state == "queued")):
        doc.summary_state = None
        count += 1
    for doc in session.scalars(select(Document).where(Document.meta_state == "queued")):
        doc.meta_state = "done" if doc.meta_suggestion else None
        count += 1
    return count


def requeue_llm_errors(session: Session, include_exhausted: bool = False) -> int:
    docs = documents_with_llm_error(session, include_exhausted)
    for doc in docs:
        doc.extraction_state = "queued"
    return len(docs)


def next_queued(session: Session) -> Document | None:
    return session.scalars(
        select(Document).where(Document.extraction_state == "queued").order_by(Document.updated_at).limit(1)
    ).first()


def meta_overview(session: Session) -> dict:
    """Stand der Metadaten-Vorschläge für „Für Paperless“."""
    from sqlalchemy import func

    states = dict(session.execute(select(Document.meta_state, func.count()).group_by(Document.meta_state)).all())
    without = session.scalar(select(func.count()).select_from(Document).where(
        Document.extraction_state == "done", Document.meta_state.is_(None)))
    nothing_open = session.scalar(select(func.count()).select_from(Document).where(
        Document.meta_state == "done", Document.meta_pending.is_(False)))
    last_error = session.scalar(select(Document.extraction_error).where(Document.meta_state == "error")
                                .order_by(Document.updated_at.desc()).limit(1))
    analyses_waiting = session.scalar(select(func.count()).select_from(Document).where(Document.extraction_state == "queued"))
    return {"done": states.get("done", 0), "queued": states.get("queued", 0) + states.get("running", 0),
            "error": states.get("error", 0), "analyzed_without": without, "nothing_open": nothing_open,
            "last_error": last_error, "analyses_waiting": analyses_waiting}


def queue_meta_for_analyzed(session: Session, include_errors: bool = True) -> int:
    """Bereits analysierte Dokumente ohne Vorschlag (und fehlgeschlagene) für Vorschläge einreihen."""
    states = [None, "error"] if include_errors else [None]
    count = 0
    for doc in session.scalars(select(Document).where(
        Document.extraction_state == "done",
        Document.meta_state.is_(None) | Document.meta_state.in_([s for s in states if s]),
    )):
        doc.meta_state = "queued"
        count += 1
    return count


def next_meta(session: Session) -> Document | None:
    return session.scalars(
        select(Document).where(Document.meta_state == "queued").order_by(Document.updated_at).limit(1)
    ).first()


def next_summary(session: Session) -> Document | None:
    return session.scalars(
        select(Document).where(Document.summary_state == "queued").order_by(Document.updated_at).limit(1)
    ).first()
