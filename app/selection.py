"""Dokumentauswahl für die Liste „Dokumente“ und das gezielte Analysieren."""

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Document
from app.pipeline import analysis_method


def parse_ids(text: str) -> list[int]:
    """„12, 15-18 20“ -> [12, 15, 16, 17, 18, 20] (Bereiche höchstens 5000 Einträge)."""
    ids: set[int] = set()
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        if m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", part):
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            ids.update(range(lo, min(hi, lo + 5000) + 1))
        elif part.isdigit():
            ids.add(int(part))
    return sorted(ids)


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@dataclass
class Filter:
    q: str = ""
    kind: str = ""
    state: str = ""
    method: str = ""
    tag: str = ""
    doctype: str = ""
    correspondent: str = ""
    date_from: str = ""
    date_to: str = ""
    ids: str = ""
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data) -> "Filter":
        return cls(**{k: str(data.get(k) or "").strip() for k in cls.__dataclass_fields__ if k != "errors"})

    @property
    def active(self) -> bool:
        return any(getattr(self, k) for k in self.__dataclass_fields__ if k != "errors")

    def query_string(self) -> str:
        from urllib.parse import urlencode

        return urlencode({k: getattr(self, k) for k in self.__dataclass_fields__ if k != "errors" and getattr(self, k)})

    def apply(self, session: Session) -> list[Document]:
        query = select(Document).order_by(Document.created.desc().nulls_last(), Document.id.desc())
        if self.q:
            pattern = f"%{self.q}%"
            query = query.where(or_(Document.title.ilike(pattern), Document.correspondent.ilike(pattern)))
        if self.kind:
            query = query.where(Document.kind == self.kind)
        if self.state:
            query = query.where(Document.extraction_state == self.state)
        if self.doctype:
            query = query.where(Document.document_type == self.doctype)
        if self.correspondent:
            query = query.where(Document.correspondent == self.correspondent)
        if d := _date(self.date_from):
            query = query.where(Document.created >= d)
        if d := _date(self.date_to):
            query = query.where(Document.created <= d)
        if self.ids:
            ids = parse_ids(self.ids)
            query = query.where(Document.id.in_(ids or [-1]))
        docs = list(session.scalars(query))
        if self.tag:
            wanted = self.tag.lower()
            docs = [d for d in docs if wanted in {t.lower() for t in d.tags or []}]
        if self.method:
            docs = [d for d in docs if analysis_method(d) == self.method]
        return docs


def facets(session: Session) -> dict[str, list[str]]:
    """Auswahllisten für die Filter (aus dem lokalen Archiv-Index)."""
    tags: set[str] = set()
    doctypes: set[str] = set()
    correspondents: set[str] = set()
    for tag_list, doctype, corr in session.execute(select(Document.tags, Document.document_type, Document.correspondent)):
        tags.update(tag_list or [])
        if doctype:
            doctypes.add(doctype)
        if corr:
            correspondents.add(corr)
    key = str.lower
    return {"tags": sorted(tags, key=key), "doctypes": sorted(doctypes, key=key), "correspondents": sorted(correspondents, key=key)}
