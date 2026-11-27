"""Zwischenspeicher für die Buchungssuche: geladene Firefly-Abbuchungen und Namenslisten (Kategorien, Tags …).

Liegt im Speicher und als Datei im Datenordner – so übersteht er Updates und Neustarts. Gilt einen Tag oder bis
„neu aus Firefly laden“. Nur Lesedaten aus Firefly, nichts Eigenes: Geht die Datei verloren, wird einfach neu geladen.
"""

import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from app.firefly import Transaction

log = logging.getLogger(__name__)
MAX_AGE = 24 * 3600


class FireflyCache:
    def __init__(self, path: Path, max_age: int = MAX_AGE):
        self.path = path
        self.max_age = max_age
        self.data: dict = {}
        self._read()

    # ------------------------------------------------------------------ Datei
    def _read(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Zwischenspeicher der Buchungssuche nicht lesbar (%s) – wird neu geladen", exc)
            return
        data: dict = {}
        if w := raw.get("withdrawals"):
            try:
                data["withdrawals"] = {
                    "start": date.fromisoformat(w["start"]), "at": float(w["at"]),
                    "loaded": datetime.fromisoformat(w["loaded"]),
                    "txs": [Transaction(**{**t, "date": date.fromisoformat(t["date"])}) for t in w["txs"]],
                }
            except (KeyError, TypeError, ValueError):
                pass
        if n := raw.get("names"):
            data["names"] = {"at": float(n.get("at", 0)), "names": n.get("names") or {}}
        self.data = data

    def _write(self) -> None:
        out: dict = {}
        if w := self.data.get("withdrawals"):
            out["withdrawals"] = {"start": w["start"].isoformat(), "at": w["at"], "loaded": w["loaded"].isoformat(),
                                  "txs": [{**asdict(t), "date": t.date.isoformat()} for t in w["txs"]]}
        if n := self.data.get("names"):
            out["names"] = n
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Zwischenspeicher der Buchungssuche nicht gespeichert: %s", exc)

    # ------------------------------------------------------------------ Zugriff
    def _fresh(self, entry: dict | None) -> bool:
        return bool(entry) and time.time() - entry["at"] < self.max_age

    def withdrawals(self, client, start: date, today: date) -> list[Transaction]:
        hit = self.data.get("withdrawals")
        if self._fresh(hit) and hit["start"] <= start:
            return [t for t in hit["txs"] if t.date >= start]
        txs = list(client.withdrawals(start, today))
        self.data["withdrawals"] = {"start": start, "at": time.time(), "loaded": datetime.now(), "txs": txs}
        self._write()
        return txs

    def names(self, client, marks) -> dict[str, list[str]]:
        hit = self.data.get("names")
        if self._fresh(hit):
            return hit["names"]
        names = {}
        for mark in marks:
            try:
                names[mark] = client.names(mark)
            except Exception:  # noqa: BLE001 – nur Auswahlliste, ohne sie geht die Suche trotzdem
                names[mark] = []
        self.data["names"] = {"at": time.time(), "names": names}
        self._write()
        return names

    @property
    def loaded(self) -> datetime | None:
        return (self.data.get("withdrawals") or {}).get("loaded")

    def clear(self) -> None:
        self.data = {}
        try:
            self.path.unlink()
        except OSError:
            pass
