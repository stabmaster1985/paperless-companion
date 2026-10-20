"""Hintergrund-Thread: Synchronisation, Analyse-Warteschlange und Erinnerungen."""

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app import pipeline, reminders
from app.config import Settings
from app.db import session_scope
from app.models import Document
from app.ollama import OllamaClient
from app.paperless import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)


def make_paperless(settings: Settings, timeout: float = 30.0) -> PaperlessClient:
    return PaperlessClient(settings.paperless_url, settings.paperless_token, timeout=timeout)


def make_ollama(settings: Settings) -> OllamaClient | None:
    if not settings.llm_enabled:
        return None
    return OllamaClient(
        settings.ollama_url,
        settings.ollama_model,
        keep_alive=settings.ollama_keep_alive,
        num_ctx=settings.ollama_num_ctx,
        timeout=settings.ollama_timeout_seconds,
    )


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="companion-worker", daemon=True)
        self._last_sync = 0.0
        self.sync_requested = True
        self.current_document: int | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        with session_scope() as session:
            # Nach Neustart abgebrochene Analysen erneut einreihen
            session.execute(update(Document).where(Document.extraction_state == "running").values(extraction_state="queued"))
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        self._thread.join(timeout=5)

    def request_sync(self) -> None:
        self.sync_requested = True
        self.wake.set()

    def _run(self) -> None:
        paperless = make_paperless(self.settings)
        ollama = make_ollama(self.settings)
        while not self._stop.is_set():
            try:
                if self.sync_requested or time.monotonic() - self._last_sync > self.settings.sync_interval_minutes * 60:
                    self.sync_requested = False
                    self._last_sync = time.monotonic()
                    self._sync(paperless)
                self._reminders()
                worked = self._process_next(paperless, ollama)
            except Exception as exc:  # noqa: BLE001 - Thread darf nicht sterben
                log.exception("Fehler im Hintergrundprozess")
                self.last_error = str(exc)
                worked = False
            if not worked:
                self.wake.wait(10)
                self.wake.clear()

    def _sync(self, paperless: PaperlessClient) -> None:
        if not self.settings.paperless_token:
            return
        try:
            with session_scope() as session:
                count = pipeline.sync(session, paperless, self.settings)
            if count:
                log.info("%s Dokumente aus Paperless synchronisiert", count)
            self.last_error = None
        except PaperlessError as exc:
            log.warning("Synchronisation fehlgeschlagen: %s", exc)
            self.last_error = str(exc)

    def _process_next(self, paperless: PaperlessClient, ollama: OllamaClient | None) -> bool:
        with session_scope() as session:
            doc = pipeline.next_queued(session)
            if doc is None:
                return False
            doc.extraction_state = "running"
            doc_id = doc.id
        self.current_document = doc_id
        try:
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                pipeline.process_document(session, doc, paperless, ollama, self.settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("Analyse von Dokument %s fehlgeschlagen: %s", doc_id, exc)
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                doc.extraction_state = "error"
                doc.extraction_error = str(exc)[:1000]
        finally:
            self.current_document = None
        return True

    def _reminders(self) -> None:
        now = datetime.now(self.tz)
        if now.hour < self.settings.reminder_hour:
            return
        today = now.date()
        with session_scope() as session:
            if pipeline.kv_get(session, "last_reminder_run") == today.isoformat():
                return
            try:
                sent = reminders.run(session, self.settings, today)
            except Exception as exc:  # noqa: BLE001
                log.warning("Erinnerungen konnten nicht gesendet werden: %s", exc)
                sent = 0
            # Auch bei Fehlern nur einmal pro Tag versuchen; manuell über /system auslösbar
            pipeline.kv_set(session, "last_reminder_run", today.isoformat())
            if sent:
                log.info("%s Erinnerungen gesendet", sent)
