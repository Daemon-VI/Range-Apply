"""Execution uses rendered artifacts: reuse, regeneration, refusal, exact references."""

import pytest

from app.application.models import ApplicationStatus
from app.config import settings
from app.documents.database.models import DocumentArtifactRow
from app.documents.models import DocumentStatus
from app.documents.service import DocumentService
from app.documents.storage import ArtifactStore
from app.execution.models import ExecutorKind
from app.pipeline.models import QueueState


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    monkeypatch.setattr(settings, "documents_root", str(root))
    return root


def test_execution_renders_once_reuses_and_records_exact_artifacts(harness, db_session, docs_root):
    a = harness.ready(company="Alpha")
    b = harness.ready(company="Beta")
    harness.execute(a)
    run = harness.service.runs_for(a.id)[0]
    assert run.resume_artifact_id and run.diagnostics["artifacts"]["RESUME"]["sha256"]
    artifact = db_session.get(DocumentArtifactRow, run.resume_artifact_id)
    assert artifact.preparation_id == a.preparation_id and artifact.version == 1 and artifact.validation_status == "PASSED"
    assert (docs_root / artifact.relative_path).is_file()
    assert run.diagnostics["artifacts"]["RESUME"]["version"] == 1 and run.diagnostics["artifacts"]["RESUME"]["sha256"] == artifact.content_hash
    assert a.status == ApplicationStatus.VERIFIED.value
    # A retry-style second execution of another attempt reuses nothing across preparations,
    # but the same preparation's document is reused (no second file).
    documents = DocumentService(db_session, harness.tenant_id, store=ArtifactStore(str(docs_root)))
    before = db_session.query(DocumentArtifactRow).filter_by(preparation_id=a.preparation_id).count()
    report = documents.ensure_for_execution(documents._preparation(a.preparation_id))
    assert report.created == 0 and report.reused == len(report.artifacts) == 2  # resume + LIGHT cover letter
    assert db_session.query(DocumentArtifactRow).filter_by(preparation_id=a.preparation_id).count() == before
    harness.execute(b, worker="w2")
    assert harness.service.runs_for(b.id)[0].resume_artifact_id != run.resume_artifact_id
    assert harness.service.summary().metrics["submitted"] == 2


def test_invalidated_artifact_is_regenerated_as_a_new_version(harness, db_session, docs_root):
    a = harness.ready(company="Alpha")
    documents = DocumentService(db_session, harness.tenant_id, store=ArtifactStore(str(docs_root)))
    v1, _ = documents.get_or_render(a.preparation_id, "RESUME")
    documents.invalidate(v1.id, "typo fixed in profile")
    db_session.commit()
    harness.execute(a)
    run = harness.service.runs_for(a.id)[0]
    used = db_session.get(DocumentArtifactRow, run.resume_artifact_id)
    assert used.id != v1.id and used.version == 2 and used.status == DocumentStatus.ACTIVE.value
    db_session.refresh(v1)
    assert v1.status == DocumentStatus.INVALIDATED.value and (docs_root / v1.relative_path).is_file(), "history is kept"


def test_corrupt_artifact_is_replaced_before_upload(harness, db_session, docs_root):
    a = harness.ready(company="Alpha")
    documents = DocumentService(db_session, harness.tenant_id, store=ArtifactStore(str(docs_root)))
    v1, _ = documents.get_or_render(a.preparation_id, "RESUME")
    db_session.commit()
    (docs_root / v1.relative_path).write_bytes(b"%PDF-1.4 corrupted by something")
    harness.execute(a)
    run = harness.service.runs_for(a.id)[0]
    used = db_session.get(DocumentArtifactRow, run.resume_artifact_id)
    assert used.version == 2 and a.status == ApplicationStatus.VERIFIED.value
    db_session.refresh(v1)
    assert v1.status == DocumentStatus.INVALIDATED.value and "SHA-256" in v1.invalidated_reason


def test_render_failure_blocks_execution_for_review(harness, db_session, docs_root, monkeypatch):
    a = harness.ready(company="Alpha")
    from app.documents import service as documents_module

    def broken(self, preparation, fmt=None):
        from app.documents.models import RenderReport

        return RenderReport(preparation_id=preparation.id, problems=["RESUME: rendered document is NEEDS_REVIEW: 7 pages exceeds the 4-page bound for RESUME"])

    monkeypatch.setattr(documents_module.DocumentService, "ensure_for_execution", broken)
    outcome = harness.execute(a)
    assert outcome["outcome"] == "document_failed"
    assert a.status == ApplicationStatus.NEEDS_REVIEW.value and "7 pages" in a.status_reason
    assert harness.item(a).state == QueueState.NEEDS_REVIEW.value and harness.mock.submit_calls[a.id] == 0
    assert harness.service.runs_for(a.id) == []


def test_package_carries_artifact_paths_and_hashes(harness, db_session, docs_root):
    harness.ready(company="Alpha")  # HIGH band: the policy's LIGHT cover letter applies
    (mine,) = harness.service.claim("w1", limit=1)
    run, package, outcome = harness.service.start(mine, "w1", ExecutorKind.MOCK)
    assert outcome["outcome"] == "started"
    files = package.execution_config["artifact_files"]
    refs = package.execution_config["artifacts"]
    assert set(files) == {"RESUME", "COVER_LETTER"} and files["RESUME"].endswith("resume-v1.pdf") and files["COVER_LETTER"].endswith("cover-letter-v1.pdf")
    assert refs["RESUME"]["sha256"] and refs["RESUME"]["format"] == "PDF" and refs["RESUME"]["id"] == run.resume_artifact_id
    assert refs["COVER_LETTER"]["id"] == run.cover_letter_artifact_id
    assert str(docs_root) in files["RESUME"]
