"""Hilfetexte der Oberfläche: Seitenhilfe (aufklappbar oben auf jeder Seite), Tooltips und Begriffe.

Bewusst an einer Stelle gesammelt, damit die Texte einheitlich bleiben.
"""

# Pfad-Präfix -> (Titel, [Absätze]); der längste passende Präfix gewinnt
PAGES: dict[str, tuple[str, list[str]]] = {
    "/": ("Übersicht", [
        "Hier siehst du, was als Nächstes ansteht: fällige Rechnungen, Kündigungsfristen, ablaufende Garantien "
        "und wie viele Dokumente in der Inbox auf deine Prüfung warten.",
        "Die Zahlen stammen aus Dokumenten, die der Companion analysiert hat. Ältere Dokumente aus dem Archiv "
        "werden nur analysiert, wenn du sie unter „Dokumente“ auswählst.",
    ]),
    "/inbox": ("Inbox", [
        "Neue Rechnungen, Verträge und Garantien landen hier, bis du die erkannten Werte geprüft hast.",
        "Öffne ein Dokument, vergleiche die Felder mit der Vorschau links und klicke „Bestätigen &amp; nächstes“. "
        "Die farbigen Kennzeichen zeigen, woher ein Wert stammt (Regel, AI, Paperless, Firefly, manuell).",
        "In Paperless ändert sich dabei nichts. Werte für deine Custom Fields findest du anschließend unter „Für Paperless“.",
    ]),
    "/contracts": ("Verträge", [
        "Alle bestätigten Verträge mit berechneter Kündigungsfrist. „Kündigung bis“ ist der letzte Tag, an dem die "
        "Kündigung beim Anbieter eingehen muss.",
        "Hast du gekündigt, trage das Datum ein – dann erinnert die App nicht mehr.",
    ]),
    "/invoices": ("Rechnungen", [
        "Offene und bezahlte Rechnungen. Lastschriften werden nicht als überfällig markiert, weil sie automatisch abgebucht werden.",
        "Mit Firefly III gleicht die App stündlich deine Abbuchungen ab. Gefundene Zahlungen erscheinen oben als Vorschlag – "
        "über die Links kannst du die Buchung in Firefly oder deinem Dashboard prüfen.",
    ]),
    "/warranties": ("Garantien", [
        "Garantien und Gewährleistungen mit Ablaufdatum. Ohne Angabe rechnet die App mit 24 Monaten gesetzlicher Gewährleistung ab Kaufdatum.",
    ]),
    "/cases": ("Vorgänge", [
        "Ein Vorgang bündelt zusammengehörige Dokumente, z.B. Schaden, Angebot, Rechnung und Erstattung, mit eigenen Notizen und Fristen.",
        "Verknüpfungen aus deinem Feld „gehört zu“ schlägt die App automatisch als Vorgang vor.",
    ]),
    "/tax": ("Steuer", [
        "Steuerrelevante Dokumente eines Jahres, gruppiert nach Kategorie. Steuerrelevant ist, was einen Steuer-Tag trägt "
        "(auch „Steuer: Handwerker“ als Kategorie) oder einen als steuerrelevant markierten Dokumenttyp hat.",
        "Der Export erzeugt eine ZIP-Datei mit allen PDFs und einer Übersicht als CSV – gut für die Steuererklärung oder den Steuerberater.",
    ]),
    "/retention": ("Aufbewahrung", [
        "Welche Dokumente nach den Aufbewahrungsregeln gelöscht werden dürften. Gelöscht wird nie automatisch.",
        "„Markieren“ setzt in Paperless nur einen Tag; „Dauerhaft behalten“ schließt ein Dokument für immer aus.",
    ]),
    "/documents/": ("Dokument prüfen", [
        "Links die Vorschau aus Paperless (Reiter „Text“ zeigt, wo die Werte im OCR-Text gefunden wurden), rechts die erkannten Felder.",
        "Die Kennzeichen an den Feldern zeigen die Herkunft und Sicherheit, z.B. „Regel 90 %“ oder „AI 70 %“. Gestrichelt = unsicher, bitte prüfen.",
        "Unter „Paperless-Metadaten“ stehen Vorschläge für Titel, Tags, Korrespondent und Dokumenttyp: erst vormerken, später übertragen.",
    ]),
    "/documents": ("Dokumente", [
        "„Bearbeitet“ sind analysierte Dokumente, „Nur Index“ die übrigen deines Archivs. Diese werden nicht analysiert, "
        "solange du sie nicht auswählst – so bleibt die CPU frei.",
        "Mit den Filtern (auch Paperless-IDs wie „12, 15-20“) wählst du gezielt aus und lässt sie analysieren oder Metadaten vorschlagen.",
    ]),
    "/receipts": ("Belege", [
        "Abbuchungen aus Firefly III, zu denen kein Dokument in Paperless zugeordnet ist – so merkst du rechtzeitig, "
        "wenn ein Kassenbon oder eine Rechnung fehlt (Garantie, Steuer).",
        "„Möglicher Beleg“ heißt: Es gibt eine Rechnung mit gleichem Betrag oder ein Dokument desselben Absenders aus den Tagen um die Buchung. "
        "Mit „Das ist der Beleg“ bestätigst du die Zuordnung. „Kein Beleg nötig“ und „immer ausblenden“ räumen die Liste auf.",
        "Alles bleibt im Companion; Paperless und Firefly werden nicht verändert.",
    ]),
    "/rules": ("Regeln", [
        "<strong>Gelernte Felder:</strong> Aus deinen bestätigten Rechnungen lernt die App je Absender Zahlungsart, IBAN und Zahlungsziel "
        "und belegt neue Rechnungen damit vor (Kennzeichen „Gelernt“). Jede gelernte Regel lässt sich abschalten.",
        "<strong>Trefferquote:</strong> wie oft die Vorschläge je Quelle beim Bestätigen stimmten.",
        "<strong>Vorschläge für Paperless-Regeln:</strong> Tragen fast alle Dokumente eines Absenders dieselben Tags/denselben Typ, "
        "schlägt die App einen Paperless-Workflow vor. Du passt ihn an, siehst die Auswirkung auf deine bisherigen Dokumente "
        "und gibst ihn frei. Angelegt wird er von dir in Paperless – der Companion schreibt nichts.",
    ]),
    "/paperless-changes": ("Für Paperless", [
        "Der Companion schreibt nie ungefragt nach Paperless. Hier sammelt sich alles, was er vorschlägt.",
        "<strong>Custom Fields:</strong> Werte wie Betrag oder Fälligkeit – auswählen und „übernehmen“.",
        "<strong>Metadaten:</strong> Titel, Tags, Korrespondent, Dokumenttyp in drei Schritten: Vorschlag → <em>vormerken</em> "
        "(nur im Companion, die App lernt daraus) → <em>übertragen</em> (erst jetzt nach Paperless). "
        "Mit dem Filter bearbeitest du z.B. erst alle Tags oder einen bestimmten Tag auf einmal.",
        "„Wie gut sind die Vorschläge?“ zeigt, wie oft du übernommen, geändert oder verworfen hast.",
    ]),
    "/settings": ("Einstellungen", [
        "<strong>Dokumenttypen:</strong> Welche Paperless-Typen als Rechnung, Vertrag oder Garantie ausgewertet werden und welche steuerrelevant sind.",
        "<strong>Weitere Einstellungen:</strong> Vorbelegt mit der .env. Was du änderst, gilt sofort und hat Vorrang (Kennzeichen „App“); "
        "„zurücksetzen“ nimmt wieder den .env-Wert.",
    ]),
    "/system": ("System", [
        "Verbindungen (Paperless, Ollama, Firefly), Warteschlange und Aktivitätsprotokoll. Hier siehst du, was der Companion gerade tut.",
        "Mit „Anhalten“ pausierst du alle Analysen, z.B. wenn der Rechner gerade anderweitig gebraucht wird.",
    ]),
    "/help": ("Hilfe", []),
}

# Tooltips für wiederkehrende Bedienelemente
TIPS: dict[str, str] = {
    "vormerken": "Nur im Companion speichern. Die App lernt daraus, Paperless bleibt unverändert.",
    "uebertragen": "Vorgemerkte Werte jetzt in Paperless schreiben. Tags werden nur ergänzt.",
    "zuruecknehmen": "Vormerkung aufheben – der Vorschlag ist wieder offen.",
    "verwerfen": "Vorschlag ablehnen. Verworfene Tags schlägt die App für denselben Absender nicht mehr vor.",
    "uebernehmen": "Ausgewählte Werte jetzt in die Custom Fields in Paperless schreiben.",
    "analysieren": "Dokument (erneut) mit Regeln und – falls nötig – der lokalen AI auswerten.",
    "ignorieren": "Dokument nicht als Rechnung/Vertrag/Garantie behandeln; es verschwindet aus der Inbox.",
    "behalten": "Dieses Dokument nie zur Löschung vorschlagen, egal welche Aufbewahrungsregel gilt.",
    "history": "Aus dem Verlauf: Andere Dokumente desselben Absenders tragen diesen Wert (inkl. Vorgemerktem).",
    "llm": "Vom lokalen KI-Modell vorgeschlagen, nur aus deinen vorhandenen Listen gewählt.",
    "neuer_tag": "Diesen Tag gibt es in Paperless noch nicht. Er wird erst beim Übertragen angelegt.",
    "vorgemerkt": "Von dir vorgemerkt, noch nicht in Paperless.",
    "stand": "Wann der Wert ermittelt, vorgeschlagen bzw. vorgemerkt wurde (lokale Zeit).",
    "trefferquote": "Anteil der Vorschläge, die du übernommen oder leicht geändert hast.",
    "nur_index": "Nur die Metadaten sind bekannt; der Inhalt wurde (noch) nicht analysiert.",
    "app_setting": "In der App geändert – hat Vorrang vor der .env.",
}

# Begriffe für die Hilfeseite
GLOSSARY: list[tuple[str, str]] = [
    ("Analyse", "Der Companion liest den OCR-Text aus Paperless und erkennt Beträge, Daten, Fristen usw. zuerst mit Regeln. "
                "Nur fehlende Werte fragt er bei der lokalen AI an."),
    ("Herkunft", "Jedes Feld zeigt, woher sein Wert stammt: Regel, AI, Paperless (deine Felder), Firefly (Bankbuchung), "
                 "Standard oder manuell. „✓“ heißt von dir bestätigt, gestrichelt heißt unsicher."),
    ("Inbox", "Neu erkannte Rechnungen, Verträge und Garantien, die du noch prüfen sollst."),
    ("Nur Index", "Dokumente aus deinem Archiv, deren Metadaten bekannt sind, die aber nicht analysiert werden, solange du sie nicht auswählst."),
    ("Vormerken", "Eine Entscheidung nur im Companion speichern. Die App lernt daraus sofort; Paperless bleibt unverändert."),
    ("Übertragen / Übernehmen", "Erst dieser Schritt schreibt nach Paperless – immer nur, was du ausgewählt hast."),
    ("Verlauf", "Vorschläge aus deinen übrigen Dokumenten desselben Absenders, ohne AI und ohne Rechenzeit."),
    ("AI-Zeitfenster", "Wenn eingestellt, fragt die App die lokale AI nur in diesen Stunden; Regeln laufen immer."),
    ("Warteschlange", "Analysen, Zusammenfassungen und Metadaten-Vorschläge laufen nacheinander im Hintergrund und lassen sich unter System anhalten."),
    ("Vorgang", "Eine Sammlung zusammengehöriger Dokumente mit Notizen und Fristen."),
]

STEPS: list[tuple[str, str]] = [
    ("1. Einrichten", "Unter <a href=\"/settings\">Einstellungen</a> die Paperless-Dokumenttypen zuordnen (Rechnung, Vertrag, Garantie). "
                      "Weitere Werte sind mit deiner .env vorbelegt."),
    ("2. Analysieren lassen", "Neue Dokumente analysiert der Companion automatisch. Ältere wählst du unter "
                              "<a href=\"/documents?view=index\">Dokumente → Nur Index</a> gezielt aus."),
    ("3. Prüfen", "In der <a href=\"/inbox\">Inbox</a> die erkannten Werte mit der Vorschau vergleichen und bestätigen."),
    ("4. Fristen im Blick", "<a href=\"/\">Übersicht</a>, Erinnerungen per ntfy/E-Mail und der Kalender-Link zeigen, was ansteht."),
    ("5. Nach Paperless zurück", "Unter <a href=\"/paperless-changes\">Für Paperless</a> sammeln, vormerken und erst dann übertragen, "
                                 "wenn du mit der Qualität zufrieden bist."),
]


def page_help(path: str) -> tuple[str, list[str]] | None:
    """Seitenhilfe zum Pfad (längster passender Präfix, „/“ nur exakt)."""
    if path == "/":
        return PAGES["/"]
    best = max((p for p in PAGES if p != "/" and path.startswith(p)), key=len, default=None)
    return PAGES[best] if best and PAGES[best][1] else None
