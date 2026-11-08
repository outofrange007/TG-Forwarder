"""PostgreSQL isolation for the test suite."""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    """Give each test its own PostgreSQL schema.

    All Store instances created during one test share the schema.
    The schema is deleted when the test finishes.
    """
    test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()

    if not test_database_url:
        pytest.fail(
            "TEST_DATABASE_URL is not set. "
            "The test suite requires a dedicated PostgreSQL database."
        )

    schema = f"test_{uuid.uuid4().hex}"

    with psycopg.connect(test_database_url, autocommit=True) as admin:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        # Prevent accidental use of an application database.
        if database_name != "forwarder_test":
            pytest.fail(
                "Tests must use a dedicated database named 'forwarder_test'; "
                f"connected to {database_name!r} instead."
            )

        admin.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )

        try:
            # Store reads DATABASE_URL; its path argument is not used by PostgreSQL.
            isolated_url = make_conninfo(
                test_database_url,
                options=f"-c search_path={schema}",
            )
            monkeypatch.setenv("DATABASE_URL", isolated_url)

            yield
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )