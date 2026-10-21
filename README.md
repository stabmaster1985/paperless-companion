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
- **Paperless bleibt unangetastet:** Vorschläge für deine Custom Fields übernimmst du gezielt
  auf der Seite „Für Paperless“.

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
- Jedes Feld zeigt dauerhaft seine **Herkunft**: *Regel* (aus dem Text), *AI*, *Paperless* (deine
  Felder), *Firefly* (Bankbuchung), *Standard* oder *manuell* (von dir eingegeben oder geändert).
  ✓ heißt, dass du einen erkannten Wert unverändert bestätigt hast. Die Herkunft bleibt auch nach
  dem Bestätigen erhalten. In der Rechnungsliste steht z.B. „bezahlt 10.10. · Firefly ✓“.

## Installation (Docker)

Voraussetzung: Docker auf einem Host im selben Netz wie Paperless. Das kann ein LXC oder
eine VM unter Proxmox sein, gern derselbe Host wie Paperless. Der Companion braucht etwa
150–300 MB RAM.

```bash
cd ~
git clone https://github.com/stabmaster1985/paperless-companion.git
cd paperless-companion
cp .env.example .env
nano .env          # mindestens PAPERLESS_URL, PAPERLESS_TOKEN, APP_PASSWORD, SECRET_KEY
docker compose up -d --build
```

Danach ist die Oberfläche unter `http://<host>:8010` erreichbar.

**Paperless-Token:** In Paperless oben rechts *Profil → API-Auth-Token* erzeugen. Zum reinen
Lesen genügen Leserechte. Schreibrechte braucht der Benutzer erst, wenn du Vorschläge unter
„Für Paperless“ übernimmst oder Aufbewahrungsaktionen ausführst. Wer ganz sicher gehen will,
gibt dem Companion einen Benutzer mit reinen Leserechten.

**Ollama im Netzwerk nutzen:** Läuft Ollama im selben Docker (`COMPOSE_PROFILES=ollama`), ist
es standardmäßig nur vom Docker-Host erreichbar. Mit `OLLAMA_PUBLISH=11434` in der `.env` und
`docker compose up -d` ist es im ganzen Netzwerk unter `http://<host>:11434` erreichbar,
z.B. für n8n oder Open WebUI. Ollama hat keine Anmeldung, deshalb nur im Heimnetz verwenden
und den Port nie im Router freigeben.

**Lokale AI (optional):** siehe [docs/ollama-lxc.md](docs/ollama-lxc.md). Ohne Ollama
`LLM_PROVIDER=none` setzen. Dann arbeitet der Companion nur mit Regeln und der Inbox.

### Aktualisieren per n8n

Das läuft wie beim firefly-dashboard. `n8n/paperless-companion-update.json` ist ein fertiger
n8n-Workflow, der den Companion auf dem Docker-Host per SSH aktualisiert:

1. Das Repository unter `~/paperless-companion` holen: beim ersten Lauf klonen, danach
   `git pull --rebase --autostash`.
2. Neu bauen und starten (`docker compose up -d --build`).
3. Alte Images aufräumen und prüfen, ob der Container läuft.

Die `.env` und der Datenordner `data/` sind nicht im Repository und bleiben bei jedem Update
erhalten.

1. In n8n **Import from File** → `n8n/paperless-companion-update.json` wählen.
2. Im Knoten **Update auf Companion-Host** SSH-Zugangsdaten anlegen: Host ist die IP des
   LXC/der VM mit Docker, dazu Benutzer und privater Schlüssel. Der Benutzer braucht
   Docker-Rechte (root oder Gruppe `docker`) sowie `git` auf dem Host.
3. Im Knoten **Webhook** Header-Auth-Zugangsdaten anlegen (Name `X-Update-Key`, langer
   Zufallswert) und den Workflow aktivieren.
4. In der `.env` des Companion `UPDATE_WEBHOOK_URL` (Production URL des Webhooks) und
   `UPDATE_WEBHOOK_SECRET` (der Zufallswert) eintragen.

**Update-Knopf:** Unter **System** zeigt die Karte **Companion-Version** den laufenden Commit.
Sobald es auf GitHub Neues gibt, erscheinen dort die neuen Commits und der Knopf **Jetzt
aktualisieren**, und im Menü steht bei „System“ ein Hinweis „Update“. Nach dem Klick ruft der
Companion den n8n-Webhook auf (mit `typ`, `von`, `auf`). Die Seite lädt nach dem Neustart von
selbst neu. Die Prüfung fragt höchstens einmal pro Stunde bei GitHub nach. `UPDATE_CHECK=false`
schaltet sie ab, `UPDATE_REPO` wählt ein anderes Repository. Die Version schreibt der
Docker-Build aus dem `.git`-Verzeichnis in die Datei `version.json`.

Alternativ startest du den Workflow per **Test workflow** oder, wenn aktiviert, täglich um
4 Uhr. Der Webhook antwortet sofort mit 202, weil sich der Companion beim Neustart beendet.
Das Ergebnis steht in den n8n-Ausführungen. Ein Fehler bricht den Workflow mit der Ausgabe ab.

**Solange das Repository privat ist:**
- Für die Update-Prüfung braucht der Companion ein Token (`UPDATE_GITHUB_TOKEN`). Das ist ein
  Fine-grained Personal Access Token, beschränkt auf dieses Repository, mit der Berechtigung
  *Contents: Read-only*.
- Der Host braucht Lesezugriff für `git pull`. Am einfachsten geht das mit einem
  **Deploy Key**: Auf dem Host `ssh-keygen -t ed25519 -f ~/.ssh/paperless_companion` erzeugen,
  den öffentlichen Schlüssel auf GitHub unter *Settings → Deploy keys* (ohne Schreibrecht)
  eintragen und einmalig per SSH klonen:
  ```bash
  GIT_SSH_COMMAND="ssh -i ~/.ssh/paperless_companion" \
    git clone git@github.com:stabmaster1985/paperless-companion.git ~/paperless-companion
  git -C ~/paperless-companion config core.sshCommand "ssh -i ~/.ssh/paperless_companion"
  ```
  Der n8n-Workflow arbeitet danach mit diesem Klon weiter.

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
- **Nichts ohne deine Zustimmung:** Der Companion liest Paperless nur. Was er für deine Felder
  vorschlägt (Betrag, bezahlt am, Fristen, Kommentar, Zusammenfassung, Verknüpfungen),
  erscheint unter **Für Paperless** als Gegenüberstellung *Paperless jetzt → Vorschlag*.
  Erst wenn du dort Einträge auswählst und übernimmst, wird geschrieben, und zwar nur die
  ausgewählten Felder. Alle anderen Custom Fields bleiben unverändert. Verworfene Vorschläge
  erscheinen erst wieder, wenn sich der Wert im Companion ändert. Unbekannte Werte werden nie
  vorgeschlagen, sie würden deine Eingaben leeren. Die einzige Ausnahme ist „bezahlt am“,
  wenn du eine Rechnung im Companion wieder auf offen setzt.
- **Kommentar:** wird auf der Dokumentseite angezeigt. Änderungen werden vorgemerkt.
- **gehört zu:** Verknüpfte Dokumente erscheinen auf der Dokumentseite, auch über mehrere
  Ecken. Mit „Als Vorgang übernehmen“ entsteht daraus ein Vorgang. Ein Vorgang schlägt
  verknüpfte Dokumente zum Hinzufügen vor. Fügst du ein Dokument hinzu, wird die Verknüpfung
  in „gehört zu“ vorgeschlagen (`CASES_PROPOSE_LINKS`).
- **KI-Zusammenfassung:** per Knopf auf der Dokumentseite oder automatisch für neue Dokumente
  (`SUMMARY_MODE=new`). Das lokale Modell bekommt den Dokumentanfang, eine feste Gliederung
  (wer, worum, Beträge/Fristen, was zu tun ist) und eine harte Längengrenze. Felder vom Typ
  *Text* fassen in Paperless nur 128 Zeichen, die Zusammenfassung wird passend gekürzt.
  Sie wird im Companion gespeichert und unter „Für Paperless“ zur Übernahme angeboten.

### Leserechte für den Companion-Benutzer

Gehören Dokumenttypen, Korrespondenten oder Tags einem anderen Paperless-Benutzer (z.B. admin),
sieht der Companion sie nicht (Hinweis unter **Einstellungen**). Freigeben ohne alles
einzeln anzuklicken: Companion-Benutzer in eine Gruppe (z.B. `ReadOnly`) aufnehmen, der Gruppe
unter *Benutzer & Gruppen* „Anzeigen“ für Dokument, Dokumenttyp, Korrespondent, Tag geben und
dann einmal mit einem **Admin-Token**:

```bash
curl -fsSLO https://raw.githubusercontent.com/stabmaster1985/paperless-companion/main/scripts/paperless_grant_view.py
python3 paperless_grant_view.py --url http://PAPERLESS:8000 --token ADMIN_TOKEN --group ReadOnly --dry-run
python3 paperless_grant_view.py --url http://PAPERLESS:8000 --token ADMIN_TOKEN --group ReadOnly
```

Bestehende Rechte bleiben erhalten (merge). `--documents` gibt zusätzlich alle Dokumente frei.
Danach in der App unter **Einstellungen** „Archiv-Index neu einlesen“.

### Metadaten-Vorschläge: Titel, Tags, Korrespondent, Dokumenttyp

Wenn ein Dokument ohnehin analysiert wird, schlägt die App auch Paperless-Metadaten vor.
Der Ablauf hat drei Schritte, und nur der letzte schreibt nach Paperless:

1. **Vorschlag** – aus dem Verlauf und/oder per AI (siehe unten).
2. **Vormerken oder verwerfen** – speichert deine Entscheidung nur im Companion. So kannst du
   in Ruhe sammeln und beobachten, wie gut die Vorschläge sind.
3. **Übertragen** – unter **Für Paperless → Vorgemerkt** einzeln, ausgewählt oder alle auf einmal.
   Tags werden dabei nur ergänzt, vorgemerkte neue Tags erst jetzt in Paperless angelegt.

Woher die Vorschläge kommen:

- **Verlauf** (ohne AI, kostet nichts): Tragen mindestens die Hälfte der übrigen Dokumente
  desselben Korrespondenten einen Tag (bzw. 60 % denselben Dokumenttyp), wird er vorgeschlagen.
- **AI**: Das lokale Modell liest den Dokumentanfang und wählt Tags, Korrespondent und
  Dokumenttyp **nur aus deinen vorhandenen** Listen (per JSON-Schema erzwungen). Neue Tags
  (höchstens zwei) sind als „neuer Tag“ markiert. Dazu kommt ein sprechender Titel.

**Die App lernt schon aus Vorgemerktem**, nicht erst aus dem, was in Paperless steht:

- Vorgemerkte Tags, Korrespondenten und Dokumenttypen zählen für den Verlauf so, als
  stünden sie bereits in Paperless. Vorgemerkte neue Tags kann die AI danach wie vorhandene
  wählen, damit gleiche Dokumente denselben neuen Tag bekommen.
- Vorgemerkte (und von dir geänderte) Titel bekommt die AI als Stilbeispiele, gleicher Absender zuerst.
- Verworfene Tags schlägt die App für denselben Korrespondenten nicht mehr vor.

**Wie gut ist das?** Die Seite „Für Paperless“ zeigt je Quelle (AI/Verlauf) und Art
(Tag, Titel, …), wie oft du übernommen, geändert oder verworfen hast, als Trefferquote.

Unter „Offene Vorschläge“ stehen nur nützliche Unterschiede: fehlende Tags, ein Titel nur,
wenn der jetzige nichtssagend ist („Scan_0012“, Dateiname), Korrespondent/Typ nur, wenn sie
fehlen. Alle Abweichungen zeigt die Dokumentseite im Abschnitt „Paperless-Metadaten“; dort
kannst du den Titel vor dem Vormerken ändern.

Der **Archiv-Check** auf „Für Paperless“ zählt Dokumente ohne Tags, mit nichtssagendem Titel
oder ohne Korrespondent. Über den Filter „Hinweis“ in der Dokumentliste wählst du sie gezielt
aus und klickst „Metadaten für ausgewählte vorschlagen“.

`META_MODE=new` (Standard) erzeugt Vorschläge automatisch für neu analysierte Dokumente,
`manual` nur auf Anforderung, `off` schaltet die Funktion ab. Die Vorschläge laufen in derselben
Warteschlange wie die Analysen (Pause, `LLM_ACTIVE_HOURS`); mit qwen2.5:3b auf der CPU dauert
einer etwa eine Minute. Ohne Ollama gibt es nur Vorschläge aus dem Verlauf.

### Zahlungsabgleich mit Firefly III

`FIREFLY_URL` und `FIREFLY_TOKEN` setzen. Das Token ist ein persönliches Zugangstoken aus
*Optionen → Profil → OAuth*. Der Companion liest stündlich die Abbuchungen und vergleicht sie
mit offenen Rechnungen. Zum Kontrollieren ist jede gefundene Buchung verlinkt (Rechnungsliste,
Dokumentseite, „Für Paperless“): direkt in Firefly und – wenn `FIREFLY_DASHBOARD_URL` gesetzt
ist – in deinem Firefly-Dashboard, gefiltert auf den Buchungstag.

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
- **Auch für die Inbox:** Direkt nach der Analyse einer Rechnung, die geprüft werden muss, und bei
  jedem stündlichen Abgleich sucht der Companion die passende Abbuchung. Auf der Prüfseite steht
  dann „In Firefly gefunden“. Status, „Bezahlt am“ und gegebenenfalls der Betrag sind daraus
  vorbelegt und mit der Quelle „Firefly“ gekennzeichnet. Fehlt der Betrag im Dokument, genügt
  die Rechnungsnummer im Verwendungszweck (oder IBAN plus Empfänger), und der Betrag kommt aus
  der Buchung. Übernommen wird erst beim Bestätigen.

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

1. Dokumenttyp in Paperless. Am einfachsten legst du das unter **Einstellungen** fest:
   Die Seite liest alle Dokumenttypen aus Paperless, und du wählst je Typ Rechnung,
   Vertrag, Garantie oder „nicht auswerten“ (und ob er steuerrelevant ist). Die
   Auswahl wird in der App gespeichert und hat Vorrang vor `DOCTYPES_INVOICE`,
   `DOCTYPES_CONTRACT`, `DOCTYPES_WARRANTY` und `TAX_DOCTYPES` aus der `.env`, die nur
   noch als Startwerte dienen.
2. Sonst Schlüsselwörter in Titel und Text („Rechnungsnummer“, „Vertrag“ + „Laufzeit“ …).
3. In der Oberfläche kann die Art jederzeit manuell geändert werden. Das Dokument wird dann
   neu analysiert.

Beim ersten Start werden Dokumente der letzten `INITIAL_SYNC_DAYS` Tage analysiert. Danach
folgt jedes neue oder geänderte Dokument automatisch. Alle übrigen stehen als „nur Index“ in
der Liste **Dokumente**. Dort kannst du nach Tag, Paperless-Dokumenttyp, Korrespondent,
Zeitraum, Paperless-IDs (`12, 15-20`), Art, Status und Methode filtern. Mit **„Alle gefilterten
analysieren“** oder per Häkchen werden die Dokumente in die Warteschlange gestellt. Bereits
bestätigte Dokumente werden dabei übersprungen, außer du setzt das Häkchen „auch bereits
bestätigte“.

**Last begrenzen:** Analysiert werden automatisch nur neue und kürzlich geänderte Dokumente,
alles andere erst nach deiner Auswahl. Die AI wird nur für Felder gefragt, die die Regeln nicht
sicher finden. Zusätzlich gibt es drei Bremsen:
- `LLM_ACTIVE_HOURS=22-6`: Die AI läuft nur nachts, tagsüber arbeiten nur die Regeln.
- `OLLAMA_NUM_THREAD=2`: Ollama nutzt höchstens zwei CPU-Threads.
- Unter **System → Warteschlange** kannst du die Analyse **anhalten** oder die Warteschlange
  **leeren**.

**Methode:** Die Spalte zeigt, womit die Werte ermittelt wurden: *Regeln*, *Regeln + AI*,
*nur Regeln (AI nicht erreichbar)*, *aus Paperless-Feldern* oder *nicht analysiert*. Auf der
Dokumentseite steht an jedem Feld außerdem die Quelle mit der Sicherheit.

**Neue Erkennung, kein neues Scannen:** Die OCR bleibt in Paperless. Wird die Erkennung im
Companion erweitert (neue Felder, bessere Regeln), steigt die Analyse-Version. Unter **System**
steht dann, wie viele nicht bestätigte Dokumente mit einer älteren Version analysiert wurden,
mit einem Knopf zum Neu-Analysieren. Bestätigte Dokumente bleiben unangetastet.

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
| `app/version.py`, `app/gitinfo.py` | Version und Update-Prüfung |
| `n8n/paperless-companion-update.json` | n8n-Workflow für Updates per SSH |
| `app/worker.py` | Hintergrund-Thread (Abgleich, Warteschlange, Index, Zahlungen, Erinnerungen) |
| `app/main.py` | Weboberfläche und API |

Die Daten liegen in einer SQLite-Datei unter `DATA_DIR`. Zum Sichern genügt dieses
Verzeichnis.

## Ideen für später

- Abgleich mit Kontoauszügen als CSV (ohne Firefly III)
- Optionale Cloud-API, pro Dokumenttyp freischaltbar
- Haushaltsnahe Dienstleistungen (§ 35a EStG): Arbeitskosten aus Handwerkerrechnungen erkennen
