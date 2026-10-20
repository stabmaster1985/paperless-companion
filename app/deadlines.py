"""Fristenberechnung für Verträge.

Konvention: ``term_end`` ist der letzte Tag, an dem der Vertrag noch läuft.
``cancel_by`` ist der letzte Tag, an dem die Kündigung beim Anbieter
eingegangen sein muss, damit der Vertrag zu ``term_end`` endet.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

ONE_DAY = timedelta(days=1)


@dataclass
class Terms:
    start_date: date | None = None
    initial_term_months: int | None = None
    renewal_mode: str = "renews"  # renews | indefinite | none
    renewal_months: int | None = None
    notice_value: int | None = None
    notice_unit: str = "months"  # days | weeks | months
    notice_to: str = "term_end"  # term_end | month_end | quarter_end | any
    end_date: date | None = None


@dataclass
class Deadline:
    term_end: date | None
    cancel_by: date | None
    anytime: bool = False  # jederzeit mit Frist kündbar
    ended: bool = False
    missing_info: bool = False


def notice_delta(value: int | None, unit: str) -> relativedelta:
    value = value or 0
    if unit == "days":
        return relativedelta(days=value)
    if unit == "weeks":
        return relativedelta(weeks=value)
    return relativedelta(months=value)


def latest_notice_day(term_end: date, value: int | None, unit: str) -> date:
    """Letzter Eingangstag einer Kündigung, damit der Vertrag zu term_end endet."""
    return term_end + ONE_DAY - notice_delta(value, unit) - ONE_DAY


def _roll_to_period_end(d: date, notice_to: str) -> date:
    if notice_to == "month_end":
        return d + relativedelta(day=31)
    if notice_to == "quarter_end":
        quarter_last_month = ((d.month - 1) // 3 + 1) * 3
        return date(d.year, quarter_last_month, 1) + relativedelta(day=31)
    return d


def earliest_end_anytime(today: date, terms: Terms) -> date:
    """Frühestes Vertragsende bei Kündigung heute (unbefristeter Vertrag)."""
    end = today + notice_delta(terms.notice_value, terms.notice_unit)
    return _roll_to_period_end(end, terms.notice_to)


def first_term_end(terms: Terms) -> date | None:
    if terms.end_date:
        return terms.end_date
    if terms.start_date and terms.initial_term_months:
        return terms.start_date + relativedelta(months=terms.initial_term_months) - ONE_DAY
    return None


def compute(terms: Terms, today: date) -> Deadline:
    end = first_term_end(terms)

    if end is None:
        if terms.notice_value and terms.renewal_mode != "none":
            # Keine Laufzeit bekannt, aber Frist -> jederzeit kündbar
            return Deadline(term_end=earliest_end_anytime(today, terms), cancel_by=today, anytime=True)
        return Deadline(term_end=None, cancel_by=None, missing_info=True)

    if terms.renewal_mode == "none":
        # Befristet, endet automatisch
        return Deadline(term_end=end, cancel_by=None, ended=end < today)

    # Sicherheitsgrenze gegen Endlosschleifen bei unsinnigen Daten
    for _ in range(1200):
        cancel_by = latest_notice_day(end, terms.notice_value, terms.notice_unit)
        if cancel_by >= today:
            return Deadline(term_end=end, cancel_by=cancel_by)
        if terms.renewal_mode == "renews":
            if not terms.renewal_months:
                return Deadline(term_end=end, cancel_by=None, missing_info=True)
            end = end + ONE_DAY + relativedelta(months=terms.renewal_months) - ONE_DAY
            continue
        # indefinite: nach Ablauf der Erstlaufzeit jederzeit mit Frist kündbar
        return Deadline(term_end=earliest_end_anytime(today, terms), cancel_by=today, anytime=True)

    return Deadline(term_end=None, cancel_by=None, missing_info=True)


def terms_from_contract(contract) -> Terms:
    return Terms(
        start_date=contract.start_date,
        initial_term_months=contract.initial_term_months,
        renewal_mode=contract.renewal_mode or "renews",
        renewal_months=contract.renewal_months,
        notice_value=contract.notice_value,
        notice_unit=contract.notice_unit or "months",
        notice_to=contract.notice_to or "term_end",
        end_date=contract.end_date,
    )
