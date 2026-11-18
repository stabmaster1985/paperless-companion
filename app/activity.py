"""Verlauf: jede Aktion über die Oberfläche (POST mit Meldung) wird mit Dokument und Zielseite festgehalten.

Die Meldung ist dieselbe, die du nach der Aktion oben auf der Seite siehst. Das betroffene Dokument wird aus der
Adresse der Aktion bestimmt (Dokument, Buchung, Rechnung, Vertrag, Zahlung). Gespeichert werden die letzten
KEEP Einträge; Paperless erfährt davon nichts.
"""

import logging
import queue
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Activity, Contract, Document, Invoice, PaymentMatch, ReceiptCheck

log = logging.getLogger(__name__)
KEEP = 2000
_queue: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()
_worker: threading.Thread | None = None
SKIP = ("/login", "/logout", "/api/", "/webhook", "/static/")


def document_for(session: Session, path: str) -> int | None:
    """Betroffenes Dokument zu einer Aktions-Adresse."""
    patterns = [
        (r"^/documents/(\d+)", lambda i: i),
        (r"^/(?:cancellations|reimbursements|reminders)/(\d+)", lambda i: i),
        (r"^/receipts/(\d+)/", lambda i: (row := session.get(ReceiptCheck, i)) and row.document_id),
        (r"^/invoices/(\d+)/", lambda i: (inv := session.get(Invoice, i)) and inv.document_id),
        (r"^/contracts/(\d+)/", lambda i: (c := session.get(Contract, i)) and c.document_id),
        (r"^/payments/(\d+)/", lambda i: (m := session.get(PaymentMatch, i)) and m.invoice and m.invoice.document_id),
    ]
    for pattern, resolve in patterns:
        if m := re.match(pattern, path):
            try:
                return resolve(int(m.group(1))) or None
            except Exception:  # noqa: BLE001 – Verlauf darf nie eine Aktion stören
                return None
    return None


def split_target(location: str) -> tuple[str, str]:
    """Zieladresse ohne Meldung und die Meldung selbst."""
    parts = urlsplit(location)
    query = parse_qs(parts.query)
    msg = (query.pop("msg", [""]) or [""])[0]
    rest = urlencode({k: v[0] for k, v in query.items()})
    return parts.path + (f"?{rest}" if rest else "") + (f"#{parts.fragment}" if parts.fragment else ""), msg


LABELS = [  # Aktionen ohne eigene Meldung: verständliche Beschreibung aus der Adresse
    (r"/documents/\d+/kind$", "Art geändert"),
    (r"/documents/\d+/confirm$", "Bestätigt bzw. gespeichert"),
    (r"/documents/\d+/reanalyze$", "Neu analysieren angestoßen"),
    (r"/documents/\d+/ignore$", "Ignoriert"),
    (r"/documents/\d+/hold$", "Aufbewahrung geändert"),
    (r"/documents/\d+/meta$", "Metadaten-Vorschläge entschieden"),
    (r"/documents/\d+/comment$", "Kommentar vorgemerkt"),
    (r"/documents/\d+/summary$", "Zusammenfassung"),
    (r"/documents/\d+/tax-year$", "Steuerjahr geändert"),
    (r"/documents/\d+/keep-title$", "Titel behalten"),
    (r"/documents/\d+/(add-to-case|case-from-links)$", "Zu Vorgang hinzugefügt"),
    (r"/invoices/\d+/paid$", "Rechnung als bezahlt markiert"),
    (r"/invoices/\d+/open$", "Rechnung wieder offen"),
    (r"/payments/\d+/accept$", "Zahlungsvorschlag übernommen"),
    (r"/payments/\d+/reject$", "Zahlungsvorschlag abgelehnt"),
    (r"/contracts/\d+/cancelled$", "Vertrag als gekündigt markiert"),
    (r"/review/\d+$", "Vorschlag entschieden"),
    (r"^/paperless-changes", "Für Paperless"),
    (r"^/cases", "Vorgang geändert"),
    (r"^/rules", "Regeln geändert"),
    (r"^/settings", "Einstellungen geändert"),
    (r"^/retention", "Aufbewahrung"),
    (r"^/system", "System"),
]


def label_for(path: str) -> str:
    for pattern, label in LABELS:
        if re.search(pattern, path):
            return label
    return "Aktion: " + path.rstrip("/").rsplit("/", 1)[-1]


def record(session: Session, path: str, text: str, target: str | None) -> None:
    if path.startswith(SKIP):
        return
    doc_id = document_for(session, path)
    if not text:
        text = label_for(path)
        doc = session.get(Document, doc_id) if doc_id else None
        if doc is not None and path.endswith("/kind"):
            from app.pipeline import KINDS

            text = f"Art geändert: {KINDS.get(doc.kind, doc.kind)}"
    session.add(Activity(text=text[:500], path=path[:300], document_id=doc_id, target=(target or "")[:300] or None))
    newest = session.scalars(select(Activity.id).order_by(Activity.id.desc()).offset(KEEP).limit(1)).first()
    if newest:
        session.execute(delete(Activity).where(Activity.id <= newest))


def state_of(doc: Document | None) -> str:
    """Wo das Dokument jetzt steht (kurz)."""
    if doc is None:
        return ""
    parts = []
    if doc.review_state == "pending":
        parts.append("in der Inbox")
    elif doc.review_state == "confirmed":
        parts.append("bestätigt")
    if doc.invoice is not None:
        inv = doc.invoice
        parts.append("bezahlt" if inv.status == "paid" else ("teilweise bezahlt" if inv.paid_amount else "offen"))
    if doc.contract is not None and doc.contract.status != "active":
        parts.append("gekündigt" if doc.contract.status == "cancelled" else "beendet")
    if doc.writeback_pending or doc.meta_pending:
        parts.append("wartet auf Übertragung nach Paperless")
    return " · ".join(parts)


def recent(session: Session, document_id: int | None = None, limit: int = 200) -> list[Activity]:
    query = select(Activity).order_by(Activity.id.desc()).limit(limit)
    if document_id is not None:
        query = query.where(Activity.document_id == document_id)
    entries = list(session.scalars(query))
    for e in entries:  # Dokument noch nicht bekannt (z.B. Buchung erst danach zugeordnet): jetzt nachtragen
        if e.document_id is None and e.path:
            e.document_id = document_for(session, e.path)
    return entries


def _run() -> None:
    from app.db import session_scope

    while True:
        path, text, target = _queue.get()
        time.sleep(0.2)  # die Aktion speichert ihre Änderungen erst, wenn die Antwort raus ist
        try:
            with session_scope() as s:  # wartet, bis die Aktion selbst gespeichert ist (SQLite-Sperre)
                record(s, path, text, target)
        except Exception as exc:  # noqa: BLE001 – der Verlauf darf nie eine Aktion stören
            log.warning("Verlauf konnte nicht geschrieben werden: %s", exc)
        finally:
            _queue.task_done()


def submit(path: str, text: str, target: str | None) -> None:
    """Eintrag im Hintergrund schreiben – die Aktion hält ihre Datenbank-Sitzung noch, bis die Antwort raus ist."""
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="verlauf", daemon=True)
        _worker.start()
    _queue.put((path, text, target))


def drain() -> None:
    """Warten, bis alle Einträge geschrieben sind (für Tests)."""
    _queue.join()


PAGES = {"/inbox": "Inbox", "/paperless-changes": "Für Paperless", "/review": "Vorschläge durchgehen", "/receipts": "Belege",
         "/bulk": "Sammelaufgaben", "/invoices": "Rechnungen", "/reimbursements": "Erstattungen", "/contracts": "Verträge",
         "/warranties": "Garantien", "/cases": "Vorgänge", "/tax": "Steuer", "/documents": "Dokumente", "/series": "Serien",
         "/rules": "Regeln", "/retention": "Aufbewahrung", "/settings": "Einstellungen", "/system": "System", "/": "Übersicht"}


def page_name(target: str | None) -> str:
    """Lesbarer Name der Seite, auf der du gelandet bist."""
    path = (target or "").split("?")[0].split("#")[0]
    if re.match(r"^/documents/\d+", path):
        return "Dokument"
    for prefix in sorted(PAGES, key=len, reverse=True):
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")) or path.startswith(prefix + "?"):
            return PAGES[prefix]
    return path or "–"
