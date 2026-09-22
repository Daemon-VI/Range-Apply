"""CareerBrainService must expose the same data from the database as it did
from the JSON file, so every Phase 2-5 consumer keeps working unchanged."""

from app.career.importer import SeedImporter
from app.intelligence.services.factory import build_orchestrator
from app.services.career_brain import CareerBrainService
from app.tailoring.evidence_selector import _resolve_evidence_text


def _dump(models, exclude=()):
    return [m.model_dump(exclude=set(exclude)) for m in models]


def test_db_mode_matches_json_mode(repo, db_session, seed_path):
    SeedImporter(repo).import_file(seed_path)

    legacy = CareerBrainService(data_path=seed_path)
    legacy.load()
    db = CareerBrainService(db=db_session, tenant_id=repo.tenant_id)
    db.load()

    assert legacy.mode == "json" and db.mode == "db"
    assert db.tenant_id == repo.tenant_id
    assert db.get_profile() == legacy.get_profile()
    assert db.get_preferences() == legacy.get_preferences()
    assert _dump(db.get_skills()) == _dump(legacy.get_skills())
    assert _dump(db.get_projects()) == _dump(legacy.get_projects())
    assert _dump(db.get_experience()) == _dump(legacy.get_experience())
    assert _dump(db.get_achievements()) == _dump(legacy.get_achievements())
    assert _dump(db.get_facts(), exclude=("created_at", "updated_at")) == _dump(
        legacy.get_facts(), exclude=("created_at", "updated_at")
    )
    assert db.get_career_summary() == legacy.get_career_summary()
    assert [p.id for p in db.find_relevant_projects("Backend Engineer")] == [
        p.id for p in legacy.find_relevant_projects("Backend Engineer")
    ]
    assert [p.id for p in db.search_projects("Redis")] == [
        p.id for p in legacy.search_projects("Redis")
    ]


def test_default_service_bootstraps_default_tenant_from_seed():
    service = CareerBrainService()
    service.load()
    assert service.mode == "db"
    assert service.get_profile().name == "Ribhu Siripurapu"
    assert service.get_project("ticket-engine").name == "Ticket Engine"
    assert any(f.id == "fact-github" for f in service.get_needs_review_facts())


def test_reload_sees_writes(repo, db_session, seed_path):
    from app.career.models import EvidenceKind, EvidenceNodeCreate

    SeedImporter(repo).import_file(seed_path)
    service = CareerBrainService(db=db_session, tenant_id=repo.tenant_id)
    service.load()
    before = len(service.get_skills())

    repo.create_node(
        EvidenceNodeCreate(key="skill-zig", kind=EvidenceKind.SKILL, label="Zig", claim="Zig"),
        actor="test",
    )
    repo.commit()
    assert len(service.get_skills()) == before  # snapshot until reload
    service.reload()
    assert len(service.get_skills()) == before + 1


def test_phase3_and_phase4_evidence_references_still_resolve(repo, db_session, seed_path):
    SeedImporter(repo).import_file(seed_path)
    service = CareerBrainService(db=db_session, tenant_id=repo.tenant_id)
    service.load()

    # Phase 4's evidence selector resolves skill/project ids: those are node keys now.
    assert _resolve_evidence_text("skill-python", service) == ("Python", "Multiple projects")
    name, text = _resolve_evidence_text("ticket-engine", service)
    assert name == "Ticket Engine" and "Redis" in text
    assert _resolve_evidence_text("no-such-key", service) is None

    # Phase 3 builds its whole object graph on the service without changes.
    orchestrator = build_orchestrator(career_brain=service)
    resolved = orchestrator.evidence_resolver.resolve_skill("Python")
    assert resolved.strength.value == "DIRECT_VERIFIED"
    assert "skill-python" in resolved.references
