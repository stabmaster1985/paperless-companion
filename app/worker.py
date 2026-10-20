"""Hintergrund-Thread: Synchronisation, Analyse-Warteschlange und Erinnerungen."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app import payments, pipeline, reminders
from app.config import Settings
from app.db import session_scope
from app.models import Document
from app.firefly import FireflyClient, FireflyError
from app.ollama import OllamaClient
from app.paperless import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)


def make_paperless(settings: Settings, timeout: float = 30.0) -> PaperlessClient:
    return PaperlessClient(settings.paperless_url, settings.paperless_token, timeout=timeout)


def make_firefly(settings: Settings) -> FireflyClient | None:
    if not settings.firefly_enabled:
        return None
    return FireflyClient(settings.firefly_url, settings.firefly_token)


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
        self._last_payments = -3600.0
        self._writeback_retry_at = 0.0
        self.sync_requested = True
        self.current_document: int | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        with session_scope() as session:
            # Nach Neustart abgebrochene Analysen erneut einreihen
            session.execute(update(Document).where(Document.extraction_state == "running").values(extraction_state="queued"))
            session.execute(update(Document).where(Document.summary_state == "running").values(summary_state="queued"))
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
                self._index_sync(paperless)
                self._payments()
                self._writebacks(paperless)
                self._reminders()
                worked = self._process_next(paperless, ollama) or self._summarize_next(paperless, ollama)
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

    def _index_sync(self, paperless: PaperlessClient) -> None:
        """Einmal täglich (und beim ersten Start) den Metadaten-Index des ganzen Archivs abgleichen."""
        if not self.settings.paperless_token:
            return
        with session_scope() as session:
            last = pipeline.kv_get(session, "last_index_sync")
        if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=24):
            return
        try:
            with session_scope() as session:
                total, removed = pipeline.index_sync(session, paperless, self.settings)
            log.info("Archiv-Index abgeglichen: %s Dokumente, %s entfernt", total, removed)
        except PaperlessError as exc:
            log.warning("Archiv-Index fehlgeschlagen: %s", exc)
            self.last_error = str(exc)
            with session_scope() as session:
                # Nicht in jeder Schleife erneut versuchen – in einer Stunde wieder
                retry = datetime.now(timezone.utc) - timedelta(hours=23)
                pipeline.kv_set(session, "last_index_sync", retry.isoformat())

    def _writebacks(self, paperless: PaperlessClient) -> None:
        """Vorgemerkte Änderungen nach Paperless schreiben (bei Fehlern höchstens alle 5 Minuten)."""
        if time.monotonic() < self._writeback_retry_at:
            return
        with session_scope() as session:
            pending = session.scalar(select(func.count()).select_from(Document).where(Document.writeback_pending.is_(True)))
            if not pending:
                return
            pipeline.flush_writebacks(session, paperless, self.settings, datetime.now(self.tz).date())
            still = session.scalar(select(func.count()).select_from(Document).where(Document.writeback_pending.is_(True)))
        if still:
            self._writeback_retry_at = time.monotonic() + 300

    def _payments(self) -> None:
        """Stündlich offene Rechnungen mit Firefly-III-Buchungen abgleichen."""
        if not self.settings.firefly_enabled or time.monotonic() - self._last_payments < 3600:
            return
        self._last_payments = time.monotonic()
        client = make_firefly(self.settings)
        try:
            with session_scope() as session:
                counts = payments.run(session, client, self.settings, datetime.now(self.tz).date())
            if any(counts.values()):
                log.info("Zahlungsabgleich: %s", counts)
        except FireflyError as exc:
            log.warning("Zahlungsabgleich fehlgeschlagen: %s", exc)
            self.last_error = str(exc)
        finally:
            client.close()

    def _summarize_next(self, paperless: PaperlessClient, ollama: OllamaClient | None) -> bool:
        """Eine Zusammenfassung erzeugen – erst wenn keine Analyse mehr wartet."""
        if ollama is None:
            return False
        with session_scope() as session:
            doc = pipeline.next_summary(session)
            if doc is None:
                return False
            doc.summary_state = "running"
            doc_id = doc.id
        self.current_document = doc_id
        try:
            with session_scope() as session:
                pipeline.process_summary(session, session.get(Document, doc_id), paperless, ollama, self.settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("Zusammenfassung von Dokument %s fehlgeschlagen: %s", doc_id, exc)
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                doc.summary_state = "error"
                doc.extraction_error = f"Zusammenfassung: {str(exc)[:500]}"
        finally:
            self.current_document = None
        return True

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
