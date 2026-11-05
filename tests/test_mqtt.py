import asyncio
import json
import socket
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import mqtt
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document, Invoice, KeyValue, Reimbursement, Warranty

TODAY = date(2026, 11, 5)


def add_data(s):
    s.add(Document(id=1, title="Stadtwerke Abschlag November", correspondent="Stadtwerke", tags=[], kind="invoice",
                   invoice=Invoice(issuer="Stadtwerke", amount=89.0, due_date=date(2026, 11, 12), status="open")))
    s.add(Document(id=2, title="Handwerker", tags=[], kind="invoice",
                   invoice=Invoice(issuer="Maler Muster", amount=323.8, due_date=date(2026, 10, 30), status="open")))
    s.add(Document(id=3, title="Versicherung", tags=[], kind="invoice",  # Lastschrift: zählt nicht als offen
                   invoice=Invoice(amount=50.0, due_date=date(2026, 11, 20), status="open", payment_method="debit")))
    s.add(Document(id=4, title="Bezahlt", tags=[], kind="invoice", invoice=Invoice(amount=10.0, status="paid")))
    s.add(Document(id=5, title="Handyvertrag", tags=[], kind="contract",
                   contract=Contract(name="Handyvertrag", start_date=date(2025, 1, 1), initial_term_months=24,
                                     renewal_mode="renews", renewal_months=12, notice_value=1, notice_unit="months")))
    s.add(Document(id=6, title="Fernseher", tags=[], kind="invoice",
                   warranty=Warranty(product="Fernseher", purchase_date=date(2024, 11, 20), months=24)))
    s.add(Document(id=7, title="Leistungsabrechnung", tags=[], kind="reimbursement",
                   reimbursement=Reimbursement(amount=54.2, statement_date=date(2026, 10, 20))))
    s.add(Document(id=8, title="Neu", tags=[], review_state="pending"))
    s.add(KeyValue(key="last_sync", value=datetime(2026, 11, 5, 8, 30, tzinfo=timezone.utc).isoformat()))


def test_state_uses_existing_logic(db, settings):
    settings.public_url = "http://192.168.0.114:8010"
    with db() as s:
        add_data(s)
    with db() as s:
        state = mqtt.build_state(s, settings, TODAY)
        assert state["offene_rechnungen"] == 2 and state["offene_rechnungen_summe"] == 412.8
        assert state["ueberfaellige_rechnungen"] == 1
        assert state["naechste_faelligkeit"] == "2026-10-30" and state["naechste_faelligkeit_titel"] == "Maler Muster"
        assert state["naechste_faelligkeit_betrag"] == 323.8
        assert state["garantien_30_tage"] == 1
        assert state["erstattungen_offen"] == 1 and state["erstattungen_summe"] == 54.2
        assert state["zur_pruefung"] == 1
        assert state["letzter_sync"] == "2026-11-05T09:30:00+01:00"  # lokale Zeit
        assert state["naechste_frist"] and state["naechste_frist_titel"] == "Handyvertrag"
        assert set(state) == {"offene_rechnungen", "offene_rechnungen_summe", "ueberfaellige_rechnungen",
                              "naechste_faelligkeit", "naechste_faelligkeit_titel", "naechste_faelligkeit_betrag",
                              "fristen_30_tage", "naechste_frist", "naechste_frist_titel", "garantien_30_tage",
                              "erstattungen_offen", "erstattungen_summe", "zur_pruefung", "letzter_sync"}
        invoices = mqtt.build_invoices(s, settings, TODAY)["rechnungen"]
        assert [i["doc_id"] for i in invoices] == [2, 1]  # nach Fälligkeit, ohne Lastschrift/bezahlte
        assert invoices[0] == {"titel": "Handwerker", "korrespondent": "Maler Muster", "betrag": 323.8,
                               "faellig": "2026-10-30", "ueberfaellig": True, "doc_id": 2,
                               "url": "http://192.168.0.114:8010/documents/2"}
        fristen = mqtt.build_deadlines(s, settings, TODAY)["fristen"]
        assert fristen[0]["titel"] == "Handyvertrag" and fristen[0]["art"] == "Kündigungsfrist"
        json.dumps(state)  # alles serialisierbar


def test_empty_state_has_nulls(db, settings):
    with db() as s:
        state = mqtt.build_state(s, settings, TODAY)
    assert state["offene_rechnungen"] == 0 and state["naechste_faelligkeit"] is None and state["letzter_sync"] is None


def test_discovery_configs(settings):
    settings.public_url = "http://192.168.0.114:8010"
    configs = dict(mqtt.discovery_configs(settings, "abc1234"))
    assert len(configs) == 12
    summe = configs["homeassistant/sensor/paperless_companion/offene_rechnungen_summe/config"]
    assert summe["device_class"] == "monetary" and summe["unit_of_measurement"] == "EUR"
    assert summe["state_topic"] == "paperless_companion/state" and summe["availability_topic"] == "paperless_companion/status"
    assert summe["unique_id"] == "paperless_companion_offene_rechnungen_summe"
    assert summe["device"] == {"identifiers": ["paperless_companion"], "name": "Paperless Companion",
                               "manufacturer": "stabmaster1985", "model": "Paperless Companion", "sw_version": "abc1234",
                               "configuration_url": "http://192.168.0.114:8010"}
    offen = configs["homeassistant/sensor/paperless_companion/offene_rechnungen/config"]
    assert offen["json_attributes_topic"] == "paperless_companion/rechnungen" and offen["icon"] == "mdi:receipt-text-clock"
    assert configs["homeassistant/sensor/paperless_companion/fristen_30_tage/config"]["json_attributes_topic"] == \
        "paperless_companion/fristen"
    assert configs["homeassistant/sensor/paperless_companion/letzter_sync/config"]["entity_category"] == "diagnostic"
    binary = configs["homeassistant/binary_sensor/paperless_companion/rechnung_ueberfaellig/config"]
    assert binary["device_class"] == "problem" and "ueberfaellige_rechnungen > 0" in binary["value_template"]
    button = configs["homeassistant/button/paperless_companion/sync/config"]
    assert button["command_topic"] == "paperless_companion/cmd/sync" and "state_topic" not in button
    nf = configs["homeassistant/sensor/paperless_companion/naechste_faelligkeit/config"]
    assert nf["device_class"] == "date" and "naechste_faelligkeit_betrag" in nf["json_attributes_template"]


# --------------------------------------------------------------------------- Client


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload.encode()


class FakeClient:
    """Ersatz für aiomqtt.Client: merkt sich Veröffentlichungen, liefert vorgegebene Nachrichten."""

    instances: list = []
    incoming: list = []

    def __init__(self, hostname, **kwargs):
        self.hostname, self.kwargs = hostname, kwargs
        self.published, self.subscribed = [], []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    @property
    def messages(self):
        async def gen():
            for topic, payload in FakeClient.incoming:
                await asyncio.sleep(0.05)
                yield FakeMessage(topic, payload)
            await asyncio.sleep(3600)
        return gen()


@pytest.fixture
def fake_mqtt(monkeypatch):
    import aiomqtt

    FakeClient.instances, FakeClient.incoming = [], []
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    return FakeClient


def test_bridge_publishes_and_handles_commands(db, settings, fake_mqtt):
    settings.mqtt_enabled, settings.mqtt_host = True, "192.168.0.102"
    with db() as s:
        add_data(s)
    fake_mqtt.incoming = [("paperless_companion/cmd/bezahlt", '{"doc_id": 1}'),
                          ("paperless_companion/cmd/sync", "PRESS"), ("homeassistant/status", "online")]
    synced = []

    async def scenario():
        bridge = mqtt.MqttBridge(settings, sw_version=lambda: "abc1234", on_sync=lambda: synced.append(1),
                                 today=lambda: TODAY)
        bridge.start()
        await asyncio.sleep(1.0)
        await bridge.stop()
        return bridge

    bridge = asyncio.run(scenario())
    client = fake_mqtt.instances[0]
    assert client.hostname == "192.168.0.102" and client.kwargs["will"].topic == "paperless_companion/status"
    topics = [t for t, _, _ in client.published]
    assert topics[0] == "paperless_companion/status" and client.published[0][1] == "online"
    assert all(retain for _, _, retain in client.published)
    assert topics.count("homeassistant/button/paperless_companion/sync/config") == 2  # Start + HA-Neustart
    assert "paperless_companion/rechnungen" in topics and "paperless_companion/fristen" in topics
    assert set(client.subscribed) == {"paperless_companion/cmd/sync", "paperless_companion/cmd/bezahlt", "homeassistant/status"}
    assert synced == [1]
    states = [json.loads(p) for t, p, _ in client.published if t == "paperless_companion/state"]
    assert states[0]["offene_rechnungen"] == 2 and states[-1]["offene_rechnungen"] == 1  # nach „bezahlt“
    with session_scope() as s:
        inv = s.get(Document, 1).invoice
        assert inv.status == "paid" and inv.paid_on == TODAY and s.get(Document, 1).writeback_pending
    assert client.published[-1][:2] == ("paperless_companion/status", "offline")
    assert not bridge.connected


def test_disabled_starts_no_client(settings, mock_api, fake_mqtt):
    settings.mqtt_enabled = False
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        time.sleep(0.3)
    assert fake_mqtt.instances == []


def test_unreachable_broker_does_not_block_start(settings, mock_api):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # Port ohne Broker
    settings.mqtt_enabled, settings.mqtt_host, settings.mqtt_port = True, "127.0.0.1", port
    started = time.monotonic()
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert time.monotonic() - started < 5
        for _ in range(30):
            if app.state.mqtt.last_error:
                break
            time.sleep(0.1)
        assert app.state.mqtt.last_error and not app.state.mqtt.connected
        assert c.get("/").status_code == 200  # App arbeitet normal weiter
