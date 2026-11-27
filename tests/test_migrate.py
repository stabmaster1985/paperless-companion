"""Migration älterer Datenbanken: fehlende Spalten werden ergänzt – auch mit Text-Standardwerten (z.B. leerer Text)."""

import sqlite3

from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401 – Tabellen registrieren
from app.db import Base, _sql_default, migrate


def test_sql_default_quotes_text():
    assert _sql_default("") == "''" and _sql_default("0") == "0" and _sql_default("3") == "3"
    assert _sql_default("open") == "'open'" and _sql_default("it's") == "'it''s'"


def test_old_database_gets_new_columns(tmp_path):
    """Jede Spalte mit Standardwert fehlt in der „alten“ Datenbank – der Start ergänzt sie alle."""
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    dropped = []
    con = sqlite3.connect(path)
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.server_default is None or column.primary_key:
                continue
            try:
                con.execute(f'ALTER TABLE "{table.name}" DROP COLUMN "{column.name}"')
                dropped.append((table.name, column.name))
            except sqlite3.OperationalError:
                pass  # z.B. indizierte Spalte – nicht entfernbar, dann eben nicht prüfen
    con.commit()
    con.close()
    assert ("contracts", "category") in dropped
    engine = create_engine(f"sqlite:///{path}")
    migrate(engine)
    insp = inspect(engine)
    for table, column in dropped:
        assert column in {c["name"] for c in insp.get_columns(table)}, (table, column)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO contracts (name, renewal_mode, notice_unit, notice_to, status) "
                             "VALUES ('Test', 'renews', 'months', 'term_end', 'active')")
        assert conn.exec_driver_sql("SELECT category FROM contracts").scalar() == ""
