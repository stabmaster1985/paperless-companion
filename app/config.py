from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Paperless
    paperless_url: str = "http://paperless:8000"
    paperless_public_url: str = ""  # URL für Links im Browser, falls abweichend
    paperless_token: str = ""

    # App
    data_dir: Path = Path("/data")
    app_password: str = ""
    secret_key: str = "bitte-aendern"
    webhook_token: str = ""
    ics_token: str = ""
    timezone: str = "Europe/Berlin"

    # Synchronisation
    sync_interval_minutes: int = 15
    initial_sync_days: int = 90

    # Zuordnung Paperless-Dokumenttyp -> Art (kommagetrennt, Groß/Klein egal)
    doctypes_invoice: str = "Rechnung,Quittung"
    doctypes_contract: str = "Vertrag,Versicherung,Police"
    doctypes_warranty: str = "Kaufbeleg,Kassenbon,Garantie"
    keyword_classification: bool = True

    # LLM
    llm_provider: Literal["none", "ollama"] = "ollama"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_keep_alive: str = "2m"
    ollama_num_ctx: int = 4096
    ollama_timeout_seconds: int = 900
    llm_max_chars: int = 6000
    llm_auto_accept: bool = False

    # Übernahme nach Paperless (nur nach Zustimmung auf der Seite „Für Paperless“):
    # fehlende Felder (Betrag, Fällig am …) dabei anlegen
    writeback_create_fields: bool = True

    # Namen der Paperless-Custom-Fields je Rolle (leer = nicht verwenden)
    field_amount: str = "Betrag"
    field_due_date: str = "Fällig am"
    field_paid_on: str = "Bezahlt am"
    field_cancel_by: str = "Kündigung bis"
    field_term_end: str = "Vertragsende"
    field_warranty_end: str = "Garantie bis"
    field_comment: str = "Kommentar"
    field_related: str = "gehört zu"
    field_summary: str = "Zusammenfassung KI"
    # Rechnungen aus „Fällig am“ ohne „Bezahlt am“, die länger als N Tage fällig sind, gelten als erledigt
    fields_open_max_age_days: int = 90
    # Vorgänge schlagen die Verknüpfung im Feld „gehört zu“ vor
    cases_propose_links: bool = True

    # KI-Zusammenfassung: manual (nur per Knopf) | new (neue Dokumente automatisch) | off
    summary_mode: Literal["off", "manual", "new"] = "manual"
    summary_max_input_chars: int = 3500

    # Steuer-Export: Tags (auch „Steuer: Kategorie“) und Dokumenttypen, die steuerrelevant sind
    tax_tags: str = "Steuer"
    tax_doctypes: str = ""

    # Zahlungsabgleich mit Firefly III
    firefly_url: str = ""
    firefly_token: str = ""
    firefly_lookback_days: int = 120
    firefly_auto_accept: bool = False  # eindeutige Treffer (Betrag + Rechnungsnr./IBAN) direkt als bezahlt markieren

    # Aufbewahrung
    retention_tag: str = "Löschkandidat"
    retention_delete_enabled: bool = False

    # Erinnerungen
    reminder_days: str = "30,7,1"
    reminder_hour: int = 8
    ntfy_url: str = ""
    ntfy_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_starttls: bool = True

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'companion.db'}"

    @property
    def paperless_link_base(self) -> str:
        return (self.paperless_public_url or self.paperless_url).rstrip("/")

    @property
    def reminder_day_list(self) -> list[int]:
        return sorted({int(d) for d in _split(self.reminder_days)}, reverse=True)

    @property
    def firefly_enabled(self) -> bool:
        return bool(self.firefly_url and self.firefly_token)

    @property
    def tax_tag_list(self) -> list[str]:
        return [t.lower() for t in _split(self.tax_tags)]

    @property
    def tax_doctype_list(self) -> list[str]:
        return [t.lower() for t in _split(self.tax_doctypes)]

    def doctype_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for kind, raw in (
            ("invoice", self.doctypes_invoice),
            ("contract", self.doctypes_contract),
            ("warranty", self.doctypes_warranty),
        ):
            for name in _split(raw):
                mapping[name.lower()] = kind
        return mapping

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "ollama" and bool(self.ollama_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
