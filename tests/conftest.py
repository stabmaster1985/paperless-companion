import json

import httpx
import pytest
import respx

from app.config import Settings
from app.db import init_db, session_scope

PAPERLESS = "http://paperless.test"
OLLAMA = "http://ollama.test"

DOCS = {
    1: {
        "id": 1, "title": "Stromrechnung Oktober", "correspondent": 10, "document_type": 20,
        "created": "2026-10-05", "modified": "2026-10-06T10:00:00+02:00", "custom_fields": [], "tags": [30, 31],
        "content": "Rechnungsnummer: RE-1\nRechnungsdatum: 05.10.2026\nGesamtbetrag: 78,00 €\nFällig am 19.10.2026",
    },
    2: {
        "id": 2, "title": "Handyvertrag", "correspondent": 11, "document_type": 21,
        "created": "2025-02-01", "modified": "2026-10-07T10:00:00+02:00", "custom_fields": [],
        "content": "Vertragsbeginn: 01.02.2025\nMindestlaufzeit 24 Monate. Verlängert sich um jeweils 12 Monate. "
                   "Kündigungsfrist 3 Monate zum Ende der Laufzeit.\nMonatlich 19,99 €",
    },
    3: {
        "id": 3, "title": "Scan 0815", "correspondent": None, "document_type": None,
        "created": "2026-09-01", "modified": "2026-10-08T10:00:00+02:00", "custom_fields": [],
        "content": "Rechnung\nVielen Dank für Ihren Einkauf.\nDer Betrag von 249,90 € ist fällig zum 1. Oktober 2026.",
    },
}


def page(results):
    return {"count": len(results), "next": None, "previous": None, "results": results}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        paperless_url=PAPERLESS,
        paperless_token="secret",
        data_dir=tmp_path,
        ollama_url=OLLAMA,
        webhook_token="hook",
        ics_token="cal",
        writeback_enabled=True,
    )


@pytest.fixture
def db(settings):
    init_db(settings.database_url)
    return session_scope


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PAPERLESS}/api/correspondents/").respond(json=page([
            {"id": 10, "name": "Stadtwerke"}, {"id": 11, "name": "Mobilfunk AG"},
        ]))
        router.get(f"{PAPERLESS}/api/document_types/").respond(json=page([
            {"id": 20, "name": "Rechnung"}, {"id": 21, "name": "Vertrag"},
        ]))
        router.get(f"{PAPERLESS}/api/tags/").respond(json=page([{"id": 30, "name": "Steuer"}, {"id": 31, "name": "Haus"}]))
        router.get(f"{PAPERLESS}/api/documents/").respond(json=page(list(DOCS.values())))
        for doc_id, doc in DOCS.items():
            router.get(f"{PAPERLESS}/api/documents/{doc_id}/").respond(json=doc)
        router.get(f"{PAPERLESS}/api/custom_fields/").respond(json=page([{"id": 1, "name": "Betrag"}]))
        counter = iter(range(2, 100))
        router.post(f"{PAPERLESS}/api/custom_fields/").mock(
            side_effect=lambda request: httpx.Response(201, json={"id": next(counter), **json.loads(request.content)})
        )
        router.patch(url__regex=rf"{PAPERLESS}/api/documents/\d+/").respond(json={})
        router.get(f"{OLLAMA}/api/tags").respond(json={"models": [{"name": "qwen2.5:3b"}]})
        yield router
