"""Vorgänge: Zeitstrahl aus Dokumenten und Notizen."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import pipeline
from app.models import Case, CaseDocument, CaseEntry, Document
from app.paperless import PaperlessClient


@dataclass
class TimelineItem:
    date: date | None
    kind: str  # document | note
    document: Document | None = None
    entry: CaseEntry | None = None


def timeline(case: Case) -> list[TimelineItem]:
    items = [TimelineItem(link.document.created, "document", document=link.document) for link in case.links]
    items += [TimelineItem(entry.date, "note", entry=entry) for entry in case.entries]
    items.sort(key=lambda i: (i.date or date.min, 0 if i.kind == "document" else 1))
    return items


def ensure_document(session: Session, paperless: PaperlessClient, doc_id: int, settings=None) -> Document:
    """Dokument aus dem lokalen Index holen oder (ohne Analyse) aus Paperless übernehmen."""
    doc = session.get(Document, doc_id)
    if doc is None:
        names = pipeline.load_names(paperless, settings)
        doc = pipeline.upsert_document(session, paperless.get_document(doc_id), names, analyze=False)
        session.flush()
    return doc


def write_links(session: Session, paperless: PaperlessClient, settings, case: Case, doc: Document) -> None:
    """Verknüpft doc im Feld „gehört zu“ mit den übrigen Dokumenten des Vorgangs."""
    others = {link.document_id for link in case.links if link.document_id != doc.id}
    current = set((doc.paperless_fields or {}).get("related") or [])
    if others and not others <= current:
        pipeline.write_fields(paperless, doc, settings, {"related": sorted(current | others)})


def link_suggestions(session: Session, case: Case) -> list[Document]:
    """Dokumente, die über „gehört zu“ mit dem Vorgang verbunden, aber noch nicht enthalten sind."""
    in_case = {link.document_id for link in case.links}
    found: set[int] = set()
    for doc_id in in_case:
        found |= pipeline.related_ids(session, doc_id)
    return [d for d in (session.get(Document, i) for i in sorted(found - in_case)) if d is not None]


def add_document(session: Session, case: Case, doc: Document) -> bool:
    if session.get(CaseDocument, (case.id, doc.id)) is not None:
        return False
    session.add(CaseDocument(case_id=case.id, document_id=doc.id))
    return True


def search_documents(session: Session, query: str, limit: int = 20) -> list[Document]:
    pattern = f"%{query.strip()}%"
    return list(session.scalars(
        select(Document)
        .where(or_(Document.title.ilike(pattern), Document.correspondent.ilike(pattern)))
        .order_by(Document.created.desc().nulls_last())
        .limit(limit)
    ))


def cases_for_document(session: Session, doc_id: int) -> list[Case]:
    return list(session.scalars(select(Case).join(CaseDocument).where(CaseDocument.document_id == doc_id)))
