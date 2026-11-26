"""Was steht nur im Companion? Übersicht für „Was verliere ich bei einer Neuinstallation?“.

Drei Gruppen:
- wartet auf Übertragung: vorgemerkt für Paperless, aber noch nicht übertragen
- nur im Companion: deine Arbeit, die weder in Paperless noch in Firefly steht
- wird neu erzeugt: kommt bei einer Neuinstallation von selbst wieder (Index, Analyse, Erkennung)

Gilt für eine Neuinstallation OHNE das Datenverzeichnis. Bleibt der Datenordner (Docker-Volume) erhalten, geht nichts
verloren; die Sicherung (backup) kopiert genau diese Datenbank.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (Activity, Case, CaseEntry, Contract, Document, Invoice, KeyValue, MetaDecision, PaymentMatch,
                        ReceiptCheck, Reimbursement, RetentionRule, RuleProposal, Warranty)


@dataclass
class Item:
    label: str
    count: int
    detail: str = ""
    link: str = ""


@dataclass
class Section:
    key: str
    title: str
    hint: str
    items: list[Item] = field(default_factory=list)
    oldest: date | None = None  # älteste Vormerkung (nur „wartet auf Übertragung“)

    @property
    def total(self) -> int:
        return sum(i.count for i in self.items)


def _count(session: Session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _kv_len(session: Session, key: str) -> int:
    item = session.get(KeyValue, key)
    if item is None:
        return 0
    try:
        value = json.loads(item.value)
    except ValueError:
        return 0
    return len(value) if isinstance(value, (list, dict)) else 1


def _oldest(stamps: list[str]) -> date | None:
    days = []
    for value in stamps:
        try:
            days.append(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    return min(days) if days else None


def _invoice_in_paperless(inv: Invoice) -> bool:
    """Wird die Rechnung bei einer Neuinstallation aus deinen Paperless-Feldern wiederhergestellt?
    (Fälligkeit bzw. Bezahlt-Datum und Betrag stehen in Paperless – siehe pipeline.apply_paperless_fields)"""
    pf = (inv.document.paperless_fields or {}) if inv.document else {}
    has_date = bool(pf.get("due_date") or pf.get("paid_on"))
    amount_ok = inv.amount is None or (pf.get("amount") is not None and abs(float(pf["amount"]) - inv.amount) < 0.01)
    status_ok = inv.status != "paid" or bool(pf.get("paid_on"))
    return has_date and amount_ok and status_ok


def inventory(session: Session, settings: Settings, today: date) -> list[Section]:
    from app import metadata, pipeline

    # ------------------------------------------------------------------ wartet auf Übertragung
    pending = Section("pending", "Wartet auf Übertragung nach Paperless",
                      "Vorgemerkt, aber noch nicht übertragen. Überträgst du es vorher unter „Für Paperless“, steht es in "
                      "Paperless und ist sicher – sonst ist es bei einer Neuinstallation weg.")
    changes = pipeline.documents_with_changes(session, settings, today)
    stamps: list[str] = []
    for doc, items in changes:
        sources = doc.field_sources or {}
        for role, _, _ in items:
            entry = sources.get(f"proposal:{role}") or sources.get(role) or {}
            if entry.get("at"):
                stamps.append(entry["at"])
    pending.items.append(Item("Dokumente mit geänderten Feldern", len(changes),
                              "Fälligkeit, Bezahlt am, Betrag, Kündigungsfristen … für Paperless-Felder", "/paperless-changes"))
    accepted = 0
    for doc in session.scalars(select(Document).where(Document.meta_pending.is_(True))):
        for item in metadata.pending(doc):
            if item["state"] == "accepted":
                accepted += 1
                if item.get("at"):
                    stamps.append(item["at"])
    pending.items.append(Item("Vorgemerkte Metadaten", accepted, "Titel, Tags, Korrespondent, Dokumenttyp",
                              "/paperless-changes#metadata"))
    pending.oldest = _oldest(stamps)
    proposed = [d for d in session.scalars(select(Document).where(Document.proposed_fields.is_not(None))) if d.proposed_fields]
    links = sum(1 for d in proposed if "related" in d.proposed_fields)
    pending.items.append(Item("Vorgemerkte Verknüpfungen „gehört zu“", links, "", "/paperless-changes"))
    other = sum(1 for d in proposed if set(d.proposed_fields) - {"related"})
    pending.items.append(Item("Vorgemerkte Kommentare / Zusammenfassungen", other, "", "/paperless-changes"))

    # ------------------------------------------------------------------ nur im Companion
    only = Section("only", "Nur im Companion gespeichert",
                   "Deine Arbeit, die weder in Paperless noch in Firefly steht. Bei einer Neuinstallation ohne Datenordner ist "
                   "das weg – mit der Sicherung oben nicht.")
    invoices = list(session.scalars(select(Invoice)))
    lost_invoices = [i for i in invoices if not _invoice_in_paperless(i)]
    only.items.append(Item("Erfasste Rechnungen", len(lost_invoices),
                           f"{len(invoices) - len(lost_invoices)} weitere kommen aus deinen Paperless-Feldern zurück "
                           "(Fälligkeit/Bezahlt am/Betrag)", "/invoices?status=all"))
    only.items.append(Item("Zahlungen aus Firefly zugeordnet (inkl. Teilzahlungen, Sammelzahlungen)",
                           _count(session, PaymentMatch, PaymentMatch.state == "accepted"), "", "/invoices?status=all"))
    only.items.append(Item("Abgelehnte Zahlungsvorschläge („passt nicht“)",
                           _count(session, PaymentMatch, PaymentMatch.state == "rejected"), "würden wieder vorgeschlagen"))
    only.items.append(Item("Rückerstattungen", _count(session, Invoice, Invoice.refund_amount.is_not(None)), "", "/invoices?status=all"))
    only.items.append(Item("Mahnungen zugeordnet", _count(session, Invoice, Invoice.reminders.is_not(None)), "", "/invoices?status=all"))
    contracts = list(session.scalars(select(Contract).where(Contract.document_id.is_not(None))))
    recoverable = [c for c in contracts if c.document and (c.document.paperless_fields or {}).get("contract_terms")]
    only.items.append(Item("Verträge (Laufzeit, Kündigungsfrist, Monatsbeitrag)", len(contracts) - len(recoverable),
                           (f"{len(recoverable)} weitere kommen aus deinen Paperless-Vertragsfeldern zurück" if recoverable else
                            "Tipp: unter Einstellungen → Paperless-Felder die Vertragsfelder einschalten und übertragen – dann "
                            "stehen sie auch in Paperless"), "/contracts"))
    only.items.append(Item("Abos ohne Dokument", _count(session, Contract, Contract.document_id.is_(None)), "", "/contracts"))
    only.items.append(Item("Eingetragene Kündigungen", _count(session, Contract, Contract.status == "cancelled"), "", "/contracts"))
    only.items.append(Item("Garantien", _count(session, Warranty), "", "/warranties"))
    only.items.append(Item("Erstattungen (Leistungsabrechnungen)", _count(session, Reimbursement), "", "/reimbursements"))
    only.items.append(Item("Belege von dir zugeordnet oder „kein Beleg nötig“",
                           _count(session, ReceiptCheck, ReceiptCheck.decided.is_(True)), "", "/receipts?state=linked"))
    only.items.append(Item("Feste Zuordnungen und ausgeblendete Empfänger (Belege)",
                           _kv_len(session, "receipt_fixed") + _kv_len(session, "receipt_ignore"), "", "/receipts"))
    only.items.append(Item("Regelmäßige Zahlungen: deine Zuordnungen, herausgenommene Buchungen, eigene Gruppen",
                           _kv_len(session, "recurring_links") + _kv_len(session, "recurring_excluded")
                           + _kv_len(session, "recurring_splits"), "", "/recurring"))
    only.items.append(Item("Ausgeblendete Hinweise (regelmäßige Zahlungen)", _kv_len(session, "recurring_dismissed"), "", "/recurring"))
    only.items.append(Item("Vorgänge mit Notizen", _count(session, Case),
                           f"{_count(session, CaseEntry)} Notizen", "/cases"))
    only.items.append(Item("Art von Hand gewählt (Rechnung, Vertrag, Sonstiges …)",
                           _count(session, Document, Document.kind_source == "manual"), "", "/documents?view=all"))
    only.items.append(Item("Bestätigte Analysen", _count(session, Document, Document.review_state == "confirmed"),
                           "Werte, die du geprüft hast – würden neu vorgeschlagen und müssten erneut bestätigt werden", "/documents"))
    only.items.append(Item("Entscheidungen zu Metadaten-Vorschlägen (Lernen)", _count(session, MetaDecision),
                           "z.B. verworfene Tags – daraus lernt die Erkennung"))
    only.items.append(Item("Regelvorschläge entschieden", _count(session, RuleProposal, RuleProposal.state != "proposed"), "", "/rules"))
    only.items.append(Item("Aufbewahrungsregeln", _count(session, RetentionRule), "", "/retention"))
    only.items.append(Item("Einstellungen in der App und Dokumenttyp-Zuordnung",
                           _kv_len(session, "app_settings") + (1 if session.get(KeyValue, "doctype_settings") else 0),
                           "was nicht in der .env steht", "/settings"))
    only.items.append(Item("Verlauf", _count(session, Activity), "", "/history"))
    only.items = [i for i in only.items if i.count]

    # ------------------------------------------------------------------ wird neu erzeugt
    regen = Section("regen", "Kommt von selbst wieder",
                    "Erzeugt der Companion nach einer Neuinstallation aus Paperless und Firefly neu – nur Zeit, keine Arbeit.")
    regen.items.append(Item("Archiv-Index (Titel, Tags, Korrespondenten, Paperless-Felder)", _count(session, Document)))
    regen.items.append(Item("Analysen und Vorschläge (Rechnungen, Verträge … in der Inbox)",
                            _count(session, Document, Document.extraction_state == "done")))
    regen.items.append(Item("Erkannte regelmäßige Zahlungen", _kv_len(session, "recurring")))
    regen.items.append(Item("Buchungen auf der Belegseite (ohne deine Entscheidungen)",
                            _count(session, ReceiptCheck, ReceiptCheck.decided.is_(False))))
    regen.items.append(Item("Serien wiederkehrender Dokumente", _kv_len(session, "series")))
    return [pending, only, regen]


def backup(settings: Settings, target: Path) -> Path:
    """Konsistente Kopie der Datenbank (auch während der Companion läuft)."""
    source = sqlite3.connect(str(settings.data_dir / "companion.db"))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return target
