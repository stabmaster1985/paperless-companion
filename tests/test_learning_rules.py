import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import learning, receipts, rules
from app.db import session_scope
from app.extraction.rules import field
from app.main import create_app
from app.models import Document, Invoice, MetaDecision, ReceiptCheck, RuleProposal
from tests.test_payments import FIREFLY, tx
from tests.test_pipeline import run_all


def confirmed_invoice(s, doc_id, corr, invoice_date, due_date, method="transfer", iban="DE02120300000000202051", amount=50.0):
    doc = Document(id=doc_id, title=f"Rechnung {doc_id}", correspondent=corr, kind="invoice", kind_source="doctype",
                   review_state="confirmed", extraction_state="done", suggestions={}, tags=[])
    doc.invoice = Invoice(amount=amount, invoice_date=invoice_date, due_date=due_date, payment_method=method, iban=iban,
                          status="open", issuer=corr)
    s.add(doc)
    return doc


# --------------------------------------------------------------------------- 5: Felder lernen


def test_profiles_and_apply(db):
    with db() as s:
        confirmed_invoice(s, 101, "Versorger", date(2026, 1, 1), date(2026, 1, 15))
        confirmed_invoice(s, 102, "Versorger", date(2026, 2, 1), date(2026, 2, 15))
        confirmed_invoice(s, 103, "Versorger", date(2026, 3, 1), date(2026, 3, 16))
        confirmed_invoice(s, 104, "Einmalig", date(2026, 3, 1), date(2026, 3, 16))  # nur eine -> nichts gelernt
    with db() as s:
        p = learning.profiles(s)
        assert list(p) == ["Versorger"]
        assert p["Versorger"]["fields"]["due_days"]["value"] == 14
        assert p["Versorger"]["fields"]["payment_method"]["value"] == "transfer"
        doc = Document(id=200, title="Neue Rechnung", correspondent="Versorger", tags=[])
        sugg = {"invoice_date": field(date(2026, 4, 1), 0.9), "payment_method": field("transfer", 0.5, "default")}
        filled = learning.apply(s, doc, "invoice", sugg, 0.8)
        assert set(filled) == {"payment_method", "iban", "due_date"}
        assert sugg["due_date"] == {"value": "2026-04-15", "confidence": 0.85, "source": "learned"}
        # Abschalten wirkt sofort
        learning.set_disabled(s, "Versorger", "iban", True)
        sugg = {"invoice_date": field(date(2026, 4, 1), 0.9)}
        assert "iban" not in learning.apply(s, doc, "invoice", sugg, 0.8)
        # Sichere Regel-Treffer werden nicht überschrieben
        sugg = {"invoice_date": field(date(2026, 4, 1), 0.9), "due_date": field(date(2026, 4, 30), 0.9)}
        learning.apply(s, doc, "invoice", sugg, 0.8)
        assert sugg["due_date"]["value"] == "2026-04-30"


@pytest.fixture
def client(settings, mock_api):
    settings.firefly_url, settings.firefly_token = FIREFLY, "t"
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_accuracy_counted_on_first_confirmation(client):
    with session_scope() as s:
        doc = s.get(Document, 3)
        doc.review_state = "pending"
        sugg = doc.suggestions
    form = {k: v["value"] for k, v in sugg.items() if not k.startswith("_") and v.get("value") is not None}
    form["amount"] = "1,00"  # Regel lag beim Betrag falsch
    client.post("/documents/3/confirm", data=form)
    with session_scope() as s:
        rows = {(r["source"], r["field"]): r for r in learning.accuracy(s)}
    assert rows[("rule", "amount")]["correct"] == 0 and rows[("rule", "amount")]["total"] == 1
    assert rows[("rule", "due_date")]["rate"] == 100
    page = client.get("/rules").text
    assert "Wie gut ist die Erkennung?" in page and "0 von 1" in page


# --------------------------------------------------------------------------- 1: Belege


def test_receipt_check_states(db, settings):
    settings.receipts_min_amount = 20
    with db() as s:
        inv_doc = confirmed_invoice(s, 101, "Elektromarkt", date(2026, 9, 1), date(2026, 9, 15), amount=249.9)
        s.add(Document(id=102, title="Kassenbon Baumarkt", correspondent="Baumarkt Müller", created=date(2026, 9, 9), tags=[]))
    transactions = [
        tx("1", date(2026, 9, 10), 249.9, "Elektromarkt GmbH", "Kauf"),  # Rechnung mit gleichem Betrag
        tx("2", date(2026, 9, 10), 87.5, "Baumarkt Müller", "Einkauf"),  # Dokument desselben Absenders in der Nähe
        tx("3", date(2026, 9, 11), 120.0, "Unbekannt AG", "Lastschrift"),  # nichts
        tx("4", date(2026, 9, 12), 12.0, "Kiosk", ""),  # unter Mindestbetrag
        tx("5", date(2026, 9, 12), 60.0, "Supermarkt Nord", ""),
    ]
    with db() as s:
        receipts.set_ignored_counterparties(s, ["supermarkt"])
    with db() as s:
        counts = receipts.check(s, transactions, settings)
        assert counts["checked"] == 4 and counts["missing"] == 1 and counts["candidate"] == 2
        rows = {r.tx_id: r for r in s.query(ReceiptCheck)}
        assert rows["1"].candidates[0]["id"] == 101 and "gleicher Betrag" in rows["1"].candidates[0]["reason"]
        assert rows["2"].candidates[0]["id"] == 102 and rows["5"].state == "ignored"
        assert receipts.link(s, rows["3"], 102)
    with db() as s:
        receipts.check(s, transactions, settings)  # deine Entscheidung bleibt
        assert s.query(ReceiptCheck).filter_by(tx_id="3").one().state == "linked"


def test_receipts_page_and_actions(client, mock_api):
    from tests.test_payments import FIREFLY_PAGE

    mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json=FIREFLY_PAGE)
    client.post("/receipts/run")
    page = client.get("/receipts").text
    assert "Stadtwerke" in page and "78,00 €" in page and "Buchung prüfen:" in page
    with session_scope() as s:
        row = s.query(ReceiptCheck).one()
        assert row.state == "candidate" and row.candidates[0]["id"] == 1
    client.post(f"/receipts/{row.id}/link", data={"doc_id": "1"})
    assert "zugeordnet" in client.get("/receipts?state=linked").text
    client.post(f"/receipts/{row.id}/reopen")
    client.post(f"/receipts/{row.id}/ignore-counterparty")
    page = client.get("/receipts?state=ignored").text
    assert "Ausgeblendete Empfänger (1)" in page and "Empfänger ausgeblendet" in page


# --------------------------------------------------------------------------- 6: Regelvorschläge


def add_docs(s, corr, n, tags, doctype="Rechnung", start=300):
    for i in range(n):
        s.add(Document(id=start + i, title=f"{corr} {i}", correspondent=corr, document_type=doctype,
                       tags=list(tags), extraction_state="skipped"))


def test_rule_proposals_and_preview(db):
    with db() as s:
        add_docs(s, "Stadtwerke Nord", 9, ["Strom", "Haus", "2024"])
        s.add(Document(id=399, title="Abschlag ohne Tags", correspondent="Stadtwerke Nord", document_type="Brief", tags=["Strom"],
                       meta_accepted={"tag:Haus": {"value": "Haus", "source": "llm"}}, extraction_state="skipped"))
        add_docs(s, "Wenige", 3, ["X"], start=500)  # zu wenige Dokumente
    with db() as s:
        found = rules.compute(s)
        assert list(found) == ["Stadtwerke Nord"]
        # Jahres-Tag wird nie vorgeschlagen; vorgemerktes „Haus“ zählt mit
        assert set(found["Stadtwerke Nord"]["tags"]) == {"Strom", "Haus"}
        assert found["Stadtwerke Nord"]["document_type"] == {"Rechnung": 9}
        assert rules.refresh(s) == 1
    with db() as s:
        p = s.query(RuleProposal).one()
        pv = rules.preview(s, p)
        assert pv["total"] == 10 and pv["unchanged"] == 9 and pv["conflicts"][0]["id"] == 399  # Typ Brief -> Rechnung
        # Verworfener Tag wird nicht (mehr) vorgeschlagen
        s.add(MetaDecision(document_id=300, key="tag:Haus", item="tag", correspondent="Stadtwerke Nord", value="Haus",
                           source="llm", decision="dismissed"))
    with db() as s:
        rules.refresh(s)
        assert s.query(RuleProposal).one().tags == ["Strom"]


def test_rules_page_edit_approve(client, mock_api):
    with session_scope() as s:
        add_docs(s, "Stadtwerke", 6, ["Haus"], start=300)
    page = client.get("/rules").text
    assert "Wenn Korrespondent = „Stadtwerke“" in page and "Auswirkung auf deine" in page
    with session_scope() as s:
        pid = s.query(RuleProposal).one().id
    client.post(f"/rules/proposals/{pid}", data={"action": "save", "tag": ["Haus", "Steuer"], "document_type": "Rechnung"})
    with session_scope() as s:
        p = s.get(RuleProposal, pid)
        assert p.tags == ["Haus", "Steuer"] and p.edited
    client.post(f"/rules/proposals/{pid}", data={"action": "approved"})
    page = client.get("/rules").text
    assert "So legst du die Regel in Paperless an" in page and "Hat Korrespondent" in page
    assert '"assign_tags": [' in page and '"filter_has_correspondent": 10' in page
    # Der Companion hat nichts nach Paperless geschrieben
    assert not [c for c in mock_api.calls if c.request.method in ("POST", "PATCH", "PUT", "DELETE")
                and "paperless" in str(c.request.url)]
    client.post(f"/rules/proposals/{pid}", data={"action": "created"})
    assert "In Paperless angelegt (1)" in client.get("/rules?show=created").text


def test_api_payload_shape():
    p = RuleProposal(correspondent="Stadtwerke", tags=["Haus", "Unbekannt"], document_type="Rechnung")
    ids = {"correspondents": {"stadtwerke": 10}, "tags": {"haus": 31}, "document_types": {"rechnung": 20}}
    assert rules.api_payload(p, ids) == {
        "name": "Companion: Stadtwerke", "order": 100, "enabled": True,
        "triggers": [{"type": 2, "filter_has_correspondent": 10}],
        "actions": [{"type": 1, "assign_tags": [31], "assign_document_type": 20}],
    }


def test_analysis_uses_learned_fields(db, settings, mock_api):
    from app import pipeline
    from app.paperless import PaperlessClient
    from tests.conftest import PAPERLESS

    with db() as s:
        confirmed_invoice(s, 101, "Stadtwerke", date(2026, 1, 5), date(2026, 1, 19), method="debit", iban=None)
        confirmed_invoice(s, 102, "Stadtwerke", date(2026, 2, 5), date(2026, 2, 19), method="debit", iban=None)
    run_all(db, settings)
    with db() as s:
        doc = s.get(Document, 1)  # Stromrechnung der Stadtwerke
        assert doc.suggestions["payment_method"] == {"value": "debit", "confidence": 0.85, "source": "learned"}
        info = pipeline.process_document(s, doc, PaperlessClient(PAPERLESS, "x"), None, settings)
        assert info["learned"] == ["payment_method"] and "payment_method" not in info["wanted"]


def test_auto_confirmed_invoices_are_not_learned_from(db):
    with db() as s:
        confirmed_invoice(s, 101, "Versorger", date(2026, 1, 1), date(2026, 1, 15), method="debit")
        confirmed_invoice(s, 102, "Versorger", date(2026, 2, 1), date(2026, 2, 15), method="debit")
        auto = confirmed_invoice(s, 103, "Versorger", date(2026, 3, 1), date(2026, 3, 15), method="transfer")
        auto.auto_confirmed = True
    with db() as s:
        assert learning.profiles(s)["Versorger"]["fields"]["payment_method"] == {
            "value": "debit", "basis": "2 von 2", "disabled": False}
