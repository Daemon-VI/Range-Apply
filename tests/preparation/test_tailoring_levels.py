"""L0/L1 deterministic and zero-AI; L2 optional AI that cannot bypass the truth gate."""

from app.pipeline.models import ApplicationPolicyUpdate, TailoringLevel
from app.pipeline.repository import PolicyRepository
from app.preparation.models import ArtifactKind, PreparationStatus
from app.preparation.service import PreparationService


class FaithfulPolisher:
    name = "fake-faithful"
    model = "fake-1"

    def __init__(self):
        self.calls = 0

    def polish(self, text, supporting_text, purpose):
        self.calls += 1
        return text.replace("Skills:", "Core skills:") if text.startswith("Skills:") else text + " (refined)"


class FabricatingPolisher:
    name = "fake-liar"
    model = "fake-1"

    def __init__(self):
        self.calls = 0

    def polish(self, text, supporting_text, purpose):
        self.calls += 1
        return text + " Scaled to 5,000,000 users on Kubernetes in 2015."


class CrashingPolisher:
    name = "fake-crash"
    model = None

    def __init__(self):
        self.calls = 0

    def polish(self, text, supporting_text, purpose):
        self.calls += 1
        raise RuntimeError("provider down")


def _resume(prep):
    return next(a for a in prep.artifacts if a.artifact_type == ArtifactKind.RESUME.value)


def test_l0_is_deterministic_and_zero_ai(service, opportunities):
    co = opportunities.make(fit_score=30)  # LOW band -> L0 by policy
    first = service.prepare(co.id)
    assert first.tailoring_level == "L0" and first.ai_calls == 0 and not first.ai_used
    assert first.status == PreparationStatus.READY.value
    resume = _resume(first)
    assert "Python" in resume.content and resume.evidence_keys
    assert all(b["evidence_keys"] for b in resume.blocks if b["kind"] == "CLAIM")
    again = service.prepare(co.id, force=True)
    assert _resume(again).content == resume.content, "same inputs, same package"
    assert again.version == 2 and again.input_fingerprint == first.input_fingerprint
    assert not any(a.artifact_type == ArtifactKind.COVER_LETTER.value for a in first.artifacts), "LOW band: no letter"


def test_l1_orders_by_matched_requirements(service, opportunities):
    go_first = opportunities.make(title="Go Engineer", fit_score=60, requirements=[("Go", ["skill-go"], 40.0), ("Python", ["skill-python"], 10.0)])
    py_first = opportunities.make(title="Python Engineer", fit_score=60, requirements=[("Python", ["skill-python"], 40.0), ("Go", ["skill-go"], 10.0)])
    a = service.prepare(go_first.id)
    b = service.prepare(py_first.id)
    assert a.tailoring_level == "L1" and a.ai_calls == 0
    skills_a = next(bl for bl in _resume(a).blocks if bl["section"] == "skills")["text"]
    skills_b = next(bl for bl in _resume(b).blocks if bl["section"] == "skills")["text"]
    assert skills_a.startswith("Skills: Go, Python") and skills_b.startswith("Skills: Python, Go")
    assert "Ticket Engine" in _resume(a).content
    letter = next(x for x in a.artifacts if x.artifact_type == ArtifactKind.COVER_LETTER.value)
    assert "Go Engineer" in letter.content and "Dear" in letter.content
    assert "admired" not in letter.content.lower()


def test_l2_with_stub_provider_makes_no_calls(service, opportunities):
    co = opportunities.make(fit_score=90)  # HIGH -> L2
    prep = service.prepare(co.id)
    assert prep.tailoring_level == "L2" and prep.ai_calls == 0 and not prep.ai_used and prep.ai_provider is None
    assert prep.status == PreparationStatus.READY.value


def test_l2_faithful_polish_is_recorded_and_fabrication_is_rejected(db_session, tenant_id, answered_bank, opportunities):
    co = opportunities.make(fit_score=90)
    faithful = PreparationService(db_session, tenant_id, polisher=FaithfulPolisher(), actor="test")
    prep = faithful.prepare(co.id)
    assert prep.ai_used and prep.ai_calls > 0 and prep.ai_provider == "fake-faithful" and prep.ai_model == "fake-1"
    assert any(b["ai_polished"] for b in _resume(prep).blocks)
    assert "Core skills:" in _resume(prep).content
    assert prep.validation_status == "PASSED" and prep.status == PreparationStatus.READY.value

    liar = PreparationService(db_session, tenant_id, polisher=FabricatingPolisher(), actor="test")
    prep2 = liar.prepare(co.id, force=True)
    assert prep2.ai_calls > 0 and not prep2.ai_used, "every rewrite failed the truth gate"
    assert "5,000,000" not in _resume(prep2).content and "Kubernetes" not in _resume(prep2).content
    assert not any(b["ai_polished"] for b in _resume(prep2).blocks)
    assert prep2.validation_status == "PASSED"


def test_provider_failure_falls_back_to_deterministic(db_session, tenant_id, answered_bank, opportunities):
    co = opportunities.make(fit_score=90)
    service = PreparationService(db_session, tenant_id, polisher=CrashingPolisher(), actor="test")
    try:
        prep = service.prepare(co.id)
    except RuntimeError:
        prep = None
    # The polisher itself raised; the service must still deliver a package.
    assert prep is not None and prep.status == PreparationStatus.READY.value and not prep.ai_used


def test_tailoring_level_never_changes_eligibility(service, opportunities, db_session, tenant_id):
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(tailoring_by_band={"HIGH": TailoringLevel.L0, "MEDIUM": TailoringLevel.L0, "LOW": TailoringLevel.L0}), "test")
    db_session.commit()
    high = opportunities.make(fit_score=95)
    low = opportunities.make(title="QA Engineer", fit_score=20)
    service.context(refresh=True)
    for co in (high, low):
        prep = service.prepare(co.id)
        assert prep.tailoring_level == "L0" and prep.status == PreparationStatus.READY.value
        db_session.refresh(co)
        assert co.eligibility_status == "ELIGIBLE" and co.state == "PREPARED"
