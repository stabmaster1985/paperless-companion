"""Screenshots für die README neu erzeugen (docs/screenshots/*.png).

Startet den Companion mit erfundenen Beispieldaten (Paperless, Ollama und Firefly werden wie in den
Tests nachgestellt, es wird nichts nach außen verbunden) und fotografiert die wichtigsten Seiten.

    .venv/bin/pip install playwright        # einmalig
    .venv/bin/playwright install chromium   # einmalig, falls kein Chromium vorhanden
    .venv/bin/python scripts/screenshots.py

Nach Änderungen an der Oberfläche ausführen und die geänderten Bilder mit committen.
Ein vorhandenes Chromium kann über CHROMIUM_PATH vorgegeben werden.
"""

import glob
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "screenshots"

# Beispielrechnung für Vorschau und Vorschaubild – alle Angaben frei erfunden
FAKE_DOC = """<!doctype html><meta charset=utf-8><style>
body{margin:0;background:#525659;font-family:Helvetica,Arial,sans-serif}
.page{background:#fff;width:595px;min-height:842px;margin:16px auto;padding:48px 52px;box-sizing:border-box;font-size:12px;color:#222;box-shadow:0 2px 8px #0005}
h1{font-size:20px;margin:28px 0 4px}.logo{font-weight:bold;font-size:18px;color:#0b6b3a}.r{text-align:right}
table{width:100%;border-collapse:collapse;margin-top:18px}td,th{border-bottom:1px solid #ddd;padding:6px 4px;text-align:left}
.sum td{font-weight:bold;border-top:2px solid #333}.muted{color:#777;font-size:10px}
@media print{body{background:#fff}.page{margin:0;box-shadow:none}}
</style><div class=page>
<div class=logo>Stadtwerke Musterstadt</div><div class=muted>Stadtwerke Musterstadt GmbH · Hauptstraße 1 · 12345 Musterstadt · www.stadtwerke-musterstadt.de</div>
<p style="margin-top:36px">Max Mustermann<br>Beispielweg 7<br>12345 Musterstadt</p>
<p class=r>Kundennummer: 4711-0815<br>Rechnungsnr.: RE-1<br>Datum: 05.10.2026</p>
<h1>Rechnung Abschlag Strom Oktober 2026</h1>
<table><tr><th>Position</th><th class=r>Betrag</th></tr><tr><td>Abschlag Strom 10/2026</td><td class=r>65,55 €</td></tr>
<tr><td>Umsatzsteuer 19 %</td><td class=r>12,45 €</td></tr><tr class=sum><td>Gesamtbetrag</td><td class=r>78,00 €</td></tr></table>
<p style="margin-top:24px">Bitte überweisen Sie den Betrag bis zum <b>19.10.2026</b> auf das Konto<br>IBAN DE89 3704 0044 0532 0130 00 unter Angabe der Rechnungsnummer.</p>
<p class=muted style="margin-top:60px">Beispieldokument für Screenshots – alle Angaben frei erfunden.</p></div>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_demo(data: Path, port: int) -> None:
    """Companion mit Beispieldaten im Hintergrund starten."""
    import uvicorn

    from app import identifiers, metadata, pipeline
    from app.config import Settings
    from app.db import session_scope
    from app.main import create_app
    from app.models import Case, CaseDocument, CaseEntry, Document, Invoice, PaymentMatch, Reimbursement
    from tests import conftest
    from tests.test_pipeline import run_all

    settings = Settings(_env_file=None, paperless_url=conftest.PAPERLESS, paperless_public_url="http://paperless.local:8000",
                        paperless_token="x", data_dir=data, ollama_url=conftest.OLLAMA, firefly_url="http://firefly.test",
                        firefly_token="t", field_due_date="bezahlbar bis", field_paid_on="bezahlt am")
    mock = conftest.mock_api.__wrapped__()  # Paperless/Ollama/Firefly wie in den Tests nachstellen
    router = next(mock)
    start_demo.mock = mock  # Referenz halten, sonst endet der Nachbau beim Aufräumen des Generators
    router.get("http://firefly.test/api/v1/about").respond(json={"data": {"version": "6.2.0"}})
    def booking(journal, day, amount, name, text):
        return {"type": "transactions", "id": str(500 + journal), "attributes": {"transactions": [{
            "transaction_journal_id": str(900 + journal), "type": "withdrawal", "date": f"{day}T00:00:00+02:00",
            "amount": str(amount), "description": text, "destination_name": name}]}}

    # Geldeingänge (Erstattungen) – vor der allgemeinen Route, damit „type=deposit“ hier landet
    router.get("http://firefly.test/api/v1/transactions", params={"type": "deposit"}).respond(json={"data": [
        {"type": "transactions", "id": "600", "attributes": {"transactions": [{
            "transaction_journal_id": "960", "type": "deposit", "date": "2026-09-22T00:00:00+02:00", "amount": "420.00",
            "description": "Erstattung Leistungsabrechnung", "source_name": "Musterversicherung a.G."}]}}],
        "meta": {"pagination": {"total": 1, "current_page": 1, "total_pages": 1}}})
    router.get("http://firefly.test/api/v1/transactions").respond(json={"data": [
        booking(0, "2026-10-10", "78.00", "Stadtwerke Musterstadt", "Abschlag RE-1"),
        booking(1, "2026-09-10", "78.00", "Stadtwerke Musterstadt", "Abschlag September"),
        booking(2, "2026-10-14", "79.50", "Stadtwerke Musterstadt", "Nachzahlung Abschlag"),
        booking(3, "2026-10-03", "78.00", "Fitnessstudio Muster", "Mitgliedsbeitrag Oktober")],
        "meta": {"pagination": {"total": 4, "current_page": 1, "total_pages": 1}}})
    app = create_app(settings, start_worker=False)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    run_all(session_scope, settings)

    with session_scope() as s:
        for i, (y, t, tags, dt, amt) in enumerate([
            (2018, "Handwerkerrechnung Dach", [], "Rechnung", None), (2019, "Kassenbon Baumarkt", [], "Kassenbon", None),
            (2025, "Spendenquittung Tierheim", ["Steuer: Spenden"], "Quittung", 50.0),
            (2025, "Fahrtkosten Fortbildung", ["Steuer: Werbungskosten"], "Rechnung", 129.9),
            (2025, "Fachbuch Python", ["Steuer: Werbungskosten"], "Rechnung", 39.99),
            (2025, "Lohnsteuerbescheinigung", ["Steuer"], "Lohnsteuerbescheinigung", None)]):
            d = Document(id=100 + i, title=t, created=date(y, 3, 10 + i), tags=tags, document_type=dt,
                         correspondent="Muster GmbH", suggestions={}, extraction_state="skipped")
            if amt:
                d.invoice = Invoice(amount=amt, status="paid")
            s.add(d)
        doc = s.get(Document, 2)
        pipeline.apply_values(s, doc, "contract", pipeline.parse_values("contract", pipeline.suggestion_values(doc)))
        doc.review_state = "confirmed"
        case = Case(title="Wasserschaden Keller", follow_up=date(2026, 10, 28), follow_up_note="Rückmeldung Gutachter?")
        s.add(case)
        s.flush()
        s.add_all([CaseDocument(case_id=case.id, document_id=3), CaseDocument(case_id=case.id, document_id=1),
                   CaseEntry(case_id=case.id, date=date(2026, 9, 15), text="Telefonat mit Versicherung: Gutachter kommt nächste Woche.")])
        s.add(PaymentMatch(invoice_id=s.get(Document, 1).invoice.id, tx_id="900", tx_group_id="500", tx_date=date(2026, 10, 10),
                           amount=78.0, counterparty="Stadtwerke Musterstadt", description="Abschlag RE-1", score=115,
                           reasons="Betrag, Rechnungsnr., Empfänger"))
        s.get(Document, 1).paperless_fields = {
            "due_date": "2026-10-19", "comment": "Abschlag wurde angepasst", "related": [3, 100],
            "summary": "Stadtwerke Musterstadt: Stromabschlag Oktober 78,00 €, fällig 19.10.2026, Überweisung nötig."}
        for j in range(3):
            s.add(Document(id=200 + j, title=f"Rechnung Online-Shop {j}", created=date(2026, 9, 1 + j),
                           correspondent="Elektronikversand Großhandel GmbH", tags=["Steuer: Werbungskosten", "Elektronik", "Garantie"],
                           document_type="Rechnung", suggestions={"_llm_error": {"value": "x", "deferred": True}},
                           extraction_state="done", review_state="pending", kind="invoice", kind_source="doctype"))

        # Metadaten-Vorschläge (Für Paperless, Vorschläge durchgehen)
        for i, title, corr, suggested in [
                (701, "Scan_0001", None, "Lohnabrechnung März 2026"), (702, "Scan_0002", None, "Lohnabrechnung April 2026"),
                (703, "Lohnabrechnung Mai 2026", None, None),
                (704, "Scan_0004", "Stadtwerke Musterstadt", "Stromrechnung Oktober 2026"),
                (705, "Scan_0005", "Stadtwerke Musterstadt", "Stromrechnung November 2026")]:
            d = Document(id=i, title=title, correspondent=corr, tags=[], original_file_name=title + ".pdf",
                         created=date(2026, 3 + i - 700, 28))
            payroll = i < 704
            d.meta_suggestion = {
                "tags": [{"name": "Gehalt" if payroll else "Haus", "source": "llm"},
                         *([] if payroll else [{"name": "Strom", "source": "history"}])],
                "document_type": {"value": "Lohnabrechnung" if payroll else "Rechnung", "source": "llm"},
                "at": "2026-10-20T08:00:00+00:00", **({"title": {"value": suggested, "source": "llm"}} if suggested else {})}
            metadata.refresh_pending(d)
            s.add(d)
        s.flush()
        metadata.accept(s, s.get(Document, 701), ["title", "tag:Gehalt"])

        # Archiv für Regelvorschläge
        text = ("Stadtwerke Musterstadt GmbH · www.stadtwerke-musterstadt.de · IBAN DE89 3704 0044 0532 0130 00 · "
                "Kundennummer: 4711-0815")
        months = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
        for i in range(14):
            d = Document(id=300 + i, title=f"Abschlag Strom {months[i % 12]} {2025 + i // 12}", correspondent="Stadtwerke Musterstadt",
                         document_type="Rechnung", tags=["Haus", "Strom"] if i != 3 else ["Strom"], extraction_state="skipped",
                         created=date(2025 + i // 12, i % 12 + 1, 5))
            identifiers.store(d, text)
            s.add(d)
        for i in range(8):
            s.add(Document(id=400 + i, title=f"Beitragsrechnung Kfz {2019 + i}", correspondent="Musterversicherung AG",
                           document_type="Rechnung", tags=["Auto", "Versicherung"], extraction_state="skipped",
                           created=date(2019 + i, 1, 2)))

        # Erstattung: Leistungsabrechnung, in Paperless mit zwei Arztrechnungen verknüpft; eine weitere Rechnung offen
        for doc_id, title, corr, amount, day in [(601, "Zahnbehandlung", "Zahnarztpraxis Dr. Muster", 500.0, date(2026, 8, 1)),
                                                 (602, "Laborrechnung", "Labor Musterstadt", 112.4, date(2026, 8, 3)),
                                                 (603, "Hautarzt Kontrolle", "Hautarztpraxis Beispiel", 65.0, date(2026, 10, 2))]:
            s.add(Document(id=doc_id, title=title, correspondent=corr, document_type="Rechnung", kind="invoice",
                           tags=["Krankheitskosten"], created=day, review_state="confirmed", extraction_state="done",
                           invoice=Invoice(issuer=corr, amount=amount, status="paid", invoice_date=day)))
        s.add(Document(id=600, title="Leistungsabrechnung September", correspondent="Musterversicherung a.G.",
                       document_type="Leistungsabrechnung", kind="reimbursement", tags=["Krankheitskosten"],
                       created=date(2026, 9, 12), review_state="confirmed", extraction_state="done",
                       paperless_fields={"related": [601, 602]},
                       reimbursement=Reimbursement(payer="Musterversicherung a.G.", amount=420.0,
                                                   statement_date=date(2026, 9, 12))))
        s.add(Document(id=604, title="Beihilfebescheid Oktober", correspondent="Beihilfestelle", document_type="Leistungsabrechnung",
                       kind="reimbursement", tags=[], created=date(2026, 10, 15), review_state="confirmed", extraction_state="done",
                       reimbursement=Reimbursement(payer="Beihilfestelle", amount=58.5, statement_date=date(2026, 10, 15))))
    settings.reimbursable_tag = "Krankheitskosten"


def launch(pw):
    try:
        return pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
    except Exception:
        # passendes Chromium fehlt – ein vorhandenes nehmen (z.B. aus PLAYWRIGHT_BROWSERS_PATH)
        root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
        found = sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"))
        if not found:
            raise
        return pw.chromium.launch(executable_path=found[-1])


def shoot(base: str) -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch(pw)
        thumb_page = browser.new_page(viewport={"width": 627, "height": 874})
        thumb_page.set_content(FAKE_DOC)
        thumb = thumb_page.screenshot(clip={"x": 16, "y": 16, "width": 595, "height": 842})
        thumb_page.emulate_media(media="print")
        pdf = thumb_page.pdf(format="A4", print_background=True)  # Vorschau als echtes PDF (für den pdf.js-Viewer)
        thumb_page.close()

        def page(width: int, height: int):
            p = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2 if width < 500 else 1,
                                 locale="de-DE")
            # Vorschau kommt sonst aus Paperless – hier die Beispielrechnung
            p.route("**/documents/*/preview*", lambda r: r.fulfill(body=thumb, content_type="image/png")
                    if "kind=thumb" in r.request.url else r.fulfill(body=pdf, content_type="application/pdf"))
            return p

        warm = page(1280, 960)
        warm.goto(base + "/rules")  # Regelvorschläge berechnen, damit die Übersicht sie zählt
        warm.goto(base + "/reimbursements")
        warm.click("text=Mit Firefly abgleichen")  # Geldeingang der Erstattung vorschlagen lassen
        warm.close()

        for name, path, width, height in [
                ("dashboard", "/", 1280, 960), ("inbox", "/inbox", 1280, 720), ("dokument", "/documents/1", 1280, 960),
                ("rechnungen", "/invoices", 1280, 760), ("erstattungen", "/reimbursements", 1280, 900),
                ("erstattung", "/documents/600", 1280, 1100), ("handy-dashboard", "/", 390, 844)]:
            p = page(width, height)
            p.goto(base + path)
            if p.locator(".docview:not([data-lazy])").count():
                p.wait_for_selector(".docview-canvas canvas")  # PDF fertig gezeichnet
            p.wait_for_timeout(400)
            p.screenshot(path=OUT / f"{name}.png")
            p.close()

        p = page(390, 844)
        p.goto(base + "/review?after=703")
        p.wait_for_selector(".docview-canvas canvas")
        p.wait_for_timeout(400)
        # Dokumentanfang und Vorschläge zusammen: hoher Ausschnitt ab der Vorschau (nur fürs Bild: Leiste unten
        # ausblenden, Vorschau etwas kürzer)
        p.add_style_tag(content=".bottom-nav{display:none!important}.docview-pages{height:560px!important}")
        p.wait_for_timeout(600)
        top = p.evaluate("document.querySelector('.docview').getBoundingClientRect().top + scrollY - 12")
        p.screenshot(path=OUT / "handy-review.png", full_page=True, clip={"x": 0, "y": top, "width": 390, "height": 1180})
        p.close()

        p = page(1280, 960)
        p.goto(base + "/paperless-changes#metadata")
        p.locator('#meta-accepted input[name=item][value="701:title"]').check()
        p.wait_for_selector("#meta-accepted .similar-inner")
        p.evaluate("document.querySelector('.section-accepted').scrollIntoView(); window.scrollBy(0, -70)")
        p.screenshot(path=OUT / "fuer-paperless.png")
        p.close()

        p = page(1280, 960)
        p.goto(base + "/rules")
        href = p.locator('a[href*="/docs"]').first.get_attribute("href")
        p.evaluate("""() => { const el = [...document.querySelectorAll('h3, .rule-group, summary')]
                         .find(e => e.textContent.includes('Korrespondent aus Text'));
                       el.scrollIntoView(); window.scrollBy(0, -70); }""")
        p.screenshot(path=OUT / "regeln.png")
        p.goto(base + href.split("?")[0])
        p.locator("details.rule-doc summary").first.click()
        p.wait_for_timeout(500)
        p.screenshot(path=OUT / "regel-dokumente.png")
        p.close()
        browser.close()


def main() -> None:
    port = free_port()
    with tempfile.TemporaryDirectory() as data:
        start_demo(Path(data), port)
        shoot(f"http://127.0.0.1:{port}")
    print(f"Screenshots aktualisiert: {', '.join(sorted(f.name for f in OUT.glob('*.png')))}")


if __name__ == "__main__":
    main()
