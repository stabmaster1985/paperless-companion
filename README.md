# Paperless Companion

Fristen-, Rechnungs- und Vertragscockpit für [Paperless NGX](https://docs.paperless-ngx.com/).

Paperless archiviert Dokumente und macht sie durchsuchbar. Der Companion leitet daraus ab,
**was zu tun ist**:

- **Verträge:** Laufzeit, Verlängerung und Kündigungsfrist werden erkannt. Der Companion
  berechnet den letzten Kündigungstag und erinnert rechtzeitig.
- **Rechnungen:** Betrag, Fälligkeit, IBAN und Lastschrift werden erkannt. Status offen,
  bezahlt oder überfällig.
- **Garantie und Gewährleistung:** Ablaufdatum je Kauf.
- **Zahlungsabgleich mit Firefly III:** Abbuchungen werden offenen Rechnungen zugeordnet
  (Betrag, Rechnungsnummer im Verwendungszweck, IBAN, Empfänger).
- **Vorgänge:** Dokumente und Notizen zu einem Fall bündeln (z.B. Schadensfall, Widerspruch),
  mit Zeitstrahl und Wiedervorlage.
- **Steuer-Export:** alle Belege eines Jahres als ZIP mit Ordnern je Kategorie und
  CSV-Übersicht.
- **Aufbewahrungsfristen:** Regeln je Dokumenttyp oder Tag, Löschvorschläge für das gesamte
  Archiv mit Protokoll.
- **Dashboard:** anstehende Fristen, offene Beträge, monatliche Fixkosten.
- **Erinnerungen** per ntfy oder E-Mail sowie ein **Kalender-Feed (ICS)** für Handy und Outlook.
- Optional **Rückschreiben** der Ergebnisse in Paperless-Custom-Fields.

Alles läuft lokal. Die Dokumente verlassen dein Netzwerk nicht.

## Wie die Erkennung funktioniert

```
Paperless ──Webhook / Abgleich alle 15 min──▶ Companion
                                               1. Art bestimmen (Dokumenttyp oder Schlüsselwörter)
                                               2. Regeln (Regex für deutsche Dokumente)
                                               3. Nur für fehlende Felder: lokales LLM (Ollama, optional)
                                               4. Sicher erkannt → automatisch übernommen
                                                  unsicher       → Inbox zur Bestätigung
```

- **Regeln** erkennen u.a. Gesamtbetrag, Rechnungsnummer und -datum, „zahlbar bis“ oder
  „innerhalb von 14 Tagen“, IBAN (mit Prüfsumme), Lastschrift, „bereits bezahlt“,
  Mindestlaufzeit, „verlängert sich um 12 Monate“, „Kündigungsfrist von 3 Monaten zum
  Monatsende“, Vertragsbeginn, monatliche oder jährliche Kosten und Garantiedauer.
- Das **LLM** wird nur für Felder gefragt, die die Regeln nicht sicher gefunden haben, und
  bekommt nur die relevanten Textstellen. Antworten werden geprüft: Beträge, Daten und
  Nummern, die nicht im Dokument stehen, werden verworfen.
- **Verträge** müssen immer bestätigt werden. Rechnungen und Garantien mit eindeutigen
  Angaben werden automatisch übernommen.
- Jedes Feld zeigt in der Oberfläche seine Quelle (Regel, AI, Paperless) und die Sicherheit.

## Installation (Docker)

Voraussetzung: Docker auf einem Host im selben Netz wie Paperless. Das kann ein LXC oder
eine VM unter Proxmox sein, gern derselbe Host wie Paperless. Der Companion braucht etwa
150–300 MB RAM.

```bash
git clone https://github.com/stabmaster1985/paperless-companion.git
cd paperless-companion
cp .env.example .env
nano .env          # mindestens PAPERLESS_URL, PAPERLESS_TOKEN, APP_PASSWORD, SECRET_KEY
docker compose up -d --build
```

Danach ist die Oberfläche unter `http://<host>:8010` erreichbar.

**Paperless-Token:** In Paperless oben rechts *Profil → API-Auth-Token* erzeugen. Für
`WRITEBACK_ENABLED=true` braucht der Benutzer Schreibrechte auf Dokumente und Custom Fields.

**Lokale AI (optional):** siehe [docs/ollama-lxc.md](docs/ollama-lxc.md). Ohne Ollama
`LLM_PROVIDER=none` setzen. Dann arbeitet der Companion nur mit Regeln und der Inbox.

### Sofortige Verarbeitung per Paperless-Workflow (optional)

Ohne Webhook gleicht der Companion alle `SYNC_INTERVAL_MINUTES` Minuten ab. Für sofortige
Verarbeitung:

1. In `.env` `WEBHOOK_TOKEN` setzen (z.B. `openssl rand -hex 16`) und neu starten.
2. In Paperless *Workflows → Workflow erstellen*:
   - **Auslöser:** *Dokument hinzugefügt*
   - **Aktion:** *Webhook*
     - URL: `http://<companion-host>:8010/api/webhook/paperless?token=<WEBHOOK_TOKEN>`
     - „Parameter für Body verwenden“: aus
     - Body: `{"doc_url": "{doc_url}"}`
     - Als JSON senden: an
3. Für `{doc_url}` muss in Paperless `PAPERLESS_URL` gesetzt sein. Alternativ funktioniert
   auch ein Body mit `doc_id`. Ohne erkennbare ID stößt der Webhook einen Abgleich an.

Die fertige URL steht im Companion unter **System**.

### Kalender abonnieren

`ICS_TOKEN` setzen und unter **System** die angezeigte URL
(`…/calendar.ics?token=…`) in der Kalender-App abonnieren (iOS, Android/DAVx⁵,
Thunderbird, Outlook). Jede Frist erscheint dort als ganztägiger Termin.

### Erinnerungen

`NTFY_URL` (z.B. `https://ntfy.sh/<geheimes-topic>` oder ein eigener Server) und/oder die
SMTP-Daten setzen. Der Companion sendet einmal täglich ab `REMINDER_HOUR` eine
Sammelnachricht für Fristen, die in `REMINDER_DAYS` (Standard 30, 7 und 1 Tag) anstehen,
sowie für überfällige Rechnungen. Unter **System** lassen sich ein Test und eine
Sofortprüfung auslösen.

### Eigene Paperless-Custom-Fields nutzen

Der Companion arbeitet mit deinen vorhandenen Custom Fields. In der `.env` trägst du ein,
welches Feld was bedeutet. Feldtypen werden aus Paperless gelesen. Beispiel:

```env
FIELD_DUE_DATE=bezahlbar bis
FIELD_PAID_ON=bezahlt am
FIELD_COMMENT=Kommentar
FIELD_RELATED=gehört zu
FIELD_SUMMARY=Zusammenfassung KI
```

- **Fälligkeit / bezahlt am / Betrag:** Werte aus Paperless haben Vorrang vor der Erkennung.
  Dokumente mit Fälligkeits- oder Bezahlt-Datum erscheinen sofort als Rechnung, auch ohne
  Analyse. Offene Rechnungen ohne Betrag werden einmal analysiert, um den Betrag zu finden.
  Rechnungen ohne Bezahlt-Datum, die länger als `FIELDS_OPEN_MAX_AGE_DAYS` fällig sind,
  gelten als erledigt, damit alte Belege nicht als überfällig gemeldet werden.
- **Rückschreiben** (`WRITEBACK_ENABLED=true`): Was du in der App änderst (bezahlt, wieder
  offen, Firefly-Treffer, bestätigte Werte), wird in die Felder geschrieben. Das geschieht
  gesammelt im Hintergrund und nur, wenn sich der Wert unterscheidet. Unbekannte Werte
  überschreiben nie deine Eingaben in Paperless.
- **Kommentar:** wird auf der Dokumentseite angezeigt und lässt sich dort bearbeiten.
- **gehört zu:** Verknüpfte Dokumente erscheinen auf der Dokumentseite, auch über mehrere
  Ecken. Mit „Als Vorgang übernehmen“ entsteht daraus ein Vorgang. Ein Vorgang schlägt
  verknüpfte Dokumente zum Hinzufügen vor und trägt neue Dokumente in „gehört zu“ ein
  (`CASES_WRITE_LINKS`).
- **KI-Zusammenfassung:** per Knopf auf der Dokumentseite oder automatisch für neue Dokumente
  (`SUMMARY_MODE=new`). Das lokale Modell bekommt den Dokumentanfang, eine feste Gliederung
  (wer, worum, Beträge/Fristen, was zu tun ist) und eine harte Längengrenze. Felder vom Typ
  *Text* fassen in Paperless nur 128 Zeichen, die Zusammenfassung wird passend gekürzt.

### Zahlungsabgleich mit Firefly III

`FIREFLY_URL` und `FIREFLY_TOKEN` setzen. Das Token ist ein persönliches Zugangstoken aus
*Optionen → Profil → OAuth*. Der Companion liest stündlich die Abbuchungen und vergleicht sie
mit offenen Rechnungen:

| Indiz | Punkte |
|---|---|
| Betrag stimmt (Pflicht) | 50 |
| Rechnungsnummer im Verwendungszweck oder in den Notizen | +40 |
| Empfänger-IBAN stimmt | +30 |
| Empfängername passt zum Aussteller | +20 |
| Zahlung vor Fälligkeit | +5 |

- Ab 60 Punkten erscheint ein Vorschlag unter **Rechnungen** („Bezahlt“ oder „passt nicht“).
- Passt nur der Betrag, wird nur vorgeschlagen, wenn genau eine Buchung infrage kommt.
- Mit `FIREFLY_AUTO_ACCEPT=true` werden Treffer ab 90 Punkten (Betrag plus Rechnungsnummer
  oder IBAN) direkt als bezahlt markiert.
- Jede Buchung wird höchstens einer Rechnung zugeordnet. Lastschriften werden ebenfalls erkannt.

### Vorgänge

Unter **Vorgänge** oder direkt auf einer Dokumentseite einen Vorgang anlegen und weitere
Dokumente hinzufügen, per Suche im Archiv-Index oder per Paperless-ID. Notizen mit Datum
ergänzen den Zeitstrahl, z.B. „Telefonat mit Versicherung“. Eine **Wiedervorlage** erscheint
im Dashboard und im Kalender-Feed und wird am Fälligkeitstag als Erinnerung gesendet.

### Steuer-Export

In Paperless den Tag `Steuer` vergeben. Kategorien entstehen über Tags wie
`Steuer: Werbungskosten` oder `Steuer/Spenden`, sonst gilt der Dokumenttyp als Kategorie.
Unter **Steuer** stehen die Belege pro Jahr mit Summen aus den erfassten Rechnungsbeträgen.
Belege, die zu einem anderen Jahr gehören (z.B. die Nebenkostenabrechnung 2025, erstellt im
Jahr 2026), lassen sich pro Dokument umstellen oder ausschließen. Das ZIP enthält einen
Ordner je Kategorie, Dateinamen wie `2025-03-01_Firma_Titel_123.pdf` und eine CSV-Übersicht,
die sich direkt in Excel öffnen lässt.

### Aufbewahrungsfristen

Der Companion gleicht einmal täglich die Metadaten des **gesamten Archivs** ab. Dabei wird
nichts analysiert, er übernimmt nur Titel, Typ, Datum und Tags. Unter **Aufbewahrung** legst
du Regeln fest:

- **Trifft zu bei:** Dokumenttyp, Tag oder Art (Rechnung, Vertrag, Garantie)
- **Frist:** z.B. 3 oder 10 Jahre, oder 0 = dauerhaft
- **Fristbeginn:** Ende des Kalenderjahres, Dokumentdatum oder Vertragsende

Treffen mehrere Regeln zu, gilt die längste Frist. Offene Rechnungen, laufende Verträge und
Garantien sowie manuell gesperrte Dokumente werden nie vorgeschlagen.

Fällige Dokumente lassen sich in Paperless mit dem Tag `Löschkandidat` markieren (Standard)
oder dauerhaft behalten. Direktes Löschen ist nur mit `RETENTION_DELETE_ENABLED=true` und
nach Eintippen von „LÖSCHEN“ möglich. Gelöschte Dokumente landen im Papierkorb von
Paperless. Jede Aktion wird protokolliert.

Die mitgelieferten Regeln sind Beispiele für Privathaushalte und keine Rechtsberatung.

## Zuordnung der Dokumente

1. Dokumenttyp in Paperless: siehe `DOCTYPES_INVOICE`, `DOCTYPES_CONTRACT` und
   `DOCTYPES_WARRANTY` (Groß-/Kleinschreibung egal).
2. Sonst Schlüsselwörter in Titel und Text („Rechnungsnummer“, „Vertrag“ + „Laufzeit“ …).
3. In der Oberfläche kann die Art jederzeit manuell geändert werden. Das Dokument wird dann
   neu analysiert.

Beim ersten Start werden Dokumente der letzten `INITIAL_SYNC_DAYS` Tage eingelesen. Ältere
Dokumente lassen sich unter **System** einzeln per Paperless-ID einlesen.

## Fristberechnung

- `Kündigung bis` ist der letzte Tag, an dem die Kündigung **beim Anbieter eingegangen**
  sein muss.
- Bei verpasster Frist rechnet der Companion automatisch mit der nächsten Verlängerungsperiode.
- „Läuft unbefristet weiter“: Nach der Mindestlaufzeit wird das frühestmögliche Vertragsende
  bei Kündigung heute angezeigt. Für Verbraucherverträge ab dem 01.03.2022 ist das nach
  § 309 Nr. 9 BGB meist der Fall (Kündigungsfrist 1 Monat).
- Die Berechnung ist eine Hilfe, keine Rechtsberatung. Im Zweifel den Vertrag prüfen.

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

Aufbau:

| Datei | Inhalt |
|---|---|
| `app/extraction/rules.py` | Regelbasierte Extraktion |
| `app/extraction/llm.py` | Gezielte LLM-Abfragen und Prüfung der Antworten |
| `app/deadlines.py` | Fristenberechnung für Verträge |
| `app/pipeline.py` | Abgleich, Klassifizierung, Übernahme, Rückschreiben |
| `app/payments.py` | Zahlungsabgleich mit Firefly III |
| `app/cases.py` | Vorgänge und Zeitstrahl |
| `app/tax.py` | Steuer-Export |
| `app/retention.py` | Aufbewahrungsregeln |
| `app/worker.py` | Hintergrund-Thread (Abgleich, Warteschlange, Index, Zahlungen, Erinnerungen) |
| `app/main.py` | Weboberfläche und API |

Die Daten liegen in einer SQLite-Datei unter `DATA_DIR`. Zum Sichern genügt dieses
Verzeichnis.

## Ideen für später

- Abgleich mit Kontoauszügen als CSV (ohne Firefly III)
- Optionale Cloud-API, pro Dokumenttyp freischaltbar
- Haushaltsnahe Dienstleistungen (§ 35a EStG): Arbeitskosten aus Handwerkerrechnungen erkennen
