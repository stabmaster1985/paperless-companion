import csv
import io
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import tax
from app.db import session_scope
from app.main import create_app
from app.models import Document, Invoice
from tests.conftest import PAPERLESS


def add(s, doc_id, created, tags=(), doctype=None, **kw):
    doc = Document(id=doc_id, title=f"Beleg {doc_id}", created=created, tags=list(tags), document_type=doctype,
                   correspondent="Firma/GmbH", suggestions={}, extraction_state="skipped", **kw)
    s.add(doc)
    return doc


def test_category_rules(settings):
    settings.tax_doctypes = "Lohnsteuerbescheinigung"
    make = lambda **kw: Document(id=1, title="x", tags=kw.get("tags", []), document_type=kw.get("doctype"))
    assert tax.category_for(make(tags=["Steuer"], doctype="Rechnung"), settings) == "Rechnung"
    assert tax.category_for(make(tags=["Steuer"]), settings) == "Sonstiges"
    assert tax.category_for(make(tags=["steuer: Werbungskosten"]), settings) == "Werbungskosten"
    assert tax.category_for(make(tags=["Steuer/Spenden", "Steuer"]), settings) == "Spenden"
    assert tax.category_for(make(doctype="Lohnsteuerbescheinigung"), settings) == "Lohnsteuerbescheinigung"
    assert tax.category_for(make(tags=["Steuerberater"]), settings) is None
    assert tax.category_for(make(tags=["Haus"]), settings) is None


def test_collect_with_year_override(db, settings):
    with db() as s:
        add(s, 1, date(2025, 3, 1), ["Steuer: Handwerker"]).invoice = Invoice(amount=100.5, status="paid")
        add(s, 2, date(2025, 4, 1), ["Steuer: Handwerker"]).invoice = Invoice(amount=50, status="paid")
        add(s, 3, date(2026, 2, 1), ["Steuer: Nebenkosten"], tax_year=2025)   # gehört zu 2025
        add(s, 4, date(2025, 5, 1), ["Steuer"], tax_year=0)                   # ausgeschlossen
        add(s, 5, date(2025, 5, 1), ["Haus"])
    with db() as s:
        items = tax.collect(s, 2025, settings)
        assert [i.doc.id for i in items] == [1, 2, 3]
        groups = tax.grouped(items)
        assert groups["Handwerker"]["sum"] == 150.5 and groups["Handwerker"]["with_amount"] == 2
        assert tax.available_years(s, settings) == [2025]
        assert [d.id for d in tax.excluded(s, settings)] == [4]


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            add(s, 10, date(2025, 3, 1), ["Steuer: Spenden"], doctype="Quittung").invoice = Invoice(amount=25, status="paid")
            add(s, 11, date(2025, 6, 1), ["Steuer"], doctype="Rechnung")
        yield c


def test_tax_page_and_zip_export(client, mock_api):
    mock_api.get(f"{PAPERLESS}/api/documents/10/download/").respond(
        content=b"%PDF-1", headers={"content-disposition": "attachment; filename*=utf-8''Spende%20%C3%A4.pdf"}
    )
    mock_api.get(f"{PAPERLESS}/api/documents/11/download/").respond(500)

    page = client.get("/tax?year=2025").text
    assert "Spenden" in page and "25,00 €" in page and "ZIP herunterladen (2 Dokumente)" in page

    response = client.get("/tax/export?year=2025")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = zf.namelist()
    assert "Spenden/2025-03-01_Firma_GmbH_Beleg 10_10.pdf" in names
    assert zf.read("Spenden/2025-03-01_Firma_GmbH_Beleg 10_10.pdf") == b"%PDF-1"
    assert "FEHLER.txt" in names
    rows = list(csv.reader(io.StringIO(zf.read("Übersicht_2025.csv").decode("utf-8-sig")), delimiter=";"))
    assert rows[0][0] == "Kategorie"
    # sortiert nach Kategorie: Rechnung (Download fehlgeschlagen, ohne Datei), dann Spenden
    assert rows[1][0] == "Rechnung" and rows[1][7] == ""
    assert rows[2][:2] == ["Spenden", "01.03.2025"] and rows[2][5] == "25,00"

    assert client.get("/tax/export?year=1999").status_code == 404


def test_change_tax_year(client):
    client.post("/documents/11/tax-year", data={"tax_year": "0", "back": "/tax?year=2025"})
    with session_scope() as s:
        assert s.get(Document, 11).tax_year == 0
    assert "Vom Export ausgeschlossen (1)" in client.get("/tax?year=2025").text
    client.post("/documents/11/tax-year", data={"tax_year": ""})
    with session_scope() as s:
        assert s.get(Document, 11).tax_year is None
