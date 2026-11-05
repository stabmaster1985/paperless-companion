"""Kennzahlen per MQTT an Home Assistant (MQTT-Discovery) – optional, standardmäßig aus.

Home Assistant legt die Sensoren über die Discovery-Configs selbst an (Gerät „Paperless Companion“).
Was als offen, überfällig, Frist, Garantie und Erstattung zählt, kommt aus der vorhandenen Logik
(events.collect wie Übersicht/Erinnerungen, reimbursements.overview) – es gibt keine zweite Berechnung.

Der Client läuft als Hintergrund-Task im Lifespan der App. Ist der Broker nicht erreichbar, wird nur
geloggt und mit Backoff (5 s bis 5 min) neu verbunden; die App startet und arbeitet normal weiter.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app import events, metadata, reimbursements
from app.config import Settings
from app.db import session_scope
from app.models import Document, Invoice, KeyValue

log = logging.getLogger(__name__)

LIST_LIMIT = 15
SOON_DAYS = 30
DEADLINE_TYPES = ("cancel", "term_end", "follow_up")  # Fristen (Garantien zählen extra)
BACKOFF_START, BACKOFF_MAX = 5, 300


# --------------------------------------------------------------------------- Inhalte


def _url(settings: Settings, doc_id: int | None) -> str | None:
    base = (settings.public_url or "").rstrip("/")
    return f"{base}/documents/{doc_id}" if base and doc_id else None


def _money(value) -> float | None:
    return round(float(value), 2) if value is not None else None


def open_invoices(session) -> list[Invoice]:
    """Offene Rechnungen wie in der Übersicht: Lastschriften zählen nicht (die werden abgebucht)."""
    return list(session.scalars(select(Invoice).where(Invoice.status == "open", Invoice.payment_method != "debit")))


def build_state(session, settings: Settings, today: date) -> dict:
    """Zustand für paperless_companion/state."""
    all_events = events.collect(session, today)
    soon = today + timedelta(days=SOON_DAYS)
    invoices = open_invoices(session)
    payable = [e for e in all_events if e.type in ("due", "overdue")]
    deadlines = [e for e in all_events if e.type in DEADLINE_TYPES and e.date >= today]
    warranties = [e for e in all_events if e.type == "warranty" and today <= e.date <= soon]
    refunds = reimbursements.overview(session, settings)
    meta_open, _ = metadata.open_counts(session)
    inbox = session.scalar(select(func.count()).select_from(Document).where(Document.review_state == "pending")) or 0
    next_due = payable[0] if payable else None
    next_deadline = deadlines[0] if deadlines else None
    last_sync = session.get(KeyValue, "last_sync")
    last_sync_local = None
    if last_sync and last_sync.value:
        last_sync_local = datetime.fromisoformat(last_sync.value).astimezone(ZoneInfo(settings.timezone)).isoformat(
            timespec="seconds")
    return {
        "offene_rechnungen": len(invoices),
        "offene_rechnungen_summe": round(sum(i.amount or 0 for i in invoices), 2),
        "ueberfaellige_rechnungen": len([e for e in payable if e.type == "overdue"]),
        "naechste_faelligkeit": next_due.date.isoformat() if next_due else None,
        "naechste_faelligkeit_titel": next_due.title if next_due else None,
        "naechste_faelligkeit_betrag": _money(next_due.amount) if next_due else None,
        "fristen_30_tage": len([e for e in deadlines if e.date <= soon]),
        "naechste_frist": next_deadline.date.isoformat() if next_deadline else None,
        "naechste_frist_titel": next_deadline.title if next_deadline else None,
        "garantien_30_tage": len(warranties),
        "erstattungen_offen": refunds["expected_count"],
        "erstattungen_summe": _money(refunds["expected_sum"]),
        # zu prüfende Dokumente (Inbox) und offene Metadaten-Vorschläge
        "zur_pruefung": inbox + meta_open,
        "letzter_sync": last_sync_local,
    }


def build_invoices(session, settings: Settings, today: date) -> dict:
    """Liste für paperless_companion/rechnungen: offene Rechnungen nach Fälligkeit (ohne Datum zuletzt)."""
    items = sorted(open_invoices(session), key=lambda i: (i.due_date is None, i.due_date or date.max, i.id))
    return {"rechnungen": [{
        "titel": (inv.document.title if inv.document else None) or inv.issuer or "Rechnung",
        "korrespondent": inv.issuer or (inv.document.correspondent if inv.document else None),
        "betrag": _money(inv.amount),
        "faellig": inv.due_date.isoformat() if inv.due_date else None,
        "ueberfaellig": bool(inv.due_date and inv.due_date < today),
        "doc_id": inv.document_id,
        "url": _url(settings, inv.document_id),
    } for inv in items[:LIST_LIMIT]]}


def build_deadlines(session, settings: Settings, today: date) -> dict:
    """Liste für paperless_companion/fristen: kommende Fristen (Kündigung, Vertragsende, Wiedervorlage)."""
    items = [e for e in events.collect(session, today) if e.type in DEADLINE_TYPES and e.date >= today]
    return {"fristen": [{
        "titel": e.title, "art": e.label, "datum": e.date.isoformat(), "doc_id": e.document_id,
        "url": _url(settings, e.document_id) if e.document_id else (
            f"{settings.public_url.rstrip('/')}{e.url}" if settings.public_url else None),
    } for e in items[:LIST_LIMIT]]}


# --------------------------------------------------------------------------- Discovery


def topics(settings: Settings) -> dict[str, str]:
    base = settings.mqtt_base_topic.strip("/") or "paperless_companion"
    return {name: f"{base}/{name}" for name in ("status", "state", "rechnungen", "fristen")} | {
        "cmd_sync": f"{base}/cmd/sync", "cmd_paid": f"{base}/cmd/bezahlt"}


# (Komponente, Objekt, Name, Wert-Template, Zusätze)
ENTITIES: list[tuple[str, str, str, str | None, dict]] = [
    ("sensor", "offene_rechnungen", "Offene Rechnungen", "{{ value_json.offene_rechnungen }}",
     {"state_class": "measurement", "icon": "mdi:receipt-text-clock", "json_attributes_topic": "@rechnungen"}),
    ("sensor", "offene_rechnungen_summe", "Offene Rechnungen Summe", "{{ value_json.offene_rechnungen_summe }}",
     {"device_class": "monetary", "unit_of_measurement": "EUR"}),
    ("sensor", "ueberfaellige_rechnungen", "Überfällige Rechnungen", "{{ value_json.ueberfaellige_rechnungen }}",
     {"icon": "mdi:alert-circle"}),
    ("sensor", "naechste_faelligkeit", "Nächste Fälligkeit", "{{ value_json.naechste_faelligkeit }}",
     {"device_class": "date", "json_attributes_topic": "@state",
      "json_attributes_template": '{{ {"titel": value_json.naechste_faelligkeit_titel, '
                                  '"betrag": value_json.naechste_faelligkeit_betrag} | tojson }}'}),
    ("sensor", "fristen_30_tage", "Fristen (30 Tage)", "{{ value_json.fristen_30_tage }}",
     {"json_attributes_topic": "@fristen"}),
    ("sensor", "naechste_frist", "Nächste Frist", "{{ value_json.naechste_frist }}",
     {"device_class": "date", "json_attributes_topic": "@state",
      "json_attributes_template": '{{ {"titel": value_json.naechste_frist_titel} | tojson }}'}),
    ("sensor", "garantien_30_tage", "Garantien laufen ab (30 Tage)", "{{ value_json.garantien_30_tage }}", {}),
    ("sensor", "erstattungen_offen", "Offene Erstattungen", "{{ value_json.erstattungen_offen }}",
     {"json_attributes_topic": "@state",
      "json_attributes_template": '{{ {"summe": value_json.erstattungen_summe} | tojson }}'}),
    ("sensor", "zur_pruefung", "Vorschläge zur Prüfung", "{{ value_json.zur_pruefung }}",
     {"icon": "mdi:clipboard-check-outline"}),
    ("sensor", "letzter_sync", "Letzter Abgleich", "{{ value_json.letzter_sync }}",
     {"device_class": "timestamp", "entity_category": "diagnostic"}),
    ("binary_sensor", "rechnung_ueberfaellig", "Rechnung überfällig",
     "{{ 'ON' if value_json.ueberfaellige_rechnungen > 0 else 'OFF' }}", {"device_class": "problem"}),
    ("button", "sync", "Jetzt abgleichen", None, {"command_topic": "@cmd_sync", "icon": "mdi:sync"}),
]


def device(settings: Settings, sw_version: str | None) -> dict:
    info = {"identifiers": ["paperless_companion"], "name": "Paperless Companion", "manufacturer": "stabmaster1985",
            "model": "Paperless Companion"}
    if sw_version:
        info["sw_version"] = sw_version
    if settings.public_url:
        info["configuration_url"] = settings.public_url
    return info


def discovery_configs(settings: Settings, sw_version: str | None = None) -> list[tuple[str, dict]]:
    """(Topic, Config) je Entität für homeassistant/<komponente>/paperless_companion/<objekt>/config."""
    t = topics(settings)
    prefix = settings.mqtt_discovery_prefix.strip("/") or "homeassistant"
    result = []
    for component, obj, name, template, extra in ENTITIES:
        config = {"name": name, "unique_id": f"paperless_companion_{obj}", "object_id": f"paperless_companion_{obj}",
                  "availability_topic": t["status"], "device": device(settings, sw_version)}
        if component != "button":
            config["state_topic"] = t["state"]
            config["value_template"] = template
        for key, value in extra.items():
            config[key] = t[value[1:]] if isinstance(value, str) and value.startswith("@") else value
        result.append((f"{prefix}/{component}/paperless_companion/{obj}/config", config))
    return result


# --------------------------------------------------------------------------- Befehle aus Home Assistant


def mark_paid(session, doc_id: int, today: date) -> bool:
    """Rechnung als bezahlt markieren – wie „Bezahlt“ in der Oberfläche (Paperless erst nach deiner Zustimmung)."""
    from app import pipeline

    inv = session.scalar(select(Invoice).where(Invoice.document_id == doc_id))
    if inv is None:
        return False
    inv.status, inv.paid_on = "paid", today
    inv.document.writeback_pending = True
    pipeline.set_source(inv.document, ["status", "paid_on"], "manual", confirmed=True)
    return True


# --------------------------------------------------------------------------- Client


class MqttBridge:
    """Verbindung zum Broker im Hintergrund. notify() (auch aus anderen Threads) löst eine Veröffentlichung aus."""

    def __init__(self, settings: Settings, sw_version=None, on_sync=None, today=None):
        self.settings = settings
        self.sw_version = sw_version  # Funktion -> str | None
        self.on_sync = on_sync  # Funktion, die einen Abgleich anstößt
        self.today = today or date.today
        self.connected = False
        self.last_error: str | None = None
        self._changed: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._published: dict[str, str] = {}

    # Aufruf aus Routen oder dem Worker-Thread
    def notify(self) -> None:
        if self._loop is not None and self._changed is not None:
            self._loop.call_soon_threadsafe(self._changed.set)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()
        self._task = asyncio.create_task(self.run(), name="mqtt")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - Beenden darf nie scheitern
                pass

    def _key(self) -> tuple:
        s = self.settings
        return (s.mqtt_enabled, s.mqtt_host, s.mqtt_port, s.mqtt_user, s.mqtt_password, s.mqtt_base_topic,
                s.mqtt_discovery_prefix, s.public_url)

    async def run(self) -> None:
        delay = BACKOFF_START
        while True:
            if not (self.settings.mqtt_enabled and self.settings.mqtt_host):
                self.connected = False
                await asyncio.sleep(30)  # aus: nichts verbinden, Einstellung gelegentlich neu lesen
                continue
            try:
                await self._session()
                delay = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Broker-Probleme nur loggen
                self.connected = False
                self.last_error = str(exc)
                log.warning("MQTT: %s – neuer Versuch in %s s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        import aiomqtt

        s, t = self.settings, topics(self.settings)
        key = self._key()
        will = aiomqtt.Will(t["status"], "offline", qos=1, retain=True)
        async with aiomqtt.Client(s.mqtt_host, port=int(s.mqtt_port), username=s.mqtt_user or None,
                                  password=s.mqtt_password or None, will=will, identifier="paperless_companion",
                                  timeout=10) as client:
            self.connected, self.last_error = True, None
            log.info("MQTT verbunden mit %s:%s", s.mqtt_host, s.mqtt_port)
            await client.publish(t["status"], "online", qos=1, retain=True)
            await self._publish_discovery(client)
            await self._publish_data(client, force=True)
            prefix = s.mqtt_discovery_prefix.strip("/") or "homeassistant"
            for topic in (t["cmd_sync"], t["cmd_paid"], f"{prefix}/status"):
                await client.subscribe(topic)
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._listen(client))
                    group.create_task(self._publisher(client, key))
            except* _Reconnect:
                pass  # Einstellungen geändert – mit neuen Werten verbinden
            finally:
                self.connected = False
                try:
                    await client.publish(t["status"], "offline", qos=1, retain=True)
                except Exception:  # noqa: BLE001
                    pass

    async def _publisher(self, client, key) -> None:
        interval = max(30, int(self.settings.mqtt_publish_interval or 900))
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=interval)
                await asyncio.sleep(2)  # mehrere Änderungen kurz hintereinander zusammenfassen
                force = False
            except TimeoutError:
                force = True  # regelmäßig, auch ohne Änderung
            self._changed.clear()
            if self._key() != key:
                raise _Reconnect
            await self._publish_data(client, force=force)

    async def _listen(self, client) -> None:
        t = topics(self.settings)
        prefix = self.settings.mqtt_discovery_prefix.strip("/") or "homeassistant"
        async for message in client.messages:
            topic = str(message.topic)
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload or "")
            try:
                if topic == f"{prefix}/status" and payload.strip().lower() == "online":
                    # Home Assistant neu gestartet: alles neu senden, damit keine Entitäten fehlen
                    await self._publish_discovery(client)
                    await self._publish_data(client, force=True)
                elif topic == t["cmd_sync"]:
                    log.info("MQTT: Abgleich aus Home Assistant angefordert")
                    if self.on_sync:
                        self.on_sync()
                elif topic == t["cmd_paid"]:
                    doc_id = int(json.loads(payload or "{}").get("doc_id"))
                    done = await asyncio.to_thread(self._mark_paid, doc_id)
                    log.info("MQTT: Rechnung zu Dokument %s %s", doc_id, "als bezahlt markiert" if done else "nicht gefunden")
                    await self._publish_data(client, force=True)
            except Exception as exc:  # noqa: BLE001 - fehlerhafte Nachricht nur loggen
                log.warning("MQTT: Nachricht auf %s nicht verarbeitet: %s", topic, exc)

    def _mark_paid(self, doc_id: int) -> bool:
        with session_scope() as session:
            return mark_paid(session, doc_id, self.today())

    async def _publish_discovery(self, client) -> None:
        version = self.sw_version() if callable(self.sw_version) else self.sw_version
        for topic, config in discovery_configs(self.settings, version):
            await client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)

    def _collect(self) -> dict[str, dict]:
        t, today = topics(self.settings), self.today()
        with session_scope() as session:
            return {t["state"]: build_state(session, self.settings, today),
                    t["rechnungen"]: build_invoices(session, self.settings, today),
                    t["fristen"]: build_deadlines(session, self.settings, today)}

    async def _publish_data(self, client, force: bool = False) -> None:
        data = await asyncio.to_thread(self._collect)  # Datenbank nicht im Event-Loop abfragen
        for topic, value in data.items():
            payload = json.dumps(value, ensure_ascii=False)
            if force or self._published.get(topic) != payload:
                await client.publish(topic, payload, qos=1, retain=True)
                self._published[topic] = payload


class _Reconnect(Exception):
    pass
