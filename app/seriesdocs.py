"""Rechnungen zu einer regelmäßigen Zahlung: Für jede Abbuchung einer Serie (Abo, Vertrag oder nur erkannte Zahlung)
die passende Rechnung suchen – gleicher Absender, gleicher Betrag, zeitlich passend – und gesammelt vorschlagen.

So brauchst du für ein Abo mit monatlichen Rechnungen keinen Vertrag: Die Rechnungen gelten als bezahlt, die Buchungen
als belegt. Alles bleibt im Companion; übernommen wird nur, was du anhakst.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import payments, receipts
from app.firefly import Transaction
from app.models import Document, PaymentMatch, ReceiptCheck
from app.payments import _compact, _words
from app.recurring import Series


def transactions_of(s: Series) -> list[Transaction]:
    return [Transaction(journal_id=s.tx_ids[i], group_id=s.group_ids[i] if i < len(s.group_ids) else "",
                        date=date.fromisoformat(day), amount=s.amounts[i], counterparty=s.payee, iban=None,
                        description=s.descriptions[i] if i < len(s.descriptions) else "",
                        text=s.descriptions[i] if i < len(s.descriptions) else "")
            for i, day in enumerate(s.dates)]


def _name_fits(name: str, payee: str) -> bool:
    if not name:
        return False
    return bool(_words(name) & _words(payee)) or _compact(name) in _compact(payee) or _compact(payee) in _compact(name)


def tx_documents(session: Session) -> dict[str, dict]:
    """Welche Buchung gehört zu welchem Dokument – und wie fest? (tx_id -> {"doc_id", "state"})
    state: "confirmed" (von dir bzw. bestätigt), "suggested" (Zahlungsabgleich schlägt nur vor), "inbox" (in der Inbox
    vorbelegt, Dokument noch nicht bestätigt)."""
    out: dict[str, dict] = {}
    for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state.in_(("accepted", "suggested")))):
        if m.invoice is not None:
            entry = {"doc_id": m.invoice.document_id, "state": "confirmed" if m.state == "accepted" else "suggested"}
            if out.get(m.tx_id, {}).get("state") != "confirmed":
                out[m.tx_id] = entry
    for doc in session.scalars(select(Document).where(Document.review_state == "pending")):
        if (p := (doc.suggestions or {}).get("_payment")) and p.get("tx_id"):
            out.setdefault(p["tx_id"], {"doc_id": doc.id, "state": "inbox"})
    for row in session.scalars(select(ReceiptCheck).where(ReceiptCheck.state == "linked", ReceiptCheck.document_id.is_not(None))):
        if out.get(row.tx_id, {}).get("state") in (None, "suggested") and row.decided:
            out[row.tx_id] = {"doc_id": row.document_id, "state": "confirmed"}
        else:
            out.setdefault(row.tx_id, {"doc_id": row.document_id, "state": "confirmed" if row.decided else "suggested"})
    return out


def pairs(session: Session, s: Series) -> list[dict]:
    """Je Abbuchung (neueste zuerst): zugeordnetes Dokument (bestätigt), ein noch zu bestätigender Vorschlag des
    Zahlungsabgleichs oder der beste eigene Vorschlag. Jedes Dokument wird höchstens einer Buchung vorgeschlagen."""
    known = tx_documents(session)
    fixed = {t: e for t, e in known.items() if e["state"] in ("confirmed", "inbox")}
    taken_docs = {e["doc_id"] for e in known.values()}
    txs = transactions_of(s)
    pool = [d for d in session.scalars(select(Document).where(Document.kind.in_(("invoice", "receipt", "other"))))
            if d.id not in taken_docs
            and _name_fits((d.invoice.issuer if d.invoice and d.invoice.issuer else None) or d.correspondent or "", s.payee)]
    scored = []
    for tx in txs:
        if tx.journal_id in known:
            continue
        for doc in pool:
            result = receipts.score_document(tx, doc, set(), set())
            if result and result[0] >= receipts.MIN_SCORE and receipts._doc_amount(doc) is not None:
                scored.append((result[0], tx.journal_id, doc, result[1]))
    scored.sort(key=lambda x: -x[0])
    chosen: dict[str, tuple] = {}
    docs_used: set[int] = set()
    for points, tx_id, doc, reasons in scored:
        if tx_id in chosen or doc.id in docs_used:
            continue
        chosen[tx_id] = (points, doc, reasons)
        docs_used.add(doc.id)
    rows = []
    for tx in sorted(txs, key=lambda t: t.date, reverse=True):
        row = {"tx": tx, "doc": None, "state": "none", "reason": "", "sure": False}
        if tx.journal_id in fixed:
            entry = fixed[tx.journal_id]
            row.update(state="done", doc=session.get(Document, entry["doc_id"]),
                       reason="in der Inbox vorbelegt" if entry["state"] == "inbox" else "")
        elif tx.journal_id in known:  # nur vorgeschlagen (Zahlungsabgleich) – bestätigen
            row.update(state="suggest", doc=session.get(Document, known[tx.journal_id]["doc_id"]),
                       reason="vom Zahlungsabgleich vorgeschlagen", sure=True)
        elif tx.journal_id in chosen:
            points, doc, reasons = chosen[tx.journal_id]
            row.update(state="suggest", doc=doc, reason=", ".join(reasons), sure="gleicher Betrag" in reasons)
        rows.append(row)
    return rows


def assign_docs(session: Session, s: Series, docs: list[Document]) -> list[dict]:
    """Gesuchte Dokumente auf die noch freien Buchungen der Serie verteilen: gleicher Betrag zuerst, dann die
    zeitlich nächste Buchung (Rechnung kurz vor oder nach der Abbuchung). Ergebnis je Dokument, mit Buchung oder ohne."""
    known = tx_documents(session)
    free = [t for t in transactions_of(s) if known.get(t.journal_id, {}).get("state") not in ("confirmed", "inbox")]
    taken_docs = {e["doc_id"]: t for t, e in known.items() if e["state"] in ("confirmed", "inbox")}
    options = []
    for doc in docs:
        if doc.id in taken_docs:
            continue
        inv = doc.invoice
        ref = (inv.invoice_date if inv else None) or doc.created or (inv.due_date if inv else None)
        amount = receipts._doc_amount(doc)
        for tx in free:
            days = abs((tx.date - ref).days) if ref else 60
            if days > 75:
                continue
            same = amount is not None and abs(amount - tx.amount) <= 0.01
            if amount is not None and not same and abs(amount - tx.amount) > max(1.0, amount * 0.05):
                continue
            options.append((0 if same else 1, days, doc.id, tx))
    options.sort(key=lambda o: o[:2])
    picked: dict[int, tuple] = {}
    used_tx: set[str] = set()
    for rank, days, doc_id, tx in options:
        if doc_id in picked or tx.journal_id in used_tx:
            continue
        picked[doc_id] = (tx, rank, days)
        used_tx.add(tx.journal_id)
    out = []
    for doc in docs:
        if doc.id in taken_docs:
            out.append({"doc": doc, "tx": None, "note": "schon einer Buchung zugeordnet"})
        elif doc.id in picked:
            tx, rank, days = picked[doc.id]
            out.append({"doc": doc, "tx": tx, "sure": rank == 0,
                        "note": ("gleicher Betrag" if rank == 0 else "Betrag fast gleich") + f", {days} Tage Abstand"})
        else:
            out.append({"doc": doc, "tx": None, "note": "keine freie Buchung passt (Betrag oder Datum)"})
    return out


def count_open(session: Session, s: Series) -> int:
    return sum(1 for r in pairs(session, s) if r["state"] == "suggest")


def apply(session: Session, s: Series, picks: list[tuple[str, int]]) -> int:
    """Angehakte Paare übernehmen: Rechnung gilt als bezahlt (bzw. in der Inbox vorbelegt), die Buchung als belegt."""
    txs = {t.journal_id: t for t in transactions_of(s)}
    done = 0
    seen_tx: set[str] = set()
    seen_doc: set[int] = set()
    for tx_id, doc_id in picks:
        tx, doc = txs.get(tx_id), session.get(Document, doc_id)
        if tx is None or doc is None or tx_id in seen_tx or doc_id in seen_doc:
            continue  # je Buchung und je Dokument nur eine Zuordnung (die erste angehakte zählt)
        seen_tx.add(tx_id)
        seen_doc.add(doc_id)
        if doc.kind == "invoice" and (doc.invoice is not None or doc.review_state == "pending"):
            payments.choose(session, doc, tx, take=())
        row = session.scalar(select(ReceiptCheck).where(ReceiptCheck.tx_id == tx_id))
        if row is None:
            row = ReceiptCheck(tx_id=tx_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=tx.amount,
                               counterparty=tx.counterparty[:200], description=tx.description[:300])
            session.add(row)
        receipts.link(session, row, doc.id)
        done += 1
    session.flush()
    receipts.sync_payments(session)
    return done


def contract_doc_suggestions(session: Session, contract, limit: int = 5) -> list[Document]:
    """Mögliche Dokumente für ein Abo ohne Dokument: Absender oder Titel passt zu Empfänger bzw. Name des Abos,
    noch kein Vertrag dran. Verträge zuerst, dann die neuesten."""
    names = [n for n in (contract.payee, contract.name, contract.provider) if n]
    if not names:
        return []
    hits = []
    for d in session.scalars(select(Document)):
        if d.contract is not None:
            continue
        if any(_name_fits(d.correspondent or "", n) or _name_fits(d.title or "", n) for n in names):
            hits.append(d)
    hits.sort(key=lambda d: (d.kind != "contract", -(d.created.toordinal() if d.created else 0)))
    return hits[:limit]


def series_for_doc(session: Session, doc_id: int) -> list[dict]:
    """Zu welchem Vertrag/Abo bzw. welcher regelmäßigen Zahlung gehört ein Dokument (über seine Abbuchung)?"""
    from app import recurring
    from app.models import Contract

    txs = {t: e for t, e in tx_documents(session).items() if e["doc_id"] == doc_id}
    if not txs:
        return []
    out = []
    for s in recurring.stored(session):
        for t, e in txs.items():
            if t not in s.tx_ids:
                continue
            i = s.tx_ids.index(t)
            c = session.get(Contract, s.contract_id) if s.contract_id else None
            href = (f"/documents/{c.document_id}#abbuchungen" if c and c.document_id else f"/contracts/{c.id}") if c else "/recurring"
            out.append({"label": c.label if c else s.payee, "kind": ("Abo" if c.virtual else "Vertrag") if c else "", "href": href, "date": date.fromisoformat(s.dates[i]),
                        "amount": s.amounts[i], "state": e["state"], "key": s.key})
    return out
