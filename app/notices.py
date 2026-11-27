"""Bescheide (Steuer, Grundsteuer, Gebühren, Beiträge …) und die Zahlungspflicht dahinter.

Ein Bescheid setzt eine Nachzahlung oder Erstattung fest und oft Vorauszahlungen (z.B. vierteljährlich). Die
Vorauszahlungen sind eine wiederkehrende Zahlungspflicht – im Companion ein Eintrag bei den Verträgen mit der Art
„Bescheid“ (Contract.category = "notice"): ohne Kündigungsfrist, mit erkannten Abbuchungen wie ein Abo. Jeder neue
Bescheid hängt an dieser Zahlungspflicht; der neueste bestimmt Betrag und Rhythmus.

Beim Umstellen (Abo → Bescheid, Rechnung/Vertrag/Sonstiges → Bescheid) bleiben alle Verknüpfungen erhalten:
dieselbe Zahlungspflicht (Abbuchungen, gemerkte Suche), zugeordnete Buchungen als Belege.
"""

from datetime import date

from dateutil.relativedelta import relativedelta
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Contract, Document, Notice, PaymentMatch, ReceiptCheck

INTERVALS = {1: "monatlich", 3: "vierteljährlich", 6: "halbjährlich", 12: "jährlich"}


def notices_of(session: Session, contract_id: int) -> list[Notice]:
    """Bescheide einer Zahlungspflicht, neuester zuerst."""
    items = list(session.scalars(select(Notice).where(Notice.contract_id == contract_id)))
    return sorted(items, key=lambda n: (n.prepayment_from or n.notice_date or date.min, n.id), reverse=True)


def sync_contract(session: Session, notice: Notice) -> str | None:
    """Zahlungspflicht zum Bescheid aktuell halten: Art „Bescheid“, Vorauszahlung aus dem neuesten Bescheid."""
    if not notice.contract_id:
        return None
    contract = session.get(Contract, notice.contract_id)
    if contract is None:
        return None
    contract.category = "notice"
    latest = next((n for n in notices_of(session, contract.id) if n.prepayment_amount), None)
    if latest is not None and latest.id == notice.id:
        contract.set_payment(notice.prepayment_amount, notice.prepayment_interval or 3)
        return (f"Vorauszahlung bei „{contract.label}“ auf {notice.prepayment_amount:.2f} € "
                f"{INTERVALS.get(contract.payment_interval, '')} gesetzt").replace(".", ",", 1)
    return None


def new_obligation(session: Session, name: str, payee: str | None = None) -> Contract:
    """Neue Zahlungspflicht (Art Bescheid) ohne Dokument."""
    contract = Contract(document_id=None, name=name, provider=name, payee=payee, renewal_mode="indefinite", notice_value=None,
                        notice_unit="months", notice_to="any", status="active", category="notice")
    session.add(contract)
    session.flush()
    return contract


def next_dates(notice: Notice, today: date, count: int = 4) -> list[date]:
    """Nächste Vorauszahlungstermine laut Bescheid (ab „gilt ab“ im Rhythmus)."""
    if not notice.prepayment_amount or not notice.prepayment_from:
        return []
    step = notice.prepayment_interval or 3
    day, out = notice.prepayment_from, []
    while day < today:
        day = day + relativedelta(months=step)
    while len(out) < count:
        out.append(day)
        day = day + relativedelta(months=step)
    return out


def _link_booking(session: Session, doc_id: int, m: PaymentMatch) -> None:
    """Zugeordnete Buchung als Beleg festhalten (überlebt das Entfernen der Rechnung)."""
    row = session.scalar(select(ReceiptCheck).where(ReceiptCheck.tx_id == m.tx_id))
    if row is None:
        row = ReceiptCheck(tx_id=m.tx_id, tx_group_id=m.tx_group_id or "", tx_date=m.tx_date, amount=m.amount,
                           counterparty=m.counterparty or "", description=m.description or "")
        session.add(row)
    row.state, row.document_id, row.decided, row.note = "linked", doc_id, True, "von dir zugeordnet"


def release_records(session: Session, doc: Document, new_kind: str) -> list[str]:
    """Vor einem Artwechsel: Verknüpfungen retten, die sonst mit dem alten Datensatz verschwinden würden.

    - Rechnung: übernommene Zahlungen bleiben als Beleg an den Buchungen.
    - Vertrag (z.B. ein Abo, an das das Dokument gehängt wurde): bleibt als Abo bzw. Zahlungspflicht bestehen –
      mit seinen Abbuchungen und der gemerkten Suche. Wird das Dokument ein Bescheid, hängt es an dieser Zahlungspflicht.
    Gibt Hinweise für die Meldung zurück."""
    notes: list[str] = []
    if doc.invoice is not None and new_kind != "invoice":
        matches = [m for m in session.scalars(select(PaymentMatch).where(PaymentMatch.invoice_id == doc.invoice.id))
                   if m.state == "accepted"]
        for m in matches:
            _link_booking(session, doc.id, m)
        if matches:
            notes.append(f"{len(matches)} zugeordnete Buchung{'en bleiben' if len(matches) != 1 else ' bleibt'} als Beleg verknüpft")
    contract = doc.contract
    if contract is not None and new_kind != "contract":
        cid = contract.id
        if not contract.payee:
            contract.payee = contract.provider or contract.name or None
        session.flush()
        # vom Dokument lösen, ohne dass der Vertrag mit dem Dokument gelöscht wird
        session.execute(update(Contract).where(Contract.id == cid).values(document_id=None))
        session.expire(doc, ["contract"])
        session.expire(contract)
        contract = session.get(Contract, cid)
        if new_kind == "notice":
            contract.category = "notice"
            notice = doc.notice or Notice(document_id=doc.id, authority=doc.correspondent, notice_date=doc.created)
            notice.contract_id = cid
            doc.notice = notice
            notes.append(f"„{contract.label}“ ist jetzt eine Zahlungspflicht (Bescheid) – Abbuchungen bleiben erhalten")
        else:
            notes.append(f"„{contract.label}“ bleibt als Abo bestehen – mit seinen Abbuchungen")
    return notes


def convert_contract(session: Session, contract: Contract, to_notice: bool) -> None:
    """Abo/Vertrag ↔ Zahlungspflicht aus Bescheiden. Alle Verknüpfungen hängen an der Zahlungspflicht und bleiben."""
    contract.category = "notice" if to_notice else ""


def attach(session: Session, contract: Contract, doc: Document) -> list[str]:
    """Dokument als Bescheid an eine Zahlungspflicht hängen (Art wird „Bescheid“, Verknüpfungen bleiben)."""
    notes = release_records(session, doc, "notice") if doc.kind != "notice" else []
    notice = doc.notice or Notice(document_id=doc.id, authority=doc.correspondent, notice_date=doc.created)
    notice.contract_id = contract.id
    doc.notice = notice
    if doc.kind != "notice":
        doc.kind, doc.kind_source = "notice", "manual"
        doc.invoice = None
        from app import pipeline

        pipeline.queue_now(doc)  # Angaben des Bescheids erkennen lassen, danach in der Inbox prüfen
    contract.category = "notice"
    session.flush()
    return notes
