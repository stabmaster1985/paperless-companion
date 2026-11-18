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
    target = getattr(inv, "open_amount", inv.amount)  # bei Teilzahlungen: der Rest (Inbox-Kandidat: Betrag)
    if target is None or abs(tx.amount - target) > 0.005:
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
    owner = used_elsewhere(session)  # auch auf der Belegseite zugeordnete Buchungen
    known_pairs = {(m.invoice_id, m.tx_id) for m in existing}
    has_suggestion = {m.invoice_id for m in existing if m.state == "suggested"}

    candidates: list[tuple[int, Invoice, Transaction, list[str]]] = []
    for inv in invoices:
        if inv.id in has_suggestion or inv.paid_amount:
            continue  # teilweise bezahlte Rechnungen: siehe partial_suggestions
        scored = []
        for tx in transactions:
            if tx.journal_id in used_tx or (inv.id, tx.journal_id) in known_pairs \
                    or owner.get(tx.journal_id, inv.document_id) != inv.document_id:
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
    counts["suggested"] += partial_suggestions(session, invoices, transactions, used_tx, known_pairs, owner)
    taken = {tx.journal_id for _, _, tx, _ in candidates}
    counts["suggested"] += collective_suggestions(session, invoices, transactions, used_tx | taken, known_pairs, owner,
                                                  has_suggestion | {inv.id for _, inv, _, _ in candidates})
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


def used_elsewhere(session: Session, exclude_doc: int | None = None) -> dict[str, int]:
    """Buchungen, die schon einem Dokument gehören (tx_id -> Dokument): Zahlungsabgleich (übernommen oder
    vorgeschlagen), in der Inbox vorbelegt und auf der Belegseite zugeordnet. Eine Buchung bezahlt nur ein Dokument –
    sie wird für andere nicht mehr vorgeschlagen."""
    from app.models import ReceiptCheck

    used: dict[str, int] = {}
    for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state.in_(("accepted", "suggested")))
                             .options(selectinload(PaymentMatch.invoice))):
        if m.invoice is not None:
            used.setdefault(m.tx_id, m.invoice.document_id)
    for doc in session.scalars(select(Document).where(Document.review_state == "pending")):
        if (p := (doc.suggestions or {}).get("_payment")) and p.get("tx_id"):
            used.setdefault(p["tx_id"], doc.id)
    for row in session.scalars(select(ReceiptCheck).where(ReceiptCheck.state == "linked", ReceiptCheck.document_id.is_not(None))):
        used.setdefault(row.tx_id, row.document_id)
    return {tx: d for tx, d in used.items() if d != exclude_doc}


def _used_transactions(session: Session, exclude_doc: int | None = None) -> set[str]:
    return set(used_elsewhere(session, exclude_doc))


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
    from app import reimbursements

    result["refunds_found"] = reimbursements.run(session, client, settings, today)
    # Rückerstattungen zu bezahlten Rechnungen (Rücksendung, Gutschrift) unter den Geldeingängen suchen
    paid_since = today - timedelta(days=settings.firefly_lookback_days + 183)
    try:
        deposits = list(client.deposits(max(paid_since, today - timedelta(days=400)), today))
        result["invoice_refunds"] = suggest_refunds(session, deposits)
    except Exception as exc:  # noqa: BLE001 – Rückerstattungen sind ein Zusatz, der Abgleich bleibt gültig
        log.warning("Rückerstattungen suchen fehlgeschlagen: %s", exc)
        result["invoice_refunds"] = 0
    # Regelmäßige Zahlungen (Abos, Verträge) aus gut einem Jahr Abbuchungen
    try:
        from app import recurring

        result["recurring"] = recurring.run(session, list(client.withdrawals(today - timedelta(days=400), today)))
    except Exception as exc:  # noqa: BLE001
        log.warning("Regelmäßige Zahlungen erkennen fehlgeschlagen: %s", exc)
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
    if m.invoice.paid_amount:  # Restzahlung nach Teilzahlungen
        recompute(session, m.invoice)
    else:
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
    """Vorbelegung der Suche: Betrag, Zeitraum und Empfänger (bei Erstattungen: Absender) aus dem Dokument."""
    from app import pipeline, reimbursements

    if doc.kind == "reimbursement":
        v = pipeline.parse_values("reimbursement", pipeline.suggestion_values(doc))
        rec = doc.reimbursement
        base = (rec.statement_date if rec else None) or v.get("statement_date") or doc.created
        return {"amount": reimbursements.reimbursed_amount(doc), "start": base - timedelta(days=10) if base else None,
                "end": base + timedelta(days=60) if base else None,
                "q": (rec.payer if rec else None) or v.get("payer") or doc.correspondent or "",
                "invoice_number": None, "iban": None}
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


def refund_defaults(doc: Document) -> dict:
    """Suche nach der Rückerstattung: ab Rechnung/Zahlung ein halbes Jahr, Betrag der Rechnung, Absender = Aussteller."""
    inv = doc.invoice
    base = (inv.paid_on or inv.invoice_date) if inv else None
    base = base or doc.created
    return {"amount": inv.amount if inv else None, "start": base, "end": base + timedelta(days=180) if base else None,
            "q": (inv.issuer if inv else None) or doc.correspondent or ""}


def set_refund(doc: Document, amount: float, day: date, tx: Transaction | None = None) -> str:
    inv = doc.invoice
    inv.refund_amount, inv.refunded_on = round(amount, 2), day
    inv.refund_tx_id, inv.refund_tx_group_id = (tx.journal_id, tx.group_id) if tx else (None, None)
    from app import pipeline

    pipeline.set_source(doc, ["refund_amount", "refunded_on"], "firefly" if tx else "manual", confirmed=True)
    full = inv.amount is not None and inv.refund_amount >= inv.amount - 0.005
    return "vollständig erstattet" if full else f"teilweise erstattet – kostet dich noch {inv.net_amount:.2f} €".replace(".", ",")


REFUND_HINT = re.compile(r"erstatt|gutschrift|storno|rücksend|ruecksend|retoure|rückzahlung|rueckzahlung|refund|return",
                         re.IGNORECASE)


def suggest_refunds(session: Session, deposits: list[Transaction]) -> int:
    """Geldeingänge vom Aussteller einer bezahlten Rechnung (bis 6 Monate nach Zahlung) als Rückerstattung vorschlagen.
    Gleicher Betrag genügt; ein kleinerer nur mit Hinweis wie „Erstattung/Gutschrift/Retoure“ im Verwendungszweck.
    Nie automatisch übernommen."""
    from app.models import Reimbursement

    taken = {r.tx_id for r in session.scalars(select(Reimbursement).where(Reimbursement.tx_id.is_not(None)))}
    taken |= set(refund_transactions(session))
    taken |= {inv.refund_suggestion["tx_id"] for inv in session.scalars(select(Invoice).where(
        Invoice.refund_suggestion.is_not(None))) if inv.refund_suggestion}
    found = 0
    invoices = session.scalars(select(Invoice).where(Invoice.status == "paid", Invoice.amount.is_not(None),
                                                     Invoice.refund_amount.is_(None), Invoice.refund_suggestion.is_(None))
                               .options(selectinload(Invoice.document)))
    for inv in invoices:
        issuer = inv.issuer or (inv.document.correspondent if inv.document else "") or ""
        base = inv.paid_on or inv.invoice_date or (inv.document.created if inv.document else None)
        if not issuer or base is None:
            continue
        rejected = set(inv.refund_rejected or [])
        best = None
        for tx in deposits:
            if tx.journal_id in taken or tx.journal_id in rejected or not base <= tx.date <= base + timedelta(days=183):
                continue
            if not _words(issuer) & _words(f"{tx.counterparty} {tx.description}"):
                continue
            same = abs(tx.amount - inv.amount) <= 0.01
            hint = bool(REFUND_HINT.search(f"{tx.description} {tx.text}"))
            if not same and not (hint and tx.amount < inv.amount):
                continue
            points = (60 if same else 30) + (30 if hint else 0) + 20
            number = inv.invoice_number
            if number and len(number) >= 3 and _compact(number) in _compact(tx.text):
                points += 40
            if best is None or points > best[0]:
                best = (points, tx, same, hint)
        if best is None:
            continue
        points, tx, same, hint = best
        reasons = ["gleicher Betrag" if same else "Teilbetrag", "Absender = Aussteller", *(["Erstattung im Text"] if hint else [])]
        inv.refund_suggestion = {"tx_id": tx.journal_id, "group_id": tx.group_id, "date": tx.date.isoformat(),
                                 "amount": tx.amount, "counterparty": tx.counterparty[:200],
                                 "description": tx.description[:300], "score": points, "reasons": ", ".join(reasons)}
        taken.add(tx.journal_id)
        found += 1
    return found


def accept_refund_suggestion(doc: Document) -> str:
    sug = doc.invoice.refund_suggestion or {}
    tx = Transaction(journal_id=sug["tx_id"], group_id=sug.get("group_id") or "", date=date.fromisoformat(sug["date"]),
                     amount=sug["amount"], counterparty=sug.get("counterparty") or "", iban=None, description="", text="")
    doc.invoice.refund_suggestion = None
    return set_refund(doc, tx.amount, tx.date, tx)


def reject_refund_suggestion(doc: Document) -> None:
    inv = doc.invoice
    if inv.refund_suggestion:
        inv.refund_rejected = [*(inv.refund_rejected or []), inv.refund_suggestion["tx_id"]]
    inv.refund_suggestion = None


def clear_refund(doc: Document) -> None:
    inv = doc.invoice
    inv.refund_amount = inv.refunded_on = inv.refund_tx_id = inv.refund_tx_group_id = inv.refund_doc_id = None


REFUND_WORDS = re.compile(r"gutschrift|storno|stornierung|rückerstattung|rueckerstattung|erstattung|rücksendung|retoure|korrektur",
                          re.IGNORECASE)


def refund_doc_suggestions(session: Session, doc: Document) -> list[Document]:
    """Gutschrift-Dokumente, die in Paperless mit der Rechnung verknüpft sind („gehört zu“, beide Richtungen),
    sonst Dokumente desselben Absenders mit „Gutschrift/Storno/…“ im Titel nach dem Rechnungsdatum."""
    from app import reimbursements

    inv = doc.invoice
    linked = [session.get(Document, i) for i in sorted(reimbursements.related_graph(session).get(doc.id, ()))]
    found = [d for d in linked if d is not None and d.kind != "reimbursement"
             and (REFUND_WORDS.search(d.title or "") or d.kind == "other")]
    if not found and doc.correspondent:
        base = (inv.invoice_date if inv else None) or doc.created
        for d in session.scalars(select(Document).where(Document.correspondent == doc.correspondent, Document.id != doc.id)):
            if REFUND_WORDS.search(d.title or "") and (base is None or d.created is None or d.created >= base):
                found.append(d)
    taken = {i.refund_doc_id for i in session.scalars(select(Invoice).where(Invoice.refund_doc_id.is_not(None)))
             if i.document_id != doc.id}
    return [d for d in found if d.id not in taken and d.id != (inv.refund_doc_id if inv else None)][:5]


def doc_amount(doc: Document) -> float | None:
    """Betrag eines Dokuments: erfasste Rechnung, sonst der erkannte Vorschlag."""
    if doc.invoice is not None and doc.invoice.amount is not None:
        return doc.invoice.amount
    value = ((doc.suggestions or {}).get("amount") or {}).get("value") if isinstance(doc.suggestions, dict) else None
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def set_refund_doc(session: Session, doc: Document, refund_doc: Document) -> str:
    """Gutschrift-Dokument an die Rückerstattung hängen. Fehlen Betrag/Datum, kommen sie aus dem Dokument.
    War die Gutschrift als eigene Rechnung in der Inbox, wird sie dort herausgenommen (sie ist keine neue Rechnung)."""
    inv = doc.invoice
    inv.refund_doc_id = refund_doc.id
    if inv.refund_amount is None and (amount := doc_amount(refund_doc)):
        inv.refund_amount, inv.refunded_on = abs(amount), refund_doc.created or inv.refunded_on
    if inv.refunded_on is None:
        inv.refunded_on = refund_doc.created
    if refund_doc.review_state == "pending" and refund_doc.kind == "invoice":
        refund_doc.kind, refund_doc.kind_source, refund_doc.review_state = "other", "manual", "none"
        refund_doc.invoice = None
    return f"„{refund_doc.title or refund_doc.id}“ als Gutschrift-Beleg zugeordnet"


def refund_transactions(session: Session, exclude_doc: int | None = None) -> dict[str, int]:
    """Geldeingänge, die schon als Rückerstattung einer Rechnung gelten (tx_id -> Dokument)."""
    return {inv.refund_tx_id: inv.document_id for inv in session.scalars(select(Invoice).where(Invoice.refund_tx_id.is_not(None)))
            if inv.document_id != exclude_doc}


def search(session: Session, doc: Document, client: FireflyClient, today: date, amount: float | None,
           start: date, end: date, q: str = "", limit: int = 30, refund: bool = False) -> list[dict]:
    """Abbuchungen im Zeitraum, die zu Betrag, Suchtext, Rechnungsnr. oder IBAN passen – beste zuerst."""
    from app import reimbursements

    d = search_defaults(doc)
    words = _words(q)
    incoming = doc.kind == "reimbursement" or refund  # Erstattung/Rückerstattung: Geldeingänge statt Abbuchungen
    partial_ids: set[str] = set()
    if refund:
        from app.models import Reimbursement

        used = {r.tx_id: r.document_id for r in session.scalars(select(Reimbursement).where(Reimbursement.tx_id.is_not(None)))}
        used.update(refund_transactions(session, exclude_doc=doc.id))
        current = doc.invoice.refund_tx_id if doc.invoice else None
        current_state = "accepted"
    elif incoming:
        from app.models import Reimbursement

        used = {r.tx_id: r.document_id for r in session.scalars(select(Reimbursement).where(Reimbursement.tx_id.is_not(None)))
                if r.document_id != doc.id}
        rec = doc.reimbursement
        if rec is not None and rec.tx_id:
            current, current_state = rec.tx_id, rec.tx_state or "suggested"
        else:
            current, current_state = ((doc.suggestions or {}).get("_deposit") or {}).get("tx_id"), "prefilled"
    else:
        used = used_elsewhere(session, exclude_doc=doc.id)
        p = payment_for(session, doc) or {}
        current, current_state = p.get("tx_id"), p.get("state")
        partial_ids = {m.tx_id for m in partial_payments(session, doc.invoice)}
        used = {k: v for k, v in used.items() if k not in partial_ids}
    found = []
    for tx in (client.deposits if incoming else client.withdrawals)(start, min(end, today)):
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
        amount_ok = amount is None or abs(tx.amount - amount) <= max(1.0, amount * 0.05)
        owner_id = used.get(tx.journal_id)
        owner_doc = session.get(Document, owner_id) if owner_id else None
        found.append({"tx": tx, "score": points, "reasons": reasons, "used": owner_id is not None, "amount_ok": amount_ok,
                      "used_by": {"id": owner_id, "title": owner_doc.title if owner_doc else f"#{owner_id}"} if owner_id else None,
                      "current": tx.journal_id == current or tx.journal_id in partial_ids,
                      # accepted = übernommen; suggested/prefilled = nur vorgeschlagen bzw. in der Inbox vorbelegt
                      "partial": tx.journal_id in partial_ids and len(partial_ids) > 1,
                      "state": "accepted" if tx.journal_id in partial_ids else (current_state if tx.journal_id == current else None),
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
    """Von dir gewählte Buchung zuordnen. Status, Datum und die gewählten Werte (`take`) werden übernommen –
    in der Inbox als Vorbelegung, bei bestätigten Dokumenten direkt gespeichert.
    Gibt Meldung und die Formularfelder zurück, die die Seite setzen soll."""
    from app.extraction.rules import field

    if doc.kind == "reimbursement":
        return _choose_deposit(session, doc, tx, take)
    reasons = ["von dir gewählt"]
    values = {"amount": tx.amount} if "amount" in take else {}
    if "issuer" in take and tx.counterparty:
        values["issuer"] = issuer_name(session, tx.counterparty)
    if "iban" in take and tx.iban:
        values["iban"] = tx.iban
    form = {"status": "paid", "paid_on": tx.date.isoformat(), **values}
    if doc.review_state == "pending":
        apply_payment(doc, AUTO_MIN, tx, reasons)
        sugg = dict(doc.suggestions)
        sugg["_payment_rejected"] = [t for t in sugg.get("_payment_rejected") or [] if t != tx.journal_id] or None
        for name, value in values.items():
            sugg[name] = field(value, 0.9, "firefly")
        doc.suggestions = {k: v for k, v in sugg.items() if v is not None}
        return "vorbelegt – bitte prüfen und bestätigen", form
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
    inv.status, inv.paid_on, inv.paid_amount = "paid", tx.date, None
    for name, value in values.items():
        setattr(inv, name, value)
    doc.writeback_pending = True
    _mark_firefly(doc, AUTO_MIN, confirmed=True)
    if values:
        from app import pipeline

        pipeline.set_source(doc, list(values), "firefly", 0.9, True)
    return "als bezahlt übernommen", form


COLLECTIVE_REASON = "Sammelzahlung"


def collective_suggestions(session: Session, invoices: list[Invoice], transactions: list[Transaction], used_tx: set[str],
                           known_pairs: set, owner: dict[str, int], busy: set[int]) -> int:
    """Eine Überweisung bezahlt mehrere Rechnungen desselben Ausstellers (Handwerker, Arzt): Buchungen, deren Betrag
    genau der Summe von 2–4 offenen Rechnungen entspricht, werden für jede dieser Rechnungen vorgeschlagen.
    Nur wenn die Kombination eindeutig ist; nie automatisch übernommen."""
    from itertools import combinations

    found = 0
    free = [inv for inv in invoices if inv.id not in busy and not inv.paid_amount and inv.amount]
    for tx in sorted(transactions, key=lambda t: t.date):
        if tx.journal_id in used_tx or tx.journal_id in owner:
            continue
        payee = _words(f"{tx.counterparty} {tx.description}")
        cands = []
        for inv in free:
            if inv.id in busy or (inv.id, tx.journal_id) in known_pairs or inv.amount >= tx.amount - 0.005:
                continue
            issuer = inv.issuer or (inv.document.correspondent if inv.document else "") or ""
            base = inv.invoice_date or (inv.document.created if inv.document else None) or inv.due_date
            if not (_words(issuer) & payee) or base is None:
                continue
            if not base - timedelta(days=7) <= tx.date <= max(inv.due_date or base, base) + timedelta(days=90):
                continue
            cands.append(inv)
        if len(cands) < 2:
            continue
        cands = sorted(cands, key=lambda i: i.invoice_date or i.due_date or date.min)[:12]
        solution = None
        for size in range(2, min(4, len(cands)) + 1):
            hits = [combo for combo in combinations(cands, size) if abs(sum(i.amount for i in combo) - tx.amount) <= 0.01]
            if len(hits) == 1:
                solution = hits[0]
            if hits:
                break  # mehrere gleich große Kombinationen: nicht eindeutig -> nichts vorschlagen
        if not solution:
            continue
        numbers = [i.invoice_number for i in solution if i.invoice_number and len(i.invoice_number) >= 3]
        all_numbers = bool(numbers) and len(numbers) == len(solution) and all(_compact(n) in _compact(tx.text) for n in numbers)
        points = 80 + (40 if all_numbers else 0)
        label = f"{COLLECTIVE_REASON} ({len(solution)} Rechnungen, zusammen {tx.amount:.2f} €)".replace(".", ",")
        reasons = [label, "Empfänger", *(["Rechnungsnr."] if all_numbers else [])]
        for inv in solution:
            session.add(PaymentMatch(
                invoice_id=inv.id, tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=inv.amount,
                counterparty=tx.counterparty[:200], description=tx.description[:300], score=points,
                reasons=", ".join(reasons), state="suggested"))
            busy.add(inv.id)
            found += 1
        used_tx.add(tx.journal_id)
    return found


PARTIAL_REASON = "Teilzahlung"


def partial_suggestions(session: Session, invoices: list[Invoice], transactions: list[Transaction], used_tx: set[str],
                        known_pairs: set, owner: dict[str, int]) -> int:
    """Weitere Raten für teilweise bezahlte Rechnungen vorschlagen: Buchungen nach der ersten Rate, die wie die
    bisherigen Teilzahlungen aussehen (gleicher Betrag und Empfänger) oder genau den Rest begleichen – zusammen
    höchstens bis zum offenen Betrag. Nie automatisch übernommen."""
    found = 0
    for inv in invoices:
        paid = partial_payments(session, inv)
        if not paid or inv.status != "open" or (inv.open_amount or 0) <= 0.005:
            continue
        open_amount = inv.open_amount
        pending = [m for m in session.scalars(select(PaymentMatch).where(
            PaymentMatch.invoice_id == inv.id, PaymentMatch.state == "suggested"))]
        budget = round(open_amount - sum(m.amount for m in pending), 2)
        amounts = {m.amount for m in paid}
        names = set().union(*(_words(f"{m.counterparty} {m.description}") for m in paid))
        payees = set().union(*(_words(m.counterparty) for m in paid))
        first = min(m.tx_date for m in paid)
        scored = []
        for tx in transactions:
            if tx.journal_id in used_tx or (inv.id, tx.journal_id) in known_pairs \
                    or owner.get(tx.journal_id, inv.document_id) != inv.document_id:
                continue
            if tx.date < first - timedelta(days=5) or tx.amount > open_amount + 0.005:
                continue
            same = any(abs(tx.amount - a) <= 0.005 for a in amounts)
            rest = abs(tx.amount - open_amount) <= 0.005
            payee = bool(payees & _words(tx.counterparty))
            if not (same or rest) or not (payee or (rest and names & _words(f"{tx.counterparty} {tx.description}"))):
                continue  # gleicher Betrag an jemand anderen ist keine Rate dieser Rechnung
            points, reasons = 50, [PARTIAL_REASON, "Restbetrag" if rest else "Betrag wie bisherige Raten"]
            if payee:
                points, reasons = points + 30, [*reasons, "Empfänger wie bisher"]
            if inv.invoice_number and len(inv.invoice_number) >= 3 and _compact(inv.invoice_number) in _compact(tx.text):
                points, reasons = points + 40, [*reasons, "Rechnungsnr."]
            if inv.iban and tx.iban and inv.iban.replace(" ", "").upper() == tx.iban:
                points, reasons = points + 30, [*reasons, "IBAN"]
            scored.append((tx.date, points, tx, reasons))
        for _, points, tx, reasons in sorted(scored, key=lambda c: c[0]):  # der Reihe nach, bis der Rest erreicht ist
            if tx.amount > budget + 0.005:
                continue
            session.add(PaymentMatch(
                invoice_id=inv.id, tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=tx.amount,
                counterparty=tx.counterparty[:200], description=tx.description[:300], score=points,
                reasons=", ".join(reasons), state="suggested"))
            used_tx.add(tx.journal_id)
            budget = round(budget - tx.amount, 2)
            found += 1
    return found


def partial_payments(session: Session, inv: Invoice | None) -> list[PaymentMatch]:
    """Alle zugeordneten Buchungen einer Rechnung (bei Teilzahlungen mehrere), älteste zuerst."""
    if inv is None or inv.id is None:
        return []
    return list(session.scalars(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id, PaymentMatch.state == "accepted")
                                .order_by(PaymentMatch.tx_date)))


def recompute(session: Session, inv: Invoice) -> None:
    """Status aus den Teilzahlungen: bezahlt, sobald die Summe den Rechnungsbetrag erreicht."""
    session.flush()
    paid = partial_payments(session, inv)
    total = round(sum(m.amount for m in paid), 2)
    if inv.amount is not None and paid and total >= inv.amount - 0.005:
        inv.status, inv.paid_on, inv.paid_amount = "paid", max(m.tx_date for m in paid), None
    else:
        inv.status, inv.paid_on, inv.paid_amount = "open", None, total or None


def add_partial(session: Session, doc: Document, tx: Transaction) -> tuple[str, dict]:
    """Buchung als Teilzahlung hinzufügen – bisherige Zahlungen bleiben. Nur für bestätigte Rechnungen."""
    inv = doc.invoice
    if inv is None or doc.review_state == "pending":
        return "Teilzahlungen gehen erst nach dem Bestätigen der Rechnung", {}
    if inv.status == "paid" and not inv.paid_amount and not partial_payments(session, inv):
        inv.paid_amount = None  # „bezahlt“ ohne Buchung: ab jetzt zählen die Teilzahlungen
    m = session.scalar(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id, PaymentMatch.tx_id == tx.journal_id))
    if m is None:
        m = PaymentMatch(invoice_id=inv.id, tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date,
                         amount=tx.amount, counterparty=tx.counterparty[:200], description=tx.description[:300],
                         score=AUTO_MIN, reasons="Teilzahlung, von dir gewählt")
        session.add(m)
    m.state = "accepted"
    recompute(session, inv)
    if inv.status == "paid":
        doc.writeback_pending = True
        _mark_firefly(doc, AUTO_MIN, confirmed=True)
        msg = "als letzte Teilzahlung übernommen – die Rechnung ist jetzt bezahlt"
    else:
        msg = f"als Teilzahlung übernommen – noch offen: {inv.open_amount:.2f} €".replace(".", ",")
    return msg, {"status": inv.status, "paid_on": inv.paid_on.isoformat() if inv.paid_on else ""}


def remove_partial(session: Session, doc: Document, tx_id: str) -> bool:
    """Eine einzelne Teilzahlung lösen (wird nicht wieder vorgeschlagen)."""
    inv = doc.invoice
    m = session.scalar(select(PaymentMatch).where(PaymentMatch.invoice_id == inv.id, PaymentMatch.tx_id == tx_id)) \
        if inv is not None else None
    if m is None:
        return False
    m.state = "rejected"
    was_paid = inv.status == "paid"
    recompute(session, inv)
    if was_paid and inv.status != "paid":
        doc.writeback_pending = True
    return True


def _choose_deposit(session: Session, doc: Document, tx: Transaction, take) -> tuple[str, dict]:
    """Geldeingang zu einer Erstattung: als erhalten übernehmen (Betrag/Absender auf Wunsch aus der Buchung)."""
    from app import pipeline, reimbursements
    from app.extraction.rules import field

    values = {"amount": tx.amount} if "amount" in take else {}
    if "issuer" in take and tx.counterparty:
        values["payer"] = issuer_name(session, tx.counterparty)
    form = {"status": "received", "received_on": tx.date.isoformat(), **values}
    rec = doc.reimbursement
    if doc.review_state == "pending" or rec is None:
        sugg = dict(doc.suggestions or {})
        sugg["_deposit"] = {"tx_id": tx.journal_id, "group_id": tx.group_id, "date": tx.date.isoformat(),
                            "amount": tx.amount, "counterparty": tx.counterparty[:200]}
        sugg["status"] = field("received", 0.9, "firefly")
        sugg["received_on"] = field(tx.date, 0.9, "firefly")
        for name, value in values.items():
            sugg[name] = field(value, 0.9, "firefly")
        doc.suggestions = sugg
        return "vorbelegt – bitte prüfen und bestätigen", form
    reimbursements.set_transaction(rec, tx, "accepted")
    for name, value in values.items():
        setattr(rec, name, value)
    pipeline.set_source(doc, ["status", "received_on", *values], "firefly", 0.9, True)
    return "als erhalten gespeichert", form


def clear(session: Session, doc: Document) -> bool:
    """„Passt nicht“: zugeordnete bzw. vorgeschlagene Buchung lösen; sie wird nicht wieder vorgeschlagen."""
    if doc.kind == "reimbursement":
        from app import reimbursements

        sugg = dict(doc.suggestions or {})
        changed = bool(sugg.pop("_deposit", None))
        if changed:
            for name in ("status", "received_on", "amount", "payer"):
                if isinstance(sugg.get(name), dict) and sugg[name].get("source") == "firefly":
                    sugg.pop(name)
            doc.suggestions = sugg
        if doc.reimbursement is not None and doc.reimbursement.tx_id:
            reimbursements.reject(doc.reimbursement)
            changed = True
        return changed
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
            if m.state == "accepted" and (inv.paid_on == m.tx_date or inv.paid_amount):
                inv.status, inv.paid_on = "open", None
                doc.writeback_pending = True
            inv.paid_amount = None
            m.state = "rejected"
            changed = True
    return changed
