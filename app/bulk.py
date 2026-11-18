"""Sammelaufgaben: alles Offene, was der Companion mit gutem Gewissen zuordnen kann, auf einer Seite.

Nichts davon geht nach Paperless – übernommen wird nur im Companion. „Sicher“ (vorausgewählt) heißt:
- Zahlung zu offener Rechnung: Betrag plus Rechnungsnummer oder IBAN (wie beim automatischen Übernehmen)
- Beleg zu Buchung: gleicher Betrag und Empfänger und kein ähnlich guter zweiter Treffer
- Inbox-Dokument: alle nötigen Angaben vorhanden und sicher erkannt (Verträge nie vorausgewählt)
- Kündigungsbestätigung: Vertrag und Vertragsende aus dem Text erkannt
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cancellations, payments
from app.models import Document, ReceiptCheck

SURE_RECEIPT = 95  # Punkte (siehe receipts.score_document): Betrag + Empfänger + zeitliche Nähe
SURE_CONFIDENCE = 0.75
REQUIRED = {"invoice": ["amount", "due_date"], "warranty": ["purchase_date"], "reimbursement": ["amount"]}


def payment_items(session: Session) -> list[dict]:
    items = []
    for m in payments.suggestions(session):
        doc = m.invoice.document
        items.append({"key": f"pay:{m.id}", "sure": m.score >= payments.AUTO_MIN, "doc": doc, "match": m})
    return items


def receipt_items(session: Session) -> list[dict]:
    items = []
    for row in session.scalars(select(ReceiptCheck).where(ReceiptCheck.state == "candidate", ReceiptCheck.decided.is_(False))
                               .order_by(ReceiptCheck.tx_date.desc())):
        cands = row.candidates or []
        if not cands:
            continue
        top = cands[0]
        score = top.get("score") or 0
        runner_up = (cands[1].get("score") or 0) if len(cands) > 1 else 0
        sure = score >= SURE_RECEIPT and "gleicher Betrag" in top.get("reason", "") and runner_up <= score - 25
        items.append({"key": f"rc:{row.id}:{top['id']}", "sure": sure, "row": row, "cand": top, "more": len(cands) - 1})
    return items


def _sure_values(doc: Document) -> bool:
    if doc.kind == "contract":
        return False  # Verträge (Kündigungsfristen!) immer selbst ansehen
    sugg = doc.suggestions or {}
    for name in REQUIRED.get(doc.kind, []):
        item = sugg.get(name)
        if name == "due_date" and (sugg.get("payment_method", {}).get("value") == "debit"
                                   or sugg.get("status", {}).get("value") == "paid"):
            continue
        if not isinstance(item, dict) or (item.get("confidence") or 0) < SURE_CONFIDENCE:
            return False
    return True


def inbox_items(session: Session, quick_check) -> list[dict]:
    items = []
    for doc in session.scalars(select(Document).where(Document.review_state == "pending")
                               .order_by(Document.correspondent, Document.created)):
        if doc.kind not in REQUIRED and doc.kind != "contract":
            continue
        values, missing = quick_check(doc)
        if missing:
            continue
        items.append({"key": f"doc:{doc.id}", "sure": _sure_values(doc), "doc": doc, "values": values})
    return items


def cancel_items(session: Session) -> list[dict]:
    return [{"key": f"cancel:{c['doc'].id}:{c['contract'].id}", "sure": bool(c["effective"]) and not c["by_title"], **c}
            for c in cancellations.pending(session) if c["contract"] is not None]
