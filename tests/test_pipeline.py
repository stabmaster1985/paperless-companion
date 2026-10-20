import json
from datetime import date

import httpx

from app import events, ics, pipeline, reminders
from app.extraction import llm
from app.models import Document, SentReminder
from app.ollama import OllamaClient
from app.paperless import PaperlessClient
from tests.conftest import OLLAMA, PAPERLESS


def ollama_reply(content: dict):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(content)}})


def run_all(session_scope, settings, ollama=None):
    paperless = PaperlessClient(PAPERLESS, "secret")
    with session_scope() as s:
        count = pipeline.sync(s, paperless, settings)
    while True:
        with session_scope() as s:
            doc = pipeline.next_queued(s)
            if doc is None:
                break
            pipeline.process_document(s, doc, paperless, ollama, settings)
    return count


def test_sync_and_rules_only(db, settings, mock_api):
    assert run_all(db, settings) == 3
    with db() as s:
        invoice_doc = s.get(Document, 1)
        assert (invoice_doc.kind, invoice_doc.kind_source) == ("invoice", "doctype")
        assert invoice_doc.review_state == "confirmed" and invoice_doc.auto_confirmed
        assert invoice_doc.invoice.amount == 78.0
        assert invoice_doc.invoice.due_date == date(2026, 10, 19)
        assert invoice_doc.invoice.issuer == "Stadtwerke"

        contract_doc = s.get(Document, 2)
        assert contract_doc.kind == "contract" and contract_doc.review_state == "pending"
        assert contract_doc.contract is None
        assert contract_doc.suggestions["notice_value"]["value"] == 3

        scan = s.get(Document, 3)
        assert (scan.kind, scan.kind_source) == ("invoice", "keyword")

    # Nichts wird von selbst nach Paperless geschrieben – nur vorgeschlagen
    assert not [c for c in mock_api.calls if c.request.method in ("PATCH", "POST", "PUT", "DELETE")]
    with db() as s:
        rows = pipeline.documents_with_changes(s, settings, date(2026, 10, 20))
        changes = {doc.id: ch for doc, ch in rows}
        assert ("amount", None, 78.0) in changes[1] and ("due_date", None, "2026-10-19") in changes[1]

        # Nach Zustimmung wird geschrieben
        pipeline.approve_changes(PaperlessClient(PAPERLESS, "x"), s.get(Document, 1), settings, ["amount"], date(2026, 10, 20))
    patch = [c for c in mock_api.calls if c.request.method == "PATCH"]
    assert len(patch) == 1 and "/api/documents/1/" in str(patch[0].request.url)
    body = json.loads(patch[0].request.content)
    assert {"field": 1, "value": "EUR78.00"} in body["custom_fields"]


def test_llm_fills_gaps_and_is_validated(db, settings, mock_api):
    calls = []

    def fake_chat(request):
        payload = json.loads(request.content)
        calls.append(payload)
        wanted = payload["format"]["required"]
        answer = {"amount": 249.90, "due_date": "2026-10-01", "invoice_number": "FAKE-123", "invoice_date": "2030-01-01"}
        return ollama_reply({k: answer.get(k) for k in wanted})

    mock_api.post(f"{OLLAMA}/api/chat").mock(side_effect=fake_chat)
    ollama = OllamaClient(OLLAMA, "qwen2.5:3b")
    run_all(db, settings, ollama)

    assert calls, "LLM sollte für unvollständige Dokumente gefragt werden"
    assert calls[0]["options"]["temperature"] == 0
    with db() as s:
        scan = s.get(Document, 3)
        sugg = scan.suggestions
        assert sugg["amount"]["value"] == 249.9 and sugg["amount"]["source"] in ("rule", "llm")
        # Erfundene Werte, die nicht im Text stehen, werden verworfen
        assert sugg.get("invoice_number", {}).get("value") != "FAKE-123"
        assert sugg.get("invoice_date", {}).get("value") != "2030-01-01"


def test_llm_error_is_recorded(db, settings, mock_api):
    mock_api.post(f"{OLLAMA}/api/chat").respond(500)
    run_all(db, settings, OllamaClient(OLLAMA, "qwen2.5:3b"))
    with db() as s:
        doc = s.get(Document, 2)
        assert doc.extraction_state == "done"
        assert "_llm_error" in doc.suggestions


def test_snippets_respect_limit():
    text = "Kopf\n" + "\n".join(f"Füllzeile {i}" for i in range(2000)) + "\nKündigungsfrist 3 Monate"
    snippet = llm.select_snippets(text, "contract", 500)
    assert len(snippet) <= 500 and snippet.startswith("Kopf")


def test_confirm_contract_events_reminders_ics(db, settings, mock_api):
    run_all(db, settings)
    with db() as s:
        doc = s.get(Document, 2)
        values = pipeline.parse_values("contract", pipeline.suggestion_values(doc))
        pipeline.apply_values(s, doc, "contract", values)
        doc.review_state = "confirmed"

    today = date(2026, 10, 20)
    with db() as s:
        evs = events.collect(s, today)
        types = {(e.type, e.date) for e in evs}
        assert ("cancel", date(2026, 10, 31)) in types
        assert ("overdue", date(2026, 10, 19)) in types
        assert events.monthly_fixed_costs(s) == 19.99

        due = reminders.due_reminders(s, today, [30, 7, 1])
        keys = {keys[0] for _, keys in due}
        # Vertrag: noch 11 Tage -> kleinste erreichte Vorlaufstufe ist 30
        assert "contract:1:cancel:2026-10-31:30" in keys
        body = reminders.format_digest(due, today, "http://p")
        assert "Kündigungsfrist" in body and "http://p/documents/2/details" in body

        calendar = ics.render(evs, "http://p")
        assert calendar.startswith("BEGIN:VCALENDAR") and "DTSTART;VALUE=DATE:20261031" in calendar
        assert all(len(line.encode()) <= 75 for line in calendar.split("\r\n"))


def test_reminders_marked_and_not_repeated(db, settings, mock_api, monkeypatch):
    run_all(db, settings)
    sent = []
    monkeypatch.setattr(reminders.notify, "send", lambda st, title, body: sent.append(body) or ["ntfy"])
    today = date(2026, 10, 20)
    with db() as s:
        assert reminders.run(s, settings, today) >= 1
    with db() as s:
        assert s.query(SentReminder).count() >= 1
        assert reminders.run(s, settings, today) == 0
    assert len(sent) == 1


def test_paperless_client_sends_no_fixed_api_version():
    import respx

    with respx.mock as router:
        route = router.get(f"{PAPERLESS}/api/documents/").respond(
            json={"count": 0, "results": []}, headers={"X-Version": "2.20.1", "X-Api-Version": "10"})
        client = PaperlessClient(PAPERLESS, "x")
        assert client.ping() == {"version": "2.20.1", "api_version": "10"}
        # Eine fest angefragte alte Version beantwortet neueres Paperless mit 406
        assert route.calls.last.request.headers["accept"] == "application/json"


def test_llm_errors_are_retried_when_ai_is_back(db, settings, mock_api):
    mock_api.post(f"{OLLAMA}/api/chat").respond(500)
    ollama = OllamaClient(OLLAMA, "qwen2.5:3b")
    run_all(db, settings, ollama)
    with db() as s:
        err = s.get(Document, 2).suggestions["_llm_error"]
        assert err["attempts"] == 1 and err["at"]
        assert [d.id for d in pipeline.documents_with_llm_error(s)] == [2, 3]
        assert pipeline.requeue_llm_errors(s) == 2
        assert s.get(Document, 2).extraction_state == "queued"

    # Erneut fehlgeschlagen -> Zähler steigt; nach 3 Versuchen kein automatischer Neuversuch mehr
    for _ in range(2):
        run_all(db, settings, ollama)
        with db() as s:
            pipeline.requeue_llm_errors(s)
    run_all(db, settings, ollama)
    with db() as s:
        assert s.get(Document, 2).suggestions["_llm_error"]["attempts"] == 3
        assert pipeline.documents_with_llm_error(s) == []
        assert len(pipeline.documents_with_llm_error(s, include_exhausted=True)) == 2

    # AI wieder da -> Hinweis verschwindet nach erneuter Analyse
    mock_api.post(f"{OLLAMA}/api/chat").respond(json={"message": {"content": "{}"}})
    with db() as s:
        pipeline.requeue_llm_errors(s, include_exhausted=True)
    run_all(db, settings, ollama)
    with db() as s:
        assert "_llm_error" not in s.get(Document, 2).suggestions


def test_llm_active_hours(settings):
    settings.llm_active_hours = "22-6"
    assert settings.llm_allowed_at(23) and settings.llm_allowed_at(3)
    assert not settings.llm_allowed_at(6) and not settings.llm_allowed_at(14)
    settings.llm_active_hours = "9-17"
    assert settings.llm_allowed_at(9) and not settings.llm_allowed_at(17)
    settings.llm_active_hours = ""
    assert settings.llm_allowed_at(12)


def test_outside_ai_hours_rules_only_and_deferred(db, settings, mock_api):
    settings.llm_active_hours = "22-6"
    chat = mock_api.post(f"{OLLAMA}/api/chat").respond(json={"message": {"content": "{}"}})
    paperless = PaperlessClient(PAPERLESS, "x")
    ollama = OllamaClient(OLLAMA, "m", num_thread=2)
    with db() as s:
        pipeline.sync(s, paperless, settings)
    with db() as s:
        while (doc := pipeline.next_queued(s)) is not None:
            pipeline.process_document(s, doc, paperless, ollama, settings, llm_allowed=False)
    assert not chat.called
    with db() as s:
        doc = s.get(Document, 2)
        assert doc.suggestions["_llm_error"]["deferred"]
        assert pipeline.analysis_method(doc) == "rules_ai_deferred"
        assert 2 in [d.id for d in pipeline.documents_with_llm_error(s)]  # wird im Zeitfenster nachgeholt
        pipeline.requeue_llm_errors(s)
    with db() as s:
        while (doc := pipeline.next_queued(s)) is not None:
            pipeline.process_document(s, doc, paperless, ollama, settings, llm_allowed=True)
    assert chat.called and json.loads(chat.calls.last.request.content)["options"]["num_thread"] == 2


def test_clear_queue(db):
    with db() as s:
        s.add(Document(id=1, title="a", suggestions={}, extraction_state="queued"))
        s.add(Document(id=2, title="b", suggestions={"amount": {}}, extraction_state="queued"))
        s.add(Document(id=3, title="c", suggestions={}, extraction_state="done", summary_state="queued"))
    with db() as s:
        assert pipeline.clear_queue(s) == 3
    with db() as s:
        assert [s.get(Document, i).extraction_state for i in (1, 2, 3)] == ["skipped", "done", "done"]
        assert s.get(Document, 3).summary_state is None
