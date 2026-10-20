from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text
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
    # none | pending | confirmed
    review_state: Mapped[str] = mapped_column(String, default="none")
    auto_confirmed: Mapped[bool] = mapped_column(default=False)
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
