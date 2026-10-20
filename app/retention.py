"""Aufbewahrungsfristen: welche Dokumente dürfen weg, welche bald?"""

from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import deadlines
from app.models import Document, RetentionRule

BASIS_LABELS = {
    "year_end": "ab Ende des Kalenderjahres",
    "created": "ab Dokumentdatum",
    "contract_end": "ab Vertragsende",
}
MATCH_LABELS = {"doctype": "Dokumenttyp", "tag": "Tag", "kind": "Art"}

# Beispielregeln für Privathaushalte – keine Rechtsberatung, in der Oberfläche anpassbar.
DEFAULT_RULES = [
    ("Rechnungen (Verjährung 3 Jahre)", "doctype", "Rechnung", 3, "year_end"),
    ("Kassenbons", "doctype", "Kassenbon", 3, "year_end"),
    ("Verträge: 3 Jahre nach Vertragsende", "kind", "contract", 3, "contract_end"),
    ("Steuerrelevante Belege", "tag", "Steuer", 10, "year_end"),
]


@dataclass
class Status:
    doc: Document
    rule: RetentionRule | None
    keep_until: date | None  # letzter Tag der Aufbewahrung; None = dauerhaft/unbekannt
    blocked: str | None = None  # Grund, warum nicht gelöscht werden darf

    @property
    def deletable_from(self) -> date | None:
        return self.keep_until + relativedelta(days=1) if self.keep_until else None


def seed_defaults(session: Session) -> None:
    if session.scalar(select(RetentionRule.id).limit(1)) is None:
        for name, match_type, value, years, basis in DEFAULT_RULES:
            session.add(RetentionRule(name=name, match_type=match_type, match_value=value, years=years, basis=basis))


def matches(rule: RetentionRule, doc: Document) -> bool:
    value = rule.match_value.strip().lower()
    if rule.match_type == "doctype":
        return (doc.document_type or "").lower() == value
    if rule.match_type == "tag":
        return value in {t.lower() for t in doc.tags or []}
    if rule.match_type == "kind":
        return doc.kind == value
    return False


def keep_until(rule: RetentionRule, doc: Document, today: date) -> tuple[date | None, str | None]:
    """Ende der Aufbewahrung nach einer Regel. (None, Grund) wenn nicht bestimmbar."""
    if rule.years == 0:
        return None, "dauerhaft aufbewahren"
    if rule.basis == "contract_end":
        contract = doc.contract
        if contract is None:
            return None, "kein Vertrag erfasst"
        if contract.status == "active":
            return None, "Vertrag läuft noch"
        end = contract.cancelled_on and deadlines.compute(deadlines.terms_from_contract(contract), contract.cancelled_on).term_end
        end = end or contract.end_date or contract.cancelled_on
        if end is None:
            return None, "Vertragsende unbekannt"
        return date(end.year + rule.years, 12, 31), None
    if doc.created is None:
        return None, "Dokumentdatum unbekannt"
    if rule.basis == "created":
        return doc.created + relativedelta(years=rule.years), None
    return date(doc.created.year + rule.years, 12, 31), None


def evaluate_doc(doc: Document, rules: list[RetentionRule], today: date) -> Status:
    matching = [r for r in rules if matches(r, doc)]
    if not matching:
        return Status(doc, None, None, "keine Regel")
    best: Status | None = None
    for rule in matching:
        until, reason = keep_until(rule, doc, today)
        if until is None:
            # Eine Regel ohne Enddatum (dauerhaft, Vertrag läuft …) schützt das Dokument
            return Status(doc, rule, None, reason)
        if best is None or until > best.keep_until:
            best = Status(doc, rule, until)
    assert best is not None
    if doc.retention_hold:
        best.blocked = "manuell behalten"
    elif doc.invoice and doc.invoice.status == "open":
        best.blocked = "Rechnung offen"
    elif doc.warranty and doc.warranty.expires_on and doc.warranty.expires_on >= today:
        best.blocked = "Garantie läuft noch"
    elif doc.contract and doc.contract.status == "active":
        best.blocked = "Vertrag läuft noch"
    return best


def evaluate(session: Session, today: date) -> list[Status]:
    rules = list(session.scalars(select(RetentionRule)))
    docs = session.scalars(
        select(Document).options(
            selectinload(Document.contract), selectinload(Document.invoice), selectinload(Document.warranty)
        )
    )
    return [evaluate_doc(doc, rules, today) for doc in docs]


def overview(session: Session, today: date, horizon_days: int = 365) -> dict[str, list[Status]]:
    due, soon, protected = [], [], []
    for status in evaluate(session, today):
        if status.keep_until is None:
            continue
        if status.blocked:
            protected.append(status)
        elif status.keep_until < today:
            due.append(status)
        elif (status.keep_until - today).days <= horizon_days:
            soon.append(status)
    due.sort(key=lambda s: s.keep_until)
    soon.sort(key=lambda s: s.keep_until)
    return {"due": due, "soon": soon, "protected": protected}
