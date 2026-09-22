"""Applications now carry tenant + opportunity, unique together (migration c4d2f8a1e6b3)."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.application.database.models import ApplicationRow
from app.database import get_engine
from app.pipeline.repository import OpportunityRepository


def test_applications_table_has_opportunity_columns_after_migration():
    columns = {c["name"] for c in inspect(get_engine()).get_columns("applications")}
    assert {"tenant_id", "opportunity_id"} <= columns
    uniques = {tuple(u["column_names"]) for u in inspect(get_engine()).get_unique_constraints("applications")}
    assert ("tenant_id", "opportunity_id") in uniques


def test_one_application_per_tenant_and_opportunity(db_session, jobs, tenant_id):
    a = jobs.make(source="GREENHOUSE", location="Remote")
    b = jobs.make(source="LEVER", location="Remote - US", remote_type="UNKNOWN")
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, a)
    opp_b, _, _ = OpportunityRepository.resolve_opportunity(db_session, b)
    assert opp.id == opp_b.id
    db_session.commit()

    first = ApplicationRow(job_id=a.id, tenant_id=tenant_id, opportunity_id=opp.id, status="DISCOVERED")
    db_session.add(first)
    db_session.commit()
    db_session.add(ApplicationRow(job_id=b.id, tenant_id=tenant_id, opportunity_id=opp.id, status="DISCOVERED"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.delete(db_session.get(ApplicationRow, first.id))
    db_session.commit()
