import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import metadata
from app.db import session_scope
from app.main import create_app
from app.models import Document
from app.ollama import OllamaClient
from app.paperless import PaperlessClient
from tests.conftest import OLLAMA, PAPERLESS
from tests.test_fields import patches
from tests.test_pipeline import run_all

ANSWER = {
    "title": "Rechnung Waschmaschine September 2026", "tags": ["haus", "Gibt es nicht"], "new_tags": ["Elektronik", "Steuer"],
    "correspondent": "Stadtwerke", "document_type": "Rechnung",
}


def chat_answer(answer):
    return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})


@pytest.mark.parametrize("title, file_name, generic", [
    ("Scan 0815", None, True), ("IMG_20260103_1230", None, True), ("20260103123011", None, True), ("", None, True),
    ("Dokument (3)", None, True), ("brief_mama", "brief_mama.pdf", True),
    ("Stromrechnung Oktober", None, False), ("Handyvertrag", "scan.pdf", False),
])
def test_generic_title(title, file_name, generic):
    assert metadata.is_generic_title(title, file_name) is generic


def test_suggest_with_ai_and_approve(db, settings, mock_api):
    run_all(db, settings)
    chat = mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=chat_answer(ANSWER))
    paperless, ollama = PaperlessClient(PAPERLESS, "secret"), OllamaClient(OLLAMA, "qwen2.5:3b")
    with db() as s:
        info = metadata.suggest(s, s.get(Document, 3), paperless, ollama, settings)
        assert info["ai"] and info["pending"] == 5
    body = json.loads(chat.calls.last.request.content)
    schema = body["format"]["properties"]
    # Auswahl ist auf vorhandene Namen beschränkt
    assert schema["tags"]["items"]["enum"] == ["Haus", "Steuer"] or set(schema["tags"]["items"]["enum"]) == {"Haus", "Steuer"}
    assert "Rechnung" in schema["document_type"]["enum"] and "Scan 0815" in body["messages"][1]["content"]
    with db() as s:
        doc = s.get(Document, 3)
        sugg = doc.meta_suggestion
        # Unbekannter Tag verworfen, „Steuer“ ist kein neuer Tag
        assert [(t["name"], t.get("new", False)) for t in sugg["tags"]] == [("Haus", False), ("Elektronik", True)]
        assert doc.meta_pending
        keys = [i["key"] for i in metadata.pending(doc)]
        assert keys == ["tag:Haus", "tag:Elektronik", "title", "correspondent", "document_type"]

    create_tag = mock_api.post(f"{PAPERLESS}/api/tags/").respond(json={"id": 99, "name": "Elektronik"})
    with db() as s:
        doc = s.get(Document, 3)
        # Vormerken schreibt nichts nach Paperless
        assert metadata.accept(s, doc, ["tag:Haus", "tag:Elektronik", "title", "correspondent"], title="Rechnung Waschmaschine") == 4
        assert patches(mock_api) == [] and doc.title == "Scan 0815"
        states = {i["key"]: i["state"] for i in metadata.pending(doc)}
        assert states == {"tag:Haus": "accepted", "tag:Elektronik": "accepted", "title": "accepted",
                          "correspondent": "accepted", "document_type": "open"}
    with db() as s:
        doc = s.get(Document, 3)
        written = metadata.transfer(s, paperless, doc)
        assert sorted(written) == ["correspondent", "tag:Elektronik", "tag:Haus", "title"]
        assert doc.title == "Rechnung Waschmaschine" and doc.correspondent == "Stadtwerke"
        assert doc.tags == ["Haus", "Elektronik"] and not doc.meta_accepted
        # Nur der Dokumenttyp ist noch offen
        assert [i["key"] for i in metadata.pending(doc)] == ["document_type"]
        q = metadata.quality(s)
        assert q["transferred"] == 4 and q["waiting"] == 0
        assert {(r["item"], r["accepted"], r["edited"]) for r in q["rows"]} == {
            ("tag", 2, 0), ("title", 0, 1), ("correspondent", 1, 0)}
    assert patches(mock_api)[-1] == {
        "title": "Rechnung Waschmaschine", "correspondent": 10, "tags": [31, 99],
    }
    assert json.loads(create_tag.calls.last.request.content)["name"] == "Elektronik"


def test_learns_from_accepted_before_transfer(db, settings, mock_api):
    """Vorgemerktes zählt für Verlauf, Vokabular und Titelbeispiele, ohne dass Paperless geändert wird."""
    run_all(db, settings)
    paperless = PaperlessClient(PAPERLESS, "secret")
    with db() as s:
        for i in range(10, 13):
            s.add(Document(id=i, title=f"Scan {i}", correspondent="Elektromarkt", tags=[], extraction_state="skipped",
                           meta_suggestion={"tags": [{"name": "Garantiefall", "source": "llm", "new": True}],
                                            "title": {"value": f"Kassenbon {i}", "source": "llm"}}))
    with db() as s:
        for i in range(10, 13):
            metadata.accept(s, s.get(Document, i), ["tag:Garantiefall", "title"],
                            title=f"Kaufbeleg Fernseher {i}" if i == 12 else None)
    with db() as s:
        s.get(Document, 3).correspondent = "Elektromarkt"
    chat = mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=chat_answer({**ANSWER, "tags": ["Garantiefall"], "new_tags": []}))
    with db() as s:
        doc = s.get(Document, 3)
        metadata.suggest(s, doc, paperless, OllamaClient(OLLAMA, "m"), settings)
        tags = {t["name"]: t for t in doc.meta_suggestion["tags"]}
        # Aus dem Verlauf (3 vorgemerkte Dokumente) und weiterhin als neuer Tag markiert
        assert tags["Garantiefall"]["source"] == "history" and tags["Garantiefall"]["new"]
    body = json.loads(chat.calls.last.request.content)
    assert "Garantiefall" in body["format"]["properties"]["tags"]["items"]["enum"]
    prompt = body["messages"][1]["content"]
    assert "„Kaufbeleg Fernseher 12“" in prompt and "Garantiefall" in prompt
    assert patches(mock_api) == []
    with db() as s:
        assert metadata.quality(s)["waiting"] == 6


def test_only_useful_items_for_paperless_page(db, settings, mock_api):
    run_all(db, settings)
    mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=chat_answer({**ANSWER, "new_tags": [], "tags": []}))
    with db() as s:
        doc = s.get(Document, 1)  # gepflegter Titel, Korrespondent und Typ vorhanden
        metadata.suggest(s, doc, PaperlessClient(PAPERLESS, "secret"), OllamaClient(OLLAMA, "m"), settings)
        assert metadata.pending(doc) == [] and not doc.meta_pending
        assert [i["key"] for i in metadata.pending(doc, everything=True)] == ["title"]


def test_history_and_rejected_tags(db, settings, mock_api):
    run_all(db, settings)
    paperless = PaperlessClient(PAPERLESS, "secret")
    with db() as s:
        for i in range(10, 14):
            s.add(Document(id=i, title=f"Abschlag {i}", correspondent="Stadtwerke", document_type="Rechnung",
                           tags=["Haus", "Strom"] if i != 13 else ["Haus"], extraction_state="skipped"))
        doc = Document(id=20, title="Brief", correspondent="Stadtwerke", tags=[], extraction_state="skipped")
        s.add(doc)
    with db() as s:
        doc = s.get(Document, 20)
        info = metadata.suggest(s, doc, paperless, None, settings)  # ohne AI: nur Verlauf
        assert not info["ai"]
        assert [(i["key"], i["source"]) for i in metadata.pending(doc)] == [
            ("tag:Haus", "history"), ("tag:Strom", "history"), ("document_type", "history")]
        metadata.dismiss(s, doc, ["tag:Strom"])
        assert [i["key"] for i in metadata.pending(doc)] == ["tag:Haus", "document_type"]
    with db() as s:
        # Abgelehnter Tag wird für den Absender nicht mehr vorgeschlagen – auch nicht bei anderen Dokumenten
        s.add(Document(id=21, title="Scan 1", correspondent="Stadtwerke", tags=[], extraction_state="skipped"))
    with db() as s:
        doc = s.get(Document, 21)
        metadata.suggest(s, doc, paperless, None, settings)
        assert "tag:Strom" not in [i["key"] for i in metadata.pending(doc)]
        assert "strom" in metadata.build_prompt(doc, "x", {"tags": [], "correspondents": [], "document_types": []},
                                               metadata.history(s, doc), metadata.rejected_tags(s, "Stadtwerke"), 100)


def test_worker_creates_suggestion_after_analysis(db, settings, mock_api):
    from app.worker import Worker

    run_all(db, settings)
    with db() as s:
        assert s.get(Document, 3).meta_state == "queued"  # META_MODE=new
    mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=chat_answer(ANSWER))
    worker = Worker(settings)
    paperless = PaperlessClient(PAPERLESS, "secret")
    ollama = OllamaClient(OLLAMA, "qwen2.5:3b")
    while worker._suggest_meta_next(paperless, ollama):
        pass
    with db() as s:
        assert all(d.meta_state == "done" for d in s.query(Document))
        assert s.get(Document, 3).meta_pending
    assert "Metadaten-Vorschlag (AI" in worker.activity[0]["text"]


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        mock_api.post(f"{OLLAMA}/api/chat").mock(return_value=chat_answer(ANSWER))
        with session_scope() as s:
            metadata.suggest(s, s.get(Document, 3), PaperlessClient(PAPERLESS, "secret"), OllamaClient(OLLAMA, "m"), settings)
        yield c


def test_web_review_and_approve(client, mock_api):
    page = client.get("/paperless-changes").text
    assert "Rechnung Waschmaschine September 2026" in page and "neuer Tag" in page and "Archiv-Check" in page
    # Menüzahl: Dokument 1 (Custom Fields) + Dokument 3 (nur Metadaten)
    assert 'Für Paperless <span class="pill">2</span>' in page
    page = client.get("/documents/3").text
    assert 'id="metadata"' in page and 'name="title" value="Rechnung Waschmaschine September 2026"' in page

    client.post("/paperless-changes/meta", data={"item": ["3:tag:Haus", "3:document_type"], "action": "accept"})
    assert patches(mock_api) == []
    page = client.get("/paperless-changes").text
    assert "Vorgemerkt – wartet auf Übertragung (2)" in page and "Trefferquote" in page
    assert "vorgemerkt" in client.get("/documents/3").text
    response = client.post("/paperless-changes/meta", data={"action": "transfer_all"}, follow_redirects=False)
    assert "2%20vorgemerkte" in response.headers["location"]
    assert patches(mock_api)[-1] == {"document_type": 20, "tags": [31]}
    with session_scope() as s:
        doc = s.get(Document, 3)
        assert doc.document_type == "Rechnung" and doc.extraction_state == "queued"  # Art wird neu bestimmt

    client.post("/documents/3/meta", data={"item": ["tag:Elektronik"], "action": "accept"})
    client.post("/documents/3/meta", data={"item": ["tag:Elektronik"], "action": "withdraw"})
    with session_scope() as s:
        assert not s.get(Document, 3).meta_accepted
    client.post("/documents/3/meta", data={"item": ["tag:Elektronik"], "action": "dismiss"})
    with session_scope() as s:
        assert "tag:Elektronik" in s.get(Document, 3).meta_dismissed


def test_hint_filter_and_bulk_suggest(client):
    page = client.get("/documents?view=all&hint=generic_title").text
    assert "Scan 0815" in page and "Handyvertrag" not in page
    client.post("/documents/suggest-meta", data={"scope": "selected", "doc": ["1", "2"]})
    with session_scope() as s:
        assert s.get(Document, 1).meta_state == "queued" and s.get(Document, 3).meta_state == "done"
