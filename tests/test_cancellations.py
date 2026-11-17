from datetime import date

from fastapi.testclient import TestClient

from app import cancellations, pipeline
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document
from tests.test_pipeline import run_all

CONFIRMATION = """Mobilfunk AG
Kundennummer: 4711-0815
Kündigungsbestätigung
Sehr geehrter Herr Muster,
wir bestätigen Ihnen den Eingang Ihrer Kündigung vom 02.10.2026.
Ihr Vertrag endet zum 31.12.2026. Bis dahin gelten die vereinbarten Konditionen."""


def add_contract(s, doc_id=500, provider="Mobilfunk AG", number="4711-0815"):
    doc = Document(id=doc_id, title="Handyvertrag", correspondent=provider, kind="contract", tags=[], review_state="confirmed")
    doc.contract = Contract(name="Handyvertrag", provider=provider, contract_number=number, start_date=date(2024, 1, 1),
                            initial_term_months=24, renewal_mode="indefinite", notice_value=1, notice_unit="months")
    s.add(doc)
    return doc.contract


def test_detect_confirmation_and_contract(db):
    assert cancellations.is_cancellation("Scan_0001", CONFIRMATION)
    assert not cancellations.is_cancellation("Handyvertrag", "Laufzeit 24 Monate, Kündigung mit 1 Monat Frist")
    found = cancellations.extract(CONFIRMATION)
    assert found["effective"] == date(2026, 12, 31) and found["contract_number"] == "4711-0815"
    with db() as s:
        contract = add_contract(s)
        add_contract(s, 501, provider="Stromversorger", number="99887766")
        doc = Document(id=502, title="Schreiben", correspondent=None, tags=[], created=date(2026, 10, 5))
        s.add(doc)
        s.flush()
        info = cancellations.detect(s, doc, CONFIRMATION)
        assert info == {"contract": contract.id, "effective": "2026-12-31", "date": "2026-10-05"}  # über die Kundennummer


def test_confirmation_is_not_a_new_contract(db, settings):
    """„Vertrag“ + „Kündigung“ im Text machte daraus bisher einen neuen Vertrag – jetzt „Sonstiges“ mit Hinweis."""
    class FakePaperless:
        def get_document(self, doc_id):
            return {"content": CONFIRMATION}

    with db() as s:
        add_contract(s)
        doc = Document(id=503, title="Kündigungsbestätigung Mobilfunk", correspondent="Mobilfunk AG", tags=[],
                       created=date(2026, 10, 5))
        s.add(doc)
        s.flush()
        pipeline.process_document(s, doc, FakePaperless(), None, settings)
        assert doc.kind == "other" and doc.review_state == "none" and doc.contract is None
        assert doc.suggestions["_cancellation"]["effective"] == "2026-12-31"


def test_contracts_page_apply_and_dismiss(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        with session_scope() as s:
            contract = add_contract(s)
            s.flush()
            contract_id = contract.id
            s.add(Document(id=504, title="Kündigungsbestätigung", correspondent="Mobilfunk AG", tags=[], created=date(2026, 10, 5),
                           kind="other", suggestions={"_cancellation": {"contract": None, "effective": "2026-12-31",
                                                                        "date": "2026-10-05"}}))
            s.add(Document(id=505, title="Ihre Kündigung vom 01.09.", correspondent="Fitness", tags=[], created=date(2026, 9, 3)))
        page = c.get("/contracts").text
        assert "Kündigungsbestätigungen (2)" in page and "nach dem Titel erkannt" in page
        r = c.post("/cancellations/504", data={"contract_id": str(contract_id), "cancelled_on": "2026-10-02",
                                               "effective": "2026-12-31"})
        assert "als gekündigt markiert (endet 31.12.2026)" in r.text
        with session_scope() as s:
            contract = s.get(Contract, contract_id)
            assert contract.status == "cancelled" and contract.cancelled_on == date(2026, 10, 2)
            assert contract.cancel_effective == date(2026, 12, 31) and contract.cancellation_doc_id == 504
        assert "Kündigungsbestätigung</a>" in c.get("/documents/500").text
        c.post("/cancellations/505", data={"action": "dismiss"})
        assert "Kündigungsbestätigungen" not in c.get("/contracts").text
