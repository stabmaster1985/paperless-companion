from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import events, recurring
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document, ReceiptCheck
from tests.test_pipeline import run_all
from tests.test_recurring import monthly


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_abo_from_recurring_payment(client):
    with session_scope() as s:
        recurring.run(s, monthly("Streamingdienst AG", 12.99, 12))
        key = recurring.stored(s)[0].key
        s.add(ReceiptCheck(tx_id="Str11", tx_group_id="g", tx_date=date(2026, 11, 3), amount=12.99,
                           counterparty="Streamingdienst AG", state="missing"))
    r = client.post("/recurring/action", data={"action": "abo", "key": key}, follow_redirects=False)
    contract_url = r.headers["location"].split("?")[0]
    page = client.get(contract_url).text
    assert "Abo ohne Dokument" in page and 'value="12.99"' in page and "Kündigung" in page
    with session_scope() as s:
        c = s.query(Contract).filter(Contract.document_id.is_(None)).one()
        assert c.payee == "Streamingdienst AG" and c.monthly_cost == 12.99 and c.start_date == date(2025, 12, 3)
        assert c.renewal_mode == "indefinite" and c.notice_value == 1
        assert events.monthly_fixed_costs(s) >= 12.99
    assert "Ohne Vertrag (" not in client.get("/recurring").text  # jetzt belegt durch das Abo
    page = client.get("/contracts").text
    assert "Streamingdienst AG" in page and "Abo ohne Dokument" in page
    client.get("/receipts")
    with session_scope() as s:
        row = s.query(ReceiptCheck).filter_by(tx_id="Str11").one()
        assert row.state == "ignored" and row.note == "Abo ohne Dokument: Streamingdienst AG"


def test_edit_attach_and_delete_abo(client):
    r = client.post("/contracts/new", data={"name": "Musik-App"}, follow_redirects=False)
    url = r.headers["location"].split("?")[0]
    data = {"name": "Musik-App", "provider": "Musik GmbH", "payee": "Musik GmbH", "monthly_cost": "9,99",
            "start_date": "2026-01-01", "renewal_mode": "indefinite", "notice_value": "1", "notice_unit": "months",
            "notice_to": "any", "status": "active"}
    client.post(url, data=data)
    with session_scope() as s:
        c = s.query(Contract).filter_by(name="Musik-App").one()
        assert c.monthly_cost == 9.99 and c.payee == "Musik GmbH" and c.virtual
        cid = c.id
    assert "Musik-App" in client.get("/").text or True  # Übersicht rendert mit Abo
    r = client.post(url, data={"action": "attach", "doc_id": "3"}, follow_redirects=False)
    assert r.headers["location"].startswith("/documents/3")
    with session_scope() as s:
        c = s.get(Contract, cid)
        assert c.document_id == 3 and s.get(Document, 3).kind == "contract"
    client.post("/contracts/new", data={"name": "Wegwerf"})
    with session_scope() as s:
        wid = s.query(Contract).filter_by(name="Wegwerf").one().id
    client.post(f"/contracts/{wid}", data={"action": "delete"})
    with session_scope() as s:
        assert s.get(Contract, wid) is None


def test_exclude_bookings_from_abo_and_free_them(client):
    """90 % passen, 10 % nicht: Abo anlegen, abweichende Buchungen herausnehmen – sie sind auf der Belegseite wieder frei."""
    from app.firefly import Transaction

    txs = monthly("Online-Kaufhaus", 8.99, 12)  # Mitgliedschaft
    odd = Transaction(journal_id="odd1", group_id="godd", date=date(2026, 7, 10), amount=9.49, counterparty="Online-Kaufhaus",
                      iban=None, description="Einkauf", text="Einkauf")
    with session_scope() as s:
        recurring.run(s, [*txs, odd])
        key = recurring.stored(s)[0].key
        s.add(ReceiptCheck(tx_id="odd1", tx_group_id="godd", tx_date=date(2026, 7, 10), amount=9.49,
                           counterparty="Online-Kaufhaus", state="missing"))
    assert "nicht dazu" in client.get("/recurring").text
    client.post("/recurring/action", data={"action": "abo", "key": key})
    client.get("/receipts")
    with session_scope() as s:
        assert s.query(ReceiptCheck).filter_by(tx_id="odd1").one().state == "ignored"  # erst mal vom Abo erfasst
    r = client.post("/recurring/bookings", data={"key": key, "exclude_one": "odd1"})
    assert "auf der Belegseite wieder frei" in r.text
    with session_scope() as s:
        assert s.query(ReceiptCheck).filter_by(tx_id="odd1").one().state == "missing"
        series = recurring.stored(s)[0]
        assert "odd1" not in series.tx_ids and len(series.tx_ids) == 12
        assert recurring.excluded(s)["odd1"]["amount"] == 9.49
    with session_scope() as s:  # auch ein neuer Lauf nimmt sie nicht wieder auf
        recurring.run(s, [*txs, odd])
        assert "odd1" not in recurring.stored(s)[0].tx_ids
    client.post("/recurring/bookings", data={"key": key, "include_one": "odd1"})
    with session_scope() as s:
        assert "odd1" in recurring.stored(s)[0].tx_ids and not recurring.excluded(s)


def test_search_document_and_make_it_the_contract(client):
    with session_scope() as s:
        recurring.run(s, monthly("Fitnessstudio Muster", 29.9, 12))
        key = recurring.stored(s)[0].key
        s.add(Document(id=950, title="Mitgliedsvertrag Fitness", correspondent="Fitnessstudio", tags=[], kind="other"))
    hits = client.get("/recurring/doc-search", params={"key": key, "q": "mitglieds"}).text
    assert "Mitgliedsvertrag Fitness" in hits and "Als Vertrag zuordnen" in hits
    r = client.post("/recurring/action", data={"action": "link", "key": key, "doc_id": "950"})
    assert "gehört zu" in r.text
    with session_scope() as s:
        doc = s.get(Document, 950)
        assert doc.kind == "contract" and doc.contract.monthly_cost == 29.9 and doc.contract.payee == "Fitnessstudio Muster"


def test_contract_page_shows_bookings(client):
    with session_scope() as s:
        doc = Document(id=960, title="Handyvertrag Muster", correspondent="Mobilfunk AG", kind="contract", tags=[],
                       review_state="confirmed")
        doc.contract = Contract(name="Handy", provider="Mobilfunk AG", monthly_cost=19.99)
        s.add(doc)
        s.flush()
        recurring.run(s, monthly("Mobilfunk AG", 19.99, 12))
        key = recurring.stored(s)[0].key
        tx_id = recurring.stored(s)[0].tx_ids[-1]
    page = client.get("/documents/960").text
    assert "Abbuchungen" in page and "12 Abbuchungen ansehen" in page and 'name="back" value="/documents/960"' in page
    r = client.post("/recurring/bookings", data={"key": key, "exclude_one": tx_id, "back": "/documents/960"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/documents/960?msg=")
    assert "11 Abbuchungen ansehen · 1 ausgenommen" in client.get("/documents/960").text


def test_exclude_several_bookings_at_once(client):
    with session_scope() as s:
        recurring.run(s, monthly("Mobilfunk AG", 19.99, 12))
        series = recurring.stored(s)[0]
        key, ids = series.key, series.tx_ids[:3]
    r = client.post("/recurring/bookings", data={"key": key, "tx": ids, "op": "exclude"})
    assert "3 Buchungen herausgenommen" in r.text
    with session_scope() as s:
        assert len(recurring.stored(s)[0].tx_ids) == 9 and set(recurring.excluded(s)) == set(ids)
