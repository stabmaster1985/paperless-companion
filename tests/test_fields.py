import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cases, fields, pipeline
from app.db import session_scope
from app.main import create_app
from app.models import Case, Document, Invoice
from app.ollama import OllamaClient
from app.paperless import PaperlessClient
from tests.conftest import OLLAMA, PAPERLESS


@pytest.fixture
def my_settings(settings):
    settings.field_due_date = "bezahlbar bis"
    settings.field_paid_on = "bezahlt am"
    return settings


def names_for(settings):
    return pipeline.load_names(PaperlessClient(PAPERLESS, "x"), settings)


def meta(doc_id, custom_fields, **kw):
    return {"id": doc_id, "title": f"Dok {doc_id}", "correspondent": 10, "document_type": None, "tags": [],
            "created": "2026-09-01", "modified": "2026-10-01T10:00:00Z", "custom_fields": custom_fields, **kw}


def patches(mock_api):
    return [json.loads(c.request.content) for c in mock_api.calls if c.request.method == "PATCH"]


def test_codec():
    money = fields.FieldDef(1, "Betrag", "monetary")
    assert fields.decode(money, "EUR12.50") == 12.5 and fields.decode(money, "7.1") == 7.1
    assert fields.encode(money, 12.5) == "EUR12.50"
    text = fields.FieldDef(2, "Kommentar", "string")
    assert len(fields.encode(text, "Wort " * 60)) <= 128
    link = fields.FieldDef(3, "gehört zu", "documentlink")
    assert fields.decode(link, [3, "4"]) == [3, 4] and fields.encode(link, {5, 4}) == [4, 5]
    select = fields.FieldDef(4, "Status", "select", {"select_options": [{"id": "a1", "label": "offen"}]})
    assert fields.decode(select, "a1") == "offen"


def test_index_creates_invoices_from_user_fields(db, my_settings, mock_api):
    names = names_for(my_settings)
    assert {r: fd.name for r, fd in names["fields"].items()} == {
        "amount": "Betrag", "due_date": "bezahlbar bis", "paid_on": "bezahlt am", "comment": "Kommentar",
        "related": "gehört zu", "summary": "Zusammenfassung KI",
    }
    with db() as s:
        open_doc = pipeline.upsert_document(s, meta(20, [{"field": 40, "value": "2026-11-01"}]), names, analyze=False)
        paid_doc = pipeline.upsert_document(s, meta(21, [{"field": 40, "value": "2026-09-15"}, {"field": 41, "value": "2026-09-10"},
                                                         {"field": 1, "value": "EUR49.90"}]), names, analyze=False)
        plain = pipeline.upsert_document(s, meta(22, [{"field": 42, "value": "Notiz"}]), names, analyze=False)
        pipeline.upsert_document(s, meta(23, [{"field": 40, "value": "2019-05-01"}]), names, analyze=False)
    with db() as s:
        open_doc, paid_doc, plain = s.get(Document, 20), s.get(Document, 21), s.get(Document, 22)
        assert (open_doc.kind, open_doc.kind_source, open_doc.review_state) == ("invoice", "fields", "confirmed")
        assert open_doc.invoice.status == "open" and open_doc.invoice.due_date == date(2026, 11, 1)
        assert open_doc.invoice.issuer == "Stadtwerke"
        assert open_doc.extraction_state == "queued"  # Betrag fehlt -> Analyse
        assert paid_doc.invoice.status == "paid" and paid_doc.invoice.paid_on == date(2026, 9, 10)
        assert paid_doc.invoice.amount == 49.9 and paid_doc.extraction_state == "skipped"
        assert plain.invoice is None and plain.paperless_fields == {"comment": "Notiz"}
        old = s.get(Document, 23)  # alt, ohne Bezahlt-Datum -> als erledigt angenommen, keine Analyse
        assert old.invoice.status == "paid" and old.invoice.paid_on is None and old.extraction_state == "skipped"


def test_paid_in_paperless_updates_existing_invoice(db, my_settings, mock_api):
    names = names_for(my_settings)
    with db() as s:
        doc = Document(id=30, title="R", suggestions={}, kind="invoice")
        doc.invoice = Invoice(amount=10, status="open")
        s.add(doc)
    with db() as s:
        pipeline.upsert_document(s, meta(30, [{"field": 41, "value": "2026-10-02"}]), names, analyze=False)
    with db() as s:
        assert s.get(Document, 30).invoice.status == "paid"
        # Eigene, noch nicht zurückgeschriebene Änderung hat Vorrang
        doc = s.get(Document, 30)
        doc.invoice.status, doc.invoice.paid_on, doc.writeback_pending = "open", None, True
    with db() as s:
        pipeline.upsert_document(s, meta(30, [{"field": 41, "value": "2026-10-02"}]), names, analyze=False)
        assert s.get(Document, 30).invoice.status == "open"


def no_writes(mock_api):
    return not [c for c in mock_api.calls if c.request.method in ("PATCH", "POST", "PUT", "DELETE")]


def test_changes_only_written_after_approval(db, my_settings, mock_api):
    names = names_for(my_settings)
    client = PaperlessClient(PAPERLESS, "x")
    today = date(2026, 10, 20)
    mock_api.get(f"{PAPERLESS}/api/documents/20/").respond(json=meta(20, []))
    with db() as s:
        doc = pipeline.upsert_document(s, meta(20, [{"field": 40, "value": "2026-11-01"}]), names, analyze=False)
        doc.invoice.amount = 78.0
        doc.invoice.status, doc.invoice.paid_on = "paid", date(2026, 10, 15)
        pipeline.mark_changed(doc)
    with db() as s:
        [(doc, changes)] = pipeline.documents_with_changes(s, my_settings, today)
        assert sorted(changes) == [("amount", None, 78.0), ("paid_on", None, "2026-10-15")]  # Fälligkeit unverändert
    assert no_writes(mock_api)

    with db() as s:
        doc = s.get(Document, 20)
        pipeline.approve_changes(client, doc, my_settings, ["paid_on"], today)
        assert doc.writeback_pending  # Betrag noch offen
    body = patches(mock_api)[-1]["custom_fields"]
    assert body == [{"field": 41, "value": "2026-10-15"}]

    # Betrag verwerfen -> verschwindet, bis sich der Wert ändert
    with db() as s:
        doc = s.get(Document, 20)
        pipeline.dismiss_changes(doc, my_settings, ["amount"], today)
        assert pipeline.pending_changes(doc, my_settings, today) == [] and not doc.writeback_pending
        doc.invoice.amount = 80.0
        assert pipeline.pending_changes(doc, my_settings, today) == [("amount", None, 80.0)]

    # Wieder offen -> Vorschlag, „bezahlt am“ zu leeren
    with db() as s:
        doc = s.get(Document, 20)
        doc.invoice.status, doc.invoice.paid_on = "open", None
        assert ("paid_on", "2026-10-15", None) in pipeline.pending_changes(doc, my_settings, today)
        pipeline.approve_changes(client, doc, my_settings, ["paid_on"], today)
    assert patches(mock_api)[-1]["custom_fields"] == [{"field": 41, "value": None}]


def test_unconfigured_roles_are_not_proposed(db, my_settings):
    my_settings.field_amount = ""
    with db() as s:
        doc = Document(id=31, title="R", suggestions={}, review_state="confirmed", writeback_pending=True)
        doc.invoice = Invoice(amount=10, status="open", due_date=date(2026, 11, 1))
        s.add(doc)
    with db() as s:
        changes = pipeline.pending_changes(s.get(Document, 31), my_settings, date(2026, 10, 20))
        assert [c[0] for c in changes] == ["due_date"]


def test_related_graph(db):
    with db() as s:
        s.add(Document(id=1, title="a", suggestions={}, paperless_fields={"related": [2]}))
        s.add(Document(id=2, title="b", suggestions={}))
        s.add(Document(id=3, title="c", suggestions={}, paperless_fields={"related": [2, 9]}))
        s.add(Document(id=4, title="d", suggestions={}))
    with db() as s:
        assert pipeline.related_ids(s, 1) == {2, 3, 9}
        assert pipeline.related_ids(s, 4) == set()


def test_summary_written_to_field(db, my_settings, mock_api):
    long_text = "Die Stadtwerke Musterstadt fordern einen Abschlag von 78,00 Euro, fällig am 19.10.2026. " * 3
    chat = mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(
        200, json={"message": {"content": json.dumps({"summary": long_text})}}))
    with db() as s:
        s.add(Document(id=1, title="Stromrechnung", correspondent="Stadtwerke", suggestions={}, summary_state="queued"))
    with db() as s:
        doc = pipeline.next_summary(s)
        pipeline.process_summary(s, doc, PaperlessClient(PAPERLESS, "x"), OllamaClient(OLLAMA, "m"), my_settings)
        assert doc.summary_state == "done" and len(doc.summary) <= 128 and doc.summary.endswith("…")
        assert pipeline.pending_changes(doc, my_settings, date(2026, 10, 20)) == [("summary", None, doc.summary)]
    prompt = json.loads(chat.calls.last.request.content)["messages"][1]["content"]
    assert "höchstens 128 Zeichen" in prompt and "Rechnungsnummer: RE-1" in prompt
    assert patches(mock_api) == []  # nur vorgeschlagen


@pytest.fixture
def client(my_settings, mock_api):
    app = create_app(my_settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            s.add(Document(id=1, title="Schaden", suggestions={}, paperless_fields={"related": [2], "comment": "alt"}))
            s.add(Document(id=2, title="Gutachten", suggestions={}))
            s.add(Document(id=3, title="Rechnung Handwerker", suggestions={}))
        yield c


def test_document_page_comment_and_links(client, mock_api):
    page = client.get("/documents/1").text
    assert "Gutachten" in page and "Als Vorgang übernehmen" in page and ">alt</textarea>" in page

    client.post("/documents/1/comment", data={"comment": "Rückruf am Montag"})
    assert patches(mock_api) == []
    assert "noch nicht in Paperless" in client.get("/documents/1").text

    response = client.post("/documents/1/case-from-links", follow_redirects=False)
    case_id = int(response.headers["location"].rsplit("/", 1)[1])
    with session_scope() as s:
        assert {link.document_id for link in s.get(Case, case_id).links} == {1, 2}

    # Hinzufügen schlägt die Verknüpfung in „gehört zu“ nur vor
    client.post(f"/cases/{case_id}/documents", data={"document_id": "3"})
    assert patches(mock_api) == []

    page = client.get("/paperless-changes").text
    assert "Rückruf am Montag" in page and "gehört zu" in page and "Für Paperless <span class=\"pill\">2</span>" in page

    # Zustimmung für beide Vorschläge
    mock_api.get(f"{PAPERLESS}/api/documents/3/").respond(json=meta(3, []))
    mock_api.get(f"{PAPERLESS}/api/documents/1/").respond(json=meta(1, [{"field": 42, "value": "alt"}, {"field": 43, "value": [2]}]))
    client.post("/paperless-changes", data={"action": "approve", "change": ["1:comment", "3:related"]})
    bodies = patches(mock_api)
    assert {"field": 42, "value": "Rückruf am Montag"} in bodies[0]["custom_fields"]
    assert {"field": 43, "value": [2]} in bodies[0]["custom_fields"]  # übrige Felder bleiben erhalten
    assert bodies[1]["custom_fields"] == [{"field": 43, "value": [1, 2]}]
    assert "Keine Vorschläge für Custom Fields" in client.get("/paperless-changes").text


def test_summary_button_queues(client):
    client.post("/documents/2/summary")
    with session_scope() as s:
        assert s.get(Document, 2).summary_state == "queued"
    client.post("/system/summaries")
    with session_scope() as s:
        assert s.get(Document, 3).summary_state == "queued"
