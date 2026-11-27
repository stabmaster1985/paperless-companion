from datetime import date

import pytest
from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient

from app import recurring
from app.db import session_scope
from app.firefly import Transaction
from app.main import create_app
from app.models import Contract, Document
from tests.test_pipeline import run_all

TODAY = date(2026, 11, 18)


def tx(i, day, amount, payee, desc=""):
    return Transaction(journal_id=f"{payee[:3]}{i}", group_id=f"g{i}", date=day, amount=amount, counterparty=payee, iban=None,
                       description=desc, text=desc)


def monthly(payee, amount, n, start=date(2025, 12, 3), change_at=None, new_amount=None):
    out = []
    for i in range(n):
        a = new_amount if change_at is not None and i >= change_at else amount
        out.append(tx(i, start + relativedelta(months=i), a, payee))
    return out


def test_detect_rhythms_and_ignore_one_offs():
    txs = [*monthly("Streamingdienst AG", 12.99, 12),
           *[tx(100 + i, date(2025, 12, 15) + relativedelta(months=3 * i), 89.0, "Tierversicherung") for i in range(4)],
           tx(200, date(2026, 2, 1), 249.0, "Elektromarkt"), tx(201, date(2026, 7, 9), 19.0, "Elektromarkt")]
    found = {s.payee: s for s in recurring.detect(txs)}
    assert set(found) == {"Streamingdienst AG", "Tierversicherung"}
    assert found["Streamingdienst AG"].months == 1 and found["Tierversicherung"].months == 3
    assert found["Tierversicherung"].monthly == 29.67


def test_findings_unknown_changed_missing(db):
    with db() as s:
        doc = Document(id=900, title="Mitgliedschaft Fitness", correspondent="Fitnessstudio Muster", kind="contract", tags=[])
        doc.contract = Contract(name="Fitness", provider="Fitnessstudio Muster", monthly_cost=29.9)
        s.add(doc)
        doc2 = Document(id=901, title="Zeitschrift", correspondent="Verlag", kind="contract", tags=[])
        doc2.contract = Contract(name="Zeitschrift", provider="Verlag Lesen", monthly_cost=9.9)
        s.add(doc2)
    txs = [*monthly("Fitnessstudio Muster", 29.9, 12, change_at=9, new_amount=34.9),    # Preiserhöhung
           *monthly("Streamingdienst AG", 12.99, 12),                                    # ohne Vertrag
           *monthly("Verlag Lesen", 9.9, 6)]                                              # seit Juni ausgeblieben
    with db() as s:
        assert recurring.run(s, txs) == 3
    with db() as s:
        f = recurring.findings(s, TODAY)
        assert [x.payee for x in f["unknown"]] == ["Streamingdienst AG"]
        changed = f["changed"][0]
        assert changed["contract"].name == "Fitness" and changed["change"][:2] == (29.9, 34.9) and changed["cost_differs"]
        assert [m["contract"].name for m in f["missing"]] == ["Zeitschrift"]


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_recurring_page_actions(client):
    with session_scope() as s:
        doc = Document(id=902, title="Fitnessvertrag", correspondent="Fitnessstudio Muster", kind="contract", tags=[])
        doc.contract = Contract(name="Fitness", provider="Fitnessstudio Muster", monthly_cost=29.9)
        s.add(doc)
        abo = Document(id=903, title="Abo", correspondent="Medien", kind="contract", tags=[])
        abo.contract = Contract(name="Medienabo", provider="Medien")
        s.add(abo)
        recurring.run(s, [*monthly("Fitnessstudio Muster", 29.9, 12, change_at=9, new_amount=34.9),
                          *monthly("Streamingdienst AG", 12.99, 12)])
    page = client.get("/recurring").text
    assert "Betrag geändert (1)" in page and "Ohne Vertrag (1)" in page and "Beitrag auf 34,90 € monatlich setzen" in page
    assert "12 Abbuchungen ansehen" in page and "03.11.2026" in page  # einzelne Buchungen aufklappbar
    with session_scope() as s:
        cid = s.get(Document, 902).contract.id
        key = next(x.key for x in recurring.stored(s) if x.payee == "Streamingdienst AG")
        abo_id = s.get(Document, 903).contract.id
    client.post("/recurring/action", data={"action": "cost", "contract_id": str(cid), "monthly": "34.9"})
    with session_scope() as s:
        assert s.get(Contract, cid).monthly_cost == 34.9
    r = client.post("/recurring/action", data={"action": "link", "key": key, "doc_id": "2"})  # Dokument wird zum Vertrag
    assert "gehört zu" in r.text
    r = client.post("/recurring/action", data={"action": "link", "key": key, "contract_id": str(abo_id)})
    assert "gehört zu" in r.text
    with session_scope() as s:
        assert s.get(Contract, abo_id).monthly_cost == 12.99  # Monatsbeitrag aus den Abbuchungen
    page = client.get("/recurring").text
    assert "Betrag geändert (" not in page and "Ohne Vertrag (" not in page


def test_price_increase_stays_one_series():
    found = recurring.detect(monthly("Mobilfunk AG", 19.99, 12, change_at=8, new_amount=24.99))
    assert len(found) == 1 and found[0].change()[:2] == (19.99, 24.99) and len(found[0].amounts) == 12


def test_long_history_and_ended_subscriptions(db, settings):
    """5 Jahre: alte, beendete Abos ohne Vertrag stehen unter „früher“, nicht unter „ohne Vertrag“."""
    assert settings.recurring_lookback_days == 1830
    old = monthly("Altes Fitnessstudio", 25.0, 24, start=date(2022, 1, 5))   # bis Ende 2023
    current = monthly("Streamingdienst AG", 12.99, 12)
    with db() as s:
        recurring.run(s, [*old, *current])
    with db() as s:
        f = recurring.findings(s, TODAY)
        assert [x.payee for x in f["unknown"]] == ["Streamingdienst AG"]
        assert [x.payee for x in f["ended"]] == ["Altes Fitnessstudio"]


def test_streams_do_not_drift_and_one_series_per_contract(db):
    """Beträge, die schrittweise wandern (10 → 12 → 14,5 …), werden keine Serie mehr; pro Vertrag automatisch nur die
    passendste Serie desselben Empfängers."""
    drift = [tx(i, date(2026, 1, 5) + relativedelta(months=i), round(10 * 1.2 ** i, 2), "Versand AG") for i in range(8)]
    assert all(max(x.amounts) / min(x.amounts) < 1.6 for x in recurring.detect(drift))
    with db() as s:
        doc = Document(id=970, title="Prime", correspondent="Versand AG", kind="contract", tags=[])
        doc.contract = Contract(name="Prime", provider="Versand AG", monthly_cost=8.99)
        s.add(doc)
    with db() as s:
        recurring.run(s, [*monthly("Versand AG", 8.99, 12), *monthly("Versand AG", 49.0, 12, start=date(2025, 12, 15))])
        series = recurring.stored(s)
        assert len(series) == 2
        assert [x.last_amount for x in series if x.contract_id] == [8.99]  # nur das Abo, nicht die anderen Käufe


def test_sum_of_running_payments(db):
    with db() as s:
        doc = Document(id=980, title="Fitness", correspondent="Fitnessstudio Muster", kind="contract", tags=[])
        doc.contract = Contract(name="Fitness", provider="Fitnessstudio Muster", monthly_cost=29.9)
        s.add(doc)
    with db() as s:
        recurring.run(s, [*monthly("Fitnessstudio Muster", 29.9, 12), *monthly("Streamingdienst AG", 12.99, 12),
                          *monthly("Altes Abo", 5.0, 6, start=date(2023, 1, 5))])  # längst beendet: zählt nicht
    with db() as s:
        sums = recurring.findings(s, TODAY)["sums"]
        assert sums == {"total": 42.89, "contract": 29.9, "other": 0.0, "open": 12.99, "count": 2}


def test_dismiss_several_unknown(db, settings, mock_api):
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.main import create_app

    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        with session_scope() as s:
            recurring.run(s, [*monthly("Sparplan Bank", 50.0, 12), *monthly("Taschengeld Kind", 20.0, 12)])
            keys = [x.key for x in recurring.stored(s)]
        page = c.get("/recurring").text
        assert "Angehakte ausblenden" in page and "ausblenden (kein Vertrag)" in page
        r = c.post("/recurring/dismiss-many", data={"token": [f"unknown:{k}" for k in keys]})
        assert "2 Einträge ausgeblendet" in r.text and "Ohne Vertrag (" not in r.text


def test_only_current_series_of_contract_counts(db):
    """Alter Beitrag 4,90 € (bis 2023) und neuer 7,99 € am selben Vertrag: kein „Betrag geändert“ und keine „ausgebliebene“
    Abbuchung wegen der alten Serie. Firefly-„(no name)“ wird über den Verwendungszweck erkannt."""
    from app.firefly import Transaction

    def fee(i, day, amount):
        return Transaction(journal_id=f"f{i}", group_id=f"gf{i}", date=day, amount=amount, counterparty="(no name)",
                           iban=None, description="Monatsabschluss", text="Monatsabschluss")

    old = [fee(i, date(2022, 11, 30) + relativedelta(months=i), 4.90) for i in range(12)]
    new = [fee(100 + i, date(2025, 11, 30) + relativedelta(months=i), 7.99) for i in range(12)]
    with db() as s:
        recurring.run(s, [*old, *new])
        series = recurring.stored(s)
        assert {x.payee for x in series} == {"Monatsabschluss"} and len(series) == 2
        contract = Contract(name="Monatsabschluss", provider="Bank")
        contract.set_payment(7.99, 1)
        s.add(contract)
        s.flush()
        for x in series:
            recurring.link_contract(s, x.key, contract.id)
    with db() as s:
        f = recurring.findings(s, date(2026, 11, 20))
        assert f["changed"] == [] and f["missing"] == []


def test_assign_bookings_by_hand_survives_new_runs(db):
    """Buchungen über PayPal (anderer Empfänger) von Hand einem Abo zuordnen: Sie bleiben bei neuen Erkennungsläufen dort."""
    stream = monthly("Streamingdienst AG", 12.99, 6)
    paypal = [tx(50 + i, date(2026, 6, 10) + relativedelta(months=i), 9.99, "PayPal Europe", "Hörbuchclub") for i in range(5)]
    with db() as s:
        recurring.run(s, [*stream, *paypal])
        abo = Contract(name="Hörbuchclub", provider="Hörbuchclub", payee="Hörbuchclub")
        s.add(abo)
        s.flush()
        hits = recurring.search_bookings([*stream, *paypal], "hörbuch", 10.0)
        assert [t.journal_id for t in hits] == [t.journal_id for t in reversed(paypal)]
        recurring.assign(s, abo.id, hits[:3])
        abo_id = abo.id
    with db() as s:
        recurring.assign(s, abo_id, recurring.search_bookings(paypal, "hörbuch"))  # Rest dazu, keine Dopplung
        recurring.run(s, [*stream, *paypal])
        mine = [x for x in recurring.stored(s) if x.contract_id == abo_id]
        assert len(mine) == 1 and sorted(mine[0].tx_ids) == sorted(t.journal_id for t in paypal) and mine[0].months == 1
        others = [x for x in recurring.stored(s) if x.contract_id != abo_id]
        assert not any(set(x.tx_ids) & set(mine[0].tx_ids) for x in others)


def test_invoices_for_series(db):
    """Abo mit monatlichen Rechnungen: Rechnung je Buchung vorschlagen und übernehmen – ohne Vertrag."""
    from app import seriesdocs
    from app.models import Invoice, ReceiptCheck

    txs = monthly("Zeitschriftenverlag Seite", 7.5, 4, start=date(2026, 7, 5))
    with db() as s:
        for i, t in enumerate(txs):
            d = Document(id=950 + i, title=f"Rechnung {i}", correspondent="Zeitschriftenverlag Seite", kind="invoice", tags=[],
                         review_state="confirmed")
            d.invoice = Invoice(issuer="Zeitschriftenverlag Seite", amount=7.5, invoice_date=t.date - relativedelta(days=3))
            s.add(d)
        other = Document(id=960, title="Sonderheft", correspondent="Zeitschriftenverlag Seite", kind="invoice", tags=[],
                         review_state="confirmed")
        other.invoice = Invoice(issuer="Zeitschriftenverlag Seite", amount=19.0, invoice_date=date(2026, 8, 1))
        s.add(other)
        recurring.run(s, txs)
        series = recurring.stored(s)[0]
        rows = seriesdocs.pairs(s, series)
        assert [r["doc"].id for r in rows] == [953, 952, 951, 950] and all(r["sure"] for r in rows)
        assert seriesdocs.apply(s, series, [(r["tx"].journal_id, r["doc"].id) for r in rows[:3]]) == 3
    with db() as s:
        series = recurring.stored(s)[0]
        assert s.get(Document, 953).invoice.status == "paid" and s.get(Document, 950).invoice.status == "open"
        assert s.query(ReceiptCheck).filter_by(state="linked").count() == 3
        states = [r["state"] for r in seriesdocs.pairs(s, series)]
        assert states == ["done", "done", "done", "suggest"]


def test_contract_booking_search_page(client, settings, mock_api):
    from tests.test_payments import FIREFLY

    settings.firefly_url, settings.firefly_token = FIREFLY, "t"
    mock_api.get(f"{FIREFLY}/api/v1/transactions").respond(json={"data": [
        {"type": "transactions", "id": f"7{i}", "attributes": {"transactions": [{
            "transaction_journal_id": f"97{i}", "type": "withdrawal", "date": f"2026-0{i + 1}-15T00:00:00+02:00",
            "amount": "9.99", "description": "Hörbuchclub Monatsbeitrag", "destination_name": "PayPal Europe",
            "category_name": "Medien", "tags": ["Hörbuch"], "bill_name": "Hörbuchclub"}]}}
        for i in range(3)], "meta": {"pagination": {"total_pages": 1}}})
    with session_scope() as s:
        abo = Contract(name="Hörbuchclub", provider="Hörbuchclub", payee="Hörbuchclub")
        abo.set_payment(9.99, 1)
        s.add(abo)
        s.flush()
        cid = abo.id
    page = client.get(f"/contracts/{cid}/bookings").text
    assert "Vorschlag:" in page and page.count('name="tx"') == 3 and "Angehakte zuordnen" in page
    import re
    values = [v.replace("&#34;", '"').replace("&quot;", '"') for v in re.findall(r'name="tx" value="([^"]+)"', page)]
    r = client.post(f"/contracts/{cid}/bookings", data={"tx": values})
    assert "3 Buchungen gehören jetzt zu" in r.text and "Rechnungen zuordnen" in r.text
    assert "gehört schon dazu" in client.get(f"/contracts/{cid}/bookings?q=hörbuch").text
    with session_scope() as s:
        key = next(x.key for x in recurring.stored(s) if x.contract_id == cid)
    assert "keine passende Rechnung gefunden" in client.get(f"/recurring/invoices?key={key}").text
    # nach Firefly-Merkmal suchen und die Suche merken
    page = client.get(f"/contracts/{cid}/bookings?q=&bill=hörbuchclub").text
    assert page.count("gehört schon dazu") == 3 and 'list="cb-category"' in page and ">Medien<" in page
    assert client.get(f"/contracts/{cid}/bookings?q=&category=Garten").text.count('name="tx"') == 0
    rule = re.search(r'name="rule" value="([^"]+)"', page).group(1).replace("&#34;", '"')
    r = client.post(f"/contracts/{cid}/bookings", data={"remember": "1", "rule": rule})
    assert "Suche gemerkt" in r.text
    with session_scope() as s:
        assert s.get(Contract, cid).booking_rule == {"bill": "hörbuchclub"}
    assert "Gemerkte Suche:" in client.get(f"/contracts/{cid}/bookings").text
    client.post(f"/contracts/{cid}/bookings", data={"op": "forget"})
    with session_scope() as s:
        assert s.get(Contract, cid).booking_rule is None


def test_remembered_search_assigns_new_bookings(db):
    """Gemerkte Suche (Firefly-Kategorie) am Vertrag: neue Buchungen kommen beim nächsten Lauf automatisch dazu,
    herausgenommene bleiben draußen."""
    def booking(i, day, cat):
        return Transaction(journal_id=f"k{i}", group_id=f"gk{i}", date=day, amount=15.0, counterparty="Bezahldienst Muster",
                           iban=None, description=f"Bestellung {i}", text="", category=cat)

    old = [booking(i, date(2026, 1, 5) + relativedelta(months=i), "Vereinsbeitrag") for i in range(4)]
    with db() as s:
        verein = Contract(name="Sportverein", provider="Sportverein", booking_rule={"category": "vereinsbeitrag"})
        s.add(verein)
        s.flush()
        cid = verein.id
        recurring.run(s, [*old, booking(99, date(2026, 3, 9), "Haushalt")])
        mine = [x for x in recurring.stored(s) if x.contract_id == cid]
        assert len(mine) == 1 and sorted(mine[0].tx_ids) == ["k0", "k1", "k2", "k3"]
        recurring.exclude_many(s, mine[0].key, ["k0"])
    new = [*old, booking(4, date(2026, 5, 5), "Vereinsbeitrag"), booking(5, date(2026, 6, 5), "Vereinsbeitrag")]
    with db() as s:
        recurring.run(s, new)
        mine = [x for x in recurring.stored(s) if x.contract_id == cid]
        assert len(mine) == 1 and sorted(mine[0].tx_ids) == ["k1", "k2", "k3", "k4", "k5"] and mine[0].months == 1
