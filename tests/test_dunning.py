from datetime import date

from fastapi.testclient import TestClient

from app import dunning, pipeline
from app.db import session_scope
from app.main import create_app
from app.models import Document, Invoice
from tests.test_pipeline import run_all

REMINDER = """Möbelhaus Muster GmbH
2. Mahnung
Rechnungsnummer: RE-2026-0815
Sehr geehrter Herr Muster,
leider konnten wir zu unserer Rechnung vom 01.09.2026 über 349,00 € noch keinen Zahlungseingang feststellen.
Bitte überweisen Sie den Betrag zuzüglich Mahngebühr 5,00 € bis spätestens 30.11.2026."""


def add_invoice(s, doc_id=800, status="open"):
    doc = Document(id=doc_id, title="Rechnung Sofa", correspondent="Möbelhaus Muster", kind="invoice", tags=[],
                   review_state="confirmed", created=date(2026, 9, 1))
    doc.invoice = Invoice(issuer="Möbelhaus Muster", amount=349.0, invoice_number="RE-2026-0815", status=status,
                          due_date=date(2026, 9, 15))
    s.add(doc)
    return doc


def test_detect_reminder_and_invoice(db):
    assert dunning.is_reminder("Scan_0042", REMINDER) and dunning.is_reminder("Zahlungserinnerung Oktober")
    assert not dunning.is_reminder("Rechnung Sofa", "Rechnungsnummer RE-1 Betrag 349,00 €")
    found = dunning.extract(REMINDER)
    assert found == {"invoice_number": "RE-2026-0815", "due": date(2026, 11, 30), "fee": 5.0}
    with db() as s:
        add_invoice(s)
        doc = Document(id=801, title="Scan", correspondent="Möbelhaus Muster", tags=[], created=date(2026, 11, 2))
        s.add(doc)
        s.flush()
        assert dunning.detect(s, doc, REMINDER) == {"invoice": 800, "due": "2026-11-30", "fee": 5.0, "date": "2026-11-02"}


def test_reminder_is_not_a_new_invoice(db, settings):
    class FakePaperless:
        def get_document(self, doc_id):
            return {"content": REMINDER}

    with db() as s:
        add_invoice(s)
        doc = Document(id=802, title="Mahnung Möbelhaus", correspondent="Möbelhaus Muster", tags=[], created=date(2026, 11, 2))
        s.add(doc)
        s.flush()
        pipeline.process_document(s, doc, FakePaperless(), None, settings)
        assert doc.kind == "other" and doc.review_state == "none" and doc.suggestions["_reminder"]["invoice"] == 800


def test_assign_reminder_on_invoices_page(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        with session_scope() as s:
            add_invoice(s)
            s.add(Document(id=803, title="Mahnung", correspondent="Möbelhaus Muster", tags=[], created=date(2026, 11, 2),
                           kind="other", suggestions={"_reminder": {"invoice": 800, "due": "2026-11-30", "fee": 5.0,
                                                                    "date": "2026-11-02"}}))
        page = c.get("/invoices").text
        assert "Mahnungen (1)" in page and 'value="2026-11-30"' in page
        r = c.post("/reminders/803", data={"invoice_doc": "800", "day": "2026-11-02", "due": "2026-11-30", "fee": "5,00",
                                            "take_due": "1", "link": "1"})
        assert "neue Frist 30.11.2026" in r.text and "„gehört zu“ vorgemerkt" in r.text
        with session_scope() as s:
            inv = s.get(Document, 800).invoice
            assert inv.due_date == date(2026, 11, 30) and inv.reminders[0]["fee"] == 5.0
            assert s.get(Document, 803).proposed_fields["related"] == [800]
        assert "1× gemahnt" in c.get("/invoices").text and "Mahnungen (" not in c.get("/invoices").text
        assert "Mahnung zur Rechnung" in c.get("/documents/803").text
        assert "1× gemahnt:" in c.get("/documents/800").text
