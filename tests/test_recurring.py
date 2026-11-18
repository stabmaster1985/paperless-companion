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
    assert "Betrag geändert (1)" in page and "Ohne Vertrag (1)" in page and "Monatsbeitrag auf 34,90 € setzen" in page
    with session_scope() as s:
        cid = s.get(Document, 902).contract.id
        key = next(x.key for x in recurring.stored(s) if x.payee == "Streamingdienst AG")
        abo_id = s.get(Document, 903).contract.id
    client.post("/recurring/action", data={"action": "cost", "contract_id": str(cid), "monthly": "34.9"})
    with session_scope() as s:
        assert s.get(Contract, cid).monthly_cost == 34.9
    r = client.post("/recurring/action", data={"action": "link", "key": key, "doc_id": "2"})  # Demo: noch kein Vertrag
    assert "(noch) kein Vertrag" in r.text
    r = client.post("/recurring/action", data={"action": "link", "key": key, "contract_id": str(abo_id)})
    assert "gehört zu" in r.text
    with session_scope() as s:
        assert s.get(Contract, abo_id).monthly_cost == 12.99  # Monatsbeitrag aus den Abbuchungen
    page = client.get("/recurring").text
    assert "Betrag geändert (" not in page and "Ohne Vertrag (" not in page


def test_price_increase_stays_one_series():
    found = recurring.detect(monthly("Mobilfunk AG", 19.99, 12, change_at=8, new_amount=24.99))
    assert len(found) == 1 and found[0].change()[:2] == (19.99, 24.99) and len(found[0].amounts) == 12
