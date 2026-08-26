"""Project model."""

from pydantic import BaseModel, Field

from app.models.enums import VerificationStatus


class ProjectMetric(BaseModel):
    name: str
    value: str
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED


class Project(BaseModel):
    id: str
    name: str
    status: str
    summary: str
    problem: str = Field(default="")
    solution: str = Field(default="")
    technologies: list[str] = Field(default_factory=list)
    primary_domain: str = Field(default="")
    relevant_roles: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    metrics: list[ProjectMetric] = Field(default_factory=list)
    technical_concepts: list[str] = Field(default_factory=list)
    resume_worthy_facts: list[str] = Field(default_factory=list)
    verified_claims: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    documentation_path: str = Field(default="")

    @property
    def verified_metrics(self) -> list[ProjectMetric]:
        return [m for m in self.metrics if m.verification_status == VerificationStatus.VERIFIED]

    def matches_query(self, query: str) -> bool:
        q = query.lower()
        searchable = " ".join(
            [
                self.name,
                self.summary,
                self.primary_domain,
                " ".join(self.technologies),
                " ".join(self.relevant_roles),
                " ".join(self.technical_concepts),
            ]
        ).lower()
        return q in searchable or any(q in t.lower() for t in self.technologies)

    def relevance_score(self, role_or_jd: str) -> float:
        q = role_or_jd.lower()
        score = 0.0
        for role in self.relevant_roles:
            if role.lower() in q or q in role.lower():
                score += 3.0
        for tech in self.technologies:
            if tech.lower() in q:
                score += 1.0
        for concept in self.technical_concepts:
            if concept.lower() in q:
                score += 0.5
        if self.primary_domain.lower() in q:
            score += 2.0
        if self.name.lower() in q:
            score += 2.0
        return score
