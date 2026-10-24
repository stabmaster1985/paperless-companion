"""Abgleich offener Rechnungen mit Abbuchungen aus Firefly III."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.firefly import FireflyClient, Transaction
from app.models import Document, Invoice, PaymentMatch

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
            _mark_firefly(inv.document, points)
            counts["accepted"] += 1
        else:
            counts["suggested"] += 1
    return counts


# --------------------------------------------------------------------------- Dokumente in der Inbox


@dataclass
class Candidate:
    """Rechnungsangaben eines noch nicht bestätigten Dokuments (aus den Vorschlägen)."""

    document: Document
    amount: float | None
    invoice_number: str | None
    iban: str | None
    issuer: str | None
    invoice_date: date | None
    due_date: date | None


# Ohne Betrag: Rechnungsnummer (mind. 4 Zeichen) im Verwendungszweck oder IBAN + Empfänger müssen passen
PARTIAL_MIN = 40


def candidate(doc: Document) -> Candidate | None:
    from app import pipeline

    if doc.kind != "invoice" or doc.review_state != "pending":
        return None
    v = pipeline.parse_values("invoice", pipeline.suggestion_values(doc))
    if v.get("status") == "paid" and (doc.suggestions or {}).get("status", {}).get("source") != "firefly":
        return None  # laut Dokument/Paperless bereits bezahlt
    return Candidate(doc, v.get("amount"), v.get("invoice_number"), v.get("iban"), v.get("issuer"),
                     v.get("invoice_date"), v.get("due_date"))


def score_candidate(c: Candidate, tx: Transaction) -> tuple[int, list[str]] | None:
    if c.amount is not None:
        return score(c, tx)  # type: ignore[arg-type] – gleiche Attribute wie Invoice
    span = window(c)  # type: ignore[arg-type]
    if span is None or not span[0] <= tx.date <= span[1]:
        return None
    points, reasons = 0, []
    if c.invoice_number and len(_compact(c.invoice_number)) >= 4 and _compact(c.invoice_number) in _compact(tx.text):
        points += 40
        reasons.append("Rechnungsnr.")
    if c.iban and tx.iban and c.iban.replace(" ", "").upper() == tx.iban:
        points += 30
        reasons.append("IBAN")
    if not reasons:
        return None  # ohne Betrag nur mit eindeutigem Merkmal
    issuer = c.issuer or c.document.correspondent or ""
    if _words(issuer) & _words(f"{tx.counterparty} {tx.description}"):
        points += 20
        reasons.append("Empfänger")
    if reasons == ["IBAN"]:
        return None  # IBAN allein reicht nicht (gleicher Empfänger, viele Rechnungen)
    return (points, reasons + ["Betrag aus Buchung"]) if points >= PARTIAL_MIN else None


def _used_transactions(session: Session, exclude_doc: int | None = None) -> set[str]:
    used = {m.tx_id for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state.in_(("accepted", "suggested"))))}
    for doc in session.scalars(select(Document).where(Document.review_state == "pending", Document.kind == "invoice")):
        if doc.id != exclude_doc and (p := (doc.suggestions or {}).get("_payment")):
            used.add(p.get("tx_id"))
    return used


def best_payment(c: Candidate, transactions: list[Transaction], used: set[str]):
    rejected = set((c.document.suggestions or {}).get("_payment_rejected") or [])  # von dir als „passt nicht“ markiert
    scored = []
    for tx in transactions:
        if tx.journal_id in used or tx.journal_id in rejected:
            continue
        if (result := score_candidate(c, tx)) is not None:
            scored.append((result[0], tx, result[1]))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    best = scored[0]
    if c.amount is not None and best[0] < SUGGEST_MIN and len(scored) > 1:
        return None  # nur der Betrag passt und ist nicht eindeutig
    return best


def apply_payment(doc: Document, points: int, tx: Transaction, reasons: list[str]) -> None:
    """Zahlung als Vorschlag am Dokument hinterlegen und Felder vorbelegen (Bestätigung bleibt beim Nutzer)."""
    from app.extraction.rules import field

    sugg = dict(doc.suggestions or {})
    conf = 0.9 if points >= AUTO_MIN else 0.75
    sugg["_payment"] = {
        "tx_id": tx.journal_id, "group_id": tx.group_id, "date": tx.date.isoformat(), "amount": tx.amount,
        "counterparty": tx.counterparty[:200], "description": tx.description[:300], "score": points,
        "reasons": ", ".join(reasons),
    }
    sugg["status"] = field("paid", conf, "firefly")
    sugg["paid_on"] = field(tx.date, conf, "firefly")
    if "Betrag aus Buchung" in reasons:
        sugg["amount"] = field(tx.amount, 0.75, "firefly")
    doc.suggestions = sugg


def pending_candidates(session: Session) -> list[Candidate]:
    docs = session.scalars(select(Document).where(Document.review_state == "pending", Document.kind == "invoice"))
    return [c for d in docs if "_payment" not in (d.suggestions or {}) and (c := candidate(d))]


def match_pending(session: Session, transactions: list[Transaction], candidates: list[Candidate]) -> int:
    used = _used_transactions(session)
    found = 0
    for c in candidates:
        if (best := best_payment(c, transactions, used)) is not None:
            apply_payment(c.document, *best)
            used.add(best[1].journal_id)
            found += 1
    return found


def check_document(session: Session, doc: Document, client: FireflyClient, today: date) -> dict | None:
    """Direkt nach der Analyse: passende Abbuchung für ein Inbox-Dokument suchen."""
    c = candidate(doc)
    if c is None or (span := window(c)) is None:  # type: ignore[arg-type]
        return None
    if span[0] > today:
        return None
    transactions = list(client.withdrawals(span[0], min(span[1], today)))
    best = best_payment(c, transactions, _used_transactions(session, exclude_doc=doc.id))
    if best is None:
        return None
    apply_payment(doc, *best)
    return doc.suggestions["_payment"]


def record_confirmed_payment(session: Session, doc: Document) -> None:
    """Beim Bestätigen einer vorbelegten Zahlung die Buchung als verwendet merken."""
    p = (doc.suggestions or {}).get("_payment")
    inv = doc.invoice
    if not p or inv is None or inv.status != "paid" or (inv.paid_on and inv.paid_on.isoformat() != p["date"]):
        return
    session.flush()
    if session.scalar(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id, PaymentMatch.tx_id == p["tx_id"])):
        return
    session.add(PaymentMatch(
        invoice_id=inv.id, tx_id=p["tx_id"], tx_group_id=p["group_id"], tx_date=date.fromisoformat(p["date"]),
        amount=p["amount"], counterparty=p["counterparty"], description=p["description"], score=p["score"],
        reasons=p["reasons"], state="accepted",
    ))


def _mark_firefly(doc: Document, points: int, confirmed: bool = False) -> None:
    from app import pipeline

    pipeline.set_source(doc, ["status", "paid_on"], "firefly", 0.9 if points >= AUTO_MIN else 0.75, confirmed)


def run(session: Session, client: FireflyClient, settings: Settings, today: date) -> dict[str, int]:
    open_invoices = list(session.scalars(select(Invoice).where(Invoice.status == "open").options(selectinload(Invoice.document))))
    with_amount = [inv for inv in open_invoices if inv.amount is not None]
    pending = pending_candidates(session)
    starts = [span[0] for item in [*with_amount, *pending] if (span := window(item))]  # type: ignore[arg-type]
    result = {"open": len(open_invoices), "open_with_amount": len(with_amount), "transactions": 0, "suggested": 0,
              "accepted": 0, "pending": len(pending), "pending_found": 0}
    if starts:
        start = max(min(starts), today - timedelta(days=settings.firefly_lookback_days))
        transactions = list(client.withdrawals(start, today))
        result["transactions"] = len(transactions)
        result.update(match(session, transactions, settings))
        result["pending_found"] = match_pending(session, transactions, pending)
    _remember(session, result)
    return result


def _remember(session: Session, result: dict) -> None:
    from app.models import KeyValue

    value = json.dumps({**result, "at": datetime.now(timezone.utc).isoformat(timespec="minutes")})
    item = session.get(KeyValue, "last_payment_run")
    if item:
        item.value = value
    else:
        session.add(KeyValue(key="last_payment_run", value=value))


def summary(session: Session) -> dict:
    """Bisheriger Nutzen des Abgleichs (für die Systemseite)."""
    from app.models import KeyValue

    matches = list(session.scalars(select(PaymentMatch).options(selectinload(PaymentMatch.invoice))))
    accepted = [m for m in matches if m.state == "accepted"]
    item = session.get(KeyValue, "last_payment_run")
    return {
        "accepted": len(accepted),
        "accepted_sum": round(sum(m.amount for m in accepted), 2),
        "suggested": len([m for m in matches if m.state == "suggested" and m.invoice and m.invoice.status == "open"]),
        "rejected": len([m for m in matches if m.state == "rejected"]),
        "last_run": json.loads(item.value) if item else None,
    }


def accept(session: Session, match_id: int) -> PaymentMatch | None:
    m = session.get(PaymentMatch, match_id)
    if m is None:
        return None
    m.state = "accepted"
    m.invoice.status, m.invoice.paid_on = "paid", m.tx_date
    m.invoice.document.writeback_pending = True
    _mark_firefly(m.invoice.document, m.score, confirmed=True)
    return m


def reject(session: Session, match_id: int) -> PaymentMatch | None:
    m = session.get(PaymentMatch, match_id)
    if m is not None:
        m.state = "rejected"
    return m


def payment_for(session: Session, doc: Document) -> dict | None:
    """Die Buchung, aus der Werte des Dokuments stammen: bestätigt, vorgeschlagen oder in der Inbox vorbelegt."""
    inv = doc.invoice
    if inv is not None:
        for state in ("accepted", "suggested"):
            m = session.scalar(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id, PaymentMatch.state == state)
                               .order_by(PaymentMatch.score.desc()).limit(1))
            if m is not None:
                return {"tx_id": m.tx_id, "group_id": m.tx_group_id, "date": m.tx_date.isoformat(), "amount": m.amount,
                        "counterparty": m.counterparty, "description": m.description, "reasons": m.reasons, "state": state}
    p = (doc.suggestions or {}).get("_payment")
    if p:
        return {**p, "state": "prefilled"}
    return None


def links(settings, group_id: str | None, day) -> list[tuple[str, str]]:
    """Links zur Kontrolle einer Buchung: direkt in Firefly und (optional) im Dashboard am Buchungstag."""
    result = []
    if settings.firefly_url and group_id:
        result.append(("Firefly", f"{settings.firefly_url.rstrip('/')}/transactions/show/{group_id}"))
    if settings.firefly_dashboard_url and day:
        iso = day if isinstance(day, str) else day.isoformat()
        result.append(("Dashboard", f"{settings.firefly_dashboard_url.rstrip('/')}/#/buchungen?von={iso}&bis={iso}"))
    return result


def suggestions(session: Session) -> list[PaymentMatch]:
    return list(session.scalars(
        select(PaymentMatch).join(Invoice)
        .where(PaymentMatch.state == "suggested", Invoice.status == "open").order_by(PaymentMatch.score.desc())
        .options(selectinload(PaymentMatch.invoice).selectinload(Invoice.document))
    ))


# --------------------------------------------------------------------------- Selbst in Firefly suchen und zuordnen


def search_defaults(doc: Document) -> dict:
    """Vorbelegung der Suche: Betrag, Zeitraum und Empfänger aus dem Dokument."""
    from app import pipeline

    if doc.invoice is not None and doc.review_state != "pending":
        inv = doc.invoice
        amount, number, issuer = inv.amount, inv.invoice_number, inv.issuer
        base, due, iban = inv.invoice_date, inv.due_date, inv.iban
    else:
        v = pipeline.parse_values("invoice", pipeline.suggestion_values(doc))
        # vorbelegter Betrag aus einer (falschen) Buchung zählt nicht
        amount_src = ((doc.suggestions or {}).get("amount") or {}).get("source")
        amount = v.get("amount") if amount_src != "firefly" else None
        number, issuer, base, due, iban = (v.get("invoice_number"), v.get("issuer"), v.get("invoice_date"),
                                           v.get("due_date"), v.get("iban"))
    base = base or doc.created or due
    start = (base - timedelta(days=14)) if base else None
    end = (max(due or base, base) + timedelta(days=90)) if base else None
    return {"amount": amount, "start": start, "end": end, "q": issuer or doc.correspondent or "",
            "invoice_number": number, "iban": iban}


def search(session: Session, doc: Document, client: FireflyClient, today: date, amount: float | None,
           start: date, end: date, q: str = "", limit: int = 30) -> list[dict]:
    """Abbuchungen im Zeitraum, die zu Betrag, Suchtext, Rechnungsnr. oder IBAN passen – beste zuerst."""
    d = search_defaults(doc)
    words = _words(q)
    used = _used_transactions(session, exclude_doc=doc.id)
    current = (payment_for(session, doc) or {}).get("tx_id")
    found = []
    for tx in client.withdrawals(start, min(end, today)):
        points, reasons = 0, []
        if amount is not None:
            diff = abs(tx.amount - amount)
            if diff <= 0.005:
                points, reasons = points + 50, [*reasons, "Betrag"]
            elif diff <= max(1.0, amount * 0.05):
                points, reasons = points + 20, [*reasons, "Betrag ähnlich"]
        if d["invoice_number"] and len(_compact(d["invoice_number"])) >= 3 and _compact(d["invoice_number"]) in _compact(tx.text):
            points, reasons = points + 40, [*reasons, "Rechnungsnr."]
        if d["iban"] and tx.iban and d["iban"].replace(" ", "").upper() == tx.iban:
            points, reasons = points + 30, [*reasons, "IBAN"]
        if words and words & _words(f"{tx.counterparty} {tx.description} {tx.text}"):
            points, reasons = points + 25, [*reasons, "Suchtext"]
        if not reasons:
            continue
        found.append({"tx": tx, "score": points, "reasons": reasons, "used": tx.journal_id in used,
                      "current": tx.journal_id == current,
                      "diff": abs(tx.amount - amount) if amount is not None else 0.0})
    found.sort(key=lambda f: (-f["score"], f["diff"], abs((f["tx"].date - (start + (end - start) / 2)).days)))
    return found[:limit]


TAKE = ("amount", "issuer", "iban")  # was aus einer gewählten Buchung übernommen werden kann (Status/Bezahlt am immer)


def issuer_name(session: Session, counterparty: str) -> str:
    """Empfänger der Buchung als Aussteller – möglichst als Name deines passenden Korrespondenten."""
    from app.metadata import correspondent_names

    simple = lambda n: " ".join(re.sub(r"[^\w\s]", " ", n.lower()).split())  # noqa: E731
    target = simple(counterparty)
    if not target:
        return counterparty
    names = correspondent_names(session)
    for name in names:
        if simple(name) == target:
            return name
    # „Stadtwerke Musterstadt GmbH“ ~ „Stadtwerke Musterstadt“: der längste Korrespondent, der darin vorkommt
    contained = [n for n in names if len(simple(n)) >= 4 and re.search(rf"\b{re.escape(simple(n))}\b", target)]
    return max(contained, key=len) if contained else counterparty


def choose(session: Session, doc: Document, tx: Transaction, take=TAKE) -> tuple[str, dict]:
    """Von dir gewählte Buchung zuordnen. Status/Bezahlt am und die gewählten Werte (`take`) werden übernommen –
    in der Inbox als Vorbelegung, bei bestätigten Rechnungen direkt gespeichert. Gibt Meldung und Werte zurück."""
    from app.extraction.rules import field

    reasons = ["von dir gewählt"]
    values = {"amount": tx.amount} if "amount" in take else {}
    if "issuer" in take and tx.counterparty:
        values["issuer"] = issuer_name(session, tx.counterparty)
    if "iban" in take and tx.iban:
        values["iban"] = tx.iban
    if doc.review_state == "pending":
        apply_payment(doc, AUTO_MIN, tx, reasons)
        sugg = dict(doc.suggestions)
        sugg["_payment_rejected"] = [t for t in sugg.get("_payment_rejected") or [] if t != tx.journal_id] or None
        for name, value in values.items():
            sugg[name] = field(value, 0.9, "firefly")
        doc.suggestions = {k: v for k, v in sugg.items() if v is not None}
        return "vorbelegt – bitte prüfen und bestätigen", values
    inv = doc.invoice
    if inv is None:
        return "keine Rechnung", {}
    chosen = None
    for m in session.scalars(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id)):
        if m.tx_id == tx.journal_id:
            chosen = m
        elif m.state in ("accepted", "suggested"):
            m.state = "rejected"
    if chosen is None:
        chosen = PaymentMatch(invoice_id=inv.id, tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date,
                              amount=tx.amount, counterparty=tx.counterparty[:200], description=tx.description[:300],
                              score=AUTO_MIN, reasons=", ".join(reasons))
        session.add(chosen)
    chosen.state = "accepted"
    inv.status, inv.paid_on = "paid", tx.date
    for name, value in values.items():
        setattr(inv, name, value)
    doc.writeback_pending = True
    _mark_firefly(doc, AUTO_MIN, confirmed=True)
    if values:
        from app import pipeline

        pipeline.set_source(doc, list(values), "firefly", 0.9, True)
    return "als bezahlt übernommen", values


def clear(session: Session, doc: Document) -> bool:
    """„Passt nicht“: zugeordnete bzw. vorgeschlagene Buchung lösen; sie wird nicht wieder vorgeschlagen."""
    changed = False
    sugg = dict(doc.suggestions or {})
    if p := sugg.pop("_payment", None):
        sugg["_payment_rejected"] = [*(sugg.get("_payment_rejected") or []), p["tx_id"]]
        for name in ("status", "paid_on", "amount"):
            if isinstance(sugg.get(name), dict) and sugg[name].get("source") == "firefly":
                sugg.pop(name)
        doc.suggestions = sugg
        changed = True
    inv = doc.invoice
    if inv is not None and doc.review_state != "pending":
        for m in session.scalars(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id,
                                                            PaymentMatch.state.in_(("accepted", "suggested")))):
            if m.state == "accepted" and inv.paid_on == m.tx_date:
                inv.status, inv.paid_on = "open", None
                doc.writeback_pending = True
            m.state = "rejected"
            changed = True
    return changed
