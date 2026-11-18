"""Regelmäßige Zahlungen aus Firefly: Abos und Verträge erkennen, die (noch) fehlen, Preisänderungen bemerken
und ausgebliebene Abbuchungen melden.

Grundlage sind die Abbuchungen der letzten Jahre (Einstellung „Regelmäßige Zahlungen: letzte … Tage“, Standard 5 Jahre). Eine „Serie“ sind Buchungen an denselben Empfänger mit
ähnlichem Betrag (±25 %, damit Preiserhöhungen dazugehören) in festem Rhythmus (monatlich, vierteljährlich,
halbjährlich, jährlich). Alles bleibt im Companion; Hinweise lassen sich ausblenden.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.firefly import Transaction
from app.models import Contract, KeyValue, PaymentMatch, ReceiptCheck
from app.receipts import fixed_documents, has_fixed, ignored_counterparties, party_key, _is_ignored

KEY = "recurring"
DISMISSED_KEY = "recurring_dismissed"
LINKS_KEY = "recurring_links"  # Serie -> Vertrag (von dir zugeordnet)
RHYTHMS = [(1, 30, 6), (3, 91, 15), (6, 182, 20), (12, 365, 25)]  # Monate, Tage, Toleranz
NAMES = {1: "monatlich", 3: "vierteljährlich", 6: "halbjährlich", 12: "jährlich"}


@dataclass
class Series:
    key: str
    payee: str
    months: int
    amounts: list[float]
    dates: list[str]
    tx_ids: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)  # für Links zur Buchung in Firefly
    descriptions: list[str] = field(default_factory=list)
    contract_id: int | None = None
    covered: str | None = None  # wodurch belegt: Vertrag, laufende Zahlung, Rechnungen

    @property
    def last_amount(self) -> float:
        return self.amounts[-1]

    @property
    def last_date(self) -> date:
        return date.fromisoformat(self.dates[-1])

    @property
    def next_date(self) -> date:
        from dateutil.relativedelta import relativedelta

        return self.last_date + relativedelta(months=self.months)

    @property
    def monthly(self) -> float:
        return round(self.last_amount / self.months, 2)

    @property
    def bookings(self) -> list[dict]:
        """Einzelne Abbuchungen, neueste zuerst (für die Anzeige)."""
        rows = []
        for i, day in enumerate(self.dates):
            rows.append({"date": date.fromisoformat(day), "amount": self.amounts[i],
                         "group_id": self.group_ids[i] if i < len(self.group_ids) else None,
                         "description": self.descriptions[i] if i < len(self.descriptions) else ""})
        return rows[::-1]

    @property
    def rhythm(self) -> str:
        return NAMES[self.months]

    def change(self) -> tuple[float, float, str] | None:
        """Betragsänderung: die letzten (mindestens eine) Buchungen anders als die davor – (alt, neu, seit)."""
        if len(self.amounts) < 3:
            return None
        new = self.amounts[-1]
        i = len(self.amounts) - 1
        while i > 0 and abs(self.amounts[i - 1] - new) <= 0.005:
            i -= 1
        if i == 0:
            return None
        old = self.amounts[i - 1]
        if abs(new - old) <= max(0.01, old * 0.005):
            return None
        return old, new, self.dates[i]


def _streams(txs: list[Transaction]) -> list[list[Transaction]]:
    """Buchungen eines Empfängers in Ströme ähnlicher Beträge teilen (Abo neben Einzelkäufen)."""
    streams: list[list[Transaction]] = []
    for tx in sorted(txs, key=lambda t: t.date):
        for s in streams:
            last = s[-1].amount
            if abs(tx.amount - last) <= max(1.0, last * 0.25) and tx.date > s[-1].date:
                s.append(tx)
                break
        else:
            streams.append([tx])
    # Preiserhöhung: ein Strom endet, ein anderer mit höherem/niedrigerem Betrag setzt direkt danach fort -> zusammenführen
    streams.sort(key=lambda s: s[0].date)
    merged: list[list[Transaction]] = []
    for s in streams:
        def continues(m: list[Transaction]) -> bool:
            if len(m) < 2 or m[-1].date >= s[0].date or not 0.6 <= s[0].amount / m[-1].amount <= 1.6:
                return False
            gap = median((b.date - a.date).days for a, b in zip(m, m[1:]))  # setzt im gleichen Rhythmus fort?
            return abs((s[0].date - m[-1].date).days - gap) <= max(6, gap * 0.2)

        prev = next((m for m in reversed(merged) if continues(m)), None)
        if prev is not None and not any(o is not prev and o[-1].date > prev[-1].date and o[0].date < s[0].date for o in merged):
            prev.extend(s)
        else:
            merged.append(s)
    return merged


def _rhythm(dates: list[date]) -> int | None:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if not gaps:
        return None
    mid = median(gaps)
    for months, days, tolerance in RHYTHMS:
        if abs(mid - days) <= tolerance:
            fitting = [g for g in gaps if abs(g - days) <= tolerance]
            needed = 2 if months == 12 else 3
            if len(dates) >= needed and len(fitting) >= max(1, round(len(gaps) * 0.6)):
                return months
    return None


def detect(transactions: list[Transaction], ignored: list[str] | None = None) -> list[Series]:
    by_payee: dict[str, list[Transaction]] = {}
    for tx in transactions:
        key = party_key(tx.counterparty)
        if key and not _is_ignored(tx.counterparty, ignored or []):
            by_payee.setdefault(key, []).append(tx)
    found = []
    for key, txs in by_payee.items():
        for stream in _streams(txs):
            months = _rhythm([t.date for t in stream])
            if months is None:
                continue
            found.append(Series(key=f"{key}|{months}|{round(stream[0].amount)}", payee=stream[-1].counterparty,
                                months=months, amounts=[t.amount for t in stream],
                                dates=[t.date.isoformat() for t in stream], tx_ids=[t.journal_id for t in stream],
                                group_ids=[t.group_id for t in stream],
                                descriptions=[(t.description or "")[:120] for t in stream]))
    return found


GENERIC = {"gmbh", "mbh", "und", "der", "die", "das", "service", "services", "deutschland", "germany", "europe", "online",
           "international", "gesellschaft", "sarl", "ltd", "inc", "bank", "zahlung", "lastschrift", "muster"}


def _words(text: str | None) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-zäöüß0-9]+", (text or "").lower()) if len(w) >= 3 and w not in GENERIC}


def match_contract(session: Session, s: Series, links: dict) -> Contract | None:
    if (cid := links.get(s.key)) and (c := session.get(Contract, cid)):
        return c
    payee = _words(s.payee)
    best, best_points = None, 0
    for c in session.scalars(select(Contract).where(Contract.status.in_(("active", "cancelled")))):
        names = _words(c.provider) | _words(c.document.correspondent if c.document else "")
        points = len(payee & names) * 2 + len(payee & _words(c.name))
        if c.payee and party_key(c.payee) == party_key(s.payee):
            points += 5  # beim Abo hinterlegter Firefly-Empfänger
        if c.monthly_cost and abs(c.monthly_cost - s.monthly) <= max(1.0, c.monthly_cost * 0.2):
            points += 1
        if points > best_points:
            best, best_points = c, points
    return best if best_points >= 2 else None


def evaluate(session: Session, series: list[Series]) -> list[Series]:
    """Serien Verträgen bzw. Belegen zuordnen (wodurch sind sie schon „bekannt“?)."""
    links = _kv(session, LINKS_KEY)
    fixed = fixed_documents(session)
    paid = {m.tx_id for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state == "accepted"))}
    linked = {r.tx_id for r in session.scalars(select(ReceiptCheck).where(ReceiptCheck.state == "linked"))}
    for s in series:
        contract = match_contract(session, s, links)
        s.contract_id = contract.id if contract else None
        if contract:
            s.covered = "Vertrag"
        elif has_fixed(fixed, s.payee, s.last_amount):
            s.covered = "feste Zuordnung"
        elif sum(t in paid or t in linked for t in s.tx_ids) >= len(s.tx_ids) / 2:
            s.covered = "Rechnungen/Belege"
    return series


def _kv(session: Session, key: str):
    item = session.get(KeyValue, key)
    return json.loads(item.value) if item else ({} if key != DISMISSED_KEY else [])


def _kv_set(session: Session, key: str, value) -> None:
    item = session.get(KeyValue, key)
    if item:
        item.value = json.dumps(value, ensure_ascii=False)
    else:
        session.add(KeyValue(key=key, value=json.dumps(value, ensure_ascii=False)))


def run(session: Session, transactions: list[Transaction]) -> int:
    """Serien aus den Abbuchungen erkennen und speichern. Gibt die Anzahl zurück."""
    series = evaluate(session, detect(transactions, ignored_counterparties(session)))
    _kv_set(session, KEY, [asdict(s) for s in series])
    return len(series)


def stored(session: Session) -> list[Series]:
    data = _kv(session, KEY)
    known = set(Series.__dataclass_fields__)
    return evaluate(session, [Series(**{k: v for k, v in d.items() if k in known}) for d in data] if isinstance(data, list) else [])


def findings(session: Session, today: date) -> dict:
    """Hinweise: Serien ohne Vertrag, Betragsänderungen an Verträgen, ausgebliebene Abbuchungen."""
    dismissed = set(_kv(session, DISMISSED_KEY))
    result = {"unknown": [], "changed": [], "missing": [], "all": [], "ended": []}
    for s in stored(session):
        result["all"].append(s)
        contract = session.get(Contract, s.contract_id) if s.contract_id else None
        tolerance = 10 if s.months == 1 else 25
        # lange vorbei (mehr als zwei Abstände ohne Abbuchung): früheres Abo, kein offener Hinweis
        if not s.covered and today > s.next_date + timedelta(days=s.months * 31 + tolerance):
            result["ended"].append(s)
            continue
        if not s.covered and f"unknown:{s.key}" not in dismissed:
            result["unknown"].append(s)
        if contract is not None:
            change = s.change()
            cost_differs = contract.monthly_cost is not None and abs(contract.monthly_cost - s.monthly) > max(0.01, s.monthly * 0.01)
            if (cost_differs or (change and contract.monthly_cost is None)) and f"changed:{s.key}:{s.last_amount}" not in dismissed:
                result["changed"].append({"series": s, "contract": contract, "change": change, "cost_differs": cost_differs})
        ended = contract is not None and (contract.status == "ended" or (
            contract.status == "cancelled" and contract.cancel_effective and contract.cancel_effective < s.next_date))
        if s.covered and not ended and today > s.next_date + timedelta(days=tolerance) \
                and f"missing:{s.key}:{s.next_date.isoformat()}" not in dismissed:
            result["missing"].append({"series": s, "contract": contract})
    result["unknown"].sort(key=lambda s: -s.monthly)
    return result


def dismiss(session: Session, token: str) -> None:
    items = _kv(session, DISMISSED_KEY)
    if token not in items:
        items.append(token)
    _kv_set(session, DISMISSED_KEY, items)


def link_contract(session: Session, series_key: str, contract_id: int | None) -> None:
    links = _kv(session, LINKS_KEY)
    if contract_id:
        links[series_key] = contract_id
    else:
        links.pop(series_key, None)
    _kv_set(session, LINKS_KEY, links)
