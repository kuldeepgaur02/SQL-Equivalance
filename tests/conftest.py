import psycopg2
import pytest

from cexgen.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


def pytest_collection_modifyitems(config, items):
    """Skip tests marked `db` when Postgres is not reachable, instead of failing them."""
    s = Settings.from_env()
    try:
        psycopg2.connect(s.dsn, connect_timeout=2).close()
        return
    except psycopg2.OperationalError:
        pass
    skip = pytest.mark.skip(reason=f"Postgres not reachable (CEX_DSN); start it with: docker compose up -d")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def model_of(settings):
    """model_of(ddl) -> SchemaModel, built in a throwaway database."""
    from cexgen.db import SchemaWorkspace
    from cexgen.schema import read_schema

    def build(ddl: str):
        with SchemaWorkspace(ddl, settings) as ws:
            return read_schema(ws.conn, ws.inventory)
    return build
