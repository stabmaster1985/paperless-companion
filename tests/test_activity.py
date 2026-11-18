from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import activity
from app.db import session_scope
from app.main import create_app
from app.models import Activity, Document, ReceiptCheck
from tests.test_pipeline import run_all


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_actions_are_recorded_with_document_and_target(client):
    with session_scope() as s:
        s.add(ReceiptCheck(tx_id="h1", tx_group_id="g1", tx_date=date(2026, 10, 1), amount=78.0, counterparty="Stadtwerke",
                           state="missing"))
    with session_scope() as s:
        row_id = s.query(ReceiptCheck).filter_by(tx_id="h1").one().id
    client.post(f"/receipts/{row_id}/link", data={"doc_id": "1"})
    client.post("/documents/2/kind", data={"kind": "warranty"})
    activity.drain()
    with session_scope() as s:
        entries = activity.recent(s)
        assert entries[0].path == "/documents/2/kind" and entries[0].document_id == 2
        assert entries[1].document_id == 1 and entries[1].text.startswith("Beleg zugeordnet")
        assert entries[1].target == "/receipts?state=open"  # ohne Meldung
    page = client.get("/history").text
    assert "Beleg zugeordnet" in page and "Stromrechnung Oktober" in page and "jetzt:" in page and "heute" in page
    assert "Verlauf dieses Dokuments" in client.get("/documents/1").text


def test_json_actions_and_limit(client, monkeypatch):
    data = {"tx_id": "9", "group_id": "g9", "date": "2026-10-10", "amount": "78.0", "counterparty": "Stadtwerke"}
    r = client.post("/documents/1/payment", data=data, headers={"X-Requested-With": "fetch"})
    assert r.json()["msg"]  # Antwort bleibt unverändert lesbar
    activity.drain()
    with session_scope() as s:
        assert "Firefly-Buchung vom 10.10.2026" in activity.recent(s)[0].text
    monkeypatch.setattr(activity, "KEEP", 3)
    for _ in range(5):
        client.post("/documents/1/hold", data={"hold": "1"})
    activity.drain()
    with session_scope() as s:
        assert s.query(Activity).count() == 3
