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
EXCLUDED_KEY = "recurring_excluded"  # tx_id -> Angaben: von dir aus einer Serie genommene Buchungen
SPLITS_KEY = "recurring_splits"  # eigene Gruppe -> {"payee", "tx_ids"}: von dir zusammengestellte Serien
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
        # Vergleich mit dem üblichen Betrag des Stroms (Median der letzten drei), nicht nur mit der letzten Buchung –
        # sonst „wandert“ ein Strom schrittweise über ganz unterschiedliche Beträge
        fitting = []
        for s in streams:
            typical = median(t.amount for t in s[-3:])
            diff = abs(tx.amount - typical)
            if diff <= max(1.0, typical * 0.25) and tx.date > s[-1].date:
                fitting.append((diff, s))
        if fitting:
            min(fitting, key=lambda f: f[0])[1].append(tx)
        else:
            streams.append([tx])
    # Preiserhöhung: ein Strom endet, ein anderer mit höherem/niedrigerem Betrag setzt direkt danach fort -> zusammenführen
    streams.sort(key=lambda s: s[0].date)
    merged: list[list[Transaction]] = []
    for s in streams:
        def continues(m: list[Transaction]) -> bool:
            if len(m) < 2 or m[-1].date >= s[0].date or not 0.6 <= s[0].amount / m[-1].amount <= 1.6:
                return False
            amounts = [t.amount for t in (*m, *s)]
            if max(amounts) / min(amounts) > 1.6:
                return False  # keine Kette aus vielen kleinen Schritten
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


NO_NAME = {"(no name)", "no name", "(kein name)", "unbekannt", ""}


def payee_of(counterparty: str | None, description: str | None) -> str:
    """Empfänger für die Erkennung. Firefly schreibt „(no name)“, wenn es keinen gibt (z.B. Kontoführungsgebühren) –
    dann zählt der Verwendungszweck („Monatsabschluss“), sonst landen alle solchen Buchungen in einem Topf."""
    name = (counterparty or "").strip()
    if name.lower() in NO_NAME:
        text = " ".join((description or "").split())[:60]
        return text or "ohne Empfänger"
    return name


def _series_of(key: str, txs: list[Transaction], months: int) -> Series:
    txs = sorted(txs, key=lambda t: t.date)
    return Series(key=key, payee=payee_of(txs[-1].counterparty, txs[-1].description), months=months, amounts=[t.amount for t in txs],
                  dates=[t.date.isoformat() for t in txs], tx_ids=[t.journal_id for t in txs],
                  group_ids=[t.group_id for t in txs], descriptions=[(t.description or "")[:120] for t in txs])


def detect(transactions: list[Transaction], ignored: list[str] | None = None, excluded: set[str] | None = None,
           splits: dict | None = None) -> list[Series]:
    found: list[Series] = []
    # von dir zusammengestellte Gruppen zuerst – sie bleiben, wie du sie angelegt hast
    in_split: dict[str, str] = {t: k for k, g in (splits or {}).items() for t in g.get("tx_ids", [])}
    grouped: dict[str, list[Transaction]] = {}
    for tx in transactions:
        if tx.journal_id in in_split and not (excluded and tx.journal_id in excluded):
            grouped.setdefault(in_split[tx.journal_id], []).append(tx)
    for key, txs in grouped.items():
        found.append(_series_of(key, txs, (splits[key].get("months") or _rhythm(sorted(t.date for t in txs)) or 1)))
    by_payee: dict[str, list[Transaction]] = {}
    for tx in transactions:
        if (excluded and tx.journal_id in excluded) or tx.journal_id in in_split:
            continue  # von dir aus der Serie genommen bzw. schon in einer eigenen Gruppe
        key = party_key(payee_of(tx.counterparty, tx.description))
        if key and not _is_ignored(tx.counterparty, ignored or []):
            by_payee.setdefault(key, []).append(tx)
    for key, txs in by_payee.items():
        for stream in _streams(txs):
            months = _rhythm([t.date for t in stream])
            if months is None:
                continue
            found.append(_series_of(f"{key}|{months}|{round(stream[0].amount)}", stream, months))
    # gleicher Empfänger, Rhythmus und Betrag (z.B. zwei Policen am selben Tag): Schlüssel eindeutig machen
    seen: dict[str, int] = {}
    for s in found:
        n = seen.get(s.key, 0) + 1
        seen[s.key] = n
        if n > 1:
            s.key = f"{s.key}|{n}"
    return found


GENERIC = {"gmbh", "mbh", "und", "der", "die", "das", "service", "services", "deutschland", "germany", "europe", "online",
           "international", "gesellschaft", "sarl", "ltd", "inc", "bank", "zahlung", "lastschrift", "muster"}


def _words(text: str | None) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-zäöüß0-9]+", (text or "").lower()) if len(w) >= 3 and w not in GENERIC}


def _contract_index(session: Session) -> list[tuple]:
    """Verträge mit vorbereiteten Namenswörtern (einmal je Auswertung statt je Serie)."""
    return [(c, _words(c.provider) | _words(c.document.correspondent if c.document else ""), _words(c.name),
             party_key(c.payee) if c.payee else None)
            for c in session.scalars(select(Contract).where(Contract.status.in_(("active", "cancelled"))))]


def match_contract(session: Session, s: Series, links: dict, index: list[tuple] | None = None) -> Contract | None:
    if (cid := links.get(s.key)) and (c := session.get(Contract, cid)):
        return c
    payee = _words(s.payee)
    payee_key = party_key(s.payee)
    best, best_points = None, 0
    for c, names, name_words, c_payee in (index if index is not None else _contract_index(session)):
        points = len(payee & names) * 2 + len(payee & name_words)
        if c_payee and c_payee == payee_key:
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
    index = _contract_index(session)
    manual = set(links.values())
    for s in series:
        contract = match_contract(session, s, links, index)
        s.contract_id = contract.id if contract else None
        if s.contract_id in manual and links.get(s.key) != s.contract_id:
            s.contract_id = None  # Vertrag hat von dir zugeordnete Buchungen – nichts automatisch dazu
    # Mehrere Serien desselben Empfängers am selben Vertrag (z.B. Abo und Einzelkäufe beim Versandhändler):
    # automatisch nur die passendste behalten – von dir zugeordnete bleiben immer
    by_contract: dict[int, list[Series]] = {}
    for s in series:
        if s.contract_id and links.get(s.key) != s.contract_id:
            by_contract.setdefault(s.contract_id, []).append(s)
    for cid, group in by_contract.items():
        if len(group) < 2:
            continue
        contract = session.get(Contract, cid)
        if contract is not None and contract.monthly_cost:
            best = min(group, key=lambda x: abs(x.monthly - contract.monthly_cost))
        else:
            best = max(group, key=lambda x: (len(x.amounts), x.dates[-1]))
        for s in group:
            if s is not best:
                s.contract_id = None
    for s in series:
        if s.contract_id:
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
    apply_rules(session, transactions)
    series = evaluate(session, detect(transactions, ignored_counterparties(session), set(excluded(session)),
                                      _kv(session, SPLITS_KEY) or {}))
    _kv_set(session, KEY, [asdict(s) for s in series])
    return len(series)


def stored(session: Session) -> list[Series]:
    data = _kv(session, KEY)
    known = set(Series.__dataclass_fields__)
    return evaluate(session, [Series(**{k: v for k, v in d.items() if k in known}) for d in data] if isinstance(data, list) else [])


def excluded(session: Session) -> dict[str, dict]:
    data = _kv(session, EXCLUDED_KEY)
    return data if isinstance(data, dict) else {}


def _save_series(session: Session, series: list[Series]) -> None:
    _kv_set(session, KEY, [{k: v for k, v in asdict(x).items() if k not in ("contract_id", "covered")} for x in series])


def exclude(session: Session, series_key: str, tx_id: str) -> dict | None:
    """Eine Buchung aus der Serie nehmen: Sie gehört dann nicht mehr zum Vertrag/Abo und ist auf der Belegseite
    wieder frei. Gibt die Angaben der Buchung zurück."""
    done = exclude_many(session, series_key, [tx_id])
    return excluded(session).get(tx_id) if done else None


def exclude_many(session: Session, series_key: str, tx_ids: list[str]) -> int:
    """Mehrere Buchungen auf einmal aus der Serie nehmen – einmal lesen, einmal speichern."""
    data = _kv(session, KEY)
    if not isinstance(data, list):
        return 0
    target = next((d for d in data if d.get("key") == series_key and set(d.get("tx_ids") or []) & set(tx_ids)),
                  next((d for d in data if d.get("key") == series_key), None))
    if target is None:
        return 0
    items = excluded(session)
    wanted, count = set(tx_ids), 0
    for i in reversed(range(len(target.get("tx_ids", [])))):
        tx_id = target["tx_ids"][i]
        if tx_id not in wanted:
            continue
        pick = lambda name: (target.get(name) or [])[i] if i < len(target.get(name) or []) else None  # noqa: E731
        items[tx_id] = {"key": series_key, "payee": target["payee"], "date": pick("dates"), "amount": pick("amounts"),
                        "group_id": pick("group_ids"), "description": pick("descriptions") or ""}
        for name in ("tx_ids", "dates", "amounts", "group_ids", "descriptions"):
            values = target.get(name) or []
            if i < len(values):
                values.pop(i)
        count += 1
    if count:
        _kv_set(session, KEY, [d for d in data if d.get("tx_ids")])
        _kv_set(session, EXCLUDED_KEY, items)
    return count


def split(session: Session, series_key: str, tx_ids: list[str]) -> str | None:
    """Angehakte Buchungen als eigene Serie abtrennen (z.B. ein zweites Abo beim selben Empfänger). Die Gruppe bleibt
    auch bei späteren Erkennungsläufen bestehen. Gibt den Schlüssel der neuen Serie zurück."""
    data = _kv(session, KEY)
    source = next((d for d in data if d.get("key") == series_key and set(d.get("tx_ids") or []) & set(tx_ids)),
                  next((d for d in data if d.get("key") == series_key), None)) if isinstance(data, list) else None
    if source is None:
        return None
    wanted = [t for t in source.get("tx_ids", []) if t in set(tx_ids)]
    if not wanted:
        return None
    columns = ("tx_ids", "dates", "amounts", "group_ids", "descriptions")
    rows = [dict(zip(columns, values)) for values in zip(*(source.get(c) or [None] * len(source["tx_ids"]) for c in columns))]
    keep = [r for r in rows if r["tx_ids"] not in wanted]
    moved = [r for r in rows if r["tx_ids"] in wanted]
    splits = _kv(session, SPLITS_KEY) or {}
    new_key = f"{party_key(source['payee'])}|eigene|{len(splits) + 1}|{round(moved[0]['amounts'] or 0)}"
    months = _rhythm([date.fromisoformat(r["dates"]) for r in moved]) or source.get("months") or 1
    splits[new_key] = {"payee": source["payee"], "tx_ids": wanted, "months": months}
    for name in columns:
        source[name] = [r[name] for r in keep]
    data = [d for d in data if d.get("tx_ids")]
    data.append({"key": new_key, "payee": source["payee"], "months": months, **{c: [r[c] for r in moved] for c in columns}})
    _kv_set(session, KEY, data)
    _kv_set(session, SPLITS_KEY, splits)
    return new_key


def assign(session: Session, contract_id: int, txs: list[Transaction]) -> str:
    """Von Hand gesuchte Buchungen einem Vertrag zuordnen: Sie kommen in die eigene Gruppe des Vertrags (wird angelegt,
    falls es keine gibt) und werden aus anderen Serien herausgenommen. Die Gruppe übersteht neue Erkennungsläufe."""
    data = _kv(session, KEY)
    data = data if isinstance(data, list) else []
    links = _kv(session, LINKS_KEY)
    splits = _kv(session, SPLITS_KEY) or {}
    key = _contract_group(splits, links, contract_id, txs)
    wanted = {t.journal_id for t in txs}
    columns = ("tx_ids", "dates", "amounts", "group_ids", "descriptions")
    for d in data:  # aus anderen Serien herausnehmen
        if d.get("key") == key:
            continue
        rows = [dict(zip(columns, v)) for v in zip(*(d.get(c) or [None] * len(d.get("tx_ids") or []) for c in columns))]
        if any(r["tx_ids"] in wanted for r in rows):
            for c in columns:
                d[c] = [r[c] for r in rows if r["tx_ids"] not in wanted]
    for g_key, group in splits.items():
        if g_key != key:
            group["tx_ids"] = [t for t in group.get("tx_ids", []) if t not in wanted]
    target = next((d for d in data if d.get("key") == key), None)
    if target is None:
        target = {"key": key, "payee": splits[key]["payee"], "months": 1, **{c: [] for c in columns}}
        data.append(target)
    have = set(target.get("tx_ids") or [])
    rows = [dict(zip(columns, v)) for v in zip(*(target.get(c) or [None] * len(have) for c in columns))] if have else []
    for t in txs:
        if t.journal_id not in have:
            rows.append({"tx_ids": t.journal_id, "dates": t.date.isoformat(), "amounts": t.amount, "group_ids": t.group_id,
                         "descriptions": (t.description or "")[:120]})
    rows.sort(key=lambda r: r["dates"])
    for c in columns:
        target[c] = [r[c] for r in rows]
    months = _rhythm([date.fromisoformat(r["dates"]) for r in rows]) or target.get("months") or 1
    target["months"] = months
    splits[key].update({"tx_ids": [r["tx_ids"] for r in rows], "months": months})
    items = excluded(session)
    for t in wanted:
        items.pop(t, None)
    _kv_set(session, KEY, [d for d in data if d.get("tx_ids")])
    _kv_set(session, SPLITS_KEY, splits)
    _kv_set(session, LINKS_KEY, links)
    _kv_set(session, EXCLUDED_KEY, items)
    return key


def _contract_group(splits: dict, links: dict, contract_id: int, txs: list[Transaction]) -> str:
    """Eigene Gruppe (Serie) eines Vertrags – wird angelegt, falls es keine gibt."""
    key = next((k for k, cid in links.items() if cid == contract_id and k in splits), None)
    if key is None:
        payee = payee_of(txs[0].counterparty, txs[0].description) if txs else "von Hand"
        key = f"{party_key(payee) or 'hand'}|eigene|{len(splits) + 1}|vertrag{contract_id}"
        splits[key] = {"payee": payee, "tx_ids": [], "months": 1}
        links[key] = contract_id
    return key


MARKS = {"category": "Kategorie", "tag": "Tag", "bill": "Firefly-Abo", "budget": "Budget"}


def search_bookings(transactions: list[Transaction], q: str = "", amount: float | None = None,
                    marks: dict | None = None) -> list[Transaction]:
    """Abbuchungen nach Text (alle Wörter in Empfänger/Verwendungszweck/Notizen), Betrag (±5 %) und Firefly-Merkmalen
    (Kategorie, Tag, Firefly-Abo, Budget – genau, ohne Groß/klein) filtern, neueste zuerst."""
    words = [w for w in (q or "").lower().split() if w]
    wanted = {k: v.strip().lower() for k, v in (marks or {}).items() if k in MARKS and v and v.strip()}
    hits = []
    for t in transactions:
        hay = f"{t.counterparty} {t.description} {t.text} {t.category} {' '.join(t.tags)} {t.bill}".lower()
        if words and not all(w in hay for w in words):
            continue
        if amount is not None and abs(t.amount - amount) > max(0.01, amount * 0.05):
            continue
        have = {(k, v.lower()) for k, v in t.marks}
        if any((k, v) not in have for k, v in wanted.items()):
            continue
        hits.append(t)
    return sorted(hits, key=lambda t: t.date, reverse=True)


def rule_matches(rule: dict | None, transactions: list[Transaction]) -> list[Transaction]:
    """Buchungen zu einer gemerkten Suche. Ohne Merkmal, Text und Betrag passt nichts (keine leere Regel)."""
    if not rule:
        return []
    marks = {k: rule.get(k) or "" for k in MARKS}
    q, amount = rule.get("q") or "", rule.get("amount")
    if not (q.strip() or amount is not None or any(v.strip() for v in marks.values())):
        return []
    return search_bookings(transactions, q, amount, marks)


def apply_rules(session: Session, transactions: list[Transaction]) -> int:
    """Gemerkte Suchen der Verträge: neue passende Buchungen kommen in die Gruppe des Vertrags (vor der Erkennung).
    Von dir herausgenommene Buchungen und Buchungen in anderen eigenen Gruppen bleiben, wo sie sind."""
    contracts = [c for c in session.scalars(select(Contract).where(Contract.booking_rule.is_not(None))) if c.booking_rule]
    if not contracts:
        return 0
    links = _kv(session, LINKS_KEY)
    splits = _kv(session, SPLITS_KEY) or {}
    skip = set(excluded(session))
    grouped = {t for g in splits.values() for t in g.get("tx_ids", [])}
    added = 0
    for c in contracts:
        hits = [t for t in rule_matches(c.booking_rule, transactions) if t.journal_id not in skip and t.journal_id not in grouped]
        if not hits:
            continue
        key = _contract_group(splits, links, c.id, hits)
        group = splits[key]
        group["tx_ids"] = [*group.get("tx_ids", []), *(t.journal_id for t in hits)]
        grouped |= {t.journal_id for t in hits}
        mine = sorted((t.date for t in transactions if t.journal_id in set(group["tx_ids"])))
        group["months"] = _rhythm(mine) or group.get("months") or 1
        added += len(hits)
    _kv_set(session, SPLITS_KEY, splits)
    _kv_set(session, LINKS_KEY, links)
    return added


def include(session: Session, tx_id: str) -> dict | None:
    """Ausgenommene Buchung wieder in ihre Serie aufnehmen."""
    items = excluded(session)
    info = items.pop(tx_id, None)
    if info is None:
        return None
    _kv_set(session, EXCLUDED_KEY, items)
    series = stored(session)
    target = next((x for x in series if x.key == info["key"]), None) or next(
        (x for x in series if party_key(x.payee) == party_key(info["payee"])), None)
    if target is not None:
        rows = list(zip(target.dates, target.amounts, target.tx_ids,
                        target.group_ids or [None] * len(target.dates), target.descriptions or [""] * len(target.dates)))
        rows.append((info["date"], info["amount"], tx_id, info.get("group_id"), info.get("description") or ""))
        rows.sort(key=lambda r: r[0])
        target.dates, target.amounts, target.tx_ids, target.group_ids, target.descriptions = (list(c) for c in zip(*rows))
        _save_series(session, series)
    return info


def excluded_for(session: Session, s: Series) -> list[dict]:
    """Ausgenommene Buchungen, die zu dieser Serie gehörten (gleicher Empfänger)."""
    return sorted(({"tx_id": t, **info} for t, info in excluded(session).items()
                   if info.get("key") == s.key or party_key(info.get("payee", "")) == party_key(s.payee)),
                  key=lambda x: x["date"], reverse=True)


def findings(session: Session, today: date) -> dict:
    """Hinweise: Serien ohne Vertrag, Betragsänderungen an Verträgen, ausgebliebene Abbuchungen."""
    dismissed = set(_kv(session, DISMISSED_KEY))
    result = {"unknown": [], "changed": [], "missing": [], "all": [], "ended": []}
    all_series = stored(session)
    # mehrere Serien an einem Vertrag (z.B. alter und neuer Beitrag): nur die aktuelle zählt für Hinweise
    current: dict[int, Series] = {}
    for s in all_series:
        if s.contract_id and (s.contract_id not in current or s.last_date > current[s.contract_id].last_date):
            current[s.contract_id] = s
    for s in all_series:
        result["all"].append(s)
        if s.contract_id and current.get(s.contract_id) is not s:
            continue  # frühere Serie dieses Vertrags: nur Verlauf
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
    ended = {id(x) for x in result["ended"]}
    running = [x for x in result["all"] if id(x) not in ended
               and today <= x.next_date + timedelta(days=x.months * 31 + (10 if x.months == 1 else 25))]
    sums = {"total": 0.0, "contract": 0.0, "other": 0.0, "open": 0.0, "count": len(running)}
    for x in running:
        part = "contract" if x.covered == "Vertrag" else ("other" if x.covered else "open")
        sums[part] += x.monthly
        sums["total"] += x.monthly
    result["sums"] = {k: round(v, 2) if isinstance(v, float) else v for k, v in sums.items()}
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
