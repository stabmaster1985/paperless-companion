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
        assert "Stadtwerke Nord" in found and "Wenige" not in found
        # Jahres-Tag wird nie vorgeschlagen; vorgemerktes „Haus“ zählt mit
        assert set(found["Stadtwerke Nord"]["tags"]) == {"Strom", "Haus"}
        assert found["Stadtwerke Nord"]["document_type"] == {"Rechnung": 9}
        assert rules.refresh(s) >= 1
    with db() as s:
        p = s.query(RuleProposal).filter_by(correspondent="Stadtwerke Nord").one()
        pv = rules.preview(s, p)
        assert pv["total"] == 10 and pv["unchanged"] == 9 and pv["conflicts"][0]["id"] == 399  # Typ Brief -> Rechnung
        # Verworfener Tag wird nicht (mehr) vorgeschlagen
        s.add(MetaDecision(document_id=300, key="tag:Haus", item="tag", correspondent="Stadtwerke Nord", value="Haus",
                           source="llm", decision="dismissed"))
    with db() as s:
        rules.refresh(s)
        assert s.query(RuleProposal).filter_by(correspondent="Stadtwerke Nord").one().tags == ["Strom"]


def test_rules_page_edit_approve(client, mock_api):
    with session_scope() as s:
        add_docs(s, "Stadtwerke", 6, ["Haus"], start=300)
    page = client.get("/rules").text
    assert "Wenn Korrespondent = „Stadtwerke“" in page and "passende Dokumente" in page
    with session_scope() as s:
        pid = s.query(RuleProposal).filter_by(correspondent="Stadtwerke").one().id
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


def test_keyword_and_doctype_rules_with_outliers(db):
    with db() as s:
        # 6 Versicherungs-Dokumente verschiedener Absender, eines ohne Tag -> Ausreißer
        for i in range(6):
            s.add(Document(id=600 + i, title=f"Versicherung Beitrag {i}", correspondent=f"Versicherer {i % 3}",
                           tags=["Versicherung"] if i else [], extraction_state="skipped"))
        # Dokumenttyp -> Tag
        for i in range(6):
            s.add(Document(id=700 + i, title=f"Abrechnung {i}", correspondent=f"Firma {i}", document_type="Lohnabrechnung",
                           tags=["Gehalt"], extraction_state="skipped"))
    with db() as s:
        found = rules.compute(s)
        # 5 von 6 = 83 % < 85 % -> noch kein Stichwort-Vorschlag; mit einem weiteren Dokument schon
        assert "wort:versicherung" not in found and found["typ:Lohnabrechnung"]["tags"] == {"Gehalt": 6}
        s.add(Document(id=606, title="Versicherung Police", correspondent="Versicherer 9", tags=["Versicherung"],
                       extraction_state="skipped"))
        for i in range(607, 612):
            s.add(Document(id=i, title=f"Versicherung Nachtrag {i}", correspondent=f"V{i}", tags=["Versicherung"],
                           extraction_state="skipped"))
    with db() as s:
        rules.refresh(s)
        p = s.query(RuleProposal).filter_by(correspondent="wort:versicherung").one()
        assert p.trigger == "keyword" and p.tags == ["Versicherung"] and rules.label(p) == "Wenn Stichwort im Titel enthält „versicherung“"
        pv = rules.preview(s, p)
        assert [e["id"] for e in pv["changed"]] == [600]
        # Ausreißer zum Korrigieren vormerken – nur im Companion
        assert rules.prefill_fixes(s, p, [600]) == 1
    with db() as s:
        doc = s.get(Document, 600)
        assert doc.meta_accepted["tag:Versicherung"]["source"] == "rule" and doc.meta_pending and doc.tags == []
        assert rules.preview(s, s.query(RuleProposal).filter_by(correspondent="wort:versicherung").one())["changed"] == []
        ids = {"tags": {"versicherung": 5}, "document_types": {}, "correspondents": {}}
        objects = {"tags": {"versicherung": {"match": "police", "matching_algorithm": 1}}}
        payload = rules.api_payload(s.query(RuleProposal).filter_by(correspondent="wort:versicherung").one(), ids, objects)
        assert payload == [{"PATCH": "/api/tags/5/", "match": "police versicherung", "matching_algorithm": 1}]


def test_similar_names(db):
    with db() as s:
        s.add(Document(id=1, title="a", correspondent="Telekom", tags=["Kfz"], extraction_state="skipped"))
        s.add(Document(id=2, title="b", correspondent="Telekom Deutschland GmbH", tags=["KFZ"], extraction_state="skipped"))
        s.add(Document(id=3, title="c", correspondent="Stadtwerke", tags=["Rechnungen", "Rechnung"], extraction_state="skipped"))
    with db() as s:
        sim = rules.similar_names(s)
        assert [n for n, _ in sim["Korrespondenten"][0]] == ["Telekom", "Telekom Deutschland GmbH"] or \
            sorted(n for n, _ in sim["Korrespondenten"][0]) == ["Telekom", "Telekom Deutschland GmbH"]
        tag_groups = [sorted(n for n, _ in g) for g in sim["Tags"]]
        assert ["KFZ", "Kfz"] in tag_groups and ["Rechnung", "Rechnungen"] in tag_groups


def test_rules_page_fix_outliers(client):
    with session_scope() as s:
        add_docs(s, "Stadtwerke", 10, ["Haus"], start=300)
        s.get(Document, 300).tags = []
    page = client.get("/rules?trigger=correspondent").text
    assert "abweichend" in page and "Ähnliche Namen" in page
    with session_scope() as s:
        pid = s.query(RuleProposal).filter_by(correspondent="Stadtwerke").one().id
    response = client.post(f"/rules/proposals/{pid}", data={"action": "fix", "fix": ["300"], "back": "trigger=correspondent"},
                           follow_redirects=False)
    assert response.headers["location"].startswith("/rules?trigger=correspondent&msg=F%C3%BCr%201%20Dokumente")
    page = client.get("/paperless-changes").text
    assert "Vorgemerkt – wartet auf Übertragung" in page and "Regel</span>" in page


# --------------------------------------------------------------------------- Korrespondent aus Text

from app import identifiers  # noqa: E402

TEXT_SW = "Stadtwerke Musterstadt GmbH · www.stadtwerke-musterstadt.de · IBAN DE89 3704 0044 0532 0130 00 · Kundennummer: 4711-0815"
TEXT_OWN = "Mein Konto IBAN DE02120300000000202051"  # eigene IBAN steht bei vielen Absendern


def test_extract_identifiers():
    found = identifiers.extract(TEXT_SW + " USt-IdNr. DE 123 456 789 info@paypal.com")
    assert found["iban"] == ["DE89370400440532013000"] and found["vat"] == ["DE123456789"]
    assert found["domain"] == ["stadtwerke-musterstadt.de"] and found["number"] == ["4711-0815"]
    import re
    pattern = identifiers.regex_for("iban", "DE89370400440532013000")
    assert re.search(pattern, "IBAN: DE89 3704 0044 0532 0130 00") and re.search(pattern, "DE89370400440532013000")


def add_text_doc(s, doc_id, corr, text):
    doc = Document(id=doc_id, title=f"Brief {doc_id}", correspondent=corr, tags=[], extraction_state="skipped")
    identifiers.store(doc, text)
    s.add(doc)


def test_content_rule_and_outliers(db):
    with db() as s:
        for i in range(4):
            add_text_doc(s, 800 + i, "Stadtwerke", TEXT_SW + " " + TEXT_OWN if i % 2 else TEXT_SW)
        add_text_doc(s, 804, "Stadtwerke", "Ablesekarte ohne Merkmale")  # wird nicht erkannt
        for i in range(3):
            add_text_doc(s, 810 + i, "Versicherung", "Allianz " + TEXT_OWN + " www.allianz.de")
        add_text_doc(s, 820, None, "Rechnung " + TEXT_SW)  # ohne Korrespondent -> würde erkannt
        s.add(Document(id=821, title="noch nicht gelesen", tags=[], extraction_state="skipped"))
    with db() as s:
        found = rules.compute_text(rules.archive(s))
        rule = found["text:Stadtwerke"]
        # eigene IBAN kommt bei zwei Absendern vor -> nie ein Merkmal
        assert "iban:DE02120300000000202051" not in rule["terms"]
        assert list(rule["terms"])[0] == "iban:DE89370400440532013000" and rule["coverage"] == 4 and rule["docs"] == 5
        rules.refresh(s)
        p = s.query(RuleProposal).filter_by(correspondent="text:Stadtwerke").one()
        assert p.trigger == "content" and rules.label(p) == "Korrespondent „Stadtwerke“ am Text erkennen"
        pv = rules.preview(s, p)
        assert pv["unchanged"] == 4 and [e["id"] for e in pv["changed"]] == [820] and [e["id"] for e in pv["missed"]] == [804]
        assert rules.prefill_fixes(s, p, [820]) == 1
    with db() as s:
        doc = s.get(Document, 820)
        assert doc.meta_accepted["correspondent"] == {"value": "Stadtwerke", "source": "rule", "new_tag": False,
                                                      "at": doc.meta_accepted["correspondent"]["at"]}
        p = s.query(RuleProposal).filter_by(correspondent="text:Stadtwerke").one()
        payload = rules.api_payload(p, {"correspondents": {"stadtwerke": 10}, "tags": {}, "document_types": {}})
        assert payload["PATCH"] == "/api/correspondents/10/" and payload["matching_algorithm"] == 4
        assert payload["match"].startswith(r"DE89\s?3704")
        # Merkmale abwählen
        rules.update(p, [], None, ["domain:stadtwerke-musterstadt.de"])
        assert p.match_terms == ["domain:stadtwerke-musterstadt.de"] and p.edited


def test_identifier_scan_and_review_tag(db, settings, mock_api):
    from app.paperless import PaperlessClient
    from tests.conftest import PAPERLESS

    run_all(db, settings)
    with db() as s:
        assert identifiers.progress(s) == {"total": 3, "done": 3}  # beim Analysieren schon gelesen
        s.add(Document(id=50, title="Archiv", correspondent="Stadtwerke", tags=[], extraction_state="skipped"))
    with db() as s:
        assert identifiers.scan(s, PaperlessClient(PAPERLESS, "x"), limit=5) == 1
        assert identifiers.progress(s)["done"] == 4
    p = RuleProposal(correspondent="Stadtwerke", trigger="correspondent", condition="Stadtwerke", tags=["Haus"])
    ids = {"correspondents": {"stadtwerke": 10}, "tags": {"haus": 31, "zu prüfen": 99}, "document_types": {}}
    assert rules.api_payload(p, ids, review_tag="Zu prüfen")["actions"][0]["assign_tags"] == [31, 99]


def test_rules_page_groups_and_colors(client):
    with session_scope() as s:
        for i in range(3):
            add_text_doc(s, 900 + i, "Stadtwerke", TEXT_SW)
            s.get(Document, 900 + i).tags = ["Haus"]
        add_docs(s, "Stadtwerke", 30, ["Haus"], start=300)
    page = client.get("/rules").text
    assert '<h3 class="rule-group t-content">Korrespondent aus Text</h3>' in page
    assert page.index("t-content") < page.index('class="rule-group t-correspondent"')
    assert 'class="rule t-content"' in page and "Textmerkmale (IBAN, Domain, Kundennr.) gelesen" in page


def test_own_identifiers_are_never_used(db, mock_api):
    from app.firefly import FireflyClient

    mock_api.get(f"{FIREFLY}/api/v1/accounts").respond(json={"data": [
        {"attributes": {"iban": "DE02 1203 0000 0000 2020 51"}}, {"attributes": {"iban": None}}]})
    assert FireflyClient(FIREFLY, "t").own_ibans() == ["DE02120300000000202051"]
    with db() as s:
        for i in range(3):  # eigene IBAN nur bei einem Absender -> wäre sonst ein Merkmal
            add_text_doc(s, 960 + i, "Allianz", "Allianz " + TEXT_OWN)
    with db() as s:
        assert "text:Allianz" in rules.compute(s)
        identifiers.set_own_terms(s, identifiers.FIREFLY_OWN_KEY, ["iban:DE02120300000000202051"])
    with db() as s:
        assert "text:Allianz" not in rules.compute(s)


def test_mark_own_term_in_ui(client):
    with session_scope() as s:
        for i in range(3):
            add_text_doc(s, 960 + i, "Allianz", "Allianz www.allianz.de " + TEXT_OWN)
    client.get("/rules")
    client.post("/rules/own-term", data={"own_term": "iban:DE02120300000000202051"})
    with session_scope() as s:
        p = s.query(RuleProposal).filter_by(correspondent="text:Allianz").one()
        assert p.match_terms == ["domain:allianz.de"]
    page = client.get("/rules?trigger=content").text
    assert "Eigene Merkmale, die nie verwendet werden" in page and "IBAN DE02120300000000202051" in page
    client.post("/rules/own-term", data={"own_term": "iban:DE02120300000000202051", "undo": "1"})
    with session_scope() as s:
        assert "iban:DE02120300000000202051" not in identifiers.own_terms(s)


# --------------------------------------------------------------------------- Testbetrieb und bestehende Paperless-Regeln

from app import paperless_rules  # noqa: E402


def test_trial_after_approval(db):
    with db() as s:
        add_docs(s, "Stadtwerke", 6, ["Haus"], start=300)
    with db() as s:
        rules.refresh(s)
        p = s.query(RuleProposal).filter_by(correspondent="Stadtwerke").one()
        rules.set_state(p, "approved", s)
        assert p.baseline_doc_id == 305
        assert rules.trial(s, p, rules.archive(s)) == {"total": 0, "correct": 0, "open": 0, "wrong": []}
        # Neue Dokumente nach der Freigabe: eines richtig, eines falsch, eines noch offen
        s.add(Document(id=400, title="neu 1", correspondent="Stadtwerke", document_type="Rechnung", tags=["Haus"],
                       extraction_state="done"))
        s.add(Document(id=401, title="neu 2", correspondent="Stadtwerke", document_type="Rechnung", tags=["Strom"],
                       extraction_state="done"))
        s.add(Document(id=402, title="neu 3", correspondent="Stadtwerke", tags=[], extraction_state="queued"))
        s.add(Document(id=403, title="anderer", correspondent="Telekom", tags=[], extraction_state="done"))
    with db() as s:
        p = s.query(RuleProposal).filter_by(correspondent="Stadtwerke").one()
        tr = rules.trial(s, p, rules.archive(s))
        assert tr["total"] == 3 and tr["correct"] == 1 and tr["open"] == 1
        assert tr["wrong"] == [{"id": 401, "title": "neu 2", "problems": ["Tag Haus fehlt"]}]
        rules.set_state(p, "proposed", s)
        assert p.baseline_doc_id is None and rules.trial(s, p, rules.archive(s)) is None


def test_existing_paperless_rules_coverage():
    data = {
        "objects": {
            "tags": [{"id": 31, "name": "Haus", "matching_algorithm": 6}, {"id": 5, "name": "Versicherung", "matching_algorithm": 1,
                                                                            "match": "police versicherung"}],
            "correspondents": [{"id": 10, "name": "Stadtwerke", "matching_algorithm": 4, "match": r"DE89\s?3704\s?0044\s?0532\s?0130\s?00"}],
            "document_types": [{"id": 20, "name": "Rechnung", "matching_algorithm": 0}],
        },
        "workflows": [{"name": "Strom", "enabled": True, "triggers": [{"type": 2, "filter_has_correspondent": 10}],
                       "actions": [{"type": 1, "assign_tags": [31], "assign_document_type": 20}]}],
        "errors": [],
    }
    wf = paperless_rules.workflows(data)
    assert wf[0]["conditions"] == [("Korrespondent", "Stadtwerke")] and wf[0]["tags"] == ["Haus"] and wf[0]["document_type"] == "Rechnung"
    assert paperless_rules.matchings(data)[0]["algorithm_label"] == "automatisch (lernend)"
    covered = RuleProposal(correspondent="Stadtwerke", trigger="correspondent", condition="Stadtwerke", tags=["Haus"],
                           document_type="Rechnung")
    assert paperless_rules.coverage(covered, data)["state"] == "covered"
    partial = RuleProposal(correspondent="Stadtwerke", trigger="correspondent", condition="Stadtwerke", tags=["Haus", "Strom"])
    assert paperless_rules.coverage(partial, data)["state"] == "partial"
    conflict = RuleProposal(correspondent="Stadtwerke", trigger="correspondent", condition="Stadtwerke", tags=[], document_type="Brief")
    assert paperless_rules.coverage(conflict, data)["state"] == "conflict"
    text = RuleProposal(correspondent="text:Stadtwerke", trigger="content", condition="Stadtwerke",
                        match_terms=["iban:DE89370400440532013000"])
    assert paperless_rules.coverage(text, data)["state"] == "covered"
    kw = RuleProposal(correspondent="wort:versicherung", trigger="keyword", condition="versicherung", tags=["Versicherung"])
    assert paperless_rules.coverage(kw, data)["state"] == "covered"


def test_rules_page_auto_marks_created(client, mock_api):
    from tests.conftest import PAPERLESS, page

    mock_api.get(f"{PAPERLESS}/api/workflows/").respond(json=page([
        {"name": "Haus-Regel", "enabled": True, "triggers": [{"type": 2, "filter_has_correspondent": 10}],
         "actions": [{"type": 1, "assign_tags": [31], "assign_document_type": 20}]}]))
    with session_scope() as s:
        add_docs(s, "Stadtwerke", 30, ["Haus"], start=300)
    client.get("/rules")
    with session_scope() as s:
        pid = s.query(RuleProposal).filter_by(correspondent="Stadtwerke").one().id
    response = client.post(f"/rules/proposals/{pid}", data={"action": "approved"})  # folgt der Weiterleitung
    assert "als angelegt markiert" in response.text and "Haus-Regel" in response.text
    with session_scope() as s:
        assert s.get(RuleProposal, pid).state == "created"
