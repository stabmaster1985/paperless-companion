"""Bescheide: Erkennung, Zahlungspflicht und das Erhalten der Verknüpfungen beim Umstellen."""

from datetime import date

import pytest
from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient

from app import notices, recurring, seriesdocs
from app.db import session_scope
from app.extraction.rules import classify_by_keywords, extract_assessment
from app.firefly import Transaction
from app.main import create_app
from app.models import Contract, Document, Invoice, Notice, PaymentMatch, ReceiptCheck
from tests.test_pipeline import run_all

TEXT = """Finanzamt Musterstadt
Steuernummer: 123/456/78901
Bescheid für 2025 über Einkommensteuer
Datum: 12.05.2026
Nachzahlung 1.234,56 €
Bitte zahlen Sie bis zum 15.06.2026.
Festsetzung der Vorauszahlungen
Die Vorauszahlungen betragen ab 10.09.2026 vierteljährlich 850,00 €
"""


def test_detect_notice():
    assert classify_by_keywords("Einkommensteuerbescheid 2025", TEXT) == "notice"
    assert classify_by_keywords("Beihilfebescheid", "Beihilfebescheid\nErstattungsbetrag 20,00 €") == "reimbursement"
    found = {k: v["value"] for k, v in extract_assessment(TEXT).items()}
    assert found["balance_kind"] == "payment" and found["balance_amount"] == 1234.56 and found["balance_due"] == "2026-06-15"
    assert found["prepayment_amount"] == 850.0 and found["prepayment_interval"] == 3
    assert found["prepayment_from"] == "2026-09-10" and found["reference"] == "123/456/78901" and found["period"] == "2025"


def quarterly(n=4):
    return [Transaction(journal_id=f"fa{i}", group_id=f"gfa{i}", date=date(2025, 12, 10) + relativedelta(months=3 * i),
                        amount=800.0, counterparty="Finanzamt Musterstadt", iban=None, description="ESt-Vorauszahlung",
                        text="") for i in range(n)]


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_abo_becomes_notice_and_keeps_links(client):
    """Ein angelegtes Abo auf „Bescheid“ umstellen: Abbuchungen, gemerkte Suche und Belege bleiben."""
    txs = quarterly()
    with session_scope() as s:
        abo = Contract(name="ESt-Vorauszahlung", provider="Finanzamt", payee="Finanzamt Musterstadt",
                       booking_rule={"q": "vorauszahlung"})
        s.add(abo)
        s.flush()
        cid = abo.id
        recurring.assign(s, cid, txs)
        recurring.run(s, txs)
        # ein Bescheid, bisher als Rechnung erfasst und mit einer Buchung als Zahlung verknüpft
        doc = Document(id=990, title="Steuerbescheid 2024", correspondent="Finanzamt Musterstadt", kind="invoice", tags=[],
                       review_state="confirmed", created=date(2025, 11, 20))
        doc.invoice = Invoice(issuer="Finanzamt", amount=800.0, status="paid", paid_on=txs[0].date)
        s.add(doc)
        s.flush()
        s.add(PaymentMatch(invoice_id=doc.invoice.id, tx_id=txs[0].journal_id, tx_group_id=txs[0].group_id,
                           tx_date=txs[0].date, amount=800.0, counterparty="Finanzamt", description="", score=90,
                           reasons="von dir gewählt", state="accepted"))
    page = client.get(f"/contracts/{cid}").text
    assert "Bescheid (Vorauszahlung, Abgabe)" in page
    form = {"action": "save", "category": "notice", "name": "ESt-Vorauszahlung", "provider": "Finanzamt",
            "payee": "Finanzamt Musterstadt", "payment_amount": "800", "payment_interval": "3", "status": "active",
            "renewal_mode": "indefinite", "notice_to": "any"}
    client.post(f"/contracts/{cid}", data=form)
    with session_scope() as s:
        c = s.get(Contract, cid)
        assert c.is_notice and c.booking_rule == {"q": "vorauszahlung"}
        assert [x for x in recurring.stored(s) if x.contract_id == cid][0].tx_ids == [t.journal_id for t in txs]
    page = client.get(f"/contracts/{cid}").text
    assert "Bescheide" in page and "Steuerbescheid 2024" in page and "als Bescheid zuordnen" in page
    assert "Bescheid · Zahlungspflicht" in page and "<span>Kündigungsfrist</span>" not in page
    # Rechnung → Bescheid: die Zahlung bleibt als Beleg an der Buchung
    r = client.post(f"/contracts/{cid}/notice", data={"doc_id": "990"})
    assert "ist jetzt ein Bescheid" in r.text and "1 zugeordnete Buchung bleibt als Beleg verknüpft" in r.text
    with session_scope() as s:
        doc = s.get(Document, 990)
        assert doc.kind == "notice" and doc.invoice is None and doc.notice.contract_id == cid
        row = s.query(ReceiptCheck).filter_by(tx_id=txs[0].journal_id).one()
        assert row.document_id == 990 and row.decided
        assert seriesdocs.series_for_doc(s, 990)[0]["kind"] == "Bescheid"
    assert "Gehört zur Zahlungspflicht" in client.get("/documents/990").text
    lst = client.get("/contracts").text
    assert "keine Kündigung" in lst


def test_contract_document_to_notice_keeps_obligation(client):
    """Ein Abo, an das der Bescheid als Vertragsdokument gehängt wurde: Art „Bescheid“ wählen – der Eintrag bleibt
    (mit seinen Abbuchungen) und der Bescheid hängt daran."""
    txs = quarterly(3)
    with session_scope() as s:
        doc = Document(id=991, title="Grundsteuerbescheid", correspondent="Stadtkasse", kind="contract", tags=[],
                       review_state="confirmed", created=date(2025, 1, 15))
        doc.contract = Contract(name="Grundsteuer", provider="Stadtkasse")
        s.add(doc)
        s.flush()
        cid = doc.contract.id
        recurring.assign(s, cid, txs)
    r = client.post("/documents/991/kind", data={"kind": "notice"})
    assert "ist jetzt eine Zahlungspflicht" in r.text
    with session_scope() as s:
        c = s.get(Contract, cid)
        assert c is not None and c.document_id is None and c.is_notice and c.payee == "Stadtkasse"
        assert s.get(Document, 991).notice.contract_id == cid
        assert len([x for x in recurring.stored(s) if x.contract_id == cid][0].tx_ids) == 3
    # Art wieder auf Sonstiges: der Eintrag bleibt trotzdem
    client.post("/documents/991/kind", data={"kind": "other"})
    with session_scope() as s:
        assert s.get(Contract, cid) is not None and s.get(Document, 991).notice is None


def test_confirm_notice_creates_obligation_and_sets_payment(client):
    with session_scope() as s:
        s.add(Document(id=992, title="Vorauszahlungsbescheid", correspondent="Finanzamt", kind="notice", tags=[],
                       review_state="pending", created=date(2026, 5, 12), suggestions={}))
    form = {"authority": "Finanzamt", "notice_date": "2026-05-12", "period": "2026", "balance_kind": "none",
            "prepayment_amount": "850,00", "prepayment_interval": "3", "prepayment_from": "2026-09-10",
            "contract_id": "new", "new_obligation": "ESt-Vorauszahlung"}
    client.post("/documents/992/confirm", data=form)
    with session_scope() as s:
        n = s.get(Document, 992).notice
        c = s.get(Contract, n.contract_id)
        assert c.name == "ESt-Vorauszahlung" and c.is_notice and c.payment == 850.0 and c.interval == 3
        assert notices.next_dates(n, date(2026, 11, 27))[:2] == [date(2026, 12, 10), date(2027, 3, 10)]
        cid = c.id
    # älterer Bescheid ändert den Betrag nicht
    with session_scope() as s:
        old = Document(id=993, title="Vorauszahlungsbescheid alt", kind="notice", tags=[], review_state="confirmed")
        old.notice = Notice(contract_id=cid, prepayment_amount=700.0, prepayment_interval=3,
                            prepayment_from=date(2025, 3, 10), notice_date=date(2025, 2, 1))
        s.add(old)
        s.flush()
        assert notices.sync_contract(s, old.notice) is None
        assert s.get(Contract, cid).payment == 850.0
    page = client.get(f"/contracts/{cid}").text
    assert "Nächste Vorauszahlungen" in page and "Vorauszahlungsbescheid alt" in page
    from app import events

    with session_scope() as s:
        evs = [e for e in events.collect(s, date(2026, 11, 27)) if e.type == "prepayment"]
        assert [(e.date, e.amount) for e in evs] == [(date(2026, 12, 10), 850.0)]
        assert not [e for e in events.collect(s, date(2026, 11, 27)) if e.contract_id == cid and e.type == "cancel"]
