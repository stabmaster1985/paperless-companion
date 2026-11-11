from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import metadata, series
from app.db import session_scope
from app.main import create_app
from app.models import Document
from tests.test_pipeline import run_all


@pytest.mark.parametrize("title, pattern", [
    ("Lohnabrechnung Mai 2026", "Lohnabrechnung {Monat} {JJJJ}"),
    ("Kontoauszug 05/2026", "Kontoauszug {MM}/{JJJJ}"),
    ("Kontoauszug 2026-05", "Kontoauszug {JJJJ}-{MM}"),
    ("Abschlag Strom Okt. 2026", "Abschlag Strom {Mon} {JJJJ}"),
    ("Depotauszug Q3 2026", "Depotauszug Q{Q} {JJJJ}"),
    ("Jahresabrechnung 2025", "Jahresabrechnung {JJJJ}"),
    ("Mietvertrag", "Mietvertrag"),
    ("Scan_0012", None),
])
def test_title_pattern(title, pattern):
    assert series.title_pattern(title) == pattern


def test_fill_pattern():
    day = date(2026, 1, 5)
    assert series.fill_pattern("Lohnabrechnung {Monat} {JJJJ}", day) == "Lohnabrechnung Januar 2026"
    assert series.fill_pattern("Lohnabrechnung {Monat} {JJJJ}", day, -1) == "Lohnabrechnung Dezember 2025"  # Vormonat
    assert series.fill_pattern("Kontoauszug {MM}/{JJJJ}", day) == "Kontoauszug 01/2026"
    assert series.fill_pattern("Mietvertrag", None) == "Mietvertrag"
    assert series.fill_pattern("Kontoauszug {MM}/{JJJJ}", None) is None


def payroll(s):
    """Drei abgelegte Lohnabrechnungen, eine nur vorgemerkte, zwei neue (eine ohne Korrespondent) und Fremdes."""
    for i, month in enumerate(("Januar", "Februar", "März"), start=1):
        s.add(Document(id=i, title=f"Lohnabrechnung {month} 2026", correspondent="Arbeitgeber GmbH", tags=["Gehalt", "2026"],
                       document_type="Lohnabrechnung", created=date(2026, i, 27)))
    # nur im Companion vorgemerkt – zählt wie abgelegt
    s.add(Document(id=4, title="Scan_0004", correspondent="Arbeitgeber GmbH", tags=[], created=date(2026, 4, 27),
                   meta_accepted={"title": {"value": "Lohnabrechnung April 2026", "source": "llm"},
                                  "tag:Gehalt": {"value": "Gehalt", "source": "llm"}}))
    s.add(Document(id=5, title="Lohnabrechnung_2026_05", correspondent="Arbeitgeber GmbH", tags=[], created=date(2026, 5, 27)))
    s.add(Document(id=6, title="Lohnabrechnung 06 2026", correspondent=None, tags=[], created=date(2026, 6, 26),
                   meta_suggestion={"tags": [{"name": "2019", "source": "llm"}, {"name": "Steuer", "source": "llm"},
                                             {"name": "Auto", "source": "llm"}, {"name": "Payroll", "source": "llm", "new": True}],
                                    "title": {"value": "Payslip June", "source": "llm"}, "at": "2026-07-01T00:00:00+00:00"}))
    s.add(Document(id=7, title="Kfz-Versicherung 2026", correspondent="Musterversicherung", tags=["Auto"], created=date(2026, 1, 2)))


def test_profile_and_suggestions(db):
    with db() as s:
        payroll(s)
    with db() as s:
        all_series = series.build(s)
        assert len(all_series) == 1
        sr = all_series[0]
        assert sr.correspondent == "Arbeitgeber GmbH" and len(sr.members) == 6 and sr.refs == 4
        assert sr.pattern == "Lohnabrechnung {Monat} {JJJJ}" and sr.month_offset == 0 and sr.name == "Lohnabrechnung"
        assert sr.tags == ["Gehalt"] and sr.document_type == "Lohnabrechnung"  # Jahres-Tag gehört nicht ins Profil
        assert series.wanted(sr, s.get(Document, 5)) == {
            "title": "Lohnabrechnung Mai 2026", "tags": ["Gehalt"], "document_type": "Lohnabrechnung"}
        assert series.wanted(sr, s.get(Document, 6))["correspondent"] == "Arbeitgeber GmbH"
        assert series.wanted(sr, s.get(Document, 1)) == {}
        result = series.refresh(s)
        assert result["series"] == 1 and result["changed"] >= 2
    with db() as s:
        five = {i["key"]: i for i in metadata.pending(s.get(Document, 5))}
        assert five["title"]["new"] == "Lohnabrechnung Mai 2026" and five["title"]["source"] == "series"
        assert five["tag:Gehalt"]["source"] == "series" and five["document_type"]["new"] == "Lohnabrechnung"
        six = s.get(Document, 6)
        names = [t["name"] for t in six.meta_suggestion["tags"]]
        # Serien-Tag vorn, Jahres-Tag und englischer neuer Tag weg, von den AI-Tags außerhalb des Profils bleibt einer
        assert names == ["Gehalt", "Steuer", "2026"]  # „2026“: Jahr des Dokuments, weil es den Tag gibt (Quelle Datum)
        assert six.meta_suggestion["title"] == {"value": "Lohnabrechnung Juni 2026", "source": "series"}
        assert six.series_key == sr.id and s.get(Document, 7).series_key is None
        # Vorgemerktes bleibt unberührt
        assert s.get(Document, 4).meta_accepted["title"]["value"] == "Lohnabrechnung April 2026"
        assert series.refresh(s)["changed"] == 0  # stabil: zweiter Lauf ändert nichts


def test_title_for_previous_month(db):
    with db() as s:
        for i, month in enumerate(("Januar", "Februar", "März"), start=1):
            s.add(Document(id=i, title=f"Kontoauszug {month} 2026", correspondent="Bank", tags=["Konto"],
                           created=date(2026, i + 1, 2)))  # Auszug kommt Anfang des Folgemonats
        s.add(Document(id=9, title="Kontoauszug neu", correspondent="Bank", tags=[], created=date(2026, 5, 2)))
    with db() as s:
        sr = series.build(s)[0]
        assert sr.month_offset == -1
        assert series.wanted(sr, s.get(Document, 9))["title"] == "Kontoauszug April 2026"
        assert "Vormonat" in series.workflow(sr.as_dict())["note"]


def test_no_pattern_when_dates_do_not_fit(db):
    with db() as s:
        for i in range(1, 4):
            s.add(Document(id=i, title=f"Jahresabrechnung {2020 + i}", correspondent="Stadtwerke", tags=["Haus"],
                           created=date(2022 + i, 8, 1)))  # Jahr im Titel ≠ Dokumentjahr
        s.add(Document(id=9, title="Jahresabrechnung", correspondent="Stadtwerke", tags=[], created=date(2026, 8, 1)))
    with db() as s:
        sr = series.build(s)[0]
        assert sr.pattern is None and series.wanted(sr, s.get(Document, 9)) == {"tags": ["Haus"]}


def test_auto_accept_only_for_uniform_series(db):
    with db() as s:
        for i in range(1, 6):
            s.add(Document(id=i, title=f"Abschlag Strom {series.MONTHS[i - 1]} 2026", correspondent="Stadtwerke",
                           tags=["Haus"], created=date(2026, i, 5)))
        s.add(Document(id=9, title="Abschlag Strom", correspondent="Stadtwerke", tags=[], created=date(2026, 6, 5)))
    with db() as s:
        assert series.refresh(s, auto_accept=False)["accepted"] == 0
        assert series.refresh(s, auto_accept=True)["accepted"] == 2
        assert s.get(Document, 9).meta_accepted["title"]["value"] == "Abschlag Strom Juni 2026"
        assert s.get(Document, 9).tags == []  # Paperless bleibt unverändert


def test_cleaning_and_questionable():
    doc = Document(id=1, title="x", tags=[], created=date(2026, 5, 1))
    sugg = {"tags": [{"name": "2019", "source": "llm"}, {"name": "2026", "source": "history"}, {"name": "Haus", "source": "llm"}],
            "title": {"value": "Invoice for electricity", "source": "llm"}}
    cleaned = metadata.clean_suggestion(sugg, doc, "date", {"2026", "Haus"})
    assert cleaned["tags"] == [{"name": "2026", "source": "date"}, {"name": "Haus", "source": "llm"}] and "title" not in cleaned
    assert [t["name"] for t in metadata.clean_suggestion(sugg, doc, "off")["tags"]] == ["Haus"]
    # Jahres-Tag des Dokuments wird ergänzt, wenn es ihn gibt – aber nie ein neuer angelegt
    assert metadata.clean_suggestion({"tags": []}, doc, "date", {"2026"})["tags"] == [{"name": "2026", "source": "date"}]
    assert metadata.clean_suggestion({"tags": []}, doc, "date", {"Haus"})["tags"] == []
    assert metadata.questionable(doc, {"key": "tag:2019", "new": "2019"}) == "Jahres-Tag passt nicht zum Dokumentdatum"
    assert metadata.questionable(doc, {"key": "tag:2026", "new": "2026"}) is None
    assert metadata.questionable(doc, {"key": "title", "new": "Bank Statement", "source": "llm"}) == "englischer Titel"
    assert metadata.questionable(doc, {"key": "title", "new": "Bank Statement", "source": "manual"}) is None  # deine Eingabe


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        with session_scope() as s:
            for doc_id in (1, 2, 3):
                s.delete(s.get(Document, doc_id))
        with session_scope() as s:
            payroll(s)
            s.get(Document, 4).meta_accepted = {**s.get(Document, 4).meta_accepted, "tag:2019": {"value": "2019", "source": "llm"}}
            metadata.refresh_pending(s.get(Document, 4))
        yield c


def test_pages_and_accept_whole_series(client):
    r = client.post("/series/refresh", follow_redirects=False)
    assert "1%20Serien%20erkannt" in r.headers["location"]
    with session_scope() as s:
        sid = s.get(Document, 5).series_key
    page = client.get("/paperless-changes").text
    assert "Serien – wiederkehrende Dokumente" in page and "Titel: Lohnabrechnung {Monat} {JJJJ}" in page
    assert "1 vorgemerkte Einträge würden heute nicht mehr vorgeschlagen" in page
    checked = client.get("/paperless-changes?check=1").text
    # nur der fragliche Jahres-Tag steht in der Liste; der Titel desselben Dokuments ist eingeklappt daneben
    assert 'value="4:tag:2019" class=""' in checked and 'value="4:title" class="extra"' in checked
    assert "Serie: Lohnabrechnung" in client.get(f"/series/{sid}").text and "created_month_name" in client.get(f"/series/{sid}").text
    assert "Lohnabrechnung" in client.get("/series").text
    filtered = client.get(f"/paperless-changes?series={sid}").text
    assert "Nur Serie <strong>Lohnabrechnung</strong>" in filtered
    r = client.post("/paperless-changes/meta", data={"action": "accept_all", "series": sid, "source": "series"},
                    follow_redirects=False)
    assert "Vorschl%C3%A4ge%20vorgemerkt" in r.headers["location"] and f"series={sid}" in r.headers["location"]
    with session_scope() as s:
        assert s.get(Document, 5).meta_accepted["title"] == {**s.get(Document, 5).meta_accepted["title"],
                                                           "value": "Lohnabrechnung Mai 2026", "source": "series"}
        assert s.get(Document, 6).meta_accepted["correspondent"]["value"] == "Arbeitgeber GmbH"
        assert "tag:Steuer" not in s.get(Document, 6).meta_accepted  # AI-Vorschlag bleibt offen
    assert client.get("/series/unbekannt").status_code == 404
