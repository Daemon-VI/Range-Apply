"""Backend audit (2026-09-14): a rolled-back render must not wedge a preparation's documents.

Reproduced on a copy of the real database: the write-once file of a render whose
transaction was rolled back stayed on disk with no row, the next render computed
the same version and failed with "artifact already exists", and so did every
later render of that preparation (execution start included).
"""

from app.documents.models import DocumentFormat
from app.documents.service import DocumentService
from tests.documents.conftest import ready_preparation


def test_a_rolled_back_render_does_not_block_the_next_one(db_session, tenant_id, opportunities, documents: DocumentService, docs_root):
    prep = ready_preparation(db_session, tenant_id, opportunities, cover_letter="DISABLED")
    prep_id = prep.id
    first, created = documents.get_or_render(prep_id, "RESUME", DocumentFormat.PDF)
    assert created and first.version == 1
    orphan = documents.store.absolute(first.relative_path)
    orphan_bytes = orphan.read_bytes()
    db_session.rollback()  # e.g. a later failure in the same request
    assert documents.active(prep_id, "RESUME", DocumentFormat.PDF) is None and orphan.exists()

    again, created = documents.get_or_render(prep_id, "RESUME", DocumentFormat.PDF)
    db_session.commit()
    assert created and again.version == 2
    assert again.relative_path != first.relative_path
    assert orphan.read_bytes() == orphan_bytes, "an orphaned write-once file is never overwritten"
    _, path = documents.materialize(again.id)
    assert path.endswith("resume-v2.pdf")
    reused, created = documents.get_or_render(prep_id, "RESUME", DocumentFormat.PDF)
    assert not created and reused.id == again.id
