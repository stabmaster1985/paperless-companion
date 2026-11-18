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


def add_pending(s, doc_id, **sugg):
    from app.extraction.rules import field

    doc = Document(id=doc_id, title=f"Rechnung {doc_id}", created=date(2026, 9, 1), correspondent="Stadtwerke",
                   kind="invoice", kind_source="doctype", review_state="pending", extraction_state="done",
                   suggestions={k: field(v, 0.9) for k, v in sugg.items()})
    s.add(doc)
    return doc


def test_pending_document_gets_payment_prefilled(db, settings):
    with db() as s:
        add_pending(s, 1, amount=78.0, invoice_date=date(2026, 9, 1), due_date=date(2026, 9, 15))
        add_pending(s, 2, invoice_number="RE-5555", invoice_date=date(2026, 9, 1))  # ohne Betrag
        add_pending(s, 3, invoice_date=date(2026, 9, 1))  # nichts Eindeutiges
    transactions = [
        tx("10", date(2026, 9, 12), 78.0, "Stadtwerke GmbH", "Abschlag"),
        tx("11", date(2026, 9, 20), 112.4, "Versorger", "Rechnung RE-5555"),
    ]
    with db() as s:
        assert payments.match_pending(s, transactions, payments.pending_candidates(s)) == 2
    with db() as s:
        one = s.get(Document, 1).suggestions
        assert one["status"]["value"] == "paid" and one["paid_on"] == {"value": "2026-09-12", "confidence": 0.75, "source": "firefly"}
        two = s.get(Document, 2).suggestions
        assert two["amount"]["value"] == 112.4 and two["amount"]["source"] == "firefly"
        assert "Betrag aus Buchung" in two["_payment"]["reasons"]
        assert "_payment" not in s.get(Document, 3).suggestions
        # Dokumente mit Vorschlag werden nicht erneut gesucht, Buchungen nicht doppelt verwendet
        assert [c.document.id for c in payments.pending_candidates(s)] == [3]
        assert payments._used_transactions(s) == {"10", "11"}


def test_check_document_after_analysis(db, mock_api):
    from tests.test_payments import FIREFLY_PAGE

    route = mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json=FIREFLY_PAGE)
    with db() as s:
        add_pending(s, 1, amount=78.0, invoice_number="RE-1", invoice_date=date(2026, 10, 5))
    with db() as s:
        found = payments.check_document(s, s.get(Document, 1), FireflyClient(FIREFLY, "t"), date(2026, 10, 20))
        assert found["date"] == "2026-10-10" and "Rechnungsnr." in found["reasons"]
        assert s.get(Document, 1).suggestions["status"]["confidence"] == 0.9
    params = dict(route.calls.last.request.url.params)
    assert params["start"] == "2026-09-28" and params["end"] == "2026-10-20"


@pytest.fixture
def review_client(settings, mock_api):
    settings.firefly_url, settings.firefly_token = FIREFLY, "t"
    mock_api.get(f"{FIREFLY}/api/v1/about").respond(json={"data": {"version": "6.2.0"}})
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            doc = add_pending(s, 1, amount=78.0, invoice_date=date(2026, 10, 5))
        with session_scope() as s:
            payments.apply_payment(s.get(Document, 1), 115, tx("900", date(2026, 10, 10), 78.0, "Stadtwerke", "Abschlag RE-1"),
                                   ["Betrag", "Rechnungsnr."])
        yield c


def test_review_page_and_confirm_records_match(review_client):
    page = review_client.get("/documents/1").text
    assert "In Firefly gefunden:" in page and "Firefly 90 %" in page and 'value="2026-10-10"' in page
    review_client.post("/documents/1/confirm", data={"amount": "78.00", "status": "paid", "paid_on": "2026-10-10",
                                                     "invoice_date": "2026-10-05"})
    with session_scope() as s:
        m = s.query(PaymentMatch).one()
        assert m.state == "accepted" and m.tx_id == "900" and s.get(Document, 1).invoice.status == "paid"


def test_accepted_payment_marks_firefly_source(client):
    client.post("/payments/run")
    with session_scope() as s:
        match_id = s.query(PaymentMatch).one().id
    client.post(f"/payments/{match_id}/accept")
    with session_scope() as s:
        src = s.get(Document, 1).field_sources["paid_on"]
        assert src["source"] == "firefly" and src["confirmed"] is True
    assert "Firefly ✓" in client.get("/invoices?status=paid").text


def test_links_to_firefly_and_dashboard(client, settings):
    settings.firefly_dashboard_url = "http://dash.test/"
    client.post("/payments/run")
    page = client.get("/invoices").text
    assert f'href="{FIREFLY}/transactions/show/500"' in page
    assert 'href="http://dash.test/#/buchungen?von=2026-10-10&amp;bis=2026-10-10"' in page
    assert "Passende Firefly-Buchung vom 10.10.2026" in client.get("/documents/1").text
    with session_scope() as s:
        match_id = s.query(PaymentMatch).one().id
    client.post(f"/payments/{match_id}/accept")
    assert "Bezahlt laut Firefly-Buchung vom 10.10.2026" in client.get("/documents/1").text
    paid = client.get("/invoices?status=paid").text
    assert "Firefly ✓" in paid and "http://dash.test/#/buchungen?von=2026-10-10" in paid


def test_inbox_prefill_links(review_client, settings):
    settings.firefly_dashboard_url = "http://dash.test"
    page = review_client.get("/documents/1").text
    assert "In Firefly gefunden:" in page and f"{FIREFLY}/transactions/show/" in page
    assert "http://dash.test/#/buchungen?von=2026-10-10&amp;bis=2026-10-10" in page


def test_search_choose_and_clear_in_inbox(review_client, mock_api):
    page = review_client.get("/documents/1").text
    assert "Passt die Buchung nicht? Andere in Firefly suchen" in page and "Passt nicht – Zuordnung lösen" in page
    second = {"type": "transactions", "id": "501", "attributes": {"transactions": [{
        "transaction_journal_id": "901", "type": "withdrawal", "date": "2026-10-12T00:00:00+02:00", "amount": "79.00",
        "description": "Strom Oktober", "destination_name": "Stadtwerke Musterstadt"}]}}
    route = mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json={**FIREFLY_PAGE, "data": [*FIREFLY_PAGE["data"], second]})
    html = review_client.get("/documents/1/firefly-search", params={"amount": "79,00", "q": "Stadtwerke",
                                                                    "start": "2026-10-01", "end": "2026-10-31"}).text
    assert html.index("79,00") < html.index("78,00")  # exakter Betrag zuerst
    assert "vorbelegt<" in html and "Werte übernehmen" in html and "Diese übernehmen" in html and "Betrag ähnlich" in html
    assert dict(route.calls.last.request.url.params)["start"] == "2026-10-01"
    # anderer Betrag (nur Empfänger passt): eingeklappt unter „weitere Buchungen“, nicht zwischen den Treffern
    third = {"type": "transactions", "id": "502", "attributes": {"transactions": [{
        "transaction_journal_id": "902", "type": "withdrawal", "date": "2026-10-20T00:00:00+02:00", "amount": "312.40",
        "description": "Jahresabrechnung", "destination_name": "Stadtwerke Musterstadt"}]}}
    mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json={**FIREFLY_PAGE, "data": [*FIREFLY_PAGE["data"], second, third]})
    html = review_client.get("/documents/1/firefly-search", params={"amount": "79,00", "q": "Stadtwerke",
                                                                    "start": "2026-10-01", "end": "2026-10-31"}).text
    assert "2 Buchungen" in html and "1 weitere Buchung mit anderem Betrag" in html
    assert html.index("312,40") > html.index("weitere Buchung")
    r = review_client.post("/documents/1/payment", data={"tx_id": "901", "group_id": "501", "date": "2026-10-12",
                                                         "amount": "79.0", "counterparty": "Stadtwerke Musterstadt",
                                                         "description": "Strom Oktober"})
    assert "vorbelegt" in r.text
    with session_scope() as s:
        sugg = s.get(Document, 1).suggestions
        assert sugg["_payment"]["tx_id"] == "901" and sugg["paid_on"]["value"] == "2026-10-12"
    review_client.post("/documents/1/payment/clear")
    with session_scope() as s:
        doc = s.get(Document, 1)
        assert "_payment" not in doc.suggestions and "paid_on" not in doc.suggestions
        assert doc.suggestions["_payment_rejected"] == ["901"]
        # wird beim nächsten Abgleich nicht wieder vorgeschlagen
        c = payments.candidate(doc)
        assert payments.best_payment(c, [tx("901", date(2026, 10, 12), 78.0, "Stadtwerke")], set()) is None


def test_choose_other_payment_for_confirmed_invoice(db):
    with db() as s:
        inv = add_invoice(s, 1, 78.0, issuer="Stadtwerke")
        inv.document.review_state = "confirmed"
        s.flush()
        s.add(PaymentMatch(invoice_id=inv.id, tx_id="900", tx_group_id="500", tx_date=date(2026, 9, 10), amount=78.0,
                           counterparty="Stadtwerke", description="falsch", score=70, reasons="Betrag", state="suggested"))
    with db() as s:
        doc = s.get(Document, 1)
        payments.choose(s, doc, tx("901", date(2026, 9, 12), 78.5, "Unbekannter Empfänger", "richtig"))
    with db() as s:
        states = {m.tx_id: m.state for m in s.query(PaymentMatch)}
        assert states == {"900": "rejected", "901": "accepted"}
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.paid_on == date(2026, 9, 12)
        assert inv.amount == 78.5 and inv.issuer == "Unbekannter Empfänger"  # überschrieben (Standard: alles übernehmen)
        assert payments.clear(s, s.get(Document, 1))
    with db() as s:
        inv = s.get(Document, 1).invoice
        assert inv.status == "open" and inv.paid_on is None
        assert {m.state for m in s.query(PaymentMatch)} == {"rejected"}


def test_partial_payments(db, settings):
    """Ratenzahlung: mehrere Buchungen, bezahlt erst wenn die Summe stimmt; der Rest wird automatisch gefunden."""
    with db() as s:
        inv = add_invoice(s, 1, 300.0, issuer="Möbelhaus")
        inv.document.review_state = "confirmed"
    with db() as s:
        msg, form = payments.add_partial(s, s.get(Document, 1), tx("901", date(2026, 9, 12), 100.0, "Möbelhaus"))
        assert "noch offen: 200,00 €" in msg and form == {"status": "open", "paid_on": ""}
        payments.add_partial(s, s.get(Document, 1), tx("902", date(2026, 10, 12), 100.0, "Möbelhaus"))
        inv = s.get(Document, 1).invoice
        assert inv.status == "open" and inv.paid_amount == 200.0 and inv.open_amount == 100.0
    with db() as s:  # Restzahlung (100 €, nicht 300 €) wird im Abgleich vorgeschlagen
        counts = payments.match(s, [tx("903", date(2026, 11, 12), 100.0, "Möbelhaus")], settings)
        assert counts["suggested"] == 1
        m = s.query(PaymentMatch).filter_by(tx_id="903").one()
        payments.accept(s, m.id)
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.paid_on == date(2026, 11, 12) and inv.paid_amount is None
        assert [p.tx_id for p in payments.partial_payments(s, inv)] == ["901", "902", "903"]
    with db() as s:  # eine Rate lösen: wieder offen
        assert payments.remove_partial(s, s.get(Document, 1), "902")
        inv = s.get(Document, 1).invoice
        assert inv.status == "open" and inv.paid_amount == 200.0 and s.get(Document, 1).writeback_pending


def test_partial_payment_in_ui(client):
    with session_scope() as s:
        s.get(Document, 1).review_state = "confirmed"
        s.get(Document, 1).invoice.amount = 156.0
    data = {"tx_id": "950", "group_id": "550", "date": "2026-10-10", "amount": "78.0", "counterparty": "Stadtwerke",
            "partial": "1"}
    r = client.post("/documents/1/payment", data=data, headers={"X-Requested-With": "fetch"})
    assert r.json()["reload"] and "noch offen" in r.json()["msg"]
    page = client.get("/documents/1").text
    assert "Teilweise bezahlt" in page and "noch offen <strong>78,00" in page
    page = client.get("/invoices?status=partial").text
    assert "Teilzahlungen (1)" in page and "1 Zahlung · 78,00 €" in page and "10.10.2026 · 78,00 €" in page
    client.post("/documents/1/payment/remove", data={"tx_id": "950"})
    assert "Teilweise bezahlt" not in client.get("/documents/1").text


def test_choose_and_clear_without_reload(review_client):
    data = {"tx_id": "901", "group_id": "501", "date": "2026-10-12", "amount": "79.0", "iban": "DE02120300000000202051",
            "counterparty": "STADTWERKE GmbH & Co. KG"}
    # Auswahl: nur Betrag und Aussteller (IBAN nicht)
    r = review_client.post("/documents/1/payment", data={**data, "take_sent": "1", "take": ["amount", "issuer"]},
                           headers={"X-Requested-With": "fetch"})
    assert r.json()["saved"] is False
    # Firefly-Name -> dein Korrespondent; IBAN nicht angehakt
    assert r.json()["set"] == {"status": "paid", "paid_on": "2026-10-12", "amount": 79.0, "issuer": "Stadtwerke"}
    assert r.json()["label"] == "Buchung vom 12.10.2026 (79,00 €)"
    with session_scope() as s:
        sugg = s.get(Document, 1).suggestions
        assert sugg["amount"]["value"] == 79.0 and sugg["amount"]["source"] == "firefly" and "iban" not in sugg
    r = review_client.post("/documents/1/payment/clear", headers={"X-Requested-With": "fetch"})
    assert r.json()["previous"] == "2026-10-12" and r.json()["date_field"] == "paid_on" and "gelöst" in r.json()["msg"]


def test_assigned_transaction_not_reused(db, settings):
    """Eine auf der Belegseite (oder per Abgleich) zugeordnete Buchung wird keiner anderen Rechnung vorgeschlagen."""
    from app.models import ReceiptCheck

    with db() as s:
        add_invoice(s, 1, 78.0, issuer="Stadtwerke")
        add_invoice(s, 2, 78.0, issuer="Stadtwerke")
        s.add(ReceiptCheck(tx_id="900", tx_group_id="500", tx_date=date(2026, 10, 10), amount=78.0, counterparty="Stadtwerke",
                           state="linked", decided=True, document_id=2))
    with db() as s:
        payments.match(s, [tx("900", date(2026, 10, 10), 78.0, "Stadtwerke", "Abschlag")], settings)
        assert [(m.invoice.document_id, m.tx_id) for m in s.query(PaymentMatch)] == [(2, "900")]  # nur für Dokument 2
        assert payments.used_elsewhere(s, exclude_doc=1) == {"900": 2}


def test_refund_on_invoice(client, mock_api):
    """Rücksendung/Gutschrift: Geldeingang aus Firefly oder von Hand; Rechnung bleibt bezahlt, es zählt der Rest."""
    from app import tax

    with session_scope() as s:
        doc = s.get(Document, 1)
        doc.review_state, doc.tags = "confirmed", ["Steuer"]
        doc.invoice.amount, doc.invoice.status = 120.0, "paid"
    route = mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json={"data": [{"type": "transactions", "id": "77", "attributes": {
        "transactions": [{"transaction_journal_id": "770", "type": "deposit", "date": "2026-10-20T00:00:00+02:00", "amount": "40.00",
                          "description": "Gutschrift Rücksendung", "source_name": "Stadtwerke"}]}}],
        "meta": {"pagination": {"total_pages": 1}}})
    page = client.get("/documents/1").text
    assert "Rückerstattung zuordnen" in page
    html = client.get("/documents/1/firefly-search", params={"refund": "1", "amount": "40", "q": "Stadtwerke",
                                                             "start": "2026-10-01", "end": "2026-10-31"}).text
    assert "Als Rückerstattung übernehmen" in html and dict(route.calls.last.request.url.params)["type"] == "deposit"
    r = client.post("/documents/1/refund", data={"tx_id": "770", "group_id": "77", "date": "2026-10-20", "amount": "40.0",
                                                 "counterparty": "Stadtwerke"})
    assert "teilweise erstattet – kostet dich noch 80,00 €" in r.text
    with session_scope() as s:
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.refund_amount == 40.0 and inv.net_amount == 80.0 and inv.refund_tx_id == "770"
        assert payments.refund_transactions(s) == {"770": 1}
    assert "40,00 € zurück · netto 80,00" in client.get("/invoices?status=all").text
    client.post("/documents/1/refund", data={"amount": "120,00", "date": "2026-10-21"})  # von Hand: alles zurück
    with session_scope() as s:
        assert s.get(Document, 1).invoice.net_amount == 0.0 and s.get(Document, 1).invoice.refund_tx_id is None
    client.post("/documents/1/refund", data={"action": "clear"})
    with session_scope() as s:
        assert s.get(Document, 1).invoice.refund_amount is None


def test_tax_counts_net_after_refund(db, settings):
    from app import tax

    with db() as s:
        inv = add_invoice(s, 1, 120.0, created=date(2026, 3, 1))
        inv.document.tags, inv.document.review_state = ["Steuer"], "confirmed"
        inv.refund_amount, inv.refunded_on = 40.0, date(2026, 3, 20)
    settings.tax_tags = "Steuer"
    with db() as s:
        item = next(i for i in tax.collect(s, 2026, settings) if i.doc.id == 1)
        assert item.amount == 80.0 and item.refund == "Rückerstattung 40,00 € am 20.03.2026, ursprünglich 120,00 €"


def test_refund_document(client):
    """Gutschrift-Dokument als Beleg: in Paperless verknüpft wird es vorgeschlagen, Betrag und Datum kommen daraus,
    und aus der Inbox fällt es als vermeintlich neue Rechnung heraus."""
    with session_scope() as s:
        doc = s.get(Document, 1)
        doc.review_state, doc.invoice.status, doc.invoice.amount = "confirmed", "paid", 120.0
        doc.paperless_fields = {"related": [950]}
        s.add(Document(id=950, title="Gutschrift Rücksendung", correspondent="Stadtwerke", kind="invoice", tags=[],
                       review_state="pending", created=date(2026, 10, 22),
                       suggestions={"amount": {"value": 40.0, "confidence": 0.8, "source": "rule"}}))
    page = client.get("/documents/1").text
    assert "Gutschrift Rücksendung" in page and "Als Gutschrift-Beleg" in page  # über „gehört zu“ vorgeschlagen
    assert "Gutschrift Rücksendung" in client.get("/documents/1/refund-docs?q=gutschrift").text
    client.post("/documents/1/refund", data={"action": "doc", "refund_doc_id": "950"})
    with session_scope() as s:
        inv = s.get(Document, 1).invoice
        assert inv.refund_doc_id == 950 and inv.refund_amount == 40.0 and inv.refunded_on == date(2026, 10, 22)
        gs = s.get(Document, 950)
        assert gs.review_state == "none" and gs.kind == "other"  # keine neue Rechnung in der Inbox
    assert "Gutschrift-Beleg zur Rechnung" in client.get("/documents/950").text
    client.post("/documents/1/refund", data={"action": "doc-clear"})
    with session_scope() as s:
        assert s.get(Document, 1).invoice.refund_doc_id is None and s.get(Document, 1).invoice.refund_amount == 40.0


def test_suggest_further_installments(db, settings):
    """Teilweise bezahlte Rechnung: weitere Buchungen wie die bisherigen Raten werden vorgeschlagen – bis zum Rest,
    nicht an andere Empfänger, nie automatisch übernommen."""
    settings.firefly_auto_accept = True
    with db() as s:
        inv = add_invoice(s, 1, 300.0, issuer="Möbelhaus", invoice_number="RE-77")
        inv.document.review_state = "confirmed"
    with db() as s:
        payments.add_partial(s, s.get(Document, 1), tx("r1", date(2026, 9, 1), 100.0, "Möbelhaus GmbH", "Rate 1"))
    later = [tx("r2", date(2026, 10, 1), 100.0, "Möbelhaus GmbH", "Rate 2"),
             tx("x1", date(2026, 10, 2), 100.0, "Tankstelle", "Tanken"),           # gleicher Betrag, anderer Empfänger
             tx("r3", date(2026, 11, 1), 100.0, "Möbelhaus GmbH", "Rate 3"),
             tx("r4", date(2026, 12, 1), 100.0, "Möbelhaus GmbH", "Rate 4")]     # wäre über dem Rest
    with db() as s:
        counts = payments.match(s, later, settings)
        sugg = {m.tx_id: m for m in s.query(PaymentMatch).filter_by(state="suggested")}
        assert set(sugg) == {"r2", "r3"} and counts["accepted"] == 0
        assert sugg["r2"].reasons.startswith("Teilzahlung") and "Empfänger wie bisher" in sugg["r2"].reasons
        payments.accept(s, sugg["r2"].id)
        inv = s.get(Document, 1).invoice
        assert inv.status == "open" and inv.open_amount == 100.0  # als Teilzahlung, nicht „bezahlt“
        payments.accept(s, sugg["r3"].id)
        assert s.get(Document, 1).invoice.status == "paid"


def test_collective_payment_for_several_invoices(db, settings):
    """Eine Überweisung über die Summe mehrerer Rechnungen desselben Ausstellers wird für jede vorgeschlagen."""
    settings.firefly_auto_accept = True
    with db() as s:
        for i, amount in enumerate([120.0, 80.5, 99.0]):
            add_invoice(s, 10 + i, amount, issuer="Praxis Dr. Muster", invoice_number=f"PR-{i + 1}")
        add_invoice(s, 20, 80.5, issuer="Anderer Handwerker")
    with db() as s:
        counts = payments.match(s, [tx("s1", date(2026, 9, 20), 200.5, "Praxis Dr. Muster", "PR-1 PR-2")], settings)
        sugg = s.query(PaymentMatch).filter_by(tx_id="s1").all()
        assert sorted(m.invoice.document_id for m in sugg) == [10, 11] and counts["accepted"] == 0
        assert sugg[0].reasons.startswith("Sammelzahlung (2 Rechnungen, zusammen 200,50 €)")
        assert {m.amount for m in sugg} == {120.0, 80.5}  # je Rechnung ihr Anteil
        for m in sugg:
            payments.accept(s, m.id)
        assert s.get(Document, 10).invoice.status == "paid" and s.get(Document, 11).invoice.status == "paid"
    with db() as s:  # nicht eindeutig (zwei gleich große Kombinationen) -> kein Vorschlag
        add_invoice(s, 30, 50.0, issuer="Schreiner Holz")
        add_invoice(s, 31, 50.0, issuer="Schreiner Holz")
        add_invoice(s, 32, 50.0, issuer="Schreiner Holz")
    with db() as s:
        payments.match(s, [tx("s2", date(2026, 9, 21), 100.0, "Schreiner Holz", "")], settings)
        assert s.query(PaymentMatch).filter_by(tx_id="s2").count() == 0


def test_suggest_refund_for_paid_invoice(client, mock_api):
    """Geld vom Aussteller nach der Zahlung: als Rückerstattung vorgeschlagen, mit einem Klick übernommen."""
    with session_scope() as s:
        doc = s.get(Document, 1)
        doc.review_state, doc.invoice.amount, doc.invoice.status = "confirmed", 59.99, "paid"
        doc.invoice.paid_on, doc.invoice.issuer = date(2026, 10, 1), "Online-Shop"
    deposits = [Transaction(journal_id="d1", group_id="g1", date=date(2026, 10, 12), amount=59.99, counterparty="Online-Shop GmbH",
                            iban=None, description="Erstattung Retoure 4711", text="Erstattung Retoure 4711"),
                Transaction(journal_id="d2", group_id="g2", date=date(2026, 10, 13), amount=59.99, counterparty="Arbeitgeber",
                            iban=None, description="Lohn", text="Lohn")]
    with session_scope() as s:
        assert payments.suggest_refunds(s, deposits) == 1
        sug = s.get(Document, 1).invoice.refund_suggestion
        assert sug["tx_id"] == "d1" and "Erstattung im Text" in sug["reasons"]
    assert "Mögliche Rückerstattungen (1)" in client.get("/invoices").text
    page = client.get("/bulk").text
    assert 'value="rf:1" checked' in page  # gleicher Betrag + Erstattung im Text: sicher
    client.post("/documents/1/refund", data={"action": "accept-suggestion"})
    with session_scope() as s:
        inv = s.get(Document, 1).invoice
        assert inv.refund_amount == 59.99 and inv.refund_tx_id == "d1" and inv.refund_suggestion is None
        assert payments.suggest_refunds(s, deposits) == 0  # schon erstattet
