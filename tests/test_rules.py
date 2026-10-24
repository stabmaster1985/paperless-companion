from datetime import date

from app.extraction.rules import (
    classify_by_keywords, extract_contract, extract_invoice, extract_warranty, find_dates, valid_iban,
)

INVOICE = """Stadtwerke Musterstadt GmbH
Rechnungsnummer: RE-2026-004711
Rechnungsdatum: 05.10.2026
Strom Abschlag   1   78,00 €
Gesamtbetrag (brutto): 1.234,56 EUR
Bitte überweisen Sie den Betrag bis zum 19.10.2026 auf
IBAN: DE89 3704 0044 0532 0130 00
"""

CONTRACT = """Vertragsbestätigung
Ihre Vertragsnummer: 99887766
Vertragsbeginn: 01.02.2025
Die Mindestvertragslaufzeit beträgt 24 Monate. Der Vertrag verlängert sich danach automatisch
um jeweils 12 Monate, wenn er nicht mit einer Frist von drei Monaten zum Ende der Laufzeit gekündigt wird.
Monatlicher Grundpreis: 39,99 €
"""


def values(result):
    return {k: v["value"] for k, v in result.items()}


def test_invoice_fields():
    v = values(extract_invoice(INVOICE))
    assert v["amount"] == 1234.56
    assert v["invoice_number"] == "RE-2026-004711"
    assert v["invoice_date"] == "2026-10-05"
    assert v["due_date"] == "2026-10-19"
    assert v["iban"] == "DE89370400440532013000"
    assert "payment_method" not in v


def test_invoice_payment_terms_and_debit():
    text = "Rechnung Nr. 4711\nSumme 49,99 €\nZahlbar innerhalb von 14 Tagen. Der Betrag wird per SEPA-Lastschrift eingezogen."
    v = values(extract_invoice(text, created=date(2026, 3, 1)))
    assert v["due_date"] == "2026-03-15"
    assert v["payment_method"] == "debit"
    assert v["invoice_date"] == "2026-03-01"


def test_invoice_paid_and_warranty():
    text = "Rechnung\nGesamtbetrag 499,00 €\nBezahlt mit PayPal\n2 Jahre Herstellergarantie"
    v = values(extract_invoice(text))
    assert v["status"] == "paid"
    assert v["warranty_months"] == 24


def test_contract_fields():
    v = values(extract_contract(CONTRACT))
    assert v["start_date"] == "2025-02-01"
    assert v["initial_term_months"] == 24
    assert v["renewal_mode"] == "renews" and v["renewal_months"] == 12
    assert (v["notice_value"], v["notice_unit"], v["notice_to"]) == (3, "months", "term_end")
    assert v["contract_number"] == "99887766"
    assert v["monthly_cost"] == 39.99


def test_contract_indefinite_month_end_yearly_cost():
    v = values(extract_contract("Kündigungsfrist: 1 Monat zum Monatsende. Der Vertrag läuft auf unbestimmte Zeit.\nJahresbeitrag 120,00 EUR"))
    assert (v["notice_value"], v["notice_to"], v["renewal_mode"]) == (1, "month_end", "indefinite")
    assert v["monthly_cost"] == 10.0


def test_contract_years_and_weeks():
    v = values(extract_contract("Laufzeit: 2 Jahre. Kündigungsfrist von 6 Wochen zum Quartalsende."))
    assert v["initial_term_months"] == 24
    assert (v["notice_value"], v["notice_unit"], v["notice_to"]) == (6, "weeks", "quarter_end")


def test_warranty_default_and_explicit():
    v = values(extract_warranty("Kassenbon\nDatum 12.03.2026\nSumme 19,99"))
    assert v["purchase_date"] == "2026-03-12" and v["months"] == 24 and v["warranty_type"] == "gewaehrleistung"
    v = values(extract_warranty("Kaufdatum 12.03.2026\n3 Jahre Garantie"))
    assert v["months"] == 36 and v["warranty_type"] == "garantie"


def test_dates_and_iban_helpers():
    assert [d for _, d in find_dates("am 3. März 2026 und 2026-04-01 sowie 31.02.2026")] == [
        date(2026, 3, 3), date(2026, 4, 1)
    ]
    assert valid_iban("DE89370400440532013000")
    assert not valid_iban("DE89370400440532013001")


def test_classification():
    assert classify_by_keywords("Stromrechnung Oktober", "") == "invoice"
    assert classify_by_keywords("Scan", CONTRACT) == "contract"
    assert classify_by_keywords("Scan", INVOICE) == "invoice"
    assert classify_by_keywords("Arztbrief", "Diagnose ...") == "other"
    # Abrechnungen, die keine Rechnungen sind
    assert classify_by_keywords("Gehaltsabrechnung März 2026", "") == "other"
    assert classify_by_keywords("Lohn- und Gehaltsabrechnung 07/2026", "") == "other"
    assert classify_by_keywords("Scan_0001", "Muster GmbH\nAbrechnung der Brutto/Netto-Bezüge für März 2026") == "other"
    assert classify_by_keywords("Kreditkartenabrechnung", "Rechnungsdatum 01.03.2026") == "other"
    assert classify_by_keywords("Nebenkostenabrechnung 2025", "") == "invoice"  # bleibt (Nachzahlung)
