"""TruthValidator stays the gate; structural checks catch invented numbers, dates, skills."""

from app.preparation.evidence import EvidenceSnapshot
from app.preparation.models import Block, BlockKind
from app.preparation.validator import PreparationValidator


def _claim(text, keys):
    return Block(kind=BlockKind.CLAIM, section="projects", text=text, evidence_keys=keys)


def test_supported_claim_passes_and_framing_is_not_checked(evidence):
    validator = PreparationValidator(EvidenceSnapshot.load(evidence))
    node = validator.snapshot.nodes["ticket-engine"]
    assert validator.validate_block(_claim(f"Ticket Engine: {node.claim}", ["ticket-engine"])) == []
    framing = Block(kind=BlockKind.FRAMING, section="summary", text="I shipped 40 systems in 1999 with Kubernetes.")
    assert validator.validate_block(framing) == []


def test_invented_number_year_and_skill_are_rejected(evidence):
    validator = PreparationValidator(EvidenceSnapshot.load(evidence))
    codes = {i.code for i in validator.validate_block(_claim("Ticket Engine handled 50,000 users per second.", ["ticket-engine"]))}
    assert "unsupported_number" in codes
    codes = {i.code for i in validator.validate_block(_claim("Built the Ticket Engine in 2019.", ["ticket-engine"]))}
    assert "unsupported_date" in codes
    codes = {i.code for i in validator.validate_block(_claim("Ticket Engine runs on Kubernetes.", ["ticket-engine"]))}
    assert "unsupported_skill" in codes
    # Numbers and skills that ARE in the cited evidence pass.
    assert validator.validate_block(_claim("Ticket Engine uses Redis and MySQL with Go.", ["ticket-engine"])) == []


def test_unsafe_removed_unresolved_and_missing_evidence(evidence):
    evidence.remove_node("skill-docker", actor="test")
    evidence.commit()
    validator = PreparationValidator(EvidenceSnapshot.load(evidence))
    assert validator.validate_block(_claim("Anything", []))[0].code == "missing_evidence"
    assert validator.validate_block(_claim("Docker", ["skill-docker"]))[0].code == "removed_evidence"
    assert validator.validate_block(_claim("Nope", ["skill-nope"]))[0].code == "unresolved_evidence"
    java = validator.validate_block(_claim("Java", ["skill-java"]))
    assert java and java[0].code == "truth_validator_rejected", "UNVERIFIED evidence is refused by TruthValidator itself"
    inferred = validator.validate_block(_claim("Located in Hyderabad, India", ["fact-inferred-location"]))
    assert inferred[0].code == "truth_validator_rejected"


def test_drop_failing_keeps_valid_blocks_and_reports(evidence):
    validator = PreparationValidator(EvidenceSnapshot.load(evidence))
    blocks = [
        Block(kind=BlockKind.FRAMING, section="summary", text="framing"),
        _claim("Ticket Engine uses Redis.", ["ticket-engine"]),
        _claim("Ticket Engine served 9,999,999 users.", ["ticket-engine"]),
    ]
    kept, report = validator.drop_failing(blocks)
    assert len(kept) == 2 and not report.passed and report.dropped_blocks == 1
    assert report.issues[0].code == "unsupported_number"


def test_a_number_followed_by_a_comma_is_still_that_number(evidence):
    # First real dry run (Notion): "FastAPI, SQLAlchemy 2, Alembic" was read as the number "2,", which the
    # evidence ("SQLAlchemy 2") does not contain, and a truthful block was dropped.
    from app.preparation.evidence import NUMBER_PATTERN

    assert NUMBER_PATTERN.findall("fastapi, sqlalchemy 2, alembic") == ["2"]
    assert NUMBER_PATTERN.findall("handled 50,000 users, 99.9% uptime, 5+ services") == ["50,000", "99.9%", "5+"]
    validator = PreparationValidator(EvidenceSnapshot.load(evidence))
    node = validator.snapshot.nodes["ticket-engine"]
    numbers = sorted(validator.snapshot.node_numbers.get("ticket-engine", set()))
    if numbers:
        assert validator.validate_block(_claim(f"{node.label} reached {numbers[0]}, as recorded.", ["ticket-engine"])) == []
    codes = {i.code for i in validator.validate_block(_claim("Ticket Engine handled 50,000, users.", ["ticket-engine"]))}
    assert "unsupported_number" in codes, "an invented number is still rejected with a trailing comma"
