"""Fixtures for Evidence Graph tests.

Every test gets its own tenant in the shared, migrated test database (see
``tests/conftest.py``), so tests are isolated from each other and from the
default tenant the rest of the suite bootstraps. Teardown deletes the tenant;
``ON DELETE CASCADE`` removes everything it owned.
"""

import json
import uuid

import pytest

from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.repository import EvidenceRepository
from app.config import settings
from app.database import get_session_factory


@pytest.fixture
def db_session():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _new_tenant_id() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


def _drop_tenant(session, tenant_id: str) -> None:
    row = session.get(TenantRow, tenant_id)
    if row is not None:
        session.delete(row)
        session.commit()


@pytest.fixture
def tenant_id(db_session):
    tid = _new_tenant_id()
    yield tid
    db_session.rollback()
    _drop_tenant(db_session, tid)


@pytest.fixture
def other_tenant_id(db_session):
    tid = _new_tenant_id()
    yield tid
    db_session.rollback()
    _drop_tenant(db_session, tid)


@pytest.fixture
def repo(db_session, tenant_id) -> EvidenceRepository:
    repository = EvidenceRepository(db_session, tenant_id)
    repository.ensure_tenant("Test tenant")
    db_session.commit()
    return repository


@pytest.fixture
def other_repo(db_session, other_tenant_id) -> EvidenceRepository:
    repository = EvidenceRepository(db_session, other_tenant_id)
    repository.ensure_tenant("Other tenant")
    db_session.commit()
    return repository


@pytest.fixture(scope="session")
def seed_path():
    return settings.career_data_path


@pytest.fixture
def seed_data(seed_path) -> dict:
    with open(seed_path, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def imported_repo(repo, seed_path) -> EvidenceRepository:
    SeedImporter(repo).import_file(seed_path)
    return repo
