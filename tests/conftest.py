import os
from pathlib import Path

import pytest

os.environ.setdefault("HYPOVISOR_TIMEZONE", "America/New_York")


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def session(db_url):
    from hypovisor import db as dbmod

    dbmod._engine = None
    dbmod.init_db(db_url)
    with dbmod.session_scope() as s:
        yield s


@pytest.fixture(scope="session")
def synth_dir(tmp_path_factory) -> Path:
    from hypovisor.synth import generate

    d = tmp_path_factory.mktemp("synth")
    generate(d, days=7, seed=3)
    return d


@pytest.fixture()
def client(db_url, tmp_path, monkeypatch):
    from hypovisor import db as dbmod
    from hypovisor.config import settings

    monkeypatch.setattr(settings, "database_url", db_url)
    monkeypatch.setattr(settings, "photo_dir", tmp_path / "photos")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    dbmod._engine = None
    dbmod.init_db(db_url)
    from fastapi.testclient import TestClient

    from hypovisor.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
