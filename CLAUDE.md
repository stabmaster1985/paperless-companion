# Hinweise für die Arbeit an diesem Projekt

- Sprache: Oberfläche, Kommentare, Commit-Messages und Antworten an den Nutzer auf Deutsch.
- Änderungen direkt auf `main` committen und pushen (so mit dem Nutzer vereinbart).
- Paperless wird nur gelesen. Nichts darf ohne ausdrückliche Aktion des Nutzers nach Paperless geschrieben
  werden (Vorschläge erst vormerken, dann gesammelt übertragen; Regeln werden nie automatisch angelegt).
- Vor jedem Push: `.venv/bin/pytest` muss grün sein (läuft auch als GitHub Action).

## Screenshots aktuell halten

Die README zeigt Screenshots aus `docs/screenshots/`. Nach jeder Änderung, die man in der Oberfläche
sieht (Templates, CSS, neue oder geänderte Seiten), die Bilder neu erzeugen und mit committen:

```bash
.venv/bin/pip install playwright   # einmalig
.venv/bin/python scripts/screenshots.py
```

- Das Skript startet den Companion mit erfundenen Beispieldaten; nie echte Dokumente oder Daten des Nutzers abbilden.
- Neue Seiten oder Funktionen, die in der README beschrieben sind, im Skript (`scripts/screenshots.py`)
  und im Abschnitt „Screenshots“ der README ergänzen.
- Die erzeugten Bilder vor dem Commit ansehen (stimmt der Ausschnitt, ist nichts leer oder abgeschnitten?).
