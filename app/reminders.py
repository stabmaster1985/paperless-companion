"""Tägliche Erinnerungen als Sammelnachricht."""

from datetime import date

from sqlalchemy.orm import Session

from app import notify
from app.config import Settings
from app.events import Event, collect, fmt_date
from app.models import SentReminder


def due_reminders(session: Session, today: date, lead_days: list[int]) -> list[tuple[Event, list[str]]]:
    """Ereignisse, für die heute eine Erinnerung fällig ist, samt der dabei zu markierenden Schlüssel.

    Pro Ereignis wird nur die kleinste erreichte Vorlaufstufe gemeldet; größere, verpasste
    Stufen (z.B. nach Ausfallzeiten) werden mit markiert, damit nichts doppelt kommt.
    """
    result = []
    for event in collect(session, today):
        days_left = (event.date - today).days
        if event.type == "overdue":
            keys = [f"{event.key}:{event.date}:overdue"]
        else:
            reached = [d for d in lead_days if days_left <= d]
            if not reached or days_left < 0:
                continue
            keys = [f"{event.key}:{event.date}:{d}" for d in sorted(reached)]
        if session.get(SentReminder, keys[0]) is None:
            result.append((event, keys))
    return result


def format_digest(items: list[tuple[Event, list[str]]], today: date, link_base: str) -> str:
    lines = []
    for event, _ in items:
        days_left = (event.date - today).days
        when = "heute" if days_left == 0 else (f"seit {-days_left} Tagen" if days_left < 0 else f"in {days_left} Tagen")
        lines.append(f"• {event.label}: {event.title} – {fmt_date(event.date)} ({when})")
        lines.append(f"  {event.detail}")
        if link_base:
            lines.append(f"  {link_base}/documents/{event.document_id}/details")
    return "\n".join(lines)


def run(session: Session, settings: Settings, today: date) -> int:
    items = due_reminders(session, today, settings.reminder_day_list)
    if not items:
        return 0
    body = format_digest(items, today, settings.paperless_link_base)
    notify.send(settings, f"Paperless Companion: {len(items)} Fristen", body)
    for _, keys in items:
        for key in keys:
            if session.get(SentReminder, key) is None:
                session.add(SentReminder(key=key))
    return len(items)
