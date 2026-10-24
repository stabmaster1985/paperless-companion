"""Steuer-Export: steuerrelevante Belege eines Jahres als ZIP mit CSV-Übersicht."""

import csv
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Document
from app.paperless import PaperlessClient, PaperlessError

SEPARATORS = (":", "/", " - ", "-")


@dataclass
class TaxItem:
    doc: Document
    category: str
    amount: float | None
    refund: str = ""  # zugehörige Erstattung, z.B. „#30 Leistungsabrechnung: 420,00 € von 612,40 € erstattet, Eigenanteil 192,40 €“


def category_for(doc: Document, settings: Settings) -> str | None:
    """Kategorie, wenn das Dokument steuerrelevant ist, sonst None."""
    bases = settings.tax_tag_list
    fallback = doc.document_type or "Sonstiges"
    relevant = False
    for tag in doc.tags or []:
        low = tag.lower()
        if low in bases:
            relevant = True
            continue
        for base in bases:
            for sep in SEPARATORS:
                if low.startswith(base + sep):
                    suffix = tag[len(base) + len(sep):].strip()
                    if suffix:
                        return suffix
    if relevant:
        return fallback
    if doc.document_type and doc.document_type.lower() in settings.tax_doctype_list:
        return doc.document_type
    return None


def effective_year(doc: Document) -> int | None:
    if doc.tax_year is not None:
        return doc.tax_year or None
    return doc.created.year if doc.created else None


def _candidates(session: Session):
    return session.scalars(select(Document).options(selectinload(Document.invoice)))


def refund_notes(session: Session) -> dict[int, str]:
    """Je Rechnung die Erstattung(en), mit denen sie in Paperless verknüpft ist – Eigenanteil je Abrechnung."""
    from app import reimbursements

    def eur(value):
        return "?" if value is None else f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

    notes: dict[int, list[str]] = {}
    graph = reimbursements.related_graph(session)
    for refund_doc in session.scalars(select(Document).where(Document.kind == "reimbursement")):
        item = reimbursements.summary(session, refund_doc, graph)
        text = (f"#{item.document.id} {item.document.title}: {eur(item.reimbursed)} von {eur(item.invoice_sum)} erstattet"
                + (f", Eigenanteil {eur(item.own_share)}" if item.own_share is not None else ""))
        for inv, _ in item.invoices:
            notes.setdefault(inv.id, []).append(text)
    return {doc_id: "; ".join(texts) for doc_id, texts in notes.items()}


def collect(session: Session, year: int, settings: Settings) -> list[TaxItem]:
    items = []
    refunds = refund_notes(session)
    for doc in _candidates(session):
        if effective_year(doc) != year:
            continue
        category = category_for(doc, settings)
        if category:
            items.append(TaxItem(doc, category, doc.invoice.amount if doc.invoice else None, refunds.get(doc.id, "")))
    items.sort(key=lambda i: (i.category.lower(), i.doc.created or i.doc.id))
    return items


def excluded(session: Session, settings: Settings) -> list[Document]:
    return [d for d in _candidates(session) if d.tax_year == 0 and category_for(d, settings)]


def available_years(session: Session, settings: Settings) -> list[int]:
    years = {effective_year(d) for d in _candidates(session) if category_for(d, settings)}
    return sorted((y for y in years if y), reverse=True)


def grouped(items: list[TaxItem]) -> dict[str, dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"items": [], "sum": 0.0, "with_amount": 0})
    for item in items:
        group = groups[item.category]
        group["items"].append(item)
        if item.amount is not None:
            group["sum"] = round(group["sum"] + item.amount, 2)
            group["with_amount"] += 1
    return dict(groups)


def safe_name(text: str, limit: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" ._")
    return text[:limit] or "Dokument"


def file_name(item: TaxItem, original: str) -> str:
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else "pdf"
    date_part = item.doc.created.isoformat() if item.doc.created else "ohne-Datum"
    parts = [date_part, item.doc.correspondent or "", item.doc.title or f"Dokument {item.doc.id}"]
    return safe_name("_".join(p for p in parts if p), 120) + f"_{item.doc.id}.{ext}"


def build_zip(items: list[TaxItem], year: int, paperless: PaperlessClient, target: IO[bytes]) -> list[str]:
    """Schreibt das ZIP in target. Gibt Fehlermeldungen für nicht ladbare Dokumente zurück."""
    errors: list[str] = []
    rows = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            try:
                content, original = paperless.download(item.doc.id)
            except PaperlessError as exc:
                errors.append(f"#{item.doc.id} {item.doc.title}: {exc}")
                path = ""
            else:
                path = f"{safe_name(item.category)}/{file_name(item, original)}"
                zf.writestr(path, content)
            rows.append([
                item.category,
                item.doc.created.strftime("%d.%m.%Y") if item.doc.created else "",
                item.doc.correspondent or "",
                item.doc.title,
                item.doc.document_type or "",
                f"{item.amount:.2f}".replace(".", ",") if item.amount is not None else "",
                item.doc.id,
                path,
                item.refund,
            ])
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["Kategorie", "Datum", "Korrespondent", "Titel", "Dokumenttyp", "Betrag (EUR)", "Paperless-ID", "Datei",
                         "Erstattung"])
        writer.writerows(rows)
        # BOM, damit Excel Umlaute korrekt erkennt
        zf.writestr(f"Übersicht_{year}.csv", "﻿" + buffer.getvalue())
        if errors:
            zf.writestr("FEHLER.txt", "Diese Dokumente konnten nicht geladen werden:\n" + "\n".join(errors))
    return errors
