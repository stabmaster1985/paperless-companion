"""Rücklagen für Verträge, die nicht monatlich gezahlt werden (vierteljährlich, halbjährlich, jährlich).

Je Vertrag: Zahlung und Intervall, die monatliche Rücklage (Zahlung ÷ Monate), die nächste Zahlung und wie viel bis heute
dafür angespart sein sollte (anteilig seit der letzten Zahlung). Die Zahltermine kommen aus den erkannten Abbuchungen in
Firefly, sonst aus dem Vertragsbeginn.
"""

from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contract

NAMES = {1: "monatlich", 3: "vierteljährlich", 6: "halbjährlich", 12: "jährlich"}


@dataclass
class Reserve:
    contract: Contract
    amount: float
    interval: int
    monthly: float
    last: date | None
    next: date | None
    saved: float | None  # sollte bis heute angespart sein
    source: str  # Firefly | Vertragsbeginn | –

    @property
    def rhythm(self) -> str:
        return NAMES.get(self.interval, f"alle {self.interval} Monate")


def _dates_from_start(start: date, interval: int, today: date) -> tuple[date, date]:
    nxt = start
    while nxt <= today:
        nxt = nxt + relativedelta(months=interval)
    return nxt - relativedelta(months=interval), nxt


def collect(session: Session, today: date) -> dict:
    from app import recurring

    series_by_contract: dict[int, object] = {}
    for s in recurring.stored(session):
        if s.contract_id and (s.contract_id not in series_by_contract or len(s.amounts) > len(series_by_contract[s.contract_id].amounts)):
            series_by_contract[s.contract_id] = s
    items = []
    for c in session.scalars(select(Contract).where(Contract.status.in_(("active", "cancelled")))):
        if c.status == "cancelled" and not (c.cancel_effective and c.cancel_effective >= today):
            continue
        if c.payment is None:
            continue
        last = nxt = None
        source = "–"
        if (s := series_by_contract.get(c.id)) is not None:
            last, nxt, source = s.last_date, s.next_date, "Firefly"
        elif c.start_date:
            last, nxt = _dates_from_start(c.start_date, c.interval, today)
            source = "Vertragsbeginn"
        saved = None
        if last and nxt and nxt > last:
            share = min(1.0, max(0.0, (today - last).days / (nxt - last).days))
            saved = round(c.payment * share, 2)
        items.append(Reserve(c, c.payment, c.interval, round(c.payment / c.interval, 2), last, nxt, saved, source))
    items.sort(key=lambda r: (r.next or date.max, r.contract.label.lower()))
    rare = [r for r in items if r.interval > 1]
    return {
        "rows": items,
        "rare": rare,
        "monthly_reserve": round(sum(r.monthly for r in rare), 2),
        "should_have": round(sum(r.saved or 0 for r in rare), 2),
        "due_30": [r for r in items if r.next and (r.next - today).days <= 31],
        "due_30_sum": round(sum(r.amount for r in items if r.next and (r.next - today).days <= 31), 2),
        "monthly_all": round(sum(r.monthly for r in items), 2),
    }
