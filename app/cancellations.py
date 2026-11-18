"""Kündigungsbestätigungen: erkennen, dem passenden Vertrag zuordnen und ihn (auf deinen Klick) als gekündigt markieren.

Eine Kündigungsbestätigung ist kein neuer Vertrag – sie wird als „Sonstiges“ eingeordnet und merkt sich den Vertrag,
zu dem sie gehört, das Datum der Bestätigung und – falls im Text – das Vertragsende („zum 31.12.2026“).
Nichts davon geht automatisch nach Paperless.
"""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.rules import date_after_label, extract_number
from app.models import Contract, Document

CANCEL_RE = re.compile(
    r"kündigungsbestätigung|kuendigungsbestaetigung|bestätigung\s+(?:ihrer|der)\s+kündigung"
    r"|(?:bestätigen|bestaetigen)\s+(?:wir\s+)?(?:ihnen\s+)?(?:hiermit\s+)?(?:den\s+eingang\s+)?ihre[rn]?\s+kündigung"
    r"|ihre\s+kündigung\s+(?:vom|haben\s+wir|ist\s+bei\s+uns)|kündigung\s+(?:wurde\s+)?bestätigt"
    r"|vertragsbeendigung|beendigung\s+ihres\s+vertrag",
    re.IGNORECASE,
)
EFFECTIVE_LABEL = (r"(?:kündigung\s+)?(?:wirksam\s+)?zum|endet\s+(?:am|mit\s+ablauf\s+des|zum)|beendet\s+zum"
                   r"|vertragsende|läuft\s+(?:noch\s+)?bis|mit\s+wirkung\s+zum")


def is_cancellation(title: str | None, text: str = "") -> bool:
    return bool(CANCEL_RE.search(title or "") or CANCEL_RE.search((text or "")[:1500]))


def _compact(value: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def _words(value: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]+", (value or "").lower()) if len(w) >= 3}


def extract(text: str) -> dict:
    """Vertragsende und Vertrags-/Kundennummer aus dem Text der Bestätigung."""
    effective = date_after_label(text or "", EFFECTIVE_LABEL, 40)
    number = extract_number(text or "", r"(?:vertrags|kunden|versicherungsschein|police|mitglieds|konto)[\s-]*(?:nr|nummer|no)")
    return {"effective": effective, "contract_number": number}


def find_contract(session: Session, doc: Document, text: str = "", number: str | None = None) -> Contract | None:
    """Passender Vertrag: Vertragsnummer im Text, sonst gleicher Anbieter/Korrespondent. Bei mehreren Treffern der,
    der am besten passt; ohne klaren Hinweis keiner."""
    head = (text or "")[:2000]
    best, best_points = None, 0
    for c in session.scalars(select(Contract).where(Contract.document_id != doc.id)):
        points = 0
        cnum = _compact(c.contract_number)
        if cnum and len(cnum) >= 4 and (cnum == _compact(number) or cnum in _compact(head)):
            points += 3
        party = c.provider or (c.document.correspondent if c.document else None)
        if party and doc.correspondent and party.lower() == doc.correspondent.lower():
            points += 2
        elif party and _words(party) & (_words(doc.correspondent) | _words(doc.title) | _words(head[:400])):
            points += 1
        if c.name and _words(c.name) & _words(doc.title):
            points += 1
        if c.status == "active":
            points += 0.5  # schon gekündigte Verträge nur, wenn sonst nichts passt
        if points > best_points:
            best, best_points = c, points
    return best if best_points >= 2 else None


def detect(session: Session, doc: Document, text: str) -> dict | None:
    """Bei der Analyse: ist das eine Kündigungsbestätigung? Dann Angaben für die Vorschläge zurückgeben."""
    if not is_cancellation(doc.title, text):
        return None
    found = extract(text)
    contract = find_contract(session, doc, text, found["contract_number"])
    return {"contract": contract.id if contract else None,
            "effective": found["effective"].isoformat() if found["effective"] else None,
            "date": doc.created.isoformat() if doc.created else None}


def pending(session: Session) -> list[dict]:
    """Erkannte Bestätigungen, die noch nicht an ihrem Vertrag hängen (zum Übernehmen auf der Vertragsseite).
    Dazu Dokumente, deren Titel nach einer Bestätigung klingt, die aber noch nicht neu analysiert wurden."""
    items = []
    linked = {c.cancellation_doc_id for c in session.scalars(select(Contract)) if c.cancellation_doc_id}
    for doc in session.scalars(select(Document).order_by(Document.created.desc().nulls_last())):
        if doc.id in linked:
            continue
        info = (doc.suggestions or {}).get("_cancellation") if isinstance(doc.suggestions, dict) else None
        if info and info.get("dismissed"):
            continue
        if info is None:
            if doc.contract is not None and doc.review_state == "confirmed" or not is_cancellation(doc.title):
                continue
            contract = find_contract(session, doc)
            info = {"contract": contract.id if contract else None, "effective": None,
                    "date": doc.created.isoformat() if doc.created else None, "by_title": True}
        contract = session.get(Contract, info["contract"]) if info.get("contract") else None
        items.append({"doc": doc, "contract": contract, "effective": _date(info.get("effective")),
                      "date": _date(info.get("date")), "by_title": info.get("by_title", False)})
    return items


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def apply(session: Session, doc: Document, contract: Contract, cancelled_on: date | None, effective: date | None) -> None:
    """Vertrag als gekündigt markieren und die Bestätigung daran hängen (nur im Companion)."""
    from app import pipeline

    contract.status = "cancelled"
    contract.cancelled_on = cancelled_on or doc.created or date.today()
    contract.cancel_effective = effective
    contract.cancellation_doc_id = doc.id
    pipeline.touch_contract(contract, ["status", "cancelled_on"])
    if doc.kind == "contract" and doc.review_state == "pending":
        doc.kind, doc.kind_source, doc.review_state = "other", "manual", "none"  # war fälschlich als Vertrag erkannt
        doc.contract = None


def dismiss(doc: Document) -> None:
    sugg = dict(doc.suggestions or {})
    sugg["_cancellation"] = {**(sugg.get("_cancellation") or {}), "dismissed": True}
    doc.suggestions = sugg
