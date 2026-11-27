from datetime import date

from app import recurring, reserves
from app.models import Contract, Document
from tests.test_recurring import tx


def test_reserves_for_quarterly_and_yearly(db):
    today = date(2026, 11, 26)
    with db() as s:
        doc = Document(id=800, title="Tierversicherung", correspondent="Tierversicherung Pfote", kind="contract", tags=[])
        doc.contract = Contract(name="Tierversicherung", provider="Tierversicherung Pfote")
        doc.contract.set_payment(90.0, 3)
        s.add(doc)
        yearly = Contract(name="Kfz-Versicherung", start_date=date(2026, 3, 1))
        yearly.set_payment(480.0, 12)
        monthly = Contract(name="Handy")
        monthly.set_payment(20.0, 1)
        s.add_all([yearly, monthly])
    with db() as s:  # Zahltermine der Tierversicherung aus Firefly: zuletzt 13.09., nächste 13.12.
        recurring.run(s, [tx(i, d, 90.0, "Tierversicherung Pfote") for i, d in
                          enumerate([date(2025, 12, 13), date(2026, 3, 13), date(2026, 6, 13), date(2026, 9, 13)])])
    with db() as s:
        rv = reserves.collect(s, today)
        by = {r.contract.label: r for r in rv["rows"]}
        assert by["Tierversicherung"].monthly == 30.0 and by["Tierversicherung"].next == date(2026, 12, 13)
        assert by["Tierversicherung"].source == "Firefly" and by["Tierversicherung"].saved == round(90 * 74 / 91, 2)  # 74 von 91 Tagen
        assert by["Kfz-Versicherung"].next == date(2027, 3, 1) and by["Kfz-Versicherung"].monthly == 40.0
        assert by["Kfz-Versicherung"].saved == round(480 * 270 / 365, 2)
        assert rv["monthly_reserve"] == 70.0  # nur nicht monatliche
        assert by["Handy"].saved is None or by["Handy"].interval == 1


def test_contract_form_payment_and_interval(db, settings):
    from app import pipeline

    with db() as s:
        doc = Document(id=801, title="Versicherung", tags=[], kind="contract", review_state="pending", suggestions={})
        s.add(doc)
        pipeline.apply_values(s, doc, "contract", pipeline.parse_values("contract", {
            "name": "Versicherung", "payment_amount": "120,00", "payment_interval": "12"}))
        assert doc.contract.monthly_cost == 10.0 and doc.contract.payment == 120.0 and doc.contract.interval == 12
        pipeline.apply_values(s, doc, "contract", pipeline.parse_values("contract", {"name": "X", "monthly_cost": "9,99"}))
        assert doc.contract.payment == 9.99 and doc.contract.interval == 1  # ältere Eingabe ohne Intervall


def test_horizons_and_monthly_plan(db):
    today = date(2026, 11, 27)
    with db() as s:
        yearly = Contract(name="Kfz", start_date=date(2026, 3, 1))
        yearly.set_payment(480.0, 12)
        quarterly = Contract(name="Tier", start_date=date(2026, 12, 15))
        quarterly.set_payment(90.0, 3)
        monthly = Contract(name="Handy", start_date=date(2026, 1, 3))
        monthly.set_payment(20.0, 1)
        s.add_all([yearly, quarterly, monthly])
    with db() as s:
        rv = reserves.collect(s, today)
        h = {x["months"]: x for x in rv["horizons"]}
        assert h[1] == {"months": 1, "total": 110.0, "rare": 90.0}  # Handy 03.12. + Tier 15.12.
        assert h[12]["rare"] == 480.0 + 4 * 90.0
        plan = {m["label"]: m for m in rv["plan"]}
        assert plan["Dezember 2026"]["rare"] == 90.0 and plan["März 2027"]["rare"] == 570.0  # Kfz + Tier im März
        assert plan["Dezember 2026"]["saving"] == 70.0  # 40 + 30 pro Monat
        assert len(rv["plan"]) == 12
