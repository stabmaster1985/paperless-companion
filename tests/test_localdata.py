import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import localdata
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document, Invoice
from tests.test_pipeline import run_all


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_inventory_separates_lost_from_recoverable(client, settings):
    with session_scope() as s:
        kept = Document(id=990, title="Rechnung mit Feldern", tags=[], kind="invoice", review_state="confirmed",
                        paperless_fields={"due_date": "2026-10-01", "amount": 50.0})
        kept.invoice = Invoice(amount=50.0, due_date=date(2026, 10, 1), status="open")
        lost = Document(id=991, title="Rechnung nur im Companion", tags=[], kind="invoice", review_state="confirmed")
        lost.invoice = Invoice(amount=80.0, status="paid", paid_on=date(2026, 10, 2))
        s.add_all([kept, lost, Contract(name="Netflix", payee="Netflix")])
        s.get(Document, 2).proposed_fields = {"related": [1]}
    with session_scope() as s:
        sections = {sec.key: sec for sec in localdata.inventory(s, settings, date(2026, 11, 24))}
        only = {i.label: i for i in sections["only"].items}
        assert only["Erfasste Rechnungen"].count >= 1 and "aus deinen Paperless-Feldern zurück" in only["Erfasste Rechnungen"].detail
        assert only["Abos ohne Dokument"].count == 1
        pending = {i.label: i.count for i in sections["pending"].items}
        assert pending["Vorgemerkte Verknüpfungen „gehört zu“"] == 1
        assert sections["regen"].items[0].count > 0
    page = client.get("/local-data").text
    assert "Was steht nur im Companion?" in page and "Sicherung herunterladen" in page and "Abos ohne Dokument" in page


def test_backup_download_is_a_working_database(client, tmp_path):
    r = client.get("/local-data/backup")
    assert r.status_code == 200 and "companion-sicherung-" in r.headers["content-disposition"]
    copy = tmp_path / "copy.db"
    copy.write_bytes(r.content)
    con = sqlite3.connect(copy)
    assert con.execute("SELECT count(*) FROM documents").fetchone()[0] > 0
    con.close()


def test_pending_age_and_transfer_all(client, mock_api, settings):
    from app import pipeline

    with session_scope() as s:
        doc = s.get(Document, 1)
        pipeline.propose(doc, "related", [2])
        sources = dict(doc.field_sources or {})
        sources["proposal:related"] = {**sources["proposal:related"], "at": "2026-09-01T10:00"}
        doc.field_sources = sources
    page = client.get("/local-data").text
    assert "Älteste Vormerkung vom 01.09.2026" in page and "Jetzt alles übertragen" in page
    r = client.post("/local-data/transfer-all")
    assert "Feldänderungen übertragen" in r.text
    calls = [c for c in mock_api.calls if c.request.method == "PATCH"]
    assert calls  # nach Paperless geschrieben – nur über diesen Klick
