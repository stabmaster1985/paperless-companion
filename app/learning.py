"""Aus deinen bestätigten Rechnungen je Absender lernen – nur im Companion.

Gelernt wird nur, was eindeutig ist (mindestens MIN_DOCS von dir bestätigte Rechnungen, fast immer gleich):
Zahlungsart, IBAN und Zahlungsziel (Tage zwischen Rechnungsdatum und Fälligkeit). Bei einer neuen
Rechnung desselben Absenders füllt die App damit fehlende Felder vor (Herkunft „Gelernt“) – dann muss
seltener die AI gefragt werden. Jede gelernte Regel lässt sich abschalten.

Außerdem zählt die App beim Bestätigen, wie oft die Vorschläge je Quelle (Regel, AI, Gelernt …) stimmten.
"""

import json
from collections import Counter
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.extraction.rules import field
from app.models import Document, Invoice, KeyValue

MIN_DOCS = 2
SHARE = 0.8
CONFIDENCE = 0.85
DISABLED_KEY = "learning_disabled"
STATS_KEY = "field_stats"
FIELDS = {"payment_method": "Zahlungsart", "iban": "IBAN", "due_days": "Zahlungsziel (Tage)"}


def _kv(session: Session, key: str, default):
    item = session.get(KeyValue, key)
    return json.loads(item.value) if item else default


def _kv_set(session: Session, key: str, value) -> None:
    item = session.get(KeyValue, key)
    if item:
        item.value = json.dumps(value)
    else:
        session.add(KeyValue(key=key, value=json.dumps(value)))


def _key(doc: Document) -> str | None:
    name = doc.correspondent or (doc.invoice.issuer if doc.invoice else None)
    return name.strip() if name else None


def _mode(values: list) -> tuple[object, int] | None:
    values = [v for v in values if v not in (None, "")]
    if len(values) < MIN_DOCS:
        return None
    value, n = Counter(values).most_common(1)[0]
    return (value, n) if n / len(values) >= SHARE and n >= MIN_DOCS else None


def profiles(session: Session) -> dict[str, dict]:
    """Gelernte Werte je Absender: {"Stadtwerke": {"payment_method": {"value": "debit", "basis": "5 von 5"}, …}}"""
    groups: dict[str, list[Invoice]] = {}
    # Nur von dir bestätigte Rechnungen – automatisch übernommene enthalten ungeprüfte Standardwerte
    for doc in session.scalars(select(Document).where(Document.review_state == "confirmed", Document.kind == "invoice",
                                                      Document.auto_confirmed.is_(False))
                               .options(selectinload(Document.invoice))):
        if doc.invoice is not None and (key := _key(doc)):
            groups.setdefault(key, []).append(doc.invoice)
    disabled = set(_kv(session, DISABLED_KEY, []))
    result: dict[str, dict] = {}
    for name, invoices in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        learned: dict[str, dict] = {}
        for attr in ("payment_method", "iban"):
            values = [getattr(inv, attr) for inv in invoices]
            if found := _mode(values):
                learned[attr] = {"value": found[0], "basis": f"{found[1]} von {len([v for v in values if v])}"}
        offsets = [(inv.due_date - inv.invoice_date).days for inv in invoices
                   if inv.due_date and inv.invoice_date and inv.payment_method != "debit"]
        if len(offsets) >= MIN_DOCS:
            mid = median(offsets)
            close = [o for o in offsets if abs(o - mid) <= 2]
            if len(close) / len(offsets) >= SHARE:
                learned["due_days"] = {"value": int(round(mid)), "basis": f"{len(close)} von {len(offsets)}"}
        for attr, entry in learned.items():
            entry["disabled"] = f"{name}|{attr}" in disabled
        if learned:
            result[name] = {"count": len(invoices), "fields": learned}
    return result


def set_disabled(session: Session, name: str, attr: str, disabled: bool) -> None:
    items = set(_kv(session, DISABLED_KEY, []))
    key = f"{name}|{attr}"
    items.add(key) if disabled else items.discard(key)
    _kv_set(session, DISABLED_KEY, sorted(items))


def apply(session: Session, doc: Document, kind: str, sugg: dict, threshold: float) -> list[str]:
    """Fehlende/unsichere Felder einer Rechnung aus dem Gelernten vorbelegen. Gibt die gefüllten Felder zurück."""
    if kind != "invoice":
        return []
    name = doc.correspondent or (sugg.get("issuer") or {}).get("value")
    profile = profiles(session).get((name or "").strip())
    if not profile:
        return []
    filled = []

    def weak(attr: str) -> bool:
        return attr not in sugg or sugg[attr]["confidence"] < threshold or sugg[attr].get("source") == "default"

    for attr in ("payment_method", "iban"):
        entry = profile["fields"].get(attr)
        if entry and not entry["disabled"] and weak(attr):
            sugg[attr] = field(entry["value"], CONFIDENCE, "learned")
            filled.append(attr)
    entry = profile["fields"].get("due_days")
    inv_date = (sugg.get("invoice_date") or {}).get("value")
    method = (sugg.get("payment_method") or {}).get("value")
    if entry and not entry["disabled"] and inv_date and method != "debit" and weak("due_date"):
        due = date.fromisoformat(inv_date) + timedelta(days=entry["value"])
        sugg["due_date"] = field(due, CONFIDENCE, "learned")
        filled.append("due_date")
    return filled


# --------------------------------------------------------------------------- Trefferquote der Erkennung


def record_accuracy(session: Session, doc: Document, kind: str, values: dict) -> None:
    """Beim ersten Bestätigen: Stimmte der Vorschlag je Feld? (gezählt je Quelle)"""
    from app.pipeline import FIELD_TYPES, coerce

    stats = _kv(session, STATS_KEY, {})
    types = FIELD_TYPES.get(kind, {})
    for name, s in (doc.suggestions or {}).items():
        if name.startswith("_") or not isinstance(s, dict) or name not in types or s.get("source") in (None, "default"):
            continue
        try:
            suggested = coerce(types[name], s.get("value"))
        except (TypeError, ValueError):
            suggested = None
        if suggested is None:
            continue
        entry = stats.setdefault(s["source"], {}).setdefault(name, [0, 0])
        entry[0] += int(values.get(name) == suggested)
        entry[1] += 1
    _kv_set(session, STATS_KEY, stats)


def accuracy(session: Session) -> list[dict]:
    rows = []
    for source, fields_ in sorted(_kv(session, STATS_KEY, {}).items()):
        for name, (correct, total) in sorted(fields_.items()):
            rows.append({"source": source, "field": name, "correct": correct, "total": total,
                         "rate": round(100 * correct / total) if total else 0})
    return rows
