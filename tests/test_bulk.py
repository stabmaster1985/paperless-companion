from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document, Invoice, PaymentMatch, ReceiptCheck
from tests.test_pipeline import run_all


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def seed(s):
    inv_doc = Document(id=700, title="Rechnung Möbel", correspondent="Möbelhaus", kind="invoice", tags=[], review_state="confirmed")
    inv_doc.invoice = Invoice(issuer="Möbelhaus", amount=300.0, status="open", invoice_number="RE-77")
    s.add(inv_doc)
    s.flush()
    s.add(PaymentMatch(invoice_id=inv_doc.invoice.id, tx_id="t1", tx_group_id="g1", tx_date=date(2026, 10, 2), amount=300.0,
                       counterparty="Möbelhaus", score=115, reasons="Betrag, Rechnungsnr., Empfänger", state="suggested"))
    s.add(ReceiptCheck(tx_id="t2", tx_group_id="g2", tx_date=date(2026, 10, 3), amount=49.99, counterparty="Shop", state="candidate",
                       candidates=[{"id": 701, "title": "Bestellung", "reason": "gleicher Betrag, Empfänger passt, 1 Tag vorher", "score": 110}]))
    s.add(Document(id=701, title="Bestellung", tags=[], kind="other"))
    s.add(ReceiptCheck(tx_id="t3", tx_group_id="g3", tx_date=date(2026, 10, 4), amount=20.0, counterparty="Kiosk", state="candidate",
                       candidates=[{"id": 701, "title": "Bestellung", "reason": "gleicher Absender, 3 Tage vorher", "score": 50}]))
    s.add(Document(id=702, title="Abschlag Gas", correspondent="Gaswerk", kind="invoice", tags=[], review_state="pending",
                   extraction_state="done", suggestions={"amount": {"value": 45.0, "confidence": 0.9, "source": "rule"},
                                                         "due_date": {"value": "2026-10-15", "confidence": 0.9, "source": "rule"}}))
    contract_doc = Document(id=703, title="Handyvertrag", correspondent="Mobilfunk", kind="contract", tags=[], review_state="confirmed")
    contract_doc.contract = Contract(name="Handyvertrag", provider="Mobilfunk")
    s.add(contract_doc)
    s.flush()
    s.add(Document(id=704, title="Kündigungsbestätigung", correspondent="Mobilfunk", tags=[], kind="other", created=date(2026, 10, 5),
                   suggestions={"_cancellation": {"contract": contract_doc.contract.id, "effective": "2026-12-31", "date": "2026-10-05"}}))


def test_bulk_page_preselects_sure_items_and_applies(client):
    with session_scope() as s:
        seed(s)
    page = client.get("/bulk").text
    assert "Zahlungen zu offenen Rechnungen" in page and "Belege zu Buchungen" in page
    assert "Inbox: vollständige Dokumente" in page and "Kündigungsbestätigungen" in page
    assert 'value="rc:' in page
    with session_scope() as s:
        pay_id = s.query(PaymentMatch).filter_by(tx_id="t1").one().id
        rc_sure = s.query(ReceiptCheck).filter_by(tx_id="t2").one().id
        rc_unsure = s.query(ReceiptCheck).filter_by(tx_id="t3").one().id
        contract_id = s.get(Document, 703).contract.id
    assert f'value="pay:{pay_id}" checked' in page and f'value="rc:{rc_sure}:701" checked' in page
    assert f'value="rc:{rc_unsure}:701" data-sure="0"' in page  # unsicher: nicht vorausgewählt
    picks = [f"pay:{pay_id}", f"rc:{rc_sure}:701", "doc:702", f"cancel:704:{contract_id}"]
    r = client.post("/bulk", data={"pick": picks})
    assert "1 Zahlungen übernommen, 1 Belege zugeordnet, 1 Dokumente bestätigt, 1 Verträge als gekündigt markiert" in r.text
    with session_scope() as s:
        assert s.get(Document, 700).invoice.status == "paid"
        assert s.query(ReceiptCheck).filter_by(tx_id="t2").one().document_id == 701
        assert s.query(ReceiptCheck).filter_by(tx_id="t3").one().state == "candidate"
        assert s.get(Document, 702).review_state == "confirmed" and s.get(Document, 702).invoice.amount == 45.0
        assert s.get(Contract, contract_id).status == "cancelled"
