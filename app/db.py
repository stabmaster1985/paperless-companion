from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_db(url: str) -> Engine:
    global _engine, _session_factory
    # timeout: bis zu 30 s warten, falls der Hintergrund-Worker gerade schreibt
    _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    from app import models  # noqa: F401  (Tabellen registrieren)

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _allow_contracts_without_document(_engine)
    _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Minimale Migration: neue Spalten bestehender Tabellen per ALTER TABLE ergänzen."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                if column.server_default is not None:
                    ddl += f" DEFAULT {column.server_default.arg}"
                conn.execute(text(ddl))


def _allow_contracts_without_document(engine: Engine) -> None:
    """Ältere Datenbanken: contracts.document_id war Pflicht – Tabelle einmalig neu anlegen (Daten bleiben erhalten),
    damit es Abos ohne Dokument geben kann. SQLite kann NOT NULL nicht per ALTER entfernen."""
    columns = {c["name"]: c for c in inspect(engine).get_columns("contracts")}
    if not columns.get("document_id") or columns["document_id"].get("nullable", True):
        return
    table = Base.metadata.tables["contracts"]
    names = ", ".join(f'"{c}"' for c in columns if c in table.columns)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text('ALTER TABLE contracts RENAME TO contracts_old'))
        for index in inspect(engine).get_indexes("contracts_old"):
            conn.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
        table.create(conn)
        conn.execute(text(f"INSERT INTO contracts ({names}) SELECT {names} FROM contracts_old"))
        conn.execute(text("DROP TABLE contracts_old"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def release(session: Session) -> None:
    """Bisherige Änderungen speichern, bevor etwas Langsames kommt (AI-Antwort, Firefly- oder Paperless-Abruf).
    SQLite erlaubt nur einen Schreiber: Eine offene Schreib-Transaktion während einer langen Wartezeit sperrt sonst
    alle anderen Aktionen („database is locked“)."""
    if session.new or session.dirty or session.deleted or session.in_transaction():
        session.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        raise RuntimeError("Datenbank nicht initialisiert")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI-Dependency."""
    with session_scope() as session:
        yield session
