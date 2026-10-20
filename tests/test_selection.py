from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import pipeline
from app.db import session_scope
from app.main import create_app
from app.models import Document
from app.selection import Filter, parse_ids


def test_parse_ids():
    assert parse_ids("12, 15-18 20;3") == [3, 12, 15, 16, 17, 18, 20]
    assert parse_ids("18-15") == [15, 16, 17, 18]
    assert parse_ids("abc, ") == []


def add(s, doc_id, **kw):
    defaults = dict(title=f"Dok {doc_id}", suggestions={}, kind="other", kind_source="keyword",
                    review_state="none", extraction_state="skipped", tags=[])
    s.add(Document(id=doc_id, **{**defaults, **kw}))


def test_methods(db):
    with db() as s:
        add(s, 1)
        add(s, 2, extraction_state="done", analyzed_at=datetime(2026, 10, 1),
            suggestions={"amount": {"value": 1, "confidence": .9, "source": "rule"}})
        add(s, 3, extraction_state="done", suggestions={"amount": {"value": 1, "confidence": .6, "source": "llm"}})
        add(s, 4, extraction_state="done", suggestions={"_llm_error": {"value": "weg"}})
        add(s, 5, kind="invoice", kind_source="fields")
    with db() as s:
        assert [pipeline.analysis_method(s.get(Document, i)) for i in range(1, 6)] == [
            "none", "rules", "rules_ai", "rules_ai_failed", "fields"]


def test_filter(db):
    with db() as s:
        add(s, 1, tags=["Steuer", "Haus"], document_type="Rechnung", correspondent="Stadtwerke", created=date(2024, 3, 1))
        add(s, 2, tags=["Haus"], document_type="Vertrag", created=date(2025, 6, 1))
        add(s, 3, tags=["steuer"], created=date(2026, 1, 1))
    with db() as s:
        ids = lambda **kw: sorted(d.id for d in Filter(**kw).apply(s))
        assert ids(tag="Steuer") == [1, 3]
        assert ids(doctype="Vertrag") == [2]
        assert ids(correspondent="Stadtwerke") == [1]
        assert ids(date_from="2025-01-01") == [2, 3]
        assert ids(date_from="2024-01-01", date_to="2025-12-31") == [1, 2]
        assert ids(ids="2-3") == [2, 3]
        assert ids(method="none") == [1, 2, 3]
        assert ids(tag="Haus", date_to="2024-12-31") == [1]


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            add(s, 10, tags=["Steuer"], created=date(2024, 3, 1))
            add(s, 11, tags=["Steuer"], created=date(2024, 4, 1), review_state="confirmed", extraction_state="done")
            add(s, 12, tags=["Haus"], created=date(2024, 5, 1))
        yield c


def test_documents_page_and_bulk_analyze(client):
    page = client.get("/documents?tag=Steuer").text
    assert "2 Dokumente" in page and "nicht analysiert" in page and "Dok 12" not in page

    response = client.post("/documents/analyze", data={"scope": "filter", "tag": "Steuer"}, follow_redirects=False)
    assert "tag=Steuer" in response.headers["location"] and "best%C3%A4tigte" in response.headers["location"]
    with session_scope() as s:
        assert s.get(Document, 10).extraction_state == "queued"
        assert s.get(Document, 11).extraction_state == "done"  # bestätigt -> übersprungen
        assert s.get(Document, 12).extraction_state == "skipped"

    client.post("/documents/analyze", data={"scope": "selected", "doc": ["11", "12"], "include_confirmed": "1"})
    with session_scope() as s:
        assert s.get(Document, 11).extraction_state == "queued"
        assert s.get(Document, 12).extraction_state == "queued"


def test_outdated_analysis(client):
    with session_scope() as s:
        add(s, 20, extraction_state="done", review_state="pending", analysis_version=1)
        add(s, 21, extraction_state="done", review_state="confirmed", analysis_version=1)
        add(s, 22, extraction_state="done", review_state="pending", analysis_version=pipeline.ANALYSIS_VERSION)
    assert "1 nicht bestätigte Dokument wurde mit einer älteren Version" in client.get("/system").text
    client.post("/system/reanalyze-outdated")
    with session_scope() as s:
        assert s.get(Document, 20).extraction_state == "queued"
        assert s.get(Document, 21).extraction_state == "done"
        assert s.get(Document, 22).extraction_state == "done"


def test_analysis_stamps_version(db, settings, mock_api):
    from tests.test_pipeline import run_all

    run_all(db, settings)
    with db() as s:
        doc = s.get(Document, 1)
        assert doc.analysis_version == pipeline.ANALYSIS_VERSION and doc.analyzed_at is not None
        assert pipeline.analysis_method(doc) == "rules"
