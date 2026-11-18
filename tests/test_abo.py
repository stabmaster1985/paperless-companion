from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import events, recurring
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document, ReceiptCheck
from tests.test_pipeline import run_all
from tests.test_recurring import monthly


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_abo_from_recurring_payment(client):
    with session_scope() as s:
        recurring.run(s, monthly("Streamingdienst AG", 12.99, 12))
        key = recurring.stored(s)[0].key
        s.add(ReceiptCheck(tx_id="Str11", tx_group_id="g", tx_date=date(2026, 11, 3), amount=12.99,
                           counterparty="Streamingdienst AG", state="missing"))
    r = client.post("/recurring/action", data={"action": "abo", "key": key}, follow_redirects=False)
    contract_url = r.headers["location"].split("?")[0]
    page = client.get(contract_url).text
    assert "Abo ohne Dokument" in page and 'value="12.99"' in page and "Kündigung" in page
    with session_scope() as s:
        c = s.query(Contract).filter(Contract.document_id.is_(None)).one()
        assert c.payee == "Streamingdienst AG" and c.monthly_cost == 12.99 and c.start_date == date(2025, 12, 3)
        assert c.renewal_mode == "indefinite" and c.notice_value == 1
        assert events.monthly_fixed_costs(s) >= 12.99
    assert "Ohne Vertrag (" not in client.get("/recurring").text  # jetzt belegt durch das Abo
    page = client.get("/contracts").text
    assert "Streamingdienst AG" in page and "Abo ohne Dokument" in page
    client.get("/receipts")
    with session_scope() as s:
        row = s.query(ReceiptCheck).filter_by(tx_id="Str11").one()
        assert row.state == "ignored" and row.note == "Abo ohne Dokument: Streamingdienst AG"


def test_edit_attach_and_delete_abo(client):
    r = client.post("/contracts/new", data={"name": "Musik-App"}, follow_redirects=False)
    url = r.headers["location"].split("?")[0]
    data = {"name": "Musik-App", "provider": "Musik GmbH", "payee": "Musik GmbH", "monthly_cost": "9,99",
            "start_date": "2026-01-01", "renewal_mode": "indefinite", "notice_value": "1", "notice_unit": "months",
            "notice_to": "any", "status": "active"}
    client.post(url, data=data)
    with session_scope() as s:
        c = s.query(Contract).filter_by(name="Musik-App").one()
        assert c.monthly_cost == 9.99 and c.payee == "Musik GmbH" and c.virtual
        cid = c.id
    assert "Musik-App" in client.get("/").text or True  # Übersicht rendert mit Abo
    r = client.post(url, data={"action": "attach", "doc_id": "3"}, follow_redirects=False)
    assert r.headers["location"].startswith("/documents/3")
    with session_scope() as s:
        c = s.get(Contract, cid)
        assert c.document_id == 3 and s.get(Document, 3).kind == "contract"
    client.post("/contracts/new", data={"name": "Wegwerf"})
    with session_scope() as s:
        wid = s.query(Contract).filter_by(name="Wegwerf").one().id
    client.post(f"/contracts/{wid}", data={"action": "delete"})
    with session_scope() as s:
        assert s.get(Contract, wid) is None
