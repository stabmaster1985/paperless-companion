from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import pipeline, reimbursements
from app.db import session_scope
from app.extraction.rules import classify_by_keywords, extract_reimbursement
from app.firefly import FireflyClient, Transaction
from app.main import create_app
from app.models import Document, Invoice, Reimbursement
from tests.test_payments import FIREFLY
from tests.test_pipeline import run_all

STATEMENT = """Musterversicherung a.G.
Leistungsabrechnung vom 12.09.2026
Eingereichte Rechnungen: 2
Rechnungsbetrag gesamt 612,40 EUR
Erstattungsbetrag 420,00 EUR
Den Betrag überweisen wir auf Ihr Konto."""


def deposit(journal_id, day, amount, name="Musterversicherung a.G."):
    return Transaction(journal_id=journal_id, group_id=f"g{journal_id}", date=day, amount=amount, counterparty=name,
                       iban=None, description="Erstattung", text="Erstattung")


def test_recognize_and_extract():
    assert classify_by_keywords("Leistungsabrechnung September", "") == "reimbursement"
    assert classify_by_keywords("Scan_0001", STATEMENT) == "reimbursement"
    found = extract_reimbursement(STATEMENT, date(2026, 9, 14))
    assert found["amount"]["value"] == 420.0  # nicht der eingereichte Rechnungsbetrag
    assert found["statement_date"]["value"] == "2026-09-12"


def add_refund_case(s):
    """Erstattung 30 ← Einreichung 31 ← Rechnungen 32, 33; dazu Erstattung 40 mit eigener Rechnung 41."""
    s.add(Document(id=30, title="Leistungsabrechnung", kind="reimbursement", correspondent="Musterversicherung", tags=[],
                   review_state="confirmed", created=date(2026, 9, 12), paperless_fields={"related": [31]},
                   reimbursement=Reimbursement(payer="Musterversicherung", amount=420.0, statement_date=date(2026, 9, 12))))
    s.add(Document(id=31, title="Einreichung", kind="other", tags=[], paperless_fields={"related": [32, 33]}))
    s.add(Document(id=32, title="Zahnarzt", kind="invoice", tags=["Krankheitskosten"], created=date(2026, 8, 1),
                   invoice=Invoice(amount=500.0, status="paid")))
    s.add(Document(id=33, title="Labor", kind="invoice", tags=["Krankheitskosten"], created=date(2026, 8, 3),
                   suggestions={"amount": {"value": 112.4, "confidence": 0.9, "source": "rule"}}))
    s.add(Document(id=40, title="Andere Abrechnung", kind="reimbursement", tags=[], paperless_fields={"related": [41, 33]},
                   reimbursement=Reimbursement(amount=50.0, statement_date=date(2026, 10, 1))))
    s.add(Document(id=41, title="Physio", kind="invoice", tags=[], invoice=Invoice(amount=80.0)))
    s.add(Document(id=42, title="Hautarzt", kind="invoice", tags=["Krankheitskosten"], invoice=Invoice(amount=65.0)))


def test_summary_from_paperless_links(db, settings):
    with db() as s:
        add_refund_case(s)
    with db() as s:
        summ = reimbursements.summary(s, s.get(Document, 30))
        assert [d.id for d, _ in summ.invoices] == [32, 33] and [d.id for d in summ.others] == [31]
        assert summ.invoice_sum == 612.4 and summ.reimbursed == 420.0 and summ.own_share == 192.4
        # andere Erstattung: eigene Rechnung, nicht die der ersten (33 hängt an beiden – zählt bei beiden)
        assert [d.id for d, _ in reimbursements.summary(s, s.get(Document, 40)).invoices] == [33, 41]
        settings.reimbursable_tag = "krankheitskosten"
        data = reimbursements.overview(s, settings)
        assert [d.id for d, _ in data["missing"]] == [42]  # erstattungsfähig, aber nirgends verknüpft
        assert data["expected_count"] == 2 and data["expected_sum"] == 470.0


def test_match_deposits_accept_and_reject(db):
    with db() as s:
        add_refund_case(s)
    deposits = [deposit("1", date(2026, 9, 20), 420.0), deposit("2", date(2026, 9, 25), 420.0, "Jemand anders"),
                deposit("3", date(2026, 10, 5), 50.0, "Beihilfestelle")]
    with db() as s:
        assert reimbursements.match(s, deposits) == 2
    with db() as s:
        rec = s.get(Document, 30).reimbursement
        assert rec.tx_id == "1" and rec.tx_state == "suggested" and rec.status == "open"  # Absender passt
        reimbursements.reject(rec)
        assert rec.tx_id is None and rec.tx_rejected == ["1"]
    with db() as s:
        assert reimbursements.match(s, deposits) == 1  # nur noch „Jemand anders“ (eindeutig, da einziger Treffer)
        rec = s.get(Document, 30).reimbursement
        assert rec.tx_id == "2"
        reimbursements.accept(rec)
        assert rec.status == "received" and rec.received_on == date(2026, 9, 25)


def test_deposits_from_firefly(mock_api):
    route = mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json={"data": [{"type": "transactions", "id": "7", "attributes": {
        "transactions": [{"transaction_journal_id": "70", "type": "deposit", "date": "2026-09-20T00:00:00+02:00", "amount": "420.00",
                          "description": "Erstattung", "source_name": "Musterversicherung a.G.", "destination_name": "Girokonto"}]}}],
        "meta": {"pagination": {"total_pages": 1}}})
    tx = list(FireflyClient(FIREFLY, "t").deposits(date(2026, 9, 1), date(2026, 9, 30)))[0]
    assert tx.counterparty == "Musterversicherung a.G." and tx.amount == 420.0
    assert dict(route.calls.last.request.url.params)["type"] == "deposit"


@pytest.fixture
def client(settings, mock_api):
    settings.firefly_url, settings.firefly_token = FIREFLY, "t"
    settings.reimbursable_tag = "Krankheitskosten"
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        with session_scope() as s:
            add_refund_case(s)
        yield c


def test_pages_and_choose_deposit(client):
    assert "2 Erstattungen erwartet" in client.get("/").text and "1 Rechnungen ohne Erstattung" in client.get("/").text
    page = client.get("/reimbursements").text
    assert "Leistungsabrechnung" in page and "Eigenanteil <strong>192,40" in page and "Hautarzt" in page
    doc_page = client.get("/documents/30").text
    assert "Zugehörige Rechnungen" in doc_page and "192,40" in doc_page and "In Firefly nach dem Geldeingang suchen" in doc_page
    data = {"tx_id": "9", "group_id": "g9", "date": "2026-09-21", "amount": "420.0", "counterparty": "Musterversicherung a.G.",
            "take_sent": "1", "take": ["amount"]}
    r = client.post("/documents/30/payment", data=data, headers={"X-Requested-With": "fetch"})
    assert r.json()["saved"] is True
    assert r.json()["set"] == {"status": "received", "received_on": "2026-09-21", "amount": 420.0}
    with session_scope() as s:
        rec = s.get(Document, 30).reimbursement
        assert rec.status == "received" and rec.tx_id == "9" and rec.tx_state == "accepted"
    assert "Erhalten laut" in client.get("/documents/30").text
    r = client.post("/documents/30/payment/clear", headers={"X-Requested-With": "fetch"})
    assert r.json() == {"msg": r.json()["msg"], "previous": "2026-09-21", "date_field": "received_on"}
    with session_scope() as s:
        rec = s.get(Document, 30).reimbursement
        assert rec.status == "open" and rec.tx_rejected == ["9"]
    # ohne Firefly als erhalten markieren und zurück
    client.post("/reimbursements/30/received", data={"back": "/reimbursements"})
    with session_scope() as s:
        assert s.get(Document, 30).reimbursement.status == "received"


def test_confirm_reimbursement_from_inbox(client):
    with session_scope() as s:
        s.add(Document(id=50, title="Leistungsabrechnung Oktober", kind="reimbursement", tags=[], review_state="pending",
                       extraction_state="done", suggestions={"amount": {"value": 80.0, "confidence": 0.9, "source": "rule"}}))
    data = {"tx_id": "8", "group_id": "g8", "date": "2026-10-20", "amount": "80.0", "counterparty": "Beihilfe"}
    client.post("/documents/50/payment", data=data, headers={"X-Requested-With": "fetch"})
    client.post("/documents/50/confirm", data={"payer": "Beihilfe", "amount": "80,00", "status": "received",
                                               "received_on": "2026-10-20"})
    with session_scope() as s:
        rec = s.get(Document, 50).reimbursement
        assert rec.amount == 80.0 and rec.status == "received" and rec.tx_id == "8" and rec.tx_state == "accepted"


def test_doctype_mapping_other_and_reimbursement(db, settings):
    with db() as s:
        pipeline.save_doctype_settings(s, settings, {"Gehaltsabrechnung": "other", "Leistungsabrechnung": "reimbursement"}, [])
    doc = Document(id=1, title="Rechnung?", document_type="Gehaltsabrechnung", tags=[])
    assert pipeline.classify(doc, "Rechnungsbetrag 10,00", settings) == ("other", "doctype")
    doc.document_type = "Leistungsabrechnung"
    assert pipeline.classify(doc, "", settings) == ("reimbursement", "doctype")


def test_tax_export_mentions_refund(db, settings):
    from app import tax

    with db() as s:
        add_refund_case(s)
        s.get(Document, 32).created = date(2026, 8, 1)
    settings.tax_tags = "Krankheitskosten"
    with db() as s:
        items = {i.doc.id: i for i in tax.collect(s, 2026, settings)}
        assert items[32].refund == "#30 Leistungsabrechnung: 420,00 € von 612,40 € erstattet, Eigenanteil 192,40 €"
