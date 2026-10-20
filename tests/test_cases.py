from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import cases, events, ics, reminders
from app.db import session_scope
from app.main import create_app
from app.models import Case, CaseDocument, CaseEntry, Document


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            s.add(Document(id=5, title="Schadensmeldung", correspondent="Versicherung AG", created=date(2026, 8, 1),
                           suggestions={}, extraction_state="skipped"))
        yield c


def test_case_lifecycle(client):
    response = client.post("/cases", data={"title": "Wasserschaden", "document_id": "5", "follow_up": "2026-11-01"},
                           follow_redirects=False)
    case_url = response.headers["location"]
    case_id = int(case_url.rsplit("/", 1)[1])

    # Dokument 1 ist lokal unbekannt -> wird aus Paperless (ohne Analyse) übernommen
    client.post(f"/cases/{case_id}/documents", data={"document_id": "1"})
    client.post(f"/cases/{case_id}/entries", data={"text": "Gutachter beauftragt", "entry_date": "2026-09-01"})

    with session_scope() as s:
        case = s.get(Case, case_id)
        assert {link.document_id for link in case.links} == {1, 5}
        assert s.get(Document, 1).extraction_state == "skipped"
        kinds = [(i.kind, i.date) for i in cases.timeline(case)]
        assert kinds == [("document", date(2026, 8, 1)), ("note", date(2026, 9, 1)), ("document", date(2026, 10, 5))]

    page = client.get(case_url).text
    assert "Gutachter beauftragt" in page and "Schadensmeldung" in page and "Stromrechnung Oktober" in page

    # Doppelt hinzufügen wird abgefangen
    response = client.post(f"/cases/{case_id}/documents", data={"document_id": "5"}, follow_redirects=False)
    assert "msg=Dokument%20ist%20bereits" in response.headers["location"]

    # Suche im lokalen Index bietet bereits verknüpfte Dokumente nicht erneut an
    assert "Keine Treffer im lokalen Index" in client.get(f"{case_url}?q=Schaden").text

    client.post(f"/cases/{case_id}/update", data={"title": "Wasserschaden Keller", "follow_up": "2026-10-20",
                                                  "follow_up_note": "Antwort Versicherung?"})
    assert "Wiedervorlage fällig: Antwort Versicherung?" in client.get(case_url).text
    dashboard = client.get("/").text
    assert "Wiedervorlage" in dashboard and f"/cases/{case_id}" in dashboard

    client.post(f"/cases/{case_id}/documents/5/remove")
    client.post(f"/cases/{case_id}/status", data={"status": "closed"})
    with session_scope() as s:
        case = s.get(Case, case_id)
        assert case.status == "closed" and case.closed_at is not None
        assert {link.document_id for link in case.links} == {1}

    client.post(f"/cases/{case_id}/delete")
    with session_scope() as s:
        assert s.get(Case, case_id) is None
        assert s.query(CaseDocument).count() == 0 and s.query(CaseEntry).count() == 0
        assert s.get(Document, 5) is not None


def test_document_page_add_to_case(client):
    client.post("/cases", data={"title": "Hausbau"})
    with session_scope() as s:
        case_id = s.query(Case).one().id
    assert "Zu Vorgang hinzufügen" in client.get("/documents/5").text
    client.post("/documents/5/add-to-case", data={"case_id": str(case_id)})
    page = client.get("/documents/5").text
    assert f'href="/cases/{case_id}"' in page


def test_search(db):
    with db() as s:
        s.add(Document(id=1, title="Kfz-Versicherung", correspondent="HUK", suggestions={}))
        s.add(Document(id=2, title="Strom", correspondent="Stadtwerke", suggestions={}))
    with db() as s:
        assert [d.id for d in cases.search_documents(s, "versich")] == [1]
        assert [d.id for d in cases.search_documents(s, "stadt")] == [2]


def test_follow_up_reminder_on_due_day(db):
    with db() as s:
        s.add(Case(title="Widerspruch", follow_up=date(2026, 10, 25), follow_up_note="Frist prüfen"))
        s.add(Case(title="Erledigt", follow_up=date(2026, 10, 1), status="closed"))
    with db() as s:
        assert reminders.due_reminders(s, date(2026, 10, 20), [30, 7, 1]) == []
        due = reminders.due_reminders(s, date(2026, 10, 25), [30, 7, 1])
        assert [e.title for e, _ in due] == ["Widerspruch"]
        body = reminders.format_digest(due, date(2026, 10, 25), "http://p")
        assert "Wiedervorlage: Widerspruch" in body and "/documents/" not in body
        evs = events.collect(s, date(2026, 10, 20))
        assert "SUMMARY:Wiedervorlage: Widerspruch" in ics.render(evs, "http://p")
