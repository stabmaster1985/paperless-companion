"""Kalender-Feed (iCalendar) für alle Fristen."""

from datetime import date, datetime, timedelta, timezone

from app.events import Event


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    parts, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 74:
            parts.append(current)
            current = " " + char
        else:
            current += char
    parts.append(current)
    return parts


def render(events: list[Event], link_base: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//paperless-companion//DE",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Paperless Fristen",
    ]
    for e in events:
        start: date = e.date
        description = e.detail
        if link_base and e.document_id:
            description += f"\n{link_base}/documents/{e.document_id}/details"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.key.replace(':', '-')}-{start.isoformat()}@paperless-companion",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(start + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(f'{e.label}: {e.title}')}",
            f"DESCRIPTION:{_escape(description)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    out = []
    for line in lines:
        out.extend(_fold(line))
    return "\r\n".join(out) + "\r\n"
