import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import backups
from app.db import session_scope
from app.main import create_app
from app.models import Contract, Document
from tests.test_pipeline import run_all


@pytest.fixture
def client(settings, mock_api):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        run_all(session_scope, settings)
        yield c


def test_create_prune_and_list(client, settings):
    settings.backup_keep = 3
    start = datetime(2026, 11, 1, 3, 0)
    for i in range(5):
        backups.create(settings, "auto", start + timedelta(days=i))
    items = backups.all_backups(settings)
    assert [b.created.day for b in items] == [5, 4, 3]  # nur die letzten 3
    assert "Es gibt noch keine Sicherung" not in client.get("/").text


def test_dashboard_warns_without_backup(client):
    assert "Es gibt noch keine Sicherung" in client.get("/").text


def test_restore_brings_back_old_state_and_keeps_current(client, settings):
    with session_scope() as s:
        s.add(Contract(name="Vor der Sicherung", payee="A"))
    r = client.post("/local-data/backup-now", follow_redirects=False)
    assert "Sicherung angelegt" in r.headers["location"] or r.status_code == 303
    saved = backups.latest(settings)
    with session_scope() as s:
        s.add(Contract(name="Nach der Sicherung", payee="B"))
    r = client.post(f"/local-data/restore/{saved.name}")
    assert "wiederhergestellt" in r.text
    with session_scope() as s:
        names = {c.name for c in s.query(Contract)}
        assert "Vor der Sicherung" in names and "Nach der Sicherung" not in names
        assert s.query(Document).count() > 0  # App läuft weiter
    before = next(b for b in backups.all_backups(settings) if b.reason == "vor-wiederherstellung")
    con = sqlite3.connect(before.path)
    assert ("Nach der Sicherung",) in con.execute("SELECT name FROM contracts").fetchall()  # vorheriger Stand gesichert
    con.close()


def test_upload_rejects_foreign_files(client, tmp_path):
    bad = tmp_path / "x.db"
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE fremd (id INTEGER)")
    con.commit()
    con.close()
    r = client.post("/local-data/restore-upload", files={"file": ("x.db", bad.read_bytes())})
    assert "keine Sicherung des Companions" in r.text
    r = client.post("/local-data/restore-upload", files={"file": ("y.db", b"kein sqlite")})
    assert "Nicht wiederhergestellt" in r.text
    with session_scope() as s:
        assert s.query(Document).count() > 0  # nichts verändert


def test_restore_older_backup_gets_new_columns(client, settings, tmp_path):
    made = backups.create(settings, "manuell")
    old = tmp_path / "alt.db"
    old.write_bytes(made.path.read_bytes())
    con = sqlite3.connect(old)
    con.execute("ALTER TABLE invoices DROP COLUMN reminders")  # wie eine Sicherung von vor den Mahnungen
    con.commit()
    con.close()
    r = client.post("/local-data/restore-upload", files={"file": ("alt.db", old.read_bytes())})
    assert "wiederhergestellt" in r.text
    assert client.get("/invoices").status_code == 200  # Spalte wieder da
