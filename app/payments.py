"""Abgleich offener Rechnungen mit Abbuchungen aus Firefly III."""

import logging
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.firefly import FireflyClient, Transaction
from app.models import Invoice, PaymentMatch

log = logging.getLogger(__name__)

AMOUNT_ONLY = 50
SUGGEST_MIN = 60  # Betrag + mindestens ein weiteres Indiz
AUTO_MIN = 90  # Betrag + Rechnungsnummer oder IBAN
STOPWORDS = {"gmbh", "mbh", "und", "der", "die", "das", "deutschland", "germany", "service", "services", "online"}


def _compact(text: str) -> str:
    return re.sub(r"[\s\-_/.]", "", text).lower()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]{4,}", text.lower()) if w not in STOPWORDS}


def window(inv: Invoice) -> tuple[date, date] | None:
    base = inv.invoice_date or (inv.document.created if inv.document else None) or inv.due_date
    if base is None:
        return None
    start = base - timedelta(days=7)
    end = max(inv.due_date or base, base) + timedelta(days=60)
    return start, end


def score(inv: Invoice, tx: Transaction) -> tuple[int, list[str]] | None:
    if inv.amount is None or abs(tx.amount - inv.amount) > 0.005:
        return None
    span = window(inv)
    if span is None or not span[0] <= tx.date <= span[1]:
        return None
    points, reasons = AMOUNT_ONLY, ["Betrag"]
    if inv.invoice_number and len(inv.invoice_number) >= 3 and _compact(inv.invoice_number) in _compact(tx.text):
        points += 40
        reasons.append("Rechnungsnr.")
    if inv.iban and tx.iban and inv.iban.replace(" ", "").upper() == tx.iban:
        points += 30
        reasons.append("IBAN")
    issuer = inv.issuer or (inv.document.correspondent if inv.document else "") or ""
    if _words(issuer) & _words(f"{tx.counterparty} {tx.description}"):
        points += 20
        reasons.append("Empfänger")
    if inv.due_date and tx.date <= inv.due_date:
        points += 5
    return points, reasons


def match(session: Session, transactions: list[Transaction], settings: Settings) -> dict[str, int]:
    """Erzeugt Vorschläge (bzw. Übernahmen) für offene Rechnungen. Gibt Zähler zurück."""
    invoices = list(session.scalars(
        select(Invoice).where(Invoice.status == "open", Invoice.amount.is_not(None)).options(selectinload(Invoice.document))
    ))
    existing = list(session.scalars(select(PaymentMatch)))
    used_tx = {m.tx_id for m in existing if m.state in ("accepted", "suggested")}
    known_pairs = {(m.invoice_id, m.tx_id) for m in existing}
    has_suggestion = {m.invoice_id for m in existing if m.state == "suggested"}

    candidates: list[tuple[int, Invoice, Transaction, list[str]]] = []
    for inv in invoices:
        if inv.id in has_suggestion:
            continue
        scored = []
        for tx in transactions:
            if tx.journal_id in used_tx or (inv.id, tx.journal_id) in known_pairs:
                continue
            if (result := score(inv, tx)) is not None:
                scored.append((result[0], inv, tx, result[1]))
        if not scored:
            continue
        scored.sort(key=lambda c: -c[0])
        best = scored[0]
        # Nur der Betrag passt: lediglich vorschlagen, wenn es genau eine solche Buchung gibt
        if best[0] < SUGGEST_MIN and len(scored) > 1:
            continue
        candidates.append(best)

    counts = {"suggested": 0, "accepted": 0}
    for points, inv, tx, reasons in sorted(candidates, key=lambda c: -c[0]):
        if tx.journal_id in used_tx:
            continue
        used_tx.add(tx.journal_id)
        auto = settings.firefly_auto_accept and points >= AUTO_MIN
        session.add(PaymentMatch(
            invoice_id=inv.id, tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=tx.amount,
            counterparty=tx.counterparty[:200], description=tx.description[:300], score=points,
            reasons=", ".join(reasons), state="accepted" if auto else "suggested",
        ))
        if auto:
            inv.status, inv.paid_on = "paid", tx.date
            inv.document.writeback_pending = True
            counts["accepted"] += 1
        else:
            counts["suggested"] += 1
    return counts


def run(session: Session, client: FireflyClient, settings: Settings, today: date) -> dict[str, int]:
    open_dates = [
        d for inv in session.scalars(select(Invoice).where(Invoice.status == "open").options(selectinload(Invoice.document)))
        if (span := window(inv)) and (d := span[0])
    ]
    if not open_dates:
        return {"suggested": 0, "accepted": 0}
    start = max(min(open_dates), today - timedelta(days=settings.firefly_lookback_days))
    transactions = list(client.withdrawals(start, today))
    return match(session, transactions, settings)


def accept(session: Session, match_id: int) -> PaymentMatch | None:
    m = session.get(PaymentMatch, match_id)
    if m is None:
        return None
    m.state = "accepted"
    m.invoice.status, m.invoice.paid_on = "paid", m.tx_date
    m.invoice.document.writeback_pending = True
    return m


def reject(session: Session, match_id: int) -> PaymentMatch | None:
    m = session.get(PaymentMatch, match_id)
    if m is not None:
        m.state = "rejected"
    return m


def suggestions(session: Session) -> list[PaymentMatch]:
    return list(session.scalars(
        select(PaymentMatch).join(Invoice)
        .where(PaymentMatch.state == "suggested", Invoice.status == "open").order_by(PaymentMatch.score.desc())
        .options(selectinload(PaymentMatch.invoice).selectinload(Invoice.document))
    ))
