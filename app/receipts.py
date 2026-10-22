"""Buchungen ohne Beleg: Firefly-Abbuchungen, zu denen (noch) kein Dokument in Paperless gefunden wurde.

Alles bleibt im Companion – nichts wird in Paperless oder Firefly geändert. Du entscheidest je Buchung:
Beleg zuordnen, „kein Beleg nötig“ oder einen Empfänger dauerhaft ausblenden.
"""

import json
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.firefly import FireflyClient, Transaction
from app.models import Document, Invoice, KeyValue, PaymentMatch, ReceiptCheck
from app.payments import _compact, _words

STATES = {"missing": "Beleg fehlt", "candidate": "Möglicher Beleg", "linked": "Zugeordnet", "ignored": "Kein Beleg nötig"}
IGNORE_KEY = "receipt_ignore"


# --------------------------------------------------------------------------- ausgeblendete Empfänger


def ignored_counterparties(session: Session) -> list[str]:
    item = session.get(KeyValue, IGNORE_KEY)
    return json.loads(item.value) if item else []


def set_ignored_counterparties(session: Session, names: list[str]) -> None:
    names = sorted({n.strip() for n in names if n.strip()}, key=str.lower)
    item = session.get(KeyValue, IGNORE_KEY)
    if item:
        item.value = json.dumps(names)
    else:
        session.add(KeyValue(key=IGNORE_KEY, value=json.dumps(names)))


def _is_ignored(counterparty: str, ignored: list[str]) -> bool:
    cp = counterparty.lower()
    return any(name.lower() in cp for name in ignored)


# --------------------------------------------------------------------------- Belege suchen


def _linked_transactions(session: Session) -> dict[str, int]:
    """Buchungen, die bereits über den Zahlungsabgleich einer Rechnung zugeordnet sind (tx_id -> Dokument)."""
    linked: dict[str, int] = {}
    for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state.in_(("accepted", "suggested")))
                             .options(selectinload(PaymentMatch.invoice))):
        if m.invoice is not None:
            linked[m.tx_id] = m.invoice.document_id
    for doc_id, sugg in session.execute(select(Document.id, Document.suggestions)):
        p = (sugg or {}).get("_payment") if isinstance(sugg, dict) else None
        if p and p.get("tx_id"):
            linked.setdefault(p["tx_id"], doc_id)
    return linked


def candidates_for(session: Session, tx: Transaction, invoices: list[Invoice], docs: list[Document]) -> list[dict]:
    """Mögliche Belege: Rechnung mit gleichem Betrag in zeitlicher Nähe, sonst Dokument des Empfängers in Zeitnähe."""
    found: list[dict] = []
    for inv in invoices:
        if inv.amount is None or abs(inv.amount - tx.amount) > 0.01:
            continue
        ref = inv.invoice_date or inv.due_date or (inv.document.created if inv.document else None)
        if ref and -10 <= (tx.date - ref).days <= 75:
            found.append({"id": inv.document_id, "title": inv.document.title if inv.document else "",
                          "reason": "gleicher Betrag" + (", Empfänger passt" if _name_match(inv.issuer, tx) else "")})
    if not found:
        words = _words(tx.counterparty)
        for doc in docs:
            if not doc.correspondent or not doc.created or abs((tx.date - doc.created).days) > 21:
                continue
            if words & _words(doc.correspondent) or _compact(doc.correspondent) in _compact(tx.counterparty):
                found.append({"id": doc.id, "title": doc.title, "reason": "gleicher Absender, Datum in der Nähe"})
    found.sort(key=lambda c: "Empfänger" not in c["reason"])
    return found[:5]


def _name_match(name: str | None, tx: Transaction) -> bool:
    return bool(name) and bool(_words(name) & _words(tx.counterparty))


def check(session: Session, transactions: list[Transaction], settings: Settings) -> dict[str, int]:
    """Buchungen ab dem Mindestbetrag prüfen. Deine Entscheidungen (zugeordnet/ignoriert) bleiben unangetastet."""
    linked = _linked_transactions(session)
    ignored = ignored_counterparties(session)
    invoices = list(session.scalars(select(Invoice).options(selectinload(Invoice.document))))
    start = min((tx.date for tx in transactions), default=date.today()) - timedelta(days=30)
    docs = list(session.scalars(select(Document).where(Document.correspondent.is_not(None), Document.created >= start)))
    existing = {r.tx_id: r for r in session.scalars(select(ReceiptCheck))}
    counts = {"checked": 0, "missing": 0, "candidate": 0, "linked": 0}
    for tx in transactions:
        if tx.amount < settings.receipts_min_amount:
            continue
        row = existing.get(tx.journal_id)
        if row is None:
            row = ReceiptCheck(tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=tx.amount,
                               counterparty=tx.counterparty[:200], description=tx.description[:300])
            session.add(row)
            existing[tx.journal_id] = row
        counts["checked"] += 1
        if row.decided:
            continue
        if tx.journal_id in linked:
            row.state, row.document_id, row.candidates = "linked", linked[tx.journal_id], None
            row.note = "über den Zahlungsabgleich"
        elif _is_ignored(tx.counterparty, ignored):
            row.state, row.candidates, row.note = "ignored", None, "Empfänger ausgeblendet"
        elif cands := candidates_for(session, tx, invoices, docs):
            row.state, row.candidates, row.note = "candidate", cands, None
        else:
            row.state, row.candidates, row.note = "missing", None, None
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


def run(session: Session, client: FireflyClient, settings: Settings, today: date) -> dict[str, int]:
    transactions = list(client.withdrawals(today - timedelta(days=settings.receipts_lookback_days), today))
    return check(session, transactions, settings)


# --------------------------------------------------------------------------- Entscheidungen


def link(session: Session, row: ReceiptCheck, doc_id: int) -> bool:
    if session.get(Document, doc_id) is None:
        return False
    row.state, row.document_id, row.decided, row.note = "linked", doc_id, True, "von dir zugeordnet"
    return True


def ignore(row: ReceiptCheck, note: str = "kein Beleg nötig") -> None:
    row.state, row.decided, row.note = "ignored", True, note


def reopen(row: ReceiptCheck) -> None:
    row.decided, row.document_id, row.note = False, None, None
    row.state = "candidate" if row.candidates else "missing"


def overview(session: Session) -> dict:
    rows = list(session.scalars(select(ReceiptCheck)))
    counts = {state: 0 for state in STATES}
    for r in rows:
        counts[r.state] = counts.get(r.state, 0) + 1
    missing_sum = round(sum(r.amount for r in rows if r.state == "missing"), 2)
    return {"counts": counts, "missing_sum": missing_sum, "total": len(rows)}
