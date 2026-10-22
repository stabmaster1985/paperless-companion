from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    """Spiegel eines Paperless-Dokuments plus Analysezustand."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # = Paperless-ID
    title: Mapped[str] = mapped_column(String, default="")
    correspondent: Mapped[str | None] = mapped_column(String)
    document_type: Mapped[str | None] = mapped_column(String)
    created: Mapped[date | None] = mapped_column(Date)
    modified: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String)

    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Werte der zugeordneten Paperless-Custom-Fields je Rolle (siehe app/fields.py)
    paperless_fields: Mapped[dict | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    # None | queued | running | done | error
    summary_state: Mapped[str | None] = mapped_column(String)
    # Companion-Daten haben sich geändert -> evtl. Vorschläge für Paperless (siehe „Für Paperless“)
    writeback_pending: Mapped[bool] = mapped_column(default=False, server_default="0")
    # Eigene Eingaben für Paperless-Felder (Kommentar, Zusammenfassung, Verknüpfungen), noch nicht übernommen
    proposed_fields: Mapped[dict | None] = mapped_column(JSON)
    # Abgelehnte Vorschläge (Rolle -> Wert), damit sie nicht erneut erscheinen
    dismissed_fields: Mapped[dict | None] = mapped_column(JSON)
    # Metadaten-Vorschlag (Titel, Tags, Korrespondent, Dokumenttyp), siehe app/metadata.py
    meta_suggestion: Mapped[dict | None] = mapped_column(JSON)
    # None | queued | running | done | error
    meta_state: Mapped[str | None] = mapped_column(String)
    # Verworfene Metadaten-Vorschläge ("tag:Strom" / "title" -> Wert)
    meta_dismissed: Mapped[dict | None] = mapped_column(JSON)
    # Vorgemerkte Vorschläge, noch nicht in Paperless ("tag:Strom" -> {"value", "source", "new_tag"})
    meta_accepted: Mapped[dict | None] = mapped_column(JSON)
    meta_pending: Mapped[bool] = mapped_column(default=False, server_default="0")
    page_count: Mapped[int | None] = mapped_column(Integer)
    original_file_name: Mapped[str | None] = mapped_column(String)

    # invoice | contract | warranty | other
    kind: Mapped[str] = mapped_column(String, default="other")
    # doctype | keyword | manual
    kind_source: Mapped[str] = mapped_column(String, default="keyword")

    # queued | running | done | error | skipped (nur Index, nicht analysiert)
    extraction_state: Mapped[str] = mapped_column(String, default="queued")
    extraction_error: Mapped[str | None] = mapped_column(Text)
    suggestions: Mapped[dict] = mapped_column(JSON, default=dict)
    # Herkunft der gespeicherten Werte je Feld: {"amount": {"source": "rule|llm|paperless|firefly|default|manual",
    #  "confidence": 0.9, "confirmed": true, "at": "..."}}
    field_sources: Mapped[dict | None] = mapped_column(JSON)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Stand der Erkennung bei der letzten Analyse (siehe pipeline.ANALYSIS_VERSION)
    analysis_version: Mapped[int | None] = mapped_column(Integer)
    # none | pending | confirmed
    review_state: Mapped[str] = mapped_column(String, default="none")
    auto_confirmed: Mapped[bool] = mapped_column(default=False)
    # Steuerjahr-Überschreibung: None = Jahr des Dokumentdatums, 0 = nicht im Steuer-Export
    tax_year: Mapped[int | None] = mapped_column(Integer)
    # Aufbewahrung: niemals zur Löschung vorschlagen
    retention_hold: Mapped[bool] = mapped_column(default=False, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    contract: Mapped["Contract | None"] = relationship(back_populates="document", cascade="all, delete-orphan")
    invoice: Mapped["Invoice | None"] = relationship(back_populates="document", cascade="all, delete-orphan")
    warranty: Mapped["Warranty | None"] = relationship(back_populates="document", cascade="all, delete-orphan")


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String, default="")
    provider: Mapped[str | None] = mapped_column(String)
    contract_number: Mapped[str | None] = mapped_column(String)
    start_date: Mapped[date | None] = mapped_column(Date)
    initial_term_months: Mapped[int | None] = mapped_column(Integer)
    # renews | indefinite | none
    renewal_mode: Mapped[str] = mapped_column(String, default="renews")
    renewal_months: Mapped[int | None] = mapped_column(Integer)
    notice_value: Mapped[int | None] = mapped_column(Integer)
    # days | weeks | months
    notice_unit: Mapped[str] = mapped_column(String, default="months")
    # term_end | month_end | quarter_end | any
    notice_to: Mapped[str] = mapped_column(String, default="term_end")
    end_date: Mapped[date | None] = mapped_column(Date)
    monthly_cost: Mapped[float | None] = mapped_column(Float)
    # active | cancelled | ended
    status: Mapped[str] = mapped_column(String, default="active")
    cancelled_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="contract")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    issuer: Mapped[str | None] = mapped_column(String)
    invoice_number: Mapped[str | None] = mapped_column(String)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[float | None] = mapped_column(Float)
    due_date: Mapped[date | None] = mapped_column(Date)
    iban: Mapped[str | None] = mapped_column(String)
    # transfer | debit | other
    payment_method: Mapped[str] = mapped_column(String, default="transfer")
    # open | paid
    status: Mapped[str] = mapped_column(String, default="open")
    paid_on: Mapped[date | None] = mapped_column(Date)

    document: Mapped[Document] = relationship(back_populates="invoice")


class Warranty(Base):
    __tablename__ = "warranties"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    product: Mapped[str] = mapped_column(String, default="")
    purchase_date: Mapped[date | None] = mapped_column(Date)
    months: Mapped[int] = mapped_column(Integer, default=24)
    # gewaehrleistung | garantie
    warranty_type: Mapped[str] = mapped_column(String, default="gewaehrleistung")

    document: Mapped[Document] = relationship(back_populates="warranty")

    @property
    def expires_on(self) -> date | None:
        if self.purchase_date is None:
            return None
        from dateutil.relativedelta import relativedelta

        return self.purchase_date + relativedelta(months=self.months)


class SentReminder(Base):
    __tablename__ = "sent_reminders"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MetaDecision(Base):
    """Deine Entscheidung zu einem Metadaten-Vorschlag – Grundlage für Lernen und Trefferquote."""

    __tablename__ = "meta_decisions"
    __table_args__ = (UniqueConstraint("document_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String)  # title | correspondent | document_type | tag:<Name>
    item: Mapped[str] = mapped_column(String)  # title | correspondent | document_type | tag
    correspondent: Mapped[str | None] = mapped_column(String)  # Absender zum Zeitpunkt der Entscheidung
    suggested: Mapped[str | None] = mapped_column(String)
    value: Mapped[str | None] = mapped_column(String)  # vorgemerkter (ggf. geänderter) Wert
    source: Mapped[str] = mapped_column(String, default="llm")  # llm | history
    decision: Mapped[str] = mapped_column(String)  # accepted | edited | dismissed
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReceiptCheck(Base):
    """Firefly-Abbuchung: Gibt es dazu einen Beleg in Paperless? (nur im Companion)"""

    __tablename__ = "receipt_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    tx_id: Mapped[str] = mapped_column(String, unique=True)
    tx_group_id: Mapped[str] = mapped_column(String)
    tx_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    counterparty: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    # missing | candidate | linked | ignored
    state: Mapped[str] = mapped_column(String, default="missing")
    # von dir entschieden (zugeordnet/ignoriert) -> wird bei neuen Läufen nicht überschrieben
    decided: Mapped[bool] = mapped_column(default=False, server_default="0")
    document_id: Mapped[int | None] = mapped_column(Integer)
    candidates: Mapped[list | None] = mapped_column(JSON)  # [{"id": 12, "title": ..., "reason": ...}]
    note: Mapped[str | None] = mapped_column(String)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RuleProposal(Base):
    """Vorschlag für eine Paperless-Regel (Workflow) – wird nur angezeigt, nie automatisch angelegt."""

    __tablename__ = "rule_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Eindeutiger Schlüssel: Korrespondent (Auslöser „correspondent“) bzw. „typ:…“ / „wort:…“
    correspondent: Mapped[str] = mapped_column(String, unique=True)
    # correspondent | document_type | keyword
    trigger: Mapped[str] = mapped_column(String, default="correspondent", server_default="correspondent")
    condition: Mapped[str | None] = mapped_column(String)  # Typ bzw. Stichwort (bei Korrespondent = correspondent)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    document_type: Mapped[str | None] = mapped_column(String)
    # Grundlage: {"docs": 12, "tags": {"Strom": 12}, "document_type": {"Rechnung": 11}}
    basis: Mapped[dict | None] = mapped_column(JSON)
    # proposed | approved | dismissed | created (von dir in Paperless angelegt)
    state: Mapped[str] = mapped_column(String, default="proposed")
    edited: Mapped[bool] = mapped_column(default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KeyValue(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class RetentionRule(Base):
    """Aufbewahrungsregel. Treffen mehrere Regeln zu, gilt die längste Frist."""

    __tablename__ = "retention_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # doctype | tag | kind
    match_type: Mapped[str] = mapped_column(String, default="doctype")
    match_value: Mapped[str] = mapped_column(String)
    years: Mapped[int] = mapped_column(Integer, default=10)  # 0 = dauerhaft aufbewahren
    # year_end (ab Ende des Kalenderjahres) | created (ab Dokumentdatum) | contract_end (ab Vertragsende)
    basis: Mapped[str] = mapped_column(String, default="year_end")


class RetentionLog(Base):
    __tablename__ = "retention_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String, default="")
    correspondent: Mapped[str | None] = mapped_column(String)
    created: Mapped[date | None] = mapped_column(Date)
    # tagged | deleted | hold
    action: Mapped[str] = mapped_column(String)
    rule: Mapped[str | None] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentMatch(Base):
    """Zuordnung einer Firefly-Buchung zu einer Rechnung."""

    __tablename__ = "payment_matches"
    __table_args__ = (UniqueConstraint("invoice_id", "tx_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    tx_id: Mapped[str] = mapped_column(String)
    tx_group_id: Mapped[str] = mapped_column(String)
    tx_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    counterparty: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    score: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[str] = mapped_column(String, default="")
    # suggested | accepted | rejected
    state: Mapped[str] = mapped_column(String, default="suggested")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    invoice: Mapped[Invoice] = relationship()


class Case(Base):
    """Vorgang: mehrere Dokumente und Notizen zu einem Fall (z.B. Schadensfall, Widerspruch)."""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String)
    # open | closed
    status: Mapped[str] = mapped_column(String, default="open")
    follow_up: Mapped[date | None] = mapped_column(Date)
    follow_up_note: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    links: Mapped[list["CaseDocument"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    entries: Mapped[list["CaseEntry"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class CaseDocument(Base):
    __tablename__ = "case_documents"

    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case] = relationship(back_populates="links")
    document: Mapped[Document] = relationship()


class CaseEntry(Base):
    __tablename__ = "case_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case] = relationship(back_populates="entries")
