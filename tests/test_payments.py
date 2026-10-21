from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import payments
from app.db import session_scope
from app.firefly import FireflyClient, Transaction
from app.main import create_app
from app.models import Document, Invoice, PaymentMatch

FIREFLY = "http://firefly.test"


def tx(journal_id, day, amount, counterparty="", description="", iban=None):
    return Transaction(journal_id=journal_id, group_id=f"g{journal_id}", date=day, amount=amount,
                       counterparty=counterparty, iban=iban, description=description, text=description)


def add_invoice(s, doc_id, amount, **kw):
    doc = Document(id=doc_id, title=f"Rechnung {doc_id}", created=kw.pop("created", date(2026, 9, 1)),
                   correspondent=kw.pop("correspondent", None), suggestions={}, kind="invoice")
    doc.invoice = Invoice(amount=amount, status="open", invoice_date=date(2026, 9, 1), due_date=date(2026, 9, 15), **kw)
    s.add(doc)
    return doc.invoice


def test_score_signals():
    inv = Invoice(amount=78.0, invoice_number="RE-2026-4711", iban="DE89370400440532013000", issuer="Stadtwerke Musterstadt",
                  invoice_date=date(2026, 9, 1), due_date=date(2026, 9, 15))
    points, reasons = payments.score(inv, tx("1", date(2026, 9, 10), 78.0, "Stadtwerke Musterstadt GmbH",
                                             "RE 2026 4711 Abschlag", "DE89370400440532013000"))
    assert points == 50 + 40 + 30 + 20 + 5 and reasons == ["Betrag", "Rechnungsnr.", "IBAN", "Empfänger"]
    assert payments.score(inv, tx("2", date(2026, 9, 10), 78.01, "Stadtwerke")) is None
    assert payments.score(inv, tx("3", date(2027, 1, 10), 78.0, "Stadtwerke")) is None  # außerhalb des Zeitfensters


def test_match_suggests_best_and_respects_uniqueness(db, settings):
    with db() as s:
        a = add_invoice(s, 1, 78.0, issuer="Stadtwerke")
        add_invoice(s, 2, 19.99, issuer="Mobilfunk AG")
        add_invoice(s, 3, 50.0, issuer="Unbekannt")
    transactions = [
        tx("10", date(2026, 9, 12), 78.0, "Stadtwerke GmbH", "Abschlag"),
        tx("11", date(2026, 9, 3), 19.99, "Mobilfunk AG"),
        tx("12", date(2026, 9, 4), 50.0, "Supermarkt"),  # nur Betrag, aber eindeutig
        tx("13", date(2026, 9, 5), 50.0, "Tankstelle"),  # zweite Buchung mit gleichem Betrag -> kein Vorschlag
    ]
    with db() as s:
        counts = payments.match(s, transactions, settings)
        assert counts == {"suggested": 2, "accepted": 0}
        pairs = {(m.invoice_id, m.tx_id) for m in s.query(PaymentMatch)}
        assert pairs == {(1, "10"), (2, "11")}

    # Erneuter Lauf erzeugt keine Duplikate
    with db() as s:
        assert payments.match(s, transactions, settings) == {"suggested": 0, "accepted": 0}


def test_auto_accept_and_reject(db, settings):
    settings.firefly_auto_accept = True
    with db() as s:
        add_invoice(s, 1, 78.0, invoice_number="RE-1234")
        add_invoice(s, 2, 30.0, issuer="Mobilfunk AG")
    transactions = [tx("10", date(2026, 9, 12), 78.0, "X", "Rechnung RE-1234"), tx("11", date(2026, 9, 12), 30.0, "Mobilfunk AG")]
    with db() as s:
        assert payments.match(s, transactions, settings) == {"suggested": 1, "accepted": 1}
    with db() as s:
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.paid_on == date(2026, 9, 12)
        suggestion = payments.suggestions(s)[0]
        payments.reject(s, suggestion.id)
    with db() as s:
        assert payments.suggestions(s) == []
        # Abgelehntes Paar wird nicht erneut vorgeschlagen
        assert payments.match(s, transactions, settings)["suggested"] == 0


FIREFLY_PAGE = {
    "data": [{
        "type": "transactions", "id": "500",
        "attributes": {"transactions": [{
            "transaction_journal_id": "900", "type": "withdrawal", "date": "2026-10-10T00:00:00+02:00",
            "amount": "78.000000000000", "description": "Abschlag Oktober", "destination_name": "Stadtwerke",
            "destination_iban": "DE89 3704 0044 0532 0130 00", "notes": "RE-1",
        }]},
    }],
    "meta": {"pagination": {"total": 1, "current_page": 1, "total_pages": 1}},
}


def test_firefly_client_parses(mock_api):
    route = mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json=FIREFLY_PAGE)
    client = FireflyClient(FIREFLY, "t")
    items = list(client.withdrawals(date(2026, 9, 1), date(2026, 10, 20)))
    assert route.calls.last.request.headers["authorization"] == "Bearer t"
    assert items[0].amount == 78.0 and items[0].iban == "DE89370400440532013000" and "RE-1" in items[0].text
    assert client.link("500") == f"{FIREFLY}/transactions/show/500"


@pytest.fixture
def client(settings, mock_api):
    settings.firefly_url, settings.firefly_token = FIREFLY, "t"
    mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json=FIREFLY_PAGE)
    mock_api.get(f"{FIREFLY}/api/v1/about").respond(json={"data": {"version": "6.2.0"}})
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            add_invoice(s, 1, 78.0, invoice_number="RE-1", issuer="Stadtwerke",
                        created=date(2026, 10, 5)).invoice_date = date(2026, 10, 5)
        yield c


def test_web_flow(client):
    assert "Mit Firefly III abgleichen" in client.get("/invoices").text
    client.post("/payments/run")
    page = client.get("/invoices").text
    assert "Zahlungen gefunden (1)" in page and "Rechnungsnr." in page
    assert "1 Zahlungen gefunden" in client.get("/").text
    assert "Version 6.2.0" in client.get("/system").text
    with session_scope() as s:
        match_id = s.query(PaymentMatch).one().id
    client.post(f"/payments/{match_id}/accept")
    with session_scope() as s:
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.paid_on == date(2026, 10, 10)


def test_summary_shows_benefit(client):
    client.post("/payments/run")
    page = client.get("/system").text
    assert "1 Vorschläge offen" in page and "Abbuchungen für 1 von 1 offenen Rechnungen geprüft" in page
    with session_scope() as s:
        match_id = s.query(PaymentMatch).one().id
    client.post(f"/payments/{match_id}/accept")
    assert "1</strong> Rechnungen über Firefly als bezahlt erkannt (78,00 €)" in client.get("/system").text
