"""Mahnungen und Zahlungserinnerungen: erkennen, der Rechnung zuordnen, Frist und Mahngebühr merken.

Eine Mahnung ist keine neue Rechnung – sie wird als „Sonstiges“ eingeordnet und merkt sich die Rechnung, zu der sie
gehört. Auf deinen Klick hängt sie an der Rechnung (mit neuer Frist und Gebühr); auf Wunsch wird zusätzlich
„gehört zu“ vorgemerkt. Nichts davon geht automatisch nach Paperless.
"""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.rules import date_after_label, extract_number, find_amounts
from app.models import Document, Invoice

REMINDER_RE = re.compile(
    r"\b(?:\d\.\s*)?mahnung\b|zahlungserinnerung|erinnerung\s+an\s+(?:die\s+)?(?:zahlung|fälligkeit)|letzte\s+aufforderung"
    r"|inkasso|mahnstufe|zahlungsaufforderung|offener\s+betrag\s+aus\s+(?:unserer\s+)?rechnung",
    re.IGNORECASE,
)
NOT_REMINDER = re.compile(r"mahnbescheid\s+beantragen\s+sie|mahnwesen-?software", re.IGNORECASE)
NUMBER_LABEL = r"(?:rechnungs?[\s-]*(?:nr|nummer|no)|rechnung\s+(?:nr|vom)|beleg[\s-]*(?:nr|nummer))"
DUE_LABEL = r"bis\s+(?:spätestens\s+)?(?:zum\s+)?|spätestens\s+(?:bis\s+)?(?:zum\s+)?|zahlbar\s+bis|neue\s+frist"
FEE_LABEL = r"mahngebühr|mahnkosten|mahnspesen|säumniszuschlag|verzugszinsen"


def is_reminder(title: str | None, text: str = "") -> bool:
    head = (text or "")[:1500]
    if NOT_REMINDER.search(head):
        return False
    return bool(REMINDER_RE.search(title or "") or REMINDER_RE.search(head))


def _compact(value: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def _words(value: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]+", (value or "").lower()) if len(w) >= 3}


def extract(text: str) -> dict:
    """Rechnungsnummer, neue Zahlungsfrist und Mahngebühr aus dem Text der Mahnung."""
    text = text or ""
    fee = None
    for m in re.finditer(FEE_LABEL, text, re.IGNORECASE):
        if amounts := find_amounts(text[m.end(): m.end() + 40]):
            fee = amounts[0][1]
            break
    return {"invoice_number": extract_number(text, NUMBER_LABEL), "due": date_after_label(text, DUE_LABEL, 30), "fee": fee}


def find_invoice(session: Session, doc: Document, text: str = "", number: str | None = None) -> Invoice | None:
    """Passende Rechnung: Rechnungsnummer im Text, sonst gleicher Aussteller und Betrag im Text; offene bevorzugt."""
    head = _compact((text or "")[:3000])
    best, best_points = None, 0
    for inv in session.scalars(select(Invoice).where(Invoice.document_id != doc.id)):
        points = 0
        num = _compact(inv.invoice_number)
        if num and len(num) >= 4 and (num == _compact(number) or num in head):
            points += 4
        party = inv.issuer or (inv.document.correspondent if inv.document else None)
        if party and doc.correspondent and party.lower() == doc.correspondent.lower():
            points += 2
        elif party and _words(party) & (_words(doc.correspondent) | _words(doc.title) | _words((text or "")[:400])):
            points += 1
        if inv.amount is not None and _compact(f"{inv.amount:.2f}".replace(".", ",")) in head:
            points += 2
        if inv.status == "open":
            points += 0.5
        if points > best_points:
            best, best_points = inv, points
    return best if best_points >= 3 else None


def detect(session: Session, doc: Document, text: str) -> dict | None:
    if not is_reminder(doc.title, text):
        return None
    found = extract(text)
    inv = find_invoice(session, doc, text, found["invoice_number"])
    return {"invoice": inv.document_id if inv else None, "due": found["due"].isoformat() if found["due"] else None,
            "fee": found["fee"], "date": doc.created.isoformat() if doc.created else None}


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def attached(session: Session) -> dict[int, int]:
    """Mahnungs-Dokument -> Rechnungs-Dokument (schon zugeordnet)."""
    result = {}
    for inv in session.scalars(select(Invoice).where(Invoice.reminders.is_not(None))):
        for r in inv.reminders or []:
            result[r["doc"]] = inv.document_id
    return result


def pending(session: Session) -> list[dict]:
    """Erkannte Mahnungen, die noch an keiner Rechnung hängen – auch ältere, nach dem Titel erkannt."""
    done = attached(session)
    items = []
    for doc in session.scalars(select(Document).order_by(Document.created.desc().nulls_last())):
        if doc.id in done:
            continue
        info = (doc.suggestions or {}).get("_reminder") if isinstance(doc.suggestions, dict) else None
        if info and info.get("dismissed"):
            continue
        if info is None:
            if doc.invoice is not None and doc.review_state == "confirmed" or not is_reminder(doc.title):
                continue
            inv = find_invoice(session, doc)
            info = {"invoice": inv.document_id if inv else None, "due": None, "fee": None,
                    "date": doc.created.isoformat() if doc.created else None, "by_title": True}
        target = session.get(Document, info["invoice"]) if info.get("invoice") else None
        items.append({"doc": doc, "invoice_doc": target if target is not None and target.invoice is not None else None,
                      "due": _date(info.get("due")), "fee": info.get("fee"), "date": _date(info.get("date")),
                      "by_title": info.get("by_title", False)})
    return items


def apply(session: Session, doc: Document, invoice_doc: Document, day: date | None, due: date | None, fee: float | None,
          take_due: bool = True, link: bool = False, settings=None, today: date | None = None) -> str:
    """Mahnung an die Rechnung hängen. Neue Frist auf Wunsch als Fälligkeit, „gehört zu“ auf Wunsch vormerken."""
    from app import pipeline

    inv = invoice_doc.invoice
    entries = [r for r in (inv.reminders or []) if r.get("doc") != doc.id]
    entries.append({"doc": doc.id, "date": (day or doc.created or date.today()).isoformat(),
                    "due": due.isoformat() if due else None, "fee": fee})
    inv.reminders = sorted(entries, key=lambda r: r["date"])
    notes = []
    if take_due and due and inv.status == "open" and (inv.due_date is None or due > inv.due_date):
        inv.due_date = due
        pipeline.set_source(invoice_doc, "due_date", "manual", confirmed=True)
        invoice_doc.writeback_pending = True
        notes.append(f"neue Frist {due:%d.%m.%Y}")
    if doc.review_state == "pending" and doc.kind == "invoice":
        doc.kind, doc.kind_source, doc.review_state = "other", "manual", "none"  # war fälschlich als Rechnung erkannt
        doc.invoice = None
    if link and settings is not None:
        current = set(pipeline.link_target(doc))
        if invoice_doc.id not in current:
            pipeline.propose(doc, "related", sorted(current | {invoice_doc.id}))
            notes.append("„gehört zu“ vorgemerkt")
    if inv.status == "paid":
        notes.append(f"Achtung: laut Companion schon bezahlt{f' am {inv.paid_on:%d.%m.%Y}' if inv.paid_on else ''} – bitte prüfen")
    return ", ".join(notes)


def dismiss(doc: Document) -> None:
    sugg = dict(doc.suggestions or {})
    sugg["_reminder"] = {**(sugg.get("_reminder") or {}), "dismissed": True}
    doc.suggestions = sugg
