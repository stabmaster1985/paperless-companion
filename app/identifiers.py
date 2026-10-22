"""Textmerkmale eines Dokuments: eindeutige Kennzeichen des Absenders (ohne AI).

IBAN, USt-IdNr., Web-/E-Mail-Domain sowie Kunden-, Vertrags- und Mitgliedsnummern. Kommt ein Merkmal
(fast) nur bei Dokumenten eines Korrespondenten vor, eignet es sich als Paperless-Zuordnung, damit
neue Dokumente ihren Korrespondenten schon aus dem Text bekommen (siehe app/rules.py).

Die Merkmale werden beim Analysieren und im Hintergrund nach und nach für das ganze Archiv gelesen
(nur lesend, gedrosselt) und am Dokument gespeichert – der Text selbst wird nicht gespeichert.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.rules import IBAN_LENGTHS, IBAN_RE, valid_iban
from app.models import Document
from app.paperless import PaperlessClient

KINDS = {"iban": "IBAN", "vat": "USt-IdNr.", "domain": "Domain", "number": "Kunden-/Vertragsnr."}
VAT_RE = re.compile(r"\b(DE\s?\d{3}\s?\d{3}\s?\d{3}|AT\s?U\d{8}|ATU\d{8})\b")
DOMAIN_RE = re.compile(r"(?:https?://|www\.|@)([a-z0-9][a-z0-9\-]{1,62}(?:\.[a-z0-9\-]{2,62})*\.(?:de|com|eu|net|org|at|ch|info|biz))\b",
                       re.IGNORECASE)
NUMBER_RE = re.compile(
    r"(?:kunden|vertrags|mitglieds|versicherungs|konto|kassenzeichen|zähler|verbrauchsstellen)[\w\-\s]{0,12}?"
    r"(?:nummer|nr\.?|zeichen)\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/]{4,24})", re.IGNORECASE)
# Häufige Dienste, die in vielen fremden Dokumenten stehen (Zahlungsdienste, Mailanbieter)
COMMON_DOMAINS = {"paypal.com", "paypal.de", "gmail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "outlook.com",
                  "hotmail.com", "icloud.com", "googlemail.com", "yahoo.de", "yahoo.com", "google.com", "apple.com",
                  "facebook.com", "instagram.com", "twitter.com", "youtube.com", "linkedin.com", "xing.com"}


def extract(text: str) -> dict[str, list[str]]:
    """Kennzeichen aus dem OCR-Text (normalisiert, ohne Leerzeichen, Domains klein)."""
    upper = (text or "").upper()
    found: dict[str, set[str]] = {k: set() for k in KINDS}
    for m in IBAN_RE.finditer(upper):
        iban = m.group(1).replace(" ", "")
        expected = IBAN_LENGTHS.get(iban[:2])
        if expected and len(iban) > expected:
            iban = iban[:expected]
        if valid_iban(iban):
            found["iban"].add(iban)
    for m in VAT_RE.finditer(upper):
        found["vat"].add(m.group(1).replace(" ", ""))
    for m in DOMAIN_RE.finditer(text or ""):
        domain = m.group(1).lower()
        domain = domain[4:] if domain.startswith("www.") else domain
        if domain not in COMMON_DOMAINS:
            found["domain"].add(domain)
    for m in NUMBER_RE.finditer(text or ""):
        value = m.group(1).strip("-/")
        if re.search(r"\d", value) and len(re.sub(r"\D", "", value)) >= 4:
            found["number"].add(value.upper())
    return {k: sorted(v) for k, v in found.items() if v}


OWN_KEY = "own_identifiers"  # von dir als „eigen“ markiert
FIREFLY_OWN_KEY = "firefly_own_ibans"  # IBANs deiner Firefly-Bestandskonten


def own_terms(session: Session) -> set[str]:
    """Eigene Merkmale (deine IBANs, Domains …), die nie einem Absender zugeordnet werden."""
    import json

    from app.models import KeyValue

    terms: set[str] = set()
    for key in (OWN_KEY, FIREFLY_OWN_KEY):
        item = session.get(KeyValue, key)
        if item:
            terms.update(json.loads(item.value))
    return terms


def set_own_terms(session: Session, key: str, terms: set[str] | list[str]) -> None:
    import json

    from app.models import KeyValue

    value = json.dumps(sorted(set(terms)))
    item = session.get(KeyValue, key)
    if item:
        item.value = value
    else:
        session.add(KeyValue(key=key, value=value))


def store(doc: Document, text: str) -> None:
    doc.identifiers = extract(text)
    doc.identifiers_at = datetime.now(timezone.utc).replace(tzinfo=None)


def pending(session: Session, limit: int) -> list[Document]:
    """Dokumente ohne gelesene Textmerkmale – zuerst die mit Korrespondent (dafür werden Regeln gebraucht)."""
    query = select(Document).where(Document.identifiers_at.is_(None))
    with_corr = list(session.scalars(query.where(Document.correspondent.is_not(None)).order_by(Document.id).limit(limit)))
    if len(with_corr) >= limit:
        return with_corr
    return with_corr + list(session.scalars(query.where(Document.correspondent.is_(None)).order_by(Document.id)
                                            .limit(limit - len(with_corr))))


def scan(session: Session, paperless: PaperlessClient, limit: int = 20) -> int:
    """Einen Schwung Dokumente lesen (nur GET in Paperless). Gibt die Anzahl zurück."""
    count = 0
    for doc in pending(session, limit):
        store(doc, paperless.get_document(doc.id).get("content") or "")
        count += 1
    return count


def progress(session: Session) -> dict[str, int]:
    from sqlalchemy import func

    total = session.scalar(select(func.count()).select_from(Document)) or 0
    done = session.scalar(select(func.count()).select_from(Document).where(Document.identifiers_at.is_not(None))) or 0
    return {"total": total, "done": done}


def regex_for(kind: str, value: str) -> str:
    """Muster, das das Merkmal im Text findet – IBAN auch mit Leerzeichen geschrieben."""
    if kind == "iban":
        return r"\s?".join(re.escape(value[i:i + 4]) for i in range(0, len(value), 4))
    if kind == "vat":
        return r"\s?".join(re.escape(c) for c in value)
    return re.escape(value)


def term_label(term: str) -> str:
    kind, _, value = term.partition(":")
    return f"{KINDS.get(kind, kind)} {value}"
