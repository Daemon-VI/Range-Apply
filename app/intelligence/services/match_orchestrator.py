import uuid
from typing import List, Optional
from datetime import datetime

from app.jobs.models.job import NormalizedJob
from app.intelligence.models.job_match import JobMatch, MatchRunInfo
from app.intelligence.models.enums import ConfidenceLevel
from app.intelligence.models.requirements import RequirementAssessment, MatchStatus, EvidenceStrength

from app.intelligence.services.evidence_resolver import EvidenceResolver
from app.intelligence.services.requirement_interpreter import RequirementInterpreter
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.intelligence.services.fit_scoring_engine import FitScoringEngine
from app.intelligence.services.explanation_generator import ExplanationGenerator

class MatchOrchestrator:
    """Connects Phase 2 Job Discovery with Phase 1 Career Brain to produce Phase 3 Job Matches."""
    
    def __init__(self, 
                 evidence_resolver: EvidenceResolver,
                 req_interpreter: RequirementInterpreter,
                 eligibility_engine: EligibilityEngine,
                 scoring_engine: FitScoringEngine,
                 explanation_gen: ExplanationGenerator):
        self.evidence_resolver = evidence_resolver
        self.req_interpreter = req_interpreter
        self.eligibility_engine = eligibility_engine
        self.scoring_engine = scoring_engine
        self.explanation_gen = explanation_gen
        
    def evaluate_job(self, job: NormalizedJob, run_id: Optional[str] = None) -> JobMatch:
        run_id = run_id or str(uuid.uuid4())
        
        # 1. Eligibility
        eligibility = self.eligibility_engine.evaluate(job)
        
        # 2. Interpret Requirements
        requirements = self.req_interpreter.interpret(job)
        
        # 3. Assess Evidence
        assessments = []
        for req in requirements:
            # We resolve skills against the requirement's normalized name
            evidence = self.evidence_resolver.resolve_skill(req.normalized_name)
            
            status = MatchStatus.MISSING
            if evidence.strength in (EvidenceStrength.DIRECT_VERIFIED, EvidenceStrength.STRONG_DEMONSTRATED):
                status = MatchStatus.MATCHED
            elif evidence.strength in (EvidenceStrength.SUPPORTING, EvidenceStrength.INDIRECT, EvidenceStrength.ADJACENT):
                status = MatchStatus.PARTIAL
                
            assessments.append(
                RequirementAssessment(
                    requirement=req,
                    status=status,
                    evidence_strength=evidence.strength,
                    evidence_references=evidence.references,
                    confidence=ConfidenceLevel.HIGH, # Hardcoded for now
                    impact="medium",
                    explanation=evidence.description
                )
            )
            
        # 4. Score
        scoring_result = self.scoring_engine.score(assessments, eligibility)
        
        # 5. Explain
        explanation = self.explanation_gen.generate(
            match_type=scoring_result["match_type"],
            strengths=scoring_result["strengths"],
            gaps=scoring_result["gaps"],
            assessments=assessments
        )
        
        match_run_info = MatchRunInfo(
            run_id=run_id,
            job_version="v1",
            career_brain_version="v1",
            policy_version="v1",
            engine_version="1.0.0",
            timestamp=datetime.utcnow()
        )
        
        return JobMatch(
            id=str(uuid.uuid4()),
            job_id=job.id if job.id else str(uuid.uuid4()),
            job_canonical_key=job.canonical_key,
            match_run=match_run_info,
            eligibility=eligibility,
            fit_score=scoring_result["fit_score"],
            priority=scoring_result["priority"],
            match_type=scoring_result["match_type"],
            confidence=ConfidenceLevel.HIGH,
            requirement_assessments=assessments,
            strengths=scoring_result["strengths"],
            gaps=scoring_result["gaps"],
            explanation=explanation
        )
