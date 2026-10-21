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


def test_review_page_with_preview_and_highlights(client):
    page = client.get("/documents/1").text
    assert 'src="/documents/1/preview#view=FitH"' in page
    assert '<mark class="hl hl-amount" title="Betrag">78,00</mark>' in page and "Text (" in page
    response = client.get("/documents/1/preview")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert client.get("/documents/1/preview?kind=evil").status_code == 404
    assert "Prüfung starten" in client.get("/inbox").text


def test_confirm_and_next(client):
    from app.db import session_scope as scope

    with scope() as s:
        s.get(Document, 3).review_state = "pending"
    response = client.post("/documents/2/confirm", data={"name": "Handyvertrag", "after": "next"}, follow_redirects=False)
    assert response.headers["location"].startswith("/documents/3?msg=")
    response = client.post("/documents/3/confirm", data={"amount": "249,90", "after": "next"}, follow_redirects=False)
    assert response.headers["location"].startswith("/inbox?msg=")


def test_field_provenance(client):
    from app.db import session_scope as scope

    # Automatisch übernommen: Herkunft aus den Vorschlägen
    page = client.get("/documents/1").text
    assert 'class="badge src-rule ' in page and "Regel 90 %" in page
    # Bestätigen mit einer Änderung: geänderter Wert = manuell, unveränderter = bestätigt
    with scope() as s:
        doc = s.get(Document, 3)
        sugg = doc.suggestions
    form = {k: v["value"] for k, v in sugg.items() if not k.startswith("_") and v.get("value") is not None}
    form["invoice_number"] = "XY-99"
    client.post("/documents/3/confirm", data=form)
    with scope() as s:
        sources = s.get(Document, 3).field_sources
        assert sources["invoice_number"]["source"] == "manual"
        assert sources["amount"]["source"] == "rule" and sources["amount"]["confirmed"] is True
    page = client.get("/documents/3").text
    assert "manuell</span>" in page and "Regel ✓" in page
    # Als bezahlt markiert -> manuell, in der Rechnungsliste sichtbar
    client.post("/invoices/1/paid")
    assert "manuell</span>" in client.get("/invoices?status=paid").text


def test_doctype_settings_page(client):
    from app.db import session_scope as scope

    page = client.get("/settings").text
    assert "Rechnung" in page and "Vertrag" in page and 'name="kind:rechnung"' in page
    # Vertrag soll künftig nicht ausgewertet werden, Rechnung wird steuerrelevant
    response = client.post("/settings/doctypes", data={"name": ["rechnung", "vertrag"], "kind:rechnung": "invoice",
                                                       "kind:vertrag": "", "tax:rechnung": "1"}, follow_redirects=False)
    assert response.status_code == 303
    settings = client.app.state.settings
    assert settings.doctype_map() == {"rechnung": "invoice"} and settings.tax_doctype_list == ["rechnung"]
    with scope() as s:
        doc = s.get(Document, 2)
        assert doc.document_type == "Vertrag" and doc.extraction_state == "queued"
        assert s.get(Document, 1).extraction_state == "done"
    page = client.get("/settings").text
    assert '<option value="" selected>nicht auswerten</option>' in page and "Auf Werte aus der .env zurücksetzen" in page
    client.post("/settings/doctypes/reset")
    assert settings.doctype_map()["vertrag"] == "contract"


def test_doctype_settings_survive_restart(settings, mock_api):
    from app.db import session_scope as scope
    from app import pipeline

    app = create_app(settings, start_worker=False)
    with TestClient(app):
        with scope() as s:
            pipeline.save_doctype_settings(s, settings, {"Beleg": "invoice"}, [])
    settings.doctypes_invoice = "Rechnung"
    with TestClient(create_app(settings, start_worker=False)):
        assert settings.doctype_map() == {"beleg": "invoice"}


def test_doctype_settings_without_paperless_types(client, mock_api):
    """Liefert Paperless keine Typen (fehlende Leserechte), Hinweis + Rückgriff auf den lokalen Index."""
    mock_api.get("http://paperless.test/api/document_types/").respond(
        json={"count": 0, "next": None, "previous": None, "results": []})
    mock_api.get("http://paperless.test/api/ui_settings/").respond(
        json={"user": {"username": "companion", "is_superuser": False}, "permissions": ["view_document", "view_tag"]})
    page = client.get("/settings").text
    assert "Dem Paperless-Benutzer „companion“ fehlt das Recht „Anzeigen“ für: Dokumenttyp, Korrespondent" in page
    assert 'name="kind:rechnung"' in page and "Archiv-Index neu einlesen" in page
    assert client.post("/settings/reindex", follow_redirects=False).status_code == 303


def test_app_settings_prefilled_saved_and_reset(client):
    from app.db import session_scope as scope
    from app import appsettings

    settings = client.app.state.settings
    page = client.get("/settings").text
    # Vorbelegt mit den Werten aus der .env, Geheimnisse nicht im Klartext
    assert 'name="ollama_model" value="qwen2.5:3b"' in page and 'value="secret"' not in page
    form = {"ollama_model": "llama3.2:3b", "llm_active_hours": "22-6", "present:llm_auto_accept": "1",
            "paperless_token": "", "sync_interval_minutes": "abc", "meta_mode": "manual"}
    response = client.post("/settings/app", data=form, follow_redirects=False)
    assert "Nicht%20%C3%BCbernommen" in response.headers["location"]  # ungültige Zahl gemeldet
    assert settings.ollama_model == "llama3.2:3b" and settings.llm_active_hours == "22-6" and settings.meta_mode == "manual"
    assert settings.paperless_token == "secret" and settings.sync_interval_minutes == 15
    with scope() as s:
        assert appsettings.stored(s) == {"ollama_model": "llama3.2:3b", "llm_active_hours": "22-6", "meta_mode": "manual"}
    assert "zurücksetzen" in client.get("/settings").text
    # Zurück auf den .env-Wert -> Eintrag entfällt
    client.post("/settings/app", data={"ollama_model": "qwen2.5:3b"})
    client.post("/settings/app/reset", data={"name": "meta_mode"})
    assert settings.ollama_model == "qwen2.5:3b" and settings.meta_mode == "new"
    with scope() as s:
        assert appsettings.stored(s) == {"llm_active_hours": "22-6"}
    client.post("/settings/app", data={"timezone": "Mars/Olympus"})
    assert settings.timezone == "Europe/Berlin"


def test_app_settings_survive_restart(settings, mock_api):
    from app.db import session_scope as scope
    from app import appsettings

    with TestClient(create_app(settings, start_worker=False)):
        with scope() as s:
            appsettings.save(s, settings, {"firefly_dashboard_url": ""}, {"firefly_dashboard_url": "http://dash"})
    settings.firefly_dashboard_url = ""
    with TestClient(create_app(settings, start_worker=False)) as c:
        assert settings.firefly_dashboard_url == "http://dash"
        assert "http://dash" in c.get("/settings").text
