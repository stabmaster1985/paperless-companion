"""Einstellungen in der App bearbeiten statt in der .env.

Startwerte sind die Werte aus der .env (bzw. den Umgebungsvariablen). Gespeichert werden nur
Abweichungen davon (KeyValue „app_settings“); sie haben Vorrang und gelten sofort.
Nicht in der App änderbar sind Werte, die schon vor dem Start feststehen müssen
(Datenverzeichnis, Sitzungsschlüssel, App-Passwort).
"""

import json
import typing

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import KeyValue

KEY = "app_settings"

# (Abschnitt, [(Feld, Beschriftung, Hinweis)])
SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Paperless", [
        ("paperless_url", "Adresse (intern)", "z.B. http://paperless:8000"),
        ("paperless_public_url", "Adresse für Links im Browser", "leer = wie oben"),
        ("paperless_token", "API-Token", ""),
        ("sync_interval_minutes", "Abgleich alle … Minuten", ""),
        ("initial_sync_days", "Automatisch analysieren: in den letzten … Tagen geänderte Dokumente", ""),
        ("keyword_classification", "Art auch per Schlüsselwort erkennen", "wenn der Dokumenttyp nicht zugeordnet ist"),
        ("timezone", "Zeitzone", "z.B. Europe/Berlin"),
    ]),
    ("Lokale AI (Ollama)", [
        ("llm_provider", "AI verwenden", ""),
        ("ollama_url", "Ollama-Adresse", "z.B. http://192.168.1.20:11434"),
        ("ollama_model", "Modell", "z.B. qwen2.5:3b"),
        ("llm_active_hours", "Zeitfenster", "z.B. 22-6 (nachts), leer = immer"),
        ("ollama_num_thread", "CPU-Threads", "0 = Ollama entscheidet"),
        ("ollama_num_ctx", "Kontextgröße (Tokens)", ""),
        ("ollama_keep_alive", "Modell im Speicher halten", "z.B. 2m"),
        ("ollama_timeout_seconds", "Zeitlimit je Anfrage (Sekunden)", ""),
        ("llm_max_chars", "Max. Zeichen für die Feld-Erkennung", ""),
        ("llm_auto_accept", "AI-Werte ohne Prüfung übernehmen", "nicht empfohlen"),
    ]),
    ("Zusammenfassung und Metadaten-Vorschläge", [
        ("summary_mode", "KI-Zusammenfassung", ""),
        ("summary_max_input_chars", "Zusammenfassung: Zeichen vom Dokumentanfang", ""),
        ("meta_mode", "Vorschläge für Titel, Tags, Korrespondent, Typ", ""),
        ("meta_max_input_chars", "Vorschläge: Zeichen vom Dokumentanfang", ""),
    ]),
    ("Paperless-Felder", [
        ("field_amount", "Betrag", "Name des Custom Fields, leer = nicht verwenden"),
        ("field_due_date", "Fällig am", ""),
        ("field_paid_on", "Bezahlt am", ""),
        ("field_cancel_by", "Kündigung bis", ""),
        ("field_term_end", "Vertragsende", ""),
        ("field_warranty_end", "Garantie bis", ""),
        ("field_comment", "Kommentar", ""),
        ("field_related", "gehört zu", ""),
        ("field_summary", "Zusammenfassung", ""),
        ("writeback_create_fields", "Fehlende Felder beim Übernehmen anlegen", ""),
        ("fields_open_max_age_days", "Unbezahlte Rechnungen nach … Tagen als erledigt ansehen", ""),
        ("cases_propose_links", "Vorgänge schlagen „gehört zu“ vor", ""),
    ]),
    ("Firefly III", [
        ("firefly_url", "Firefly-Adresse", "leer = kein Zahlungsabgleich"),
        ("firefly_token", "Zugangstoken", ""),
        ("firefly_dashboard_url", "Firefly-Dashboard-Adresse", "für Links zur Kontrolle von Buchungen"),
        ("firefly_lookback_days", "Buchungen der letzten … Tage prüfen", ""),
        ("firefly_auto_accept", "Eindeutige Treffer direkt als bezahlt markieren", ""),
        ("receipts_min_amount", "Belege prüfen ab Betrag (€)", "Buchungen ohne Beleg"),
        ("receipts_lookback_days", "Belege prüfen: letzte … Tage", ""),
    ]),
    ("Regeln", [
        ("text_scan_enabled", "Textmerkmale im Hintergrund lesen", "IBAN, Domain, Kundennr. – für „Korrespondent aus Text“; nur lesend, ohne AI"),
        ("rules_review_tag", "Prüf-Tag in Regel-Vorschlägen", "z.B. „Zu prüfen“ – wird in jede Regel-Anleitung aufgenommen, leer = nicht"),
    ]),
    ("Steuer und Aufbewahrung", [
        ("tax_tags", "Steuer-Tags", "kommagetrennt; Dokumenttypen unter „Dokumenttypen“"),
        ("retention_tag", "Tag für Löschkandidaten", ""),
        ("retention_delete_enabled", "Löschen aus der App erlauben", ""),
    ]),
    ("Erinnerungen", [
        ("reminder_days", "Tage vorher erinnern", "kommagetrennt, z.B. 30,7,1"),
        ("reminder_hour", "Uhrzeit (Stunde)", ""),
        ("ntfy_url", "ntfy-Adresse inkl. Topic", ""),
        ("ntfy_token", "ntfy-Token", ""),
        ("smtp_host", "SMTP-Server", ""),
        ("smtp_port", "SMTP-Port", ""),
        ("smtp_user", "SMTP-Benutzer", ""),
        ("smtp_password", "SMTP-Passwort", ""),
        ("smtp_from", "Absender", ""),
        ("smtp_to", "Empfänger", ""),
        ("smtp_starttls", "STARTTLS", ""),
    ]),
    ("Zugänge und Updates", [
        ("webhook_token", "Token für den Paperless-Webhook", ""),
        ("ics_token", "Token für den Kalender-Link", ""),
        ("update_check", "Auf Updates prüfen", ""),
        ("update_repo", "GitHub-Repository", ""),
        ("update_github_token", "GitHub-Token (privates Repo)", ""),
        ("update_webhook_url", "n8n-Webhook für Updates", ""),
        ("update_webhook_header", "Header-Name", ""),
        ("update_webhook_secret", "Header-Wert", ""),
    ]),
]
FIELDS = {name: (label, hint) for _, items in SECTIONS for name, label, hint in items}
SECRETS = {"paperless_token", "firefly_token", "ntfy_token", "smtp_password", "update_github_token",
           "update_webhook_secret", "webhook_token", "ics_token"}
CHOICES = {
    "llm_provider": [("ollama", "ja (Ollama)"), ("none", "nein, nur Regeln")],
    "summary_mode": [("off", "aus"), ("manual", "nur per Knopf"), ("new", "automatisch für neue Dokumente")],
    "meta_mode": [("off", "aus"), ("manual", "nur auf Anforderung"), ("new", "automatisch für neu analysierte Dokumente")],
}


def kind(name: str) -> str:
    annotation = Settings.model_fields[name].annotation
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if typing.get_origin(annotation) is typing.Literal:
        return "choice"
    return "secret" if name in SECRETS else "text"


def coerce(name: str, raw):
    """Formular-/JSON-Wert in den Typ der Einstellung umwandeln (ValueError bei ungültigen Werten)."""
    annotation = Settings.model_fields[name].annotation
    if annotation is str:
        value = str(raw or "").strip()
        if name == "timezone":
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(value)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"„{FIELDS[name][0]}“: unbekannte Zeitzone {value!r}") from exc
        return value
    try:
        return TypeAdapter(annotation).validate_python(raw.strip() if isinstance(raw, str) else raw)
    except ValidationError as exc:
        raise ValueError(f"„{FIELDS[name][0]}“: ungültiger Wert {raw!r}") from exc


def stored(session: Session) -> dict:
    item = session.get(KeyValue, KEY)
    return json.loads(item.value) if item else {}


def apply(session: Session, settings: Settings) -> list[str]:
    """Gespeicherte Werte übernehmen. Gibt die Namen der geänderten Einstellungen zurück."""
    applied = []
    for name, value in stored(session).items():
        if name in FIELDS:
            try:
                setattr(settings, name, coerce(name, value))
                applied.append(name)
            except ValueError:
                continue  # alter, inzwischen ungültiger Wert -> .env gilt
    return applied


def save(session: Session, settings: Settings, env: dict, form) -> tuple[list[str], list[str]]:
    """Formular übernehmen. Gespeichert wird nur, was von der .env abweicht.

    Gibt (geänderte Namen, Fehlermeldungen) zurück; ungültige Werte bleiben unverändert.
    """
    overrides = stored(session)
    changed = []
    errors = []
    for name in FIELDS:
        k = kind(name)
        if k == "bool":
            if f"present:{name}" not in form:
                continue
            raw = form.get(name) == "1"
        elif name not in form:
            continue
        else:
            raw = form.get(name)
            if k == "secret" and not raw and not form.get(f"clear:{name}"):
                continue  # leer gelassen = unverändert
        try:
            value = coerce(name, raw)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if value == getattr(settings, name):
            continue
        setattr(settings, name, value)
        changed.append(name)
        if value == env.get(name):
            overrides.pop(name, None)
        else:
            overrides[name] = value
    _store(session, overrides)
    return changed, errors


def reset(session: Session, settings: Settings, env: dict, names: list[str] | None = None) -> list[str]:
    """Einzelne (oder alle) Einstellungen wieder aus der .env nehmen."""
    overrides = stored(session)
    names = list(overrides) if names is None else [n for n in names if n in overrides]
    for name in names:
        overrides.pop(name, None)
        if name in env:
            setattr(settings, name, env[name])
    _store(session, overrides)
    return names


def _store(session: Session, overrides: dict) -> None:
    item = session.get(KeyValue, KEY)
    if not overrides:
        if item:
            session.delete(item)
        return
    value = json.dumps(overrides, default=str)
    if item:
        item.value = value
    else:
        session.add(KeyValue(key=KEY, value=value))
