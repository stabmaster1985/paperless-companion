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


def fixed_documents(session: Session) -> dict[str, int]:
    return _kv(session, FIXED_KEY)


def set_fixed(session: Session, counterparty: str, doc_id: int | None) -> None:
    """Dauerauftrag: Buchungen an diesen Empfänger künftig immer diesem Dokument zuordnen (None = aufheben)."""
    fixed = fixed_documents(session)
    key = party_key(counterparty)
    if doc_id is None:
        fixed.pop(key, None)
    elif key:
        fixed[key] = doc_id
    _kv_set(session, FIXED_KEY, fixed)


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


def candidates_for(session: Session, tx: Transaction, invoices: list[Invoice], docs: list[Document],
                   learned: dict | None = None) -> list[dict]:
    """Mögliche Belege: Rechnung mit gleichem Betrag in zeitlicher Nähe, sonst Dokument des Empfängers in Zeitnähe –
    auch über gelernte Zuordnungen (Firefly-Name -> Korrespondent)."""
    found: list[dict] = []
    known = {c.lower() for c in ((learned or {}).get(party_key(tx.counterparty)) or {})}
    for inv in invoices:
        if inv.amount is None or abs(inv.amount - tx.amount) > 0.01:
            continue
        ref = inv.invoice_date or inv.due_date or (inv.document.created if inv.document else None)
        if ref and -10 <= (tx.date - ref).days <= 75:
            found.append({"id": inv.document_id, "title": inv.document.title if inv.document else "",
                          "reason": "gleicher Betrag" + (", Empfänger passt" if _name_match(inv.issuer, tx) else "")})
    if not found:
        words = _words(tx.counterparty)
        for doc in docs:
            if not doc.correspondent or not doc.created or abs((tx.date - doc.created).days) > 21:
                continue
            days = abs((tx.date - doc.created).days)
            if doc.correspondent.lower() in known:
                found.append({"id": doc.id, "title": doc.title, "reason": "früher so zugeordnet, Datum in der Nähe", "_days": days})
            elif words & _words(doc.correspondent) or _compact(doc.correspondent) in _compact(tx.counterparty):
                found.append({"id": doc.id, "title": doc.title, "reason": "gleicher Absender, Datum in der Nähe", "_days": days})
    # passender Empfänger zuerst, dann gelernte Zuordnungen, dann das zeitlich nächste Dokument
    found.sort(key=lambda c: ("Empfänger" not in c["reason"], "früher" not in c["reason"], c.get("_days", 0)))
    return [{k: v for k, v in c.items() if k != "_days"} for c in found[:5]]


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
    invoices = list(session.scalars(select(Invoice).options(selectinload(Invoice.document))))
    start = min((tx.date for tx in transactions), default=date.today()) - timedelta(days=30)
    docs = list(session.scalars(select(Document).where(Document.correspondent.is_not(None), Document.created >= start)))
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
        elif (fixed_id := fixed.get(party_key(tx.counterparty))) and session.get(Document, fixed_id) is not None:
            row.state, row.document_id, row.candidates = "linked", fixed_id, None
            row.note = "Dauerauftrag: immer dieses Dokument"
        elif cands := candidates_for(session, tx, invoices, docs, learned):
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


def search_documents(session: Session, row: ReceiptCheck, q: str, limit: int = 8) -> list[Document]:
    """Suche für die Buchungskarte: Titel, Absender oder ID im lokalen Index – zeitlich nahe Dokumente zuerst."""
    needle = q.strip().lower()
    if len(needle) < 2:
        return []
    hits = [d for d in session.scalars(select(Document))
            if needle in (d.title or "").lower() or needle in (d.correspondent or "").lower() or needle == str(d.id)]
    hits.sort(key=lambda d: abs((row.tx_date - d.created).days) if d.created else 99999)
    return hits[:limit]


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
