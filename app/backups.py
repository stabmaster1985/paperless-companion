"""Sicherungen der Companion-Datenbank: anlegen (täglich automatisch oder von Hand), aufräumen, wiederherstellen.

Eine Sicherung ist eine vollständige SQLite-Kopie (Backup-API, auch im laufenden Betrieb konsistent). Sie enthält deine
Arbeit im Companion, keine Dokumente (die liegen in Paperless) und keine Zugangsdaten aus der .env.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import Settings

PATTERN = re.compile(r"^companion-(\d{4}-\d{2}-\d{2})-(\d{4})(?:-([a-z-]+))?\.db$")
REQUIRED_TABLES = {"documents", "kv", "invoices", "contracts"}


class BackupError(Exception):
    pass


@dataclass
class Backup:
    path: Path
    created: datetime
    reason: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def folder(settings: Settings) -> Path:
    path = Path(settings.backup_dir) if settings.backup_dir else settings.data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database(settings: Settings) -> Path:
    return settings.data_dir / "companion.db"


def _copy(source: Path, target: Path) -> None:
    src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create(settings: Settings, reason: str = "auto", now: datetime | None = None) -> Backup:
    now = now or datetime.now()
    suffix = "" if reason == "auto" else f"-{reason}"
    target = folder(settings) / f"companion-{now:%Y-%m-%d-%H%M}{suffix}.db"
    if target.exists():
        target = target.with_name(target.stem + f"-{now:%S}.db")
    _copy(database(settings), target)
    prune(settings)
    return Backup(target, now, reason, target.stat().st_size)


def all_backups(settings: Settings) -> list[Backup]:
    items = []
    for path in folder(settings).glob("companion-*.db"):
        m = PATTERN.match(path.name)
        if not m:
            continue
        created = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M")
        items.append(Backup(path, created, m.group(3) or "auto", path.stat().st_size))
    return sorted(items, key=lambda b: b.created, reverse=True)


def prune(settings: Settings) -> int:
    """Nur die letzten `backup_keep` Sicherungen behalten (Sicherungen vor einer Wiederherstellung zählen mit)."""
    keep = max(1, settings.backup_keep or 1)
    removed = 0
    for old in all_backups(settings)[keep:]:
        old.path.unlink(missing_ok=True)
        removed += 1
    return removed


def latest(settings: Settings) -> Backup | None:
    items = all_backups(settings)
    return items[0] if items else None


def validate(path: Path) -> None:
    """Ist das eine Companion-Datenbank? Sonst BackupError mit verständlicher Meldung."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Keine lesbare Datenbank: {exc}") from exc
    if ok != "ok":
        raise BackupError(f"Datenbank beschädigt: {ok}")
    missing = REQUIRED_TABLES - tables
    if missing:
        raise BackupError("Das ist keine Sicherung des Companions (Tabellen fehlen: " + ", ".join(sorted(missing)) + ")")


def restore(settings: Settings, source: Path) -> Backup:
    """Sicherung zurückspielen: prüfen, vorher den jetzigen Stand sichern, dann die laufende Datenbank ersetzen und auf den
    aktuellen Stand der Tabellen bringen. Gibt die Sicherung des vorherigen Stands zurück."""
    from app import db

    validate(source)
    before = create(settings, "vor-wiederherstellung")
    engine = db.engine()
    engine.dispose()  # offene Verbindungen schließen
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        src = sqlite3.connect(str(source))
        try:
            src.backup(raw)
        finally:
            src.close()
    engine.dispose()
    db.migrate(engine)  # ältere Sicherung: neue Spalten/Tabellen ergänzen
    return before
