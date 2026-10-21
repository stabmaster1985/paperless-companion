"""Hintergrund-Thread: Synchronisation, Analyse-Warteschlange und Erinnerungen."""

import logging
import threading
import time
from collections import deque
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
        num_thread=settings.ollama_num_thread,
    )


class Worker:
    def __init__(self, settings: Settings, version=None):
        self.settings = settings
        self.version = version
        self.tz = ZoneInfo(settings.timezone)
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="companion-worker", daemon=True)
        self._last_sync = 0.0
        self._last_payments = -3600.0
        self._last_tidy = 0.0
        self._last_llm_retry = 0.0
        self.sync_requested = True
        self.current_document: int | None = None
        self.current_task: str | None = None
        self.current_since: float | None = None
        # Letzte Aktivitäten für die Systemseite (nur im Speicher, neueste zuerst)
        self.activity: deque[dict] = deque(maxlen=40)
        self.stats = {"day": None, "analyses": 0, "ai_calls": 0, "ai_seconds": 0.0, "summaries": 0}
        self._noted_idle_reason: str | None = None
        self.last_error: str | None = None

    def note(self, text: str, level: str = "info", document_id: int | None = None) -> None:
        self.activity.appendleft({
            "at": datetime.now(self.tz).strftime("%d.%m. %H:%M:%S"), "text": text, "level": level,
            "document_id": document_id,
        })

    def _count(self, key: str, amount: float = 1) -> None:
        today = datetime.now(self.tz).date().isoformat()
        if self.stats["day"] != today:
            self.stats.update(day=today, analyses=0, ai_calls=0, ai_seconds=0.0, summaries=0)
        self.stats[key] += amount

    def _idle(self, reason: str | None) -> None:
        """Pausen-/Wartegründe nur bei Änderung protokollieren, damit das Protokoll lesbar bleibt."""
        if reason and reason != self._noted_idle_reason:
            self.note(reason, "muted")
        self._noted_idle_reason = reason

    def start(self) -> None:
        with session_scope() as session:
            # Nach Neustart abgebrochene Analysen erneut einreihen
            session.execute(update(Document).where(Document.extraction_state == "running").values(extraction_state="queued"))
            session.execute(update(Document).where(Document.summary_state == "running").values(summary_state="queued"))
        ai = "aus" if not self.settings.llm_enabled else (
            f"Modell {self.settings.ollama_model}" + (f", nur {self.settings.llm_active_hours} Uhr" if self.settings.llm_active_hours else "")
        )
        self.note(f"Companion gestartet (AI: {ai})")
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
                self._tidy_changes()
                self._retry_llm_errors(ollama)
                if self.version is not None:
                    self.version.refresh()  # höchstens stündlich bei GitHub
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
                self.note(f"Abgleich mit Paperless: {count} neue oder geänderte Dokumente")
            self.last_error = None
        except PaperlessError as exc:
            log.warning("Synchronisation fehlgeschlagen: %s", exc)
            self.last_error = str(exc)
            self.note(f"Abgleich mit Paperless fehlgeschlagen: {exc}", "error")

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

    def _retry_llm_errors(self, ollama: OllamaClient | None) -> None:
        """Ist Ollama (wieder) erreichbar, Dokumente erneut analysieren, bei denen die AI fehlte."""
        if ollama is None or time.monotonic() - self._last_llm_retry < 600 or not self.llm_allowed_now():
            return
        self._last_llm_retry = time.monotonic()
        with session_scope() as session:
            if not pipeline.documents_with_llm_error(session):
                return
        status = ollama.status()
        if not (status["reachable"] and status["model_present"]):
            return
        with session_scope() as session:
            count = pipeline.requeue_llm_errors(session)
        if count:
            log.info("AI wieder erreichbar: %s Dokumente werden erneut analysiert", count)
            self.note(f"AI bereit: {count} Dokumente werden mit AI nachanalysiert")

    def _tidy_changes(self) -> None:
        """Markierungen ohne tatsächliche Unterschiede entfernen (für die Zahl im Menü). Schreibt nichts nach Paperless."""
        if time.monotonic() - self._last_tidy < 60:
            return
        self._last_tidy = time.monotonic()
        with session_scope() as session:
            pipeline.documents_with_changes(session, self.settings, datetime.now(self.tz).date())

    def _payments(self) -> None:
        """Stündlich offene Rechnungen mit Firefly-III-Buchungen abgleichen."""
        if not self.settings.firefly_enabled or time.monotonic() - self._last_payments < 3600:
            return
        self._last_payments = time.monotonic()
        client = make_firefly(self.settings)
        try:
            with session_scope() as session:
                counts = payments.run(session, client, self.settings, datetime.now(self.tz).date())
            if counts["open"]:
                log.info("Zahlungsabgleich: %s", counts)
                found = counts["suggested"] + counts["accepted"]
                self.note(
                    f"Zahlungsabgleich mit Firefly: {counts['transactions']} Abbuchungen geprüft für "
                    f"{counts['open_with_amount']} offene Rechnungen – "
                    + (f"{found} passende Zahlungen gefunden" if found else "keine neuen Treffer")
                )
        except FireflyError as exc:
            log.warning("Zahlungsabgleich fehlgeschlagen: %s", exc)
            self.last_error = str(exc)
            self.note(f"Zahlungsabgleich mit Firefly fehlgeschlagen: {exc}", "error")
        finally:
            client.close()

    def _summarize_next(self, paperless: PaperlessClient, ollama: OllamaClient | None) -> bool:
        """Eine Zusammenfassung erzeugen – erst wenn keine Analyse mehr wartet."""
        if ollama is None or self.paused() or not self.llm_allowed_now():
            return False
        with session_scope() as session:
            doc = pipeline.next_summary(session)
            if doc is None:
                return False
            doc.summary_state = "running"
            doc_id, title = doc.id, doc.title
        self.current_document, self.current_task, self.current_since = doc_id, "Zusammenfassung", time.monotonic()
        started = time.monotonic()
        try:
            with session_scope() as session:
                pipeline.process_summary(session, session.get(Document, doc_id), paperless, ollama, self.settings)
            seconds = time.monotonic() - started
            self._count("summaries")
            self._count("ai_calls")
            self._count("ai_seconds", seconds)
            self.note(f"„{title}“: Zusammenfassung erstellt · {seconds:.1f} s", document_id=doc_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Zusammenfassung von Dokument %s fehlgeschlagen: %s", doc_id, exc)
            self.note(f"„{title}“: Zusammenfassung fehlgeschlagen – {exc}", "error", doc_id)
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                doc.summary_state = "error"
                doc.extraction_error = f"Zusammenfassung: {str(exc)[:500]}"
        finally:
            self.current_document = self.current_task = self.current_since = None
        return True

    def llm_allowed_now(self) -> bool:
        return self.settings.llm_allowed_at(datetime.now(self.tz).hour)

    def paused(self) -> bool:
        with session_scope() as session:
            return pipeline.kv_get(session, "queue_paused") == "1"

    def _process_next(self, paperless: PaperlessClient, ollama: OllamaClient | None) -> bool:
        if self.paused():
            with session_scope() as session:
                waiting = session.scalar(select(func.count()).select_from(Document).where(Document.extraction_state == "queued"))
            self._idle(f"Warteschlange angehalten – {waiting} Analysen warten" if waiting else None)
            return False
        with session_scope() as session:
            doc = pipeline.next_queued(session)
            if doc is None:
                self._idle(None)
                return False
            doc.extraction_state = "running"
            doc_id, title = doc.id, doc.title
        self._idle(None)
        self.current_document, self.current_task, self.current_since = doc_id, "Analyse", time.monotonic()
        started = time.monotonic()
        try:
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                info = pipeline.process_document(
                    session, doc, paperless, ollama, self.settings, llm_allowed=self.llm_allowed_now()
                )
            self._count("analyses")
            self.note(self._describe(title, info, time.monotonic() - started), document_id=doc_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Analyse von Dokument %s fehlgeschlagen: %s", doc_id, exc)
            self.note(f"„{title}“: Analyse fehlgeschlagen – {exc}", "error", doc_id)
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                doc.extraction_state = "error"
                doc.extraction_error = str(exc)[:1000]
        finally:
            self.current_document = self.current_task = self.current_since = None
        return True

    def _describe(self, title: str, info: dict, seconds: float) -> str:
        kind = pipeline.KINDS.get(info.get("kind"), "Dokument")
        ai = info.get("ai")
        if ai == "used":
            self._count("ai_calls")
            self._count("ai_seconds", info["ai_seconds"] or 0)
            found = len(info.get("ai_fields") or [])
            detail = f"Regeln + AI ({info['ai_seconds']} s, {found} von {len(info['wanted'])} fehlenden Feldern gefunden)"
        elif ai == "failed":
            detail = "AI nicht erreichbar – nur Regeln"
        elif ai == "deferred":
            detail = "nur Regeln, AI folgt im AI-Zeitfenster"
        elif ai == "disabled":
            detail = "nur Regeln (AI ausgeschaltet)"
        elif info.get("kind") == "other":
            detail = "keine Rechnung/Vertrag/Garantie erkannt"
        else:
            detail = "Regeln haben alles gefunden, AI nicht nötig"
        review = {"pending": " → Inbox", "confirmed": " → automatisch erfasst"}.get(info.get("review"), "")
        return f"„{title}“ ({kind}): {detail}{review} · {seconds:.1f} s"

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
