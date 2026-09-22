"""Operational diagnostics: counts only, tenant-scoped, warnings for the
states an operator must act on, and the dashboard page."""

from app.execution.executors import mock as m
from tests.conftest import TEST_API_KEY
from tests.hardening.conftest import begin, expire_lease


def test_diagnostics_reports_queue_leases_uncertain_and_caps(client, db_session, tenant_id, scheduler, opportunities, answered_bank):
    from tests.execution.conftest import scripted

    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN)
    a = h.ready(company="Diag A")
    b = h.ready(company="Diag B")
    h.execute(a)
    item, run, _ = begin(h, b, worker="dead")
    expire_lease(db_session, item)
    db_session.commit()
    d = client.get("/api/v1/ops/diagnostics").json()
    assert d["tenant_id"] == tenant_id and d["queue"]["stale_leases"] == 1 and d["executions"]["running"] == 1 and d["attempts"]["uncertain"] == 1
    assert d["caps"]["day"]["used"] == 2 and d["caps"]["day"]["cap"] == 50 and d["ai"]["gateway"]["provider_calls"] == 0
    assert d["executions"]["last_24h"].get("execution:unknown") == 1 and d["signals"]["review_queue"] >= 1
    assert any("lease" in w for w in d["warnings"]) and any("UNCERTAIN" in w for w in d["warnings"])
    assert "excerpt" not in d and "prompt" not in str(d).lower() and "resume" not in str(d).lower()
    assert client.get("/dashboard/ops").status_code == 401
    client.get("/dashboard/", params={"key": TEST_API_KEY})
    page = client.get("/dashboard/ops").text
    assert "Operational diagnostics" in page and "warning" in page and "stale leases 1" in page


def test_diagnostics_is_tenant_scoped(client, harness, other_tenant_id):
    from tests.hardening.conftest import as_tenant

    harness.ready(company="Scoped Diag")
    as_tenant(other_tenant_id)
    d = client.get("/api/v1/ops/diagnostics").json()
    assert d["tenant_id"] == other_tenant_id and d["attempts"]["by_status"] == {} and d["queue"]["by_state"] == {}
    as_tenant(harness.tenant_id)
    assert client.get("/api/v1/ops/diagnostics").json()["attempts"]["by_status"].get("READY") == 1
