"""Erstattungen (z.B. Leistungsabrechnungen der Krankenversicherung oder Beihilfe).

Welche Rechnungen zu einer Erstattung gehören, kommt aus den Paperless-Dokumentverknüpfungen („gehört zu“) –
im Companion wird nichts doppelt gepflegt. Daraus ergeben sich Summe der Rechnungen, erstatteter Betrag und
Eigenanteil (für die Steuer). Den Geldeingang sucht der Abgleich unter den Einnahmen in Firefly.
"""

import re
from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.firefly import FireflyClient, Transaction
from app.models import Document, Reimbursement

STATUS = {"open": "Geld erwartet", "received": "erhalten"}
STOPWORDS = {"gmbh", "mbh", "und", "der", "die", "das", "versicherung", "krankenversicherung", "ag", "a.g.", "vvag",
             "deutschland", "service", "kasse"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]{3,}", (text or "").lower()) if w not in STOPWORDS}


def invoice_amount(doc: Document) -> float | None:
    """Betrag einer Rechnung: bestätigter Datensatz, sonst Vorschlag, sonst Paperless-Feld."""
    if doc.invoice is not None and doc.invoice.amount is not None:
        return doc.invoice.amount
    value = ((doc.suggestions or {}).get("amount") or {}).get("value")
    if value is None:
        value = (doc.paperless_fields or {}).get("amount")
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def reimbursed_amount(doc: Document) -> float | None:
    if doc.reimbursement is not None and doc.reimbursement.amount is not None:
        return doc.reimbursement.amount
    value = ((doc.suggestions or {}).get("amount") or {}).get("value")
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Summary:
    """Eine Erstattung mit den verknüpften Rechnungen."""

    document: Document
    invoices: list[tuple[Document, float | None]] = dc_field(default_factory=list)
    others: list[Document] = dc_field(default_factory=list)  # verknüpft, aber keine Rechnung (z.B. Einreichung)

    @property
    def record(self) -> Reimbursement | None:
        return self.document.reimbursement

    @property
    def reimbursed(self) -> float | None:
        return reimbursed_amount(self.document)

    @property
    def invoice_sum(self) -> float | None:
        amounts = [a for _, a in self.invoices if a is not None]
        return round(sum(amounts), 2) if amounts else None

    @property
    def own_share(self) -> float | None:
        """Eigenanteil = Summe der Rechnungen − Erstattung (nur, wenn alle Beträge bekannt sind)."""
        if not self.invoices or any(a is None for _, a in self.invoices) or self.reimbursed is None:
            return None
        return round(max(self.invoice_sum - self.reimbursed, 0), 2)

    @property
    def status(self) -> str:
        return self.record.status if self.record else "open"


def related_graph(session: Session) -> dict[int, set[int]]:
    """Verknüpfungsgraph aus den Paperless-Feldern (beide Richtungen, direkt – ohne Umwege über andere Erstattungen)."""
    graph: dict[int, set[int]] = {}
    for doc in session.scalars(select(Document).where(Document.paperless_fields.is_not(None))):
        for other in (doc.paperless_fields or {}).get("related") or []:
            graph.setdefault(doc.id, set()).add(other)
            graph.setdefault(other, set()).add(doc.id)
    return graph


def summary(session: Session, doc: Document, graph: dict[int, set[int]] | None = None) -> Summary:
    """Verknüpfte Rechnungen einer Erstattung (auch über ein dazwischenliegendes Einreichungs-Dokument)."""
    graph = graph if graph is not None else related_graph(session)
    result = Summary(doc)
    seen, todo = {doc.id}, [doc.id]
    while todo:
        for other_id in sorted(graph.get(todo.pop(), ())):
            if other_id in seen:
                continue
            seen.add(other_id)
            other = session.get(Document, other_id)
            if other is None:
                continue
            if other.kind == "invoice":
                result.invoices.append((other, invoice_amount(other)))
            elif other.kind == "reimbursement":
                continue  # andere Erstattung: deren Rechnungen gehören nicht hierher
            else:
                result.others.append(other)
                todo.append(other_id)  # z.B. Einreichungsschreiben, an dem die Rechnungen hängen
    result.invoices.sort(key=lambda item: (item[0].created is None, item[0].created or date.min, item[0].id))
    return result


def overview(session: Session, settings: Settings) -> dict:
    """Alle Erstattungen und – falls ein Tag für erstattungsfähige Rechnungen gesetzt ist – Rechnungen ohne Erstattung."""
    graph = related_graph(session)
    docs = session.scalars(select(Document).where(Document.kind == "reimbursement")
                           .order_by(Document.created.desc(), Document.id.desc())).all()
    items = [summary(session, d, graph) for d in docs]
    covered = {inv.id for item in items for inv, _ in item.invoices}
    missing: list[tuple[Document, float | None]] = []
    tag = (settings.reimbursable_tag or "").strip().lower()
    if tag:
        for doc in session.scalars(select(Document).where(Document.kind == "invoice").order_by(Document.created.desc())):
            if doc.id not in covered and tag in {t.lower() for t in doc.tags or []}:
                missing.append((doc, invoice_amount(doc)))
    expected = [i for i in items if i.status != "received" and i.reimbursed]
    return {
        "items": items,
        "missing": missing,
        "expected_sum": round(sum(i.reimbursed for i in expected), 2),
        "expected_count": len(expected),
        "found": [i for i in items if i.record and i.record.tx_state == "suggested"],
    }


# --------------------------------------------------------------------------- Geldeingang in Firefly


def window(rec: Reimbursement, doc: Document) -> tuple[date, date] | None:
    base = rec.statement_date or doc.created
    if base is None:
        return None
    return base - timedelta(days=10), base + timedelta(days=60)


def score(rec: Reimbursement, doc: Document, tx: Transaction) -> tuple[int, list[str]] | None:
    """Passt der Geldeingang zur Erstattung? Betrag muss stimmen, Absender gibt Zusatzpunkte."""
    if rec.amount is None or abs(tx.amount - rec.amount) > 0.005:
        return None
    span = window(rec, doc)
    if span is None or not span[0] <= tx.date <= span[1]:
        return None
    points, reasons = 50, ["Betrag"]
    if _words(rec.payer or doc.correspondent or "") & _words(f"{tx.counterparty} {tx.description}"):
        points, reasons = points + 30, [*reasons, "Absender"]
    return points, reasons


def used_transactions(session: Session, exclude: int | None = None) -> set[str]:
    return {r.tx_id for r in session.scalars(select(Reimbursement).where(Reimbursement.tx_id.is_not(None)))
            if r.document_id != exclude}


def match(session: Session, deposits: list[Transaction]) -> int:
    """Für offene Erstattungen einen passenden Geldeingang vorschlagen (bestätigst du auf der Seite „Erstattungen“)."""
    used = used_transactions(session)
    found = 0
    for rec in session.scalars(select(Reimbursement).where(Reimbursement.status == "open", Reimbursement.tx_id.is_(None),
                                                         Reimbursement.amount.is_not(None))):
        rejected = set(rec.tx_rejected or [])
        scored = sorted(((result, tx) for tx in deposits if tx.journal_id not in used and tx.journal_id not in rejected
                         and (result := score(rec, rec.document, tx))), key=lambda item: -item[0][0])
        if not scored:
            continue
        (points, reasons), tx = scored[0]
        if points < 80 and len(scored) > 1:
            continue  # nur der Betrag passt und ist nicht eindeutig
        set_transaction(rec, tx, "suggested")
        used.add(tx.journal_id)
        found += 1
    return found


def run(session: Session, client: FireflyClient, settings: Settings, today: date) -> int:
    open_recs = list(session.scalars(select(Reimbursement).where(Reimbursement.status == "open",
                                                                 Reimbursement.tx_id.is_(None))))
    starts = [span[0] for rec in open_recs if (span := window(rec, rec.document))]
    if not starts:
        return 0
    start = max(min(starts), today - timedelta(days=settings.firefly_lookback_days))
    return match(session, list(client.deposits(start, today)))


def record_confirmed(doc: Document) -> None:
    """Beim Bestätigen: in der Inbox gewählten Geldeingang an der Erstattung festhalten."""
    d = (doc.suggestions or {}).get("_deposit")
    rec = doc.reimbursement
    if not d or rec is None or (rec.received_on and rec.received_on.isoformat() != d["date"]):
        return
    set_transaction(rec, Transaction(journal_id=d["tx_id"], group_id=d["group_id"], date=date.fromisoformat(d["date"]),
                                     amount=d["amount"], counterparty=d.get("counterparty") or "", iban=None,
                                     description="", text=""), "accepted")


def set_transaction(rec: Reimbursement, tx: Transaction, state: str) -> None:
    rec.tx_id, rec.tx_group_id, rec.tx_date, rec.tx_amount = tx.journal_id, tx.group_id, tx.date, tx.amount
    rec.tx_counterparty, rec.tx_state = tx.counterparty[:200], state
    if state == "accepted":
        rec.status, rec.received_on = "received", tx.date


def accept(rec: Reimbursement) -> None:
    rec.tx_state, rec.status, rec.received_on = "accepted", "received", rec.tx_date


def reject(rec: Reimbursement) -> None:
    """„Passt nicht“: Buchung lösen und nicht wieder vorschlagen."""
    if rec.tx_id:
        rec.tx_rejected = sorted({*(rec.tx_rejected or []), rec.tx_id})
    if rec.status == "received" and rec.received_on == rec.tx_date:
        rec.status, rec.received_on = "open", None
    rec.tx_id = rec.tx_group_id = rec.tx_date = rec.tx_amount = rec.tx_counterparty = rec.tx_state = None
