"""Artifact model, storage security, hashing, versioning, immutability, reuse, API."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.core.errors import NotFoundError
from app.documents.database.models import DocumentArtifactRow
from app.documents.models import DocumentFormat, DocumentStatus
from app.documents.service import DocumentError, DocumentService
from app.documents.storage import ArtifactStore, StorageError, safe_component, sha256_file
from app.main import app
from app.preparation.service import PreparationService
from tests.conftest import AUTH_HEADERS
from tests.documents.conftest import ready_preparation

# ------------------------------------------------------------------ storage


def test_storage_paths_are_tenant_scoped_and_traversal_safe(tmp_path):
    store = ArtifactStore(str(tmp_path))
    rel = store.relative_path("tenant-a", "co-1", "prep-1", "RESUME", DocumentFormat.PDF, 2)
    assert rel == "tenant-a/co-1/prep-1/resume-v2.pdf"
    assert store.absolute(rel) == (tmp_path / "tenant-a" / "co-1" / "prep-1" / "resume-v2.pdf").resolve()
    for bad in ("../x", "a/b", "..", "", "x" * 200, "tenant\\x", "a;b"):
        with pytest.raises(StorageError):
            safe_component(bad, "id")
    for bad_rel in ("../etc/passwd", "/abs/path", "C:/x", "t/../../x", "t\\x"):
        with pytest.raises(StorageError):
            store.absolute(bad_rel)
    with pytest.raises(StorageError):
        store.relative_path("t", "c", "p", "PASSPORT", DocumentFormat.PDF, 1)


def test_storage_write_once_hash_and_mutation_detection(tmp_path):
    store = ArtifactStore(str(tmp_path))
    rel = store.relative_path("t", "c", "p", "RESUME", DocumentFormat.PDF, 1)
    data = b"%PDF-1.7 fake but well-formed enough for the magic check"
    path = store.write_immutable(rel, data)
    assert path.read_bytes() == data
    with pytest.raises(StorageError):
        store.write_immutable(rel, b"%PDF-1.7 overwrite attempt")
    digest = sha256_file(path)
    assert store.verify(rel, digest, len(data), DocumentFormat.PDF) == []
    assert store.verify(rel, digest, len(data), DocumentFormat.DOCX) == ["file is not a DOCX"]
    path.write_bytes(data + b"\n%tampered")
    problems = store.verify(rel, digest, len(data), DocumentFormat.PDF)
    assert any("size" in p for p in problems) and any("SHA-256" in p for p in problems)
    assert store.verify("t/c/p/resume-v9.pdf", digest, 1, DocumentFormat.PDF) == ["file missing"]
    assert not list(tmp_path.glob("**/.tmp-*")), "no temp files left behind"


# ------------------------------------------------------------------ service


def test_render_reuse_version_invalidate(db_session, tenant_id, opportunities, documents, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    row, created = documents.get_or_render(prep.id, "RESUME", DocumentFormat.PDF)
    assert created and row.version == 1 and row.status == DocumentStatus.ACTIVE.value and row.validation_status == "PASSED"
    assert row.preparation_id == prep.id and row.preparation_version == prep.version and row.evidence_fingerprint == prep.inputs["evidence_fingerprint"]
    assert row.renderer == "fpdf2" and row.renderer_version == "pdf-v1" and row.page_count == 1 and row.byte_size > 1000
    path = docs_root / row.relative_path
    assert path.is_file() and sha256_file(path) == row.content_hash
    assert row.relative_path.startswith(f"{tenant_id}/{prep.candidate_opportunity_id}/{prep.id}/resume-v1.pdf")
    # Unchanged input: reused, no new version, no new file.
    again, created = documents.get_or_render(prep.id, "RESUME", DocumentFormat.PDF)
    assert not created and again.id == row.id and documents.stats == {"rendered": 1, "reused": 1, "invalidated": 0}
    # DOCX is an independent artifact of the same preparation.
    docx_row, _ = documents.get_or_render(prep.id, "RESUME", DocumentFormat.DOCX)
    assert docx_row.format == "DOCX" and docx_row.version == 1 and docx_row.content_hash != row.content_hash
    letter, _ = documents.get_or_render(prep.id, "COVER_LETTER", DocumentFormat.PDF)
    assert letter.relative_path.endswith("cover-letter-v1.pdf")
    # Forced regeneration: v1 invalidated (file kept), v2 is a new immutable file.
    v2 = documents.regenerate(prep.id, "RESUME", DocumentFormat.PDF)
    db_session.refresh(row)
    assert v2.version == 2 and row.status == DocumentStatus.INVALIDATED.value and row.invalidated_reason
    assert path.is_file() and (docs_root / v2.relative_path).is_file()
    assert v2.content_hash == row.content_hash, "same input, same renderer: byte-identical output"
    with pytest.raises(DocumentError):
        documents.materialize(row.id)
    assert documents.materialize(v2.id)[1].endswith("resume-v2.pdf")
    assert len(documents.list_for(prep.id)) == 4


def test_corrupt_file_is_invalidated_and_rerendered(db_session, tenant_id, opportunities, documents, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    row, _ = documents.get_or_render(prep.id, "RESUME")
    (docs_root / row.relative_path).write_bytes(b"%PDF-1.4 corrupted")
    with pytest.raises(DocumentError, match="not intact"):
        documents.materialize(row.id)
    fresh, created = documents.get_or_render(prep.id, "RESUME")
    db_session.refresh(row)
    assert created and fresh.version == 2 and row.status == DocumentStatus.INVALIDATED.value


def test_changed_preparation_or_renderer_makes_a_new_version(db_session, tenant_id, opportunities, documents, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    v1, _ = documents.get_or_render(prep.id, "RESUME")
    # Renderer version participates in identity.
    documents.renderers[DocumentFormat.PDF].version = "pdf-v2-test"
    v2, created = documents.get_or_render(prep.id, "RESUME")
    assert created and v2.version == 2 and v2.renderer_version == "pdf-v2-test"
    documents.renderers[DocumentFormat.PDF].version = "pdf-v1"
    # A new preparation version is a different preparation id: its own artifacts.
    regenerated = PreparationService(db_session, tenant_id, actor="test").prepare(prep.candidate_opportunity_id, force=True)
    db_session.commit()
    assert regenerated.id != prep.id
    with pytest.raises(DocumentError, match="SUPERSEDED"):
        documents.get_or_render(prep.id, "RESUME")
    new, created = documents.get_or_render(regenerated.id, "RESUME")
    assert created and new.version == 1 and new.preparation_id == regenerated.id


def test_no_cover_letter_means_no_document(db_session, tenant_id, opportunities, documents):
    prep = ready_preparation(db_session, tenant_id, opportunities, cover_letter="DISABLED", company="NoLetter")
    with pytest.raises(DocumentError, match="no COVER_LETTER content"):
        documents.get_or_render(prep.id, "COVER_LETTER")
    report = documents.ensure_for_execution(prep)
    assert [a.artifact_type for a in report.artifacts] == ["RESUME"] and report.problems == []


def test_tenant_isolation(db_session, tenant_id, other_tenant_id, opportunities, documents, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    row, _ = documents.get_or_render(prep.id, "RESUME")
    other = DocumentService(db_session, other_tenant_id, actor="b", store=ArtifactStore(str(docs_root)))
    with pytest.raises(NotFoundError):
        other.require(row.id)
    with pytest.raises(NotFoundError):
        other.get_or_render(prep.id, "RESUME")
    assert other.list_for(prep.id) == []
    assert row.relative_path.split("/")[0] == tenant_id != other_tenant_id


def test_audit_never_contains_document_text(db_session, tenant_id, opportunities, documents):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    row, _ = documents.get_or_render(prep.id, "RESUME")
    events = documents.repo.list_audit("document_artifact", row.id)
    assert events and events[0].action == "rendered"
    payload = str(events[0].after)
    assert "Skills:" not in payload and "ribhu@example.com" not in payload and row.content_hash in payload


# ---------------------------------------------------------------------- API

TENANT = f"api-p8-{uuid.uuid4().hex[:8]}"


def test_api_flow(db_session, tenant_id, opportunities, documents, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities)
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        client = TestClient(app, follow_redirects=False)
        assert client.post("/api/v1/documents/render", json={"preparation_id": prep.id}).status_code == 401
        resp = client.post("/api/v1/documents/render", json={"preparation_id": prep.id, "artifact_type": "RESUME", "format": "PDF"}, headers=AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True and body["artifact"]["version"] == 1 and body["artifact"]["format"] == "PDF"
        assert "/" in body["artifact"]["relative_path"] and ":" not in body["artifact"]["relative_path"]
        artifact_id = body["artifact"]["id"]
        assert client.post("/api/v1/documents/render", json={"preparation_id": prep.id}, headers=AUTH_HEADERS).json()["created"] is False
        listed = client.get(f"/api/v1/documents/by-preparation/{prep.id}").json()
        assert [a["id"] for a in listed] == [artifact_id]
        assert client.get(f"/api/v1/documents/{artifact_id}").json()["content_hash"] == body["artifact"]["content_hash"]
        ready = client.post(f"/api/v1/documents/{artifact_id}/materialize", headers=AUTH_HEADERS).json()
        assert ready["ready"] is True and ready["problems"] == []
        download = client.get(f"/api/v1/documents/{artifact_id}/file", headers=AUTH_HEADERS)
        assert download.status_code == 200 and download.content[:4] == b"%PDF" and download.headers["X-Content-SHA256"] == body["artifact"]["content_hash"]
        ensure = client.post(f"/api/v1/documents/ensure/{prep.id}", headers=AUTH_HEADERS).json()
        assert ensure["reused"] == 1 and ensure["created"] == 1 and {a["artifact_type"] for a in ensure["artifacts"]} == {"RESUME", "COVER_LETTER"}
        regenerated = client.post(f"/api/v1/documents/{artifact_id}/regenerate", headers=AUTH_HEADERS).json()
        assert regenerated["artifact"]["version"] == 2
        invalidated = client.post(f"/api/v1/documents/{regenerated['artifact']['id']}/invalidate", json={"reason": "typo in profile"}, headers=AUTH_HEADERS).json()
        assert invalidated["status"] == "INVALIDATED"
        assert client.post(f"/api/v1/documents/{artifact_id}/materialize", headers=AUTH_HEADERS).json()["ready"] is False
        assert client.get("/api/v1/documents/nope").status_code == 404
    finally:
        if previous is not None:
            app.dependency_overrides[get_tenant_id] = previous
        else:
            app.dependency_overrides.pop(get_tenant_id, None)
    assert db_session.query(DocumentArtifactRow).filter_by(tenant_id=tenant_id).count() == 3
