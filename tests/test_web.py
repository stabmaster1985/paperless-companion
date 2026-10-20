import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import create_app
from app.models import Document
from tests.test_pipeline import run_all


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_pages_render(client):
    for path in ("/", "/inbox", "/contracts", "/invoices", "/invoices?status=all", "/warranties",
                 "/documents", "/documents/1", "/documents/2", "/documents/3", "/system"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert "Stadtwerke" in client.get("/invoices").text
    assert "Handyvertrag" in client.get("/inbox").text


def test_confirm_contract_flow(client):
    form = {
        "name": "Handyvertrag", "provider": "Mobilfunk AG", "start_date": "2025-02-01",
        "initial_term_months": "24", "renewal_mode": "renews", "renewal_months": "12",
        "notice_value": "3", "notice_unit": "months", "notice_to": "term_end",
        "monthly_cost": "19,99", "status": "active", "next": "/inbox",
    }
    response = client.post("/documents/2/confirm", data=form, follow_redirects=False)
    assert response.status_code == 303
    with session_scope() as s:
        doc = s.get(Document, 2)
        assert doc.review_state == "confirmed" and doc.contract.monthly_cost == 19.99
    page = client.get("/contracts").text
    assert "Handyvertrag" in page and "19,99 €" in page

    contract_id = 1
    client.post(f"/contracts/{contract_id}/cancelled", data={"cancelled_on": "2026-10-20"})
    assert "gekündigt 20.10.2026" in client.get("/contracts").text


def test_change_kind_and_mark_paid(client):
    client.post("/documents/3/kind", data={"kind": "warranty"})
    with session_scope() as s:
        doc = s.get(Document, 3)
        assert doc.kind == "warranty" and doc.kind_source == "manual" and doc.extraction_state == "queued"
    client.post("/invoices/1/paid")
    assert "Stadtwerke" in client.get("/invoices?status=paid").text


def test_webhook_and_ics_tokens(client):
    assert client.post("/api/webhook/paperless", json={"doc_url": "x"}).status_code == 403
    response = client.post("/api/webhook/paperless?token=hook", json={"doc_url": "http://p/documents/2/details"})
    assert response.json() == {"status": "queued", "document": 2}
    assert client.get("/calendar.ics").status_code == 403
    response = client.get("/calendar.ics?token=cal")
    assert response.status_code == 200 and "BEGIN:VCALENDAR" in response.text


def test_login_required(settings, mock_api):
    settings.app_password = "geheim"
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 303
        assert c.post("/login", data={"password": "falsch"}).status_code == 401
        assert c.post("/login", data={"password": "geheim"}, follow_redirects=False).status_code == 303
        assert c.get("/").status_code == 200
        assert c.get("/health").status_code == 200


def test_document_search(client):
    page = client.get("/documents?q=strom").text
    assert "Stromrechnung Oktober" in page and "Handyvertrag" not in page
