"""Removed workbenches must stay absent without deleting historical data."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import db, main
from app.db import Base


REMOVED_TABLES = (
    "sub2api_relogin_jobs",
    "sub2api_relogin_items",
    "link_extraction_jobs",
    "link_extraction_items",
)


def test_removed_apis_are_not_registered():
    paths = main.app.openapi()["paths"]
    assert not any(
        path.startswith(("/api/sub2api/relogin", "/api/link-extraction"))
        for path in paths
    )
    assert "/api/sub2api/upload" in paths
    assert "/api/accounts/oauth/jobs" in paths
    client = TestClient(main.app)
    for path in ("/api/sub2api/relogin/jobs", "/api/link-extraction/jobs"):
        assert client.get(path).status_code == 404


def test_fresh_database_does_not_create_removed_tables():
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        db._migrate_legacy_tables(engine)
        tables = set(inspect(engine).get_table_names())
        assert not tables.intersection(REMOVED_TABLES)
        assert {"accounts", "registrations", "account_sub2api_uploads"} <= tables
    finally:
        engine.dispose()


@pytest.mark.parametrize("keep_history", [False, True])
def test_startup_cleanup_preserves_legacy_data(monkeypatch, keep_history):
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        if keep_history:
            with engine.begin() as connection:
                for table in REMOVED_TABLES:
                    connection.execute(
                        text(
                            f"CREATE TABLE IF NOT EXISTS {table} "
                            "(id INTEGER PRIMARY KEY, marker TEXT)"
                        )
                    )
                    connection.execute(
                        text(f"INSERT INTO {table} (id, marker) VALUES (1, 'history')")
                    )
        monkeypatch.setattr(db, "engine", engine)
        db.init_db()
        main._cleanup_stale_registrations()
        if keep_history:
            with engine.connect() as connection:
                for table in REMOVED_TABLES:
                    marker = connection.execute(
                        text(f"SELECT marker FROM {table} WHERE id=1")
                    ).scalar_one()
                    assert marker == "history"
                    assert {
                        column["name"]
                        for column in inspect(connection).get_columns(table)
                    } == {"id", "marker"}
    finally:
        engine.dispose()
