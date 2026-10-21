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


def test_unwritable_data_dir_gives_clear_error(tmp_path):
    import pytest

    from app.main import ensure_writable

    blocker = tmp_path / "data"
    blocker.write_text("keine Ordner")  # mkdir schlägt fehl wie bei fehlenden Rechten
    with pytest.raises(RuntimeError, match="chown -R 1000:1000"):
        ensure_writable(blocker)
    ensure_writable(tmp_path / "ok")


def test_retry_llm_button(client):
    from app.db import session_scope as scope

    with scope() as s:
        doc = s.get(Document, 2)
        doc.suggestions = {**doc.suggestions, "_llm_error": {"value": "No route to host", "attempts": 5}}
    assert "Jetzt mit AI neu analysieren" in client.get("/system").text
    assert "war die AI nicht erreichbar" in client.get("/documents/2").text
    client.post("/system/retry-llm")
    with scope() as s:
        assert s.get(Document, 2).extraction_state == "queued"


def test_pause_resume_queue(client):
    client.post("/system/queue/pause")
    page = client.get("/system").text
    assert "Angehalten" in page and "Fortsetzen" in page
    assert client.app.state.worker.paused()
    client.post("/system/queue/resume")
    assert not client.app.state.worker.paused()


def test_activity_log(client, mock_api):
    worker = client.app.state.worker
    info = {"kind": "invoice", "ai": "used", "ai_seconds": 42.5, "ai_fields": ["due_date"], "wanted": ["due_date", "iban"],
            "review": "pending"}
    worker.note(worker._describe("Stromrechnung", info, 43.0), document_id=1)
    assert worker.stats["ai_calls"] == 1
    mock_api.get("http://ollama.test/api/ps").respond(
        json={"models": [{"name": "qwen2.5:3b", "size": 2_400_000_000, "expires_at": "2026-10-21T10:00:00Z"}]})
    html = client.get("/system/activity").text
    assert "Regeln + AI (42.5 s, 1 von 2 fehlenden Feldern gefunden) → Inbox" in html
    assert "qwen2.5:3b</code> im Speicher (2.4 GB)" in html and "1 AI-Abfragen" in html
    assert 'id="activity"' in client.get("/system").text
