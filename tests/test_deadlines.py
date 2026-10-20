from datetime import date

from app.deadlines import Terms, compute, latest_notice_day


def test_latest_notice_day_months():
    assert latest_notice_day(date(2026, 12, 31), 3, "months") == date(2026, 9, 30)
    assert latest_notice_day(date(2026, 2, 28), 1, "months") == date(2026, 1, 31)


def test_latest_notice_day_weeks_and_days():
    assert latest_notice_day(date(2026, 12, 31), 2, "weeks") == date(2026, 12, 17)
    assert latest_notice_day(date(2026, 12, 31), 30, "days") == date(2026, 12, 1)


def test_renewing_contract_rolls_forward():
    terms = Terms(start_date=date(2025, 2, 1), initial_term_months=24, renewal_mode="renews",
                  renewal_months=12, notice_value=3, notice_unit="months")
    # Erstlaufzeit endet 31.01.2027, Kündigung bis 31.10.2026
    assert compute(terms, date(2026, 10, 20)).cancel_by == date(2026, 10, 31)
    # Frist verpasst -> nächste Periode
    result = compute(terms, date(2026, 11, 1))
    assert result.term_end == date(2028, 1, 31)
    assert result.cancel_by == date(2027, 10, 31)


def test_indefinite_after_initial_term():
    terms = Terms(start_date=date(2024, 1, 1), initial_term_months=12, renewal_mode="indefinite",
                  notice_value=1, notice_unit="months", notice_to="month_end")
    result = compute(terms, date(2026, 10, 20))
    assert result.anytime
    assert result.term_end == date(2026, 11, 30)


def test_anytime_without_term():
    terms = Terms(notice_value=4, notice_unit="weeks", notice_to="any")
    result = compute(terms, date(2026, 10, 20))
    assert result.anytime and result.term_end == date(2026, 11, 17)


def test_fixed_term_ends():
    terms = Terms(end_date=date(2026, 12, 31), renewal_mode="none")
    result = compute(terms, date(2026, 10, 20))
    assert result.term_end == date(2026, 12, 31) and result.cancel_by is None and not result.ended


def test_missing_info():
    assert compute(Terms(), date(2026, 1, 1)).missing_info
    terms = Terms(start_date=date(2025, 1, 1), initial_term_months=12, renewal_mode="renews", notice_value=3)
    assert compute(terms, date(2026, 1, 1)).missing_info


def test_quarter_end():
    terms = Terms(notice_value=6, notice_unit="weeks", notice_to="quarter_end", renewal_mode="indefinite")
    assert compute(terms, date(2026, 10, 20)).term_end == date(2026, 12, 31)
    assert compute(terms, date(2026, 11, 20)).term_end == date(2027, 3, 31)
