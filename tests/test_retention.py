import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import pipeline, retention
from app.db import init_db, session_scope
from app.main import create_app
from app.models import Contract, Document, Invoice, RetentionLog, RetentionRule, Warranty
from app.paperless import PaperlessClient
from tests.conftest import PAPERLESS

TODAY = date(2026, 10, 20)


def add_doc(s, doc_id, created, doctype="Rechnung", tags=(), **kw):
    doc = Document(id=doc_id, title=f"Dok {doc_id}", created=created, document_type=doctype, tags=list(tags),
                   suggestions={}, extraction_state="skipped", **kw)
    s.add(doc)
    return doc


def test_index_sync_adds_skipped_and_removes_missing(db, mock_api):
    with db() as s:
        add_doc(s, 99, date(2020, 1, 1))
    with db() as s:
        total, removed = pipeline.index_sync(s, PaperlessClient(PAPERLESS, "x"))
    assert (total, removed) == (3, 1)
    with db() as s:
        doc = s.get(Document, 1)
        assert doc.extraction_state == "skipped" and doc.tags == ["Haus", "Steuer"]
        assert s.get(Document, 99) is None


def test_skipped_document_is_analyzed_after_change(db, settings, mock_api):
    with db() as s:
        pipeline.index_sync(s, PaperlessClient(PAPERLESS, "x"))
    with db() as s:
        pipeline.sync(s, PaperlessClient(PAPERLESS, "x"), settings)
        assert s.get(Document, 1).extraction_state == "queued"


def test_evaluate_rules(db):
    with db() as s:
        retention.seed_defaults(s)
        add_doc(s, 1, date(2020, 5, 1))                              # Rechnung, 3 J. -> bis 31.12.2023
        add_doc(s, 2, date(2020, 5, 1), tags=["Steuer"])              # 10 J. gewinnt -> 2030
        add_doc(s, 3, date(2020, 5, 1), retention_hold=True)          # manuell behalten
        add_doc(s, 4, date(2020, 5, 1)).invoice = Invoice(amount=10, status="open")
        add_doc(s, 5, date(2023, 6, 1))                               # bis 31.12.2026 -> bald
        add_doc(s, 6, date(2020, 1, 1), doctype="Arztbrief")          # keine Regel
        add_doc(s, 7, date(2020, 1, 1)).warranty = Warranty(product="TV", purchase_date=date(2025, 1, 1), months=24)
        doc = add_doc(s, 8, date(2015, 1, 1), doctype="Vertrag", kind="contract")
        doc.contract = Contract(name="Alt", start_date=date(2015, 1, 1), initial_term_months=12, renewal_mode="renews",
                                renewal_months=12, notice_value=3, status="cancelled", cancelled_on=date(2019, 3, 1))
        doc = add_doc(s, 9, date(2024, 1, 1), doctype="Vertrag", kind="contract")
        doc.contract = Contract(name="Läuft", status="active")

    with db() as s:
        statuses = {st.doc.id: st for st in retention.evaluate(s, TODAY)}
        assert statuses[1].keep_until == date(2023, 12, 31) and not statuses[1].blocked
        assert statuses[2].keep_until == date(2030, 12, 31)
        assert statuses[3].blocked == "manuell behalten"
        assert statuses[4].blocked == "Rechnung offen"
        assert statuses[6].rule is None
        assert statuses[7].blocked == "Garantie läuft noch"
        # Gekündigt 01.03.2019 -> Ende 31.12.2019 -> + 3 Jahre
        assert statuses[8].keep_until == date(2022, 12, 31)
        assert statuses[9].keep_until is None and statuses[9].blocked == "Vertrag läuft noch"

        ov = retention.overview(s, TODAY)
        assert [st.doc.id for st in ov["due"]] == [8, 1]
        assert [st.doc.id for st in ov["soon"]] == [5]
        assert {st.doc.id for st in ov["protected"]} == {3, 4, 7}


def test_zero_years_keeps_forever(db):
    with db() as s:
        s.add(RetentionRule(name="Zeugnisse", match_type="doctype", match_value="Zeugnis", years=0))
        add_doc(s, 1, date(1999, 1, 1), doctype="zeugnis")
    with db() as s:
        st = retention.evaluate(s, TODAY)[0]
        assert st.keep_until is None and st.blocked == "dauerhaft aufbewahren"


def test_migration_adds_missing_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE documents (id INTEGER PRIMARY KEY, title VARCHAR)"))
        conn.execute(text("INSERT INTO documents (id, title) VALUES (1, 'alt')"))
    init_db(url)
    with session_scope() as s:
        assert s.get(Document, 1).retention_hold is False


@pytest.fixture
def client(settings, mock_api):
    settings.retention_delete_enabled = True
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            add_doc(s, 50, date(2018, 1, 1))
            add_doc(s, 51, date(2018, 1, 1))
        yield c


def test_retention_page_and_tag_action(client, mock_api):
    mock_api.get(f"{PAPERLESS}/api/tags/", params={"name__iexact": "Löschkandidat"}).respond(
        json={"count": 0, "next": None, "results": []}
    )
    mock_api.post(f"{PAPERLESS}/api/tags/").respond(201, json={"id": 77, "name": "Löschkandidat"})
    bulk = mock_api.post(f"{PAPERLESS}/api/documents/bulk_edit/").respond(json={"result": "OK"})

    page = client.get("/retention").text
    assert "Dok 50" in page and "Rechnungen (Verjährung 3 Jahre)" in page

    client.post("/retention/apply", data={"action": "tag", "doc": ["50", "51"]})
    payload = json.loads(bulk.calls.last.request.content)
    assert payload == {"documents": [50, 51], "method": "add_tag", "parameters": {"tag": 77}}
    with session_scope() as s:
        assert s.query(RetentionLog).filter_by(action="tagged").count() == 2


def test_delete_requires_confirmation(client, mock_api):
    delete = mock_api.delete(f"{PAPERLESS}/api/documents/50/").respond(204)
    client.post("/retention/apply", data={"action": "delete", "doc": ["50"]})
    assert not delete.called
    client.post("/retention/apply", data={"action": "delete", "doc": ["50"], "confirm": "LÖSCHEN"})
    assert delete.called
    with session_scope() as s:
        assert s.get(Document, 50) is None
        assert s.query(RetentionLog).filter_by(action="deleted", document_id=50).count() == 1


def test_rule_crud(client):
    client.post("/retention/rules", data={"name": "Zeugnisse", "match_type": "doctype", "match_value": "Zeugnis",
                                          "years": "0", "basis": "year_end"})
    with session_scope() as s:
        rule = s.query(RetentionRule).filter_by(name="Zeugnisse").one()
        rule_id = rule.id
    client.post("/retention/rules", data={"rule_id": rule_id, "name": "Zeugnisse", "match_type": "tag",
                                          "match_value": "Zeugnis", "years": "5", "basis": "created"})
    with session_scope() as s:
        assert s.get(RetentionRule, rule_id).years == 5
    client.post(f"/retention/rules/{rule_id}/delete")
    with session_scope() as s:
        assert s.get(RetentionRule, rule_id) is None
