# Paperless Companion

Fristen-, Rechnungs- und Vertragscockpit für [Paperless NGX](https://docs.paperless-ngx.com/).

Paperless archiviert Dokumente und macht sie durchsuchbar. Der Companion leitet daraus ab,
**was zu tun ist**:

- **Verträge:** Laufzeit, Verlängerung und Kündigungsfrist werden erkannt. Der Companion
  berechnet den letzten Kündigungstag und erinnert rechtzeitig.
- **Rechnungen:** Betrag, Fälligkeit, IBAN und Lastschrift werden erkannt. Status offen,
  bezahlt oder überfällig.
- **Garantie und Gewährleistung:** Ablaufdatum je Kauf.
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
| `app/worker.py` | Hintergrund-Thread (Abgleich, Warteschlange, Erinnerungen) |
| `app/main.py` | Weboberfläche und API |

Die Daten liegen in einer SQLite-Datei unter `DATA_DIR`. Zum Sichern genügt dieses
Verzeichnis.

## Ideen für später

- Vorgänge: mehrere Dokumente zu einem Fall bündeln (z.B. Schadensfall) mit Zeitstrahl
- Aufbewahrungsfristen (6 bzw. 10 Jahre) mit Löschvorschlägen
- Steuer-Export: Belege pro Jahr als ZIP
- Abgleich offener Rechnungen mit Kontoauszügen (CSV, Firefly III)
- Optionale Cloud-API, pro Dokumenttyp freischaltbar
