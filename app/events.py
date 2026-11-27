"""Termine aus Verträgen, Rechnungen und Garantien – Basis für Dashboard, Erinnerungen und ICS."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import deadlines
from app.models import Case, Contract, Invoice, Notice, Warranty

LABELS = {
    "cancel": "Kündigungsfrist",
    "term_end": "Vertragsende",
    "due": "Rechnung fällig",
    "overdue": "Rechnung überfällig",
    "warranty": "Garantie endet",
    "follow_up": "Wiedervorlage",
    "notice_due": "Nachzahlung fällig",
    "prepayment": "Vorauszahlung fällig",
}


@dataclass
class Event:
    date: date
    type: str
    title: str
    detail: str
    document_id: int | None
    key: str
    amount: float | None = None
    case_id: int | None = None
    contract_id: int | None = None  # Abo ohne Dokument

    @property
    def url(self) -> str:
        if self.case_id:
            return f"/cases/{self.case_id}"
        return f"/documents/{self.document_id}" if self.document_id else f"/contracts/{self.contract_id}"

    @property
    def label(self) -> str:
        return LABELS[self.type]


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "–"


def fmt_money(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def collect(session: Session, today: date) -> list[Event]:
    events: list[Event] = []

    contracts = session.scalars(select(Contract).where(Contract.status == "active").options(selectinload(Contract.document)))
    for c in contracts:
        if c.is_notice:
            continue  # Zahlungspflicht aus Bescheiden: keine Kündigung, Termine kommen aus dem Bescheid (unten)
        result = deadlines.compute(deadlines.terms_from_contract(c), today)
        if result.cancel_by and not result.anytime:
            events.append(Event(
                date=result.cancel_by, type="cancel", title=c.label,
                detail=f"Kündigung muss bis {fmt_date(result.cancel_by)} eingehen, sonst Verlängerung "
                       f"(aktuelles Laufzeitende {fmt_date(result.term_end)}).",
                document_id=c.document_id, key=f"contract:{c.id}:cancel", contract_id=c.id,
            ))
        elif c.renewal_mode == "none" and result.term_end and not result.ended:
            events.append(Event(
                date=result.term_end, type="term_end", title=c.label,
                detail="Vertrag endet automatisch.", document_id=c.document_id, key=f"contract:{c.id}:end", contract_id=c.id,
            ))

    invoices = session.scalars(
        select(Invoice).where(Invoice.status == "open", Invoice.payment_method != "debit", Invoice.due_date.is_not(None))
    )
    for inv in invoices:
        overdue = inv.due_date < today
        events.append(Event(
            date=inv.due_date, type="overdue" if overdue else "due",
            title=inv.issuer or (inv.document.title if inv.document else "Rechnung"),
            detail=f"{fmt_money(inv.open_amount)}" + (" (Rest nach Teilzahlungen)" if inv.paid_amount else "")
            + (f", Rechnungsnr. {inv.invoice_number}" if inv.invoice_number else ""),
            document_id=inv.document_id, key=f"invoice:{inv.id}", amount=inv.open_amount,
        ))

    from app import notices  # lokal, um Importzyklen zu vermeiden

    latest: dict[int, Notice] = {}
    for n in session.scalars(select(Notice).options(selectinload(Notice.document))):
        title = n.authority or (n.document.title if n.document else "Bescheid")
        if n.balance_kind == "payment" and n.balance_amount and n.balance_due and n.balance_due >= today:
            events.append(Event(
                date=n.balance_due, type="notice_due", title=title,
                detail=f"Nachzahlung {fmt_money(n.balance_amount)}" + (f" ({n.period})" if n.period else ""),
                document_id=n.document_id, key=f"notice:{n.id}:due", amount=n.balance_amount,
            ))
        if n.contract_id and n.prepayment_amount:
            current = latest.get(n.contract_id)
            if current is None or (n.prepayment_from or n.notice_date or date.min) > (current.prepayment_from or current.notice_date or date.min):
                latest[n.contract_id] = n
    for contract_id, n in latest.items():
        c = session.get(Contract, contract_id)
        if c is None or c.status != "active":
            continue
        for day in notices.next_dates(n, today, count=1):
            events.append(Event(
                date=day, type="prepayment", title=c.label,
                detail=f"Vorauszahlung {fmt_money(n.prepayment_amount)} laut Bescheid vom {fmt_date(n.notice_date)}",
                document_id=None, key=f"notice:{n.id}:prepay:{day.isoformat()}", amount=n.prepayment_amount,
                contract_id=c.id,
            ))

    for w in session.scalars(select(Warranty)):
        expires = w.expires_on
        if expires and expires >= today:
            kind = "Garantie" if w.warranty_type == "garantie" else "Gewährleistung"
            events.append(Event(
                date=expires, type="warranty", title=w.product,
                detail=f"{kind} ({w.months} Monate) endet am {fmt_date(expires)}.",
                document_id=w.document_id, key=f"warranty:{w.id}",
            ))

    for case in session.scalars(select(Case).where(Case.status == "open", Case.follow_up.is_not(None))):
        events.append(Event(
            date=case.follow_up, type="follow_up", title=case.title,
            detail=case.follow_up_note or "Vorgang wieder vorlegen.",
            document_id=None, key=f"case:{case.id}:follow_up", case_id=case.id,
        ))

    events.sort(key=lambda e: e.date)
    return events


def monthly_fixed_costs(session: Session, today: date | None = None) -> float:
    """Monatliche Fixkosten: aktive Verträge und gekündigte, die laut Bestätigung noch laufen."""
    today = today or date.today()
    total = 0.0
    for c in session.scalars(select(Contract).where(Contract.status.in_(("active", "cancelled")))):
        if c.monthly_cost and (c.status == "active" or (c.cancel_effective and c.cancel_effective >= today)):
            total += c.monthly_cost
    return round(total, 2)
