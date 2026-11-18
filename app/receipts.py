"""Buchungen ohne Beleg: Firefly-Abbuchungen, zu denen (noch) kein Dokument in Paperless gefunden wurde.

Alles bleibt im Companion – nichts wird in Paperless oder Firefly geändert. Du entscheidest je Buchung:
Beleg zuordnen, „kein Beleg nötig“ oder einen Empfänger dauerhaft ausblenden.
"""

import json
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.firefly import FireflyClient, Transaction
from app.models import Document, Invoice, KeyValue, PaymentMatch, ReceiptCheck
from app.payments import _compact, _words

STATES = {"missing": "Beleg fehlt", "candidate": "Möglicher Beleg", "linked": "Zugeordnet", "ignored": "Kein Beleg nötig"}
IGNORE_KEY = "receipt_ignore"
ALIAS_KEY = "receipt_aliases"  # gelernt: Firefly-Empfänger -> Korrespondenten deiner Zuordnungen
FIXED_KEY = "receipt_fixed"  # Daueraufträge: Empfänger -> immer dieses Dokument
TEXT_SEARCH_PER_RUN = 25  # Volltextsuchen in Paperless je Lauf (schont Paperless)


def _kv(session: Session, key: str) -> dict:
    item = session.get(KeyValue, key)
    return json.loads(item.value) if item else {}


def _kv_set(session: Session, key: str, value: dict) -> None:
    item = session.get(KeyValue, key)
    if item:
        item.value = json.dumps(value, ensure_ascii=False)
    else:
        session.add(KeyValue(key=key, value=json.dumps(value, ensure_ascii=False)))


def party_key(counterparty: str) -> str:
    """Empfänger vereinheitlicht: nur die Namenswörter, ohne Ziffern, Sonderzeichen und kurze Zusätze
    („AMZN Mktp DE*2K4…“ und „AMZN Mktp DE*9X1“ -> „amzn mktp“)."""
    words = re.findall(r"[a-zäöüß]+", (counterparty or "").lower())
    return " ".join([w for w in words if len(w) >= 3] or words)


def aliases(session: Session) -> dict[str, dict[str, int]]:
    return _kv(session, ALIAS_KEY)


def fixed_documents(session: Session) -> dict[str, int | dict]:
    """Feste Zuordnungen je Empfänger: Dokument-ID (immer dieses Dokument) oder
    {"mode": "latest", "correspondent": …, "doc": …} (laufende Zahlung: jeweils neuestes Dokument des Korrespondenten)."""
    return _kv(session, FIXED_KEY)


def _amount_entries(value) -> list[dict]:
    """Betragsgebundene Einträge eines Empfängers (Raten): Liste von {"doc", "amount", …}."""
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    if isinstance(value, dict) and value.get("amount") is not None:
        return [value]
    return []


def set_fixed(session: Session, counterparty: str, doc_id: int | None, mode: str = "doc",
              amount: float | None = None) -> None:
    """Dauerauftrag/Abo: Buchungen an diesen Empfänger künftig fest zuordnen (None = aufheben).

    mode "doc": immer dieses Dokument. mode "latest": laufende Zahlung (Strom, Gas, Versicherung) – jeweils das
    neueste Dokument desselben Korrespondenten bis zum Buchungstag (Vertrag, Abschlagsplan, Jahresabrechnung).
    Mit `amount`: nur Buchungen genau dieses Betrags (Raten bei PayPal o.ä. – derselbe Empfänger hat sonst viele
    andere Buchungen). Mehrere Raten (z.B. zwei Kredite) beim selben Empfänger sind möglich.
    """
    fixed = fixed_documents(session)
    key = party_key(counterparty)
    if not key:
        return
    if doc_id is None:
        if amount is not None and (entries := _amount_entries(fixed.get(key))):
            rest = [e for e in entries if abs(e["amount"] - amount) > 0.005]
            if rest:
                fixed[key] = rest
            else:
                fixed.pop(key, None)
        else:
            fixed.pop(key, None)
    else:
        doc = session.get(Document, doc_id)
        if mode == "latest" and doc is not None and doc.correspondent:
            entry = {"mode": "latest", "correspondent": doc.correspondent, "doc": doc_id}
        else:
            entry = {"mode": "doc", "doc": doc_id} if amount is not None else doc_id
        if amount is not None:
            entry["amount"] = round(amount, 2)
            others = [e for e in _amount_entries(fixed.get(key)) if abs(e["amount"] - amount) > 0.005]
            fixed[key] = [*others, entry]
        else:
            fixed[key] = entry
    _kv_set(session, FIXED_KEY, fixed)


def has_fixed(fixed: dict, counterparty: str, amount: float) -> bool:
    """Gilt für diese Buchung eine feste Zuordnung (bei Raten: nur mit passendem Betrag)?"""
    value = fixed.get(party_key(counterparty))
    if value is None:
        return False
    entries = _amount_entries(value)
    return not entries or any(abs(e["amount"] - amount) <= 0.005 for e in entries)


def fixed_label(session: Session, entry) -> str:
    """Kurzbeschreibung einer festen Zuordnung für die Liste."""
    if isinstance(entry, list):
        return "; ".join(fixed_label(session, e) for e in entry)
    if isinstance(entry, dict):
        rate = f" (nur Buchungen über {entry['amount']:.2f} €)".replace(".", ",") if entry.get("amount") is not None else ""
        if entry.get("mode") == "latest":
            return f"laufende Zahlung – jeweils neuestes Dokument von {entry.get('correspondent')}{rate}"
        return fixed_label(session, entry.get("doc")) + rate
    doc = session.get(Document, entry) if isinstance(entry, int) else None
    return doc.title if doc and doc.title else f"#{entry}"


def fixed_target(session: Session, entry, day: date, amount: float | None = None) -> tuple[int | None, str]:
    """Zu welchem Dokument gehört eine Buchung vom Tag `day` (über `amount`) laut fester Zuordnung?
    Gibt (ID, Hinweis) zurück."""
    if isinstance(entry, list):
        match = next((e for e in entry if amount is not None and abs(e.get("amount", -1) - amount) <= 0.005), None)
        return fixed_target(session, match, day, amount) if match else (None, "")
    if isinstance(entry, dict) and entry.get("amount") is not None and \
            (amount is None or abs(entry["amount"] - amount) > 0.005):
        return None, ""
    if isinstance(entry, dict) and entry.get("mode") == "latest":
        # Leistungsabrechnungen derselben Krankenversicherung sind kein Beleg für den Beitrag
        docs = [d for d in session.scalars(select(Document).where(Document.correspondent == entry["correspondent"],
                                                                  Document.created.is_not(None)).order_by(Document.created))
                if d.kind != "reimbursement"]
        before = [d for d in docs if d.created <= day]
        doc = before[-1] if before else (docs[0] if docs else session.get(Document, entry.get("doc")))
        if doc is None:
            return None, ""
        return doc.id, f"laufende Zahlung: neuestes Dokument von {entry['correspondent']}"
    doc_id = entry.get("doc") if isinstance(entry, dict) else entry if isinstance(entry, int) else None
    if doc_id is None or session.get(Document, doc_id) is None:
        return None, ""
    if isinstance(entry, dict) and entry.get("amount") is not None:
        return doc_id, f"Rate {entry['amount']:.2f} €: immer dieses Dokument".replace(".", ",")
    return doc_id, "Dauerauftrag: immer dieses Dokument"


# --------------------------------------------------------------------------- ausgeblendete Empfänger


def ignored_counterparties(session: Session) -> list[str]:
    item = session.get(KeyValue, IGNORE_KEY)
    return json.loads(item.value) if item else []


def set_ignored_counterparties(session: Session, names: list[str]) -> None:
    names = sorted({n.strip() for n in names if n.strip()}, key=str.lower)
    item = session.get(KeyValue, IGNORE_KEY)
    if item:
        item.value = json.dumps(names)
    else:
        session.add(KeyValue(key=IGNORE_KEY, value=json.dumps(names)))


def _is_ignored(counterparty: str, ignored: list[str]) -> bool:
    cp = counterparty.lower()
    return any(name.lower() in cp for name in ignored)


# --------------------------------------------------------------------------- Belege suchen


def _linked_transactions(session: Session) -> dict[str, int]:
    """Buchungen, die bereits über den Zahlungsabgleich einer Rechnung zugeordnet sind (tx_id -> Dokument)."""
    linked: dict[str, int] = {}
    for m in session.scalars(select(PaymentMatch).where(PaymentMatch.state.in_(("accepted", "suggested")))
                             .options(selectinload(PaymentMatch.invoice))):
        if m.invoice is not None:
            linked[m.tx_id] = m.invoice.document_id
    for doc_id, sugg in session.execute(select(Document.id, Document.suggestions)):
        p = (sugg or {}).get("_payment") if isinstance(sugg, dict) else None
        if p and p.get("tx_id"):
            linked.setdefault(p["tx_id"], doc_id)
    return linked


def _doc_amount(doc: Document) -> float | None:
    """Betrag eines Dokuments: erfasste Rechnung (bei Teilzahlungen der Rest), sonst der erkannte Vorschlag."""
    inv = doc.invoice
    if inv is not None and inv.amount is not None:
        return inv.open_amount if inv.status == "open" else inv.amount
    value = ((doc.suggestions or {}).get("amount") or {}).get("value") if isinstance(doc.suggestions, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


MIN_SCORE = 50  # darunter kein Vorschlag


def score_document(tx: Transaction, doc: Document, known: set[str], paid_elsewhere: set[int]) -> tuple[int, list[str]] | None:
    """Wie gut passt ein Dokument als Beleg zur Buchung? Betrag, Empfänger, Rechnungsnummer und Datum zählen.

    Ein Dokument mit bekanntem, aber deutlich anderem Betrag scheidet aus – auch wenn der Absender passt
    (z.B. ein Schreiben der Stadtwerke über eine andere Summe)."""
    inv = doc.invoice
    ref = (inv.invoice_date if inv else None) or doc.created or (inv.due_date if inv else None)
    if doc.id in paid_elsewhere or ref is None:
        return None
    days = (tx.date - ref).days
    latest = (inv.due_date if inv and inv.due_date else ref) + timedelta(days=60 if inv else 30)
    if days < -10 or tx.date > latest:
        return None
    points, reasons = 0, []
    amount = _doc_amount(doc)
    if amount is not None:
        diff = abs(amount - tx.amount)
        if diff <= 0.01:
            points, reasons = points + 60, ["gleicher Betrag"]
        elif diff <= max(1.0, amount * 0.02):
            points, reasons = points + 25, [f"Betrag fast gleich ({amount:.2f} €)".replace(".", ",")]
        else:
            return None  # Beleg über einen anderen Betrag
    name = (inv.issuer if inv and inv.issuer else None) or doc.correspondent or ""
    if name and name.lower() in known:
        points, reasons = points + 35, [*reasons, "früher so zugeordnet"]
    elif name and (_words(tx.counterparty) & _words(name) or _compact(name) in _compact(tx.counterparty)):
        points, reasons = points + 35, [*reasons, "gleicher Absender" if amount is None else "Empfänger passt"]
    number = inv.invoice_number if inv else None
    if number and len(_compact(number)) >= 3 and _compact(number) in _compact(f"{tx.description} {tx.text}"):
        points, reasons = points + 40, [*reasons, "Rechnungsnr. im Verwendungszweck"]
    if amount is None and len(reasons) == 0:
        return None  # weder Betrag noch Absender: kein Hinweis
    points += max(0, 15 - abs(days) // 3)  # zeitlich nah: etwas besser
    reasons.append(f"{abs(days)} Tag{'e' if abs(days) != 1 else ''} {'vorher' if days >= 0 else 'nachher'}"
                   if days else "am selben Tag")
    return points, reasons


def candidates_for(session: Session, tx: Transaction, invoices: list[Invoice], docs: list[Document],
                   learned: dict | None = None, paid_elsewhere: set[int] | None = None) -> list[dict]:
    """Mögliche Belege, beste zuerst: Betrag (auch erkannte Beträge ungeprüfter Dokumente und Restbeträge nach
    Teilzahlungen), Empfänger bzw. gelernte Zuordnung, Rechnungsnummer im Verwendungszweck und zeitliche Nähe."""
    known = {c.lower() for c in ((learned or {}).get(party_key(tx.counterparty)) or {})}
    pool = {d.id: d for d in docs}
    for inv in invoices:
        if inv.document is not None:
            pool.setdefault(inv.document_id, inv.document)
    scored = []
    for doc in pool.values():
        if (result := score_document(tx, doc, known, paid_elsewhere or set())) and result[0] >= MIN_SCORE:
            scored.append((result[0], doc, result[1]))
    if not scored:
        return []
    scored.sort(key=lambda c: -c[0])
    best = scored[0][0]
    keep = [c for c in scored if c[0] >= best - 25][:5]  # deutlich schwächere Treffer nicht anzeigen
    return [{"id": d.id, "title": d.title, "reason": ", ".join(r), "score": p} for p, d, r in keep]


def text_candidates(session: Session, paperless, row: ReceiptCheck) -> list[dict]:
    """Betrag im Dokumenttext suchen (Paperless-Volltext, nur lesend) – findet auch Belege, die nie als Rechnung
    erfasst wurden. Zeitraum: 45 Tage vor bis 10 Tage nach der Buchung."""
    amount = f"{row.amount:.2f}".replace(".", ",")
    found = []
    for hit in paperless.search(f'"{amount}"', row.tx_date - timedelta(days=45), row.tx_date + timedelta(days=10), limit=5):
        doc = session.get(Document, hit["id"])
        found.append({"id": hit["id"], "title": (doc.title if doc else hit.get("title")) or f"#{hit['id']}",
                      "reason": f"Betrag {amount} € im Dokumenttext"})
    return found


def _name_match(name: str | None, tx: Transaction) -> bool:
    return bool(name) and bool(_words(name) & _words(tx.counterparty))


def check(session: Session, transactions: list[Transaction], settings: Settings, paperless=None) -> dict[str, int]:
    """Buchungen ab dem Mindestbetrag prüfen. Deine Entscheidungen (zugeordnet/ignoriert) bleiben unangetastet.

    Mit `paperless` wird für Buchungen ohne möglichen Beleg einmalig der Betrag im Volltext gesucht.
    """
    linked = _linked_transactions(session)
    ignored = ignored_counterparties(session)
    learned, fixed = aliases(session), fixed_documents(session)
    searches = 0
    start = min((tx.date for tx in transactions), default=date.today()) - timedelta(days=120)
    invoices = list(session.scalars(select(Invoice).options(selectinload(Invoice.document))))
    docs = list(session.scalars(select(Document).where(Document.created >= start, Document.kind != "contract")
                                .options(selectinload(Document.invoice))))
    # Rechnungen, die schon vollständig über eine Firefly-Buchung bezahlt sind (ohne Teilzahlungen): kein Beleg mehr
    paid_elsewhere = {m.invoice.document_id for m in session.scalars(
        select(PaymentMatch).where(PaymentMatch.state == "accepted").options(selectinload(PaymentMatch.invoice)))
        if m.invoice is not None and m.invoice.status == "paid"}
    existing = {r.tx_id: r for r in session.scalars(select(ReceiptCheck))}
    counts = {"checked": 0, "missing": 0, "candidate": 0, "linked": 0}
    for tx in transactions:
        if tx.amount < settings.receipts_min_amount:
            continue
        row = existing.get(tx.journal_id)
        if row is None:
            row = ReceiptCheck(tx_id=tx.journal_id, tx_group_id=tx.group_id, tx_date=tx.date, amount=tx.amount,
                               counterparty=tx.counterparty[:200], description=tx.description[:300])
            session.add(row)
            existing[tx.journal_id] = row
        counts["checked"] += 1
        if row.decided:
            continue
        if tx.journal_id in linked:
            row.state, row.document_id, row.candidates = "linked", linked[tx.journal_id], None
            row.note = "über den Zahlungsabgleich"
        elif _is_ignored(tx.counterparty, ignored):
            row.state, row.candidates, row.note = "ignored", None, "Empfänger ausgeblendet"
        elif (target := fixed_target(session, fixed.get(party_key(tx.counterparty)), tx.date, tx.amount))[0]:
            row.state, row.document_id, row.candidates = "linked", target[0], None
            row.note = target[1]
        elif cands := candidates_for(session, tx, invoices, docs, learned, paid_elsewhere - {linked.get(tx.journal_id)}):
            row.state, row.candidates, row.note = "candidate", cands, None
        else:
            cands = [c for c in row.candidates or [] if "Dokumenttext" in c.get("reason", "")]  # frühere Textsuche behalten
            if paperless is not None and not row.text_searched and searches < TEXT_SEARCH_PER_RUN:
                searches += 1
                row.text_searched = True
                try:
                    cands = text_candidates(session, paperless, row)
                except Exception:  # noqa: BLE001 - Suche ist nur ein Zusatz
                    cands = []
            row.state, row.candidates, row.note = ("candidate", cands, None) if cands else ("missing", None, None)
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


def run(session: Session, client: FireflyClient, settings: Settings, today: date, paperless=None) -> dict[str, int]:
    transactions = list(client.withdrawals(today - timedelta(days=settings.receipts_lookback_days), today))
    return check(session, transactions, settings, paperless)


# --------------------------------------------------------------------------- Entscheidungen


def link(session: Session, row: ReceiptCheck, doc_id: int) -> bool:
    doc = session.get(Document, doc_id)
    if doc is None:
        return False
    row.state, row.document_id, row.decided, row.note = "linked", doc_id, True, "von dir zugeordnet"
    # lernen: Buchungen dieses Empfängers gehören zu Dokumenten dieses Korrespondenten
    key = party_key(row.counterparty)
    if key and doc.correspondent:
        learned = aliases(session)
        counts = learned.setdefault(key, {})
        counts[doc.correspondent] = counts.get(doc.correspondent, 0) + 1
        _kv_set(session, ALIAS_KEY, learned)
    return True


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[0-9a-zäöüß]+", (text or "").lower()))


def search_documents(session: Session, row: ReceiptCheck, q: str, limit: int = 8) -> list[Document]:
    """Suche für die Buchung: Paperless-ID, Titel, Absender oder Dateiname im lokalen Index – egal ob Rechnung,
    unbearbeitet oder sonstiges. Genaue ID und genauer Titel zuerst, dann zeitlich nahe Dokumente."""
    needle = _norm(q)
    if len(needle) < 2 and not needle.isdigit():
        return []
    words = needle.split()
    by_id = session.get(Document, int(needle)) if needle.isdigit() else None
    hits = []
    for d in session.scalars(select(Document)):
        if d is by_id:
            continue
        title = _norm(d.title)
        hay = f"{title} {_norm(d.correspondent)} {_norm(d.original_file_name)}"
        if all(w in hay for w in words):
            rank = 0 if title == needle else 1 if title.startswith(needle) else 2 if needle in title else 3
            days = abs((row.tx_date - d.created).days) if d.created else 99999
            hits.append((rank, days, d))
    hits.sort(key=lambda h: h[:2])
    return ([by_id] if by_id else []) + [d for _, _, d in hits][:limit - (1 if by_id else 0)]


def linked_totals(session: Session) -> dict[int, tuple[int, float]]:
    """Je Dokument: wie viele Buchungen schon zugeordnet sind und ihre Summe (z.B. Raten, Abschläge)."""
    totals: dict[int, tuple[int, float]] = {}
    for r in session.scalars(select(ReceiptCheck).where(ReceiptCheck.state == "linked", ReceiptCheck.document_id.is_not(None))):
        n, total = totals.get(r.document_id, (0, 0.0))
        totals[r.document_id] = (n + 1, round(total + r.amount, 2))
    return totals


def ignore(row: ReceiptCheck, note: str = "kein Beleg nötig") -> None:
    row.state, row.decided, row.note = "ignored", True, note


def reopen(row: ReceiptCheck) -> None:
    row.decided, row.document_id, row.note = False, None, None
    row.state = "candidate" if row.candidates else "missing"


def overview(session: Session) -> dict:
    rows = list(session.scalars(select(ReceiptCheck)))
    counts = {state: 0 for state in STATES}
    for r in rows:
        counts[r.state] = counts.get(r.state, 0) + 1
    missing_sum = round(sum(r.amount for r in rows if r.state == "missing"), 2)
    return {"counts": counts, "missing_sum": missing_sum, "total": len(rows)}
