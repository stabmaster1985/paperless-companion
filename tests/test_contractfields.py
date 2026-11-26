from datetime import date

from app import contractfields, pipeline
from app.models import Contract, Document


def test_terms_text_roundtrip():
    c = Contract(initial_term_months=24, renewal_mode="renews", renewal_months=12, notice_value=3, notice_unit="months",
                 notice_to="term_end")
    text = contractfields.to_text(c)
    assert text == "Laufzeit 24 Monate · verlängert sich um 12 Monate · Kündigungsfrist 3 Monate zum Laufzeitende"
    assert contractfields.from_text(text) == {"initial_term_months": 24, "renewal_mode": "renews", "renewal_months": 12,
                                              "notice_value": 3, "notice_unit": "months", "notice_to": "term_end"}
    abo = Contract(renewal_mode="indefinite", notice_value=1, notice_unit="months", notice_to="any")
    assert contractfields.from_text(contractfields.to_text(abo)) == {"renewal_mode": "indefinite", "notice_value": 1,
                                                                     "notice_unit": "months", "notice_to": "any"}
    fixed = Contract(renewal_mode="none", end_date=date(2027, 12, 31))
    assert contractfields.from_text(contractfields.to_text(fixed))["end_date"] == date(2027, 12, 31)


def test_contract_written_and_rebuilt_from_fields(db, settings):
    settings.field_monthly_cost, settings.field_contract_start, settings.field_contract_terms = "Monatsbeitrag", "Beginn", "Konditionen"
    with db() as s:
        doc = Document(id=700, title="Handyvertrag", correspondent="Mobilfunk AG", kind="contract", tags=[], review_state="confirmed")
        doc.contract = Contract(name="Handy", provider="Mobilfunk AG", monthly_cost=19.99, start_date=date(2025, 1, 1),
                                initial_term_months=24, renewal_mode="indefinite", notice_value=1, notice_unit="months",
                                notice_to="month_end")
        s.add(doc)
        s.flush()
        changes = {role: new for role, _, new in pipeline.pending_changes(doc, settings, date(2026, 11, 26))}
        assert changes["monthly_cost"] == 19.99 and changes["contract_start"] == "2025-01-01"
        assert changes["contract_terms"] == "Laufzeit 24 Monate · danach unbefristet · Kündigungsfrist 1 Monat zum Monatsende"
        fields = {"monthly_cost": 19.99, "contract_start": "2025-01-01", "contract_terms": changes["contract_terms"]}
    # Neuinstallation: nur der Index aus Paperless ist da
    with db() as s:
        fresh = Document(id=701, title="Handyvertrag", correspondent="Mobilfunk AG", tags=[], kind="other",
                         kind_source="keyword", paperless_fields=fields)
        s.add(fresh)
        pipeline.apply_paperless_fields(fresh)
        c = fresh.contract
        assert fresh.kind == "contract" and fresh.review_state == "confirmed"
        assert (c.monthly_cost, c.start_date, c.initial_term_months, c.renewal_mode, c.notice_value, c.notice_to) == \
            (19.99, date(2025, 1, 1), 24, "indefinite", 1, "month_end")
