"""Benachrichtigungen per ntfy und/oder E-Mail."""

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


def channels(settings: Settings) -> list[str]:
    result = []
    if settings.ntfy_url:
        result.append("ntfy")
    if settings.smtp_host and settings.smtp_to:
        result.append("email")
    return result


def send_ntfy(settings: Settings, title: str, body: str) -> None:
    headers = {"Title": title, "Tags": "date"}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    response = httpx.post(settings.ntfy_url, content=body.encode("utf-8"), headers=headers, timeout=15)
    response.raise_for_status()


def send_email(settings: Settings, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to
    msg.set_content(body)
    if settings.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        if settings.smtp_starttls:
            server.starttls()
    with server:
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def send(settings: Settings, title: str, body: str) -> list[str]:
    """Sendet über alle konfigurierten Kanäle. Gibt die erfolgreichen Kanäle zurück."""
    senders = {"ntfy": send_ntfy, "email": send_email}
    ok, errors = [], []
    for name in channels(settings):
        try:
            senders[name](settings, title, body)
            ok.append(name)
        except Exception as exc:  # noqa: BLE001 - jeder Kanal soll unabhängig scheitern dürfen
            log.warning("Benachrichtigung über %s fehlgeschlagen: %s", name, exc)
            errors.append(f"{name}: {exc}")
    if not ok:
        raise NotifyError("; ".join(errors) or "Kein Benachrichtigungskanal konfiguriert")
    return ok
