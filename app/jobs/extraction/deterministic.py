"""Deterministic extraction layer for job postings."""

import html
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.jobs.models.enums import EmploymentType, ExperienceLevel, RemoteType
from app.jobs.models.job import GraduationRequirement

# Canonical tech & skills vocabulary (extensible dictionary)
CANONICAL_SKILLS_AND_TECH = {
    # Programming Languages
    "python": "Python",
    "go": "Go",
    "golang": "Go",
    "java": "Java",
    "c++": "C++",
    "c#": "C#",
    "rust": "Rust",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "sql": "SQL",
    # Backend & Frameworks
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "express": "Express",
    "spring": "Spring Boot",
    "spring boot": "Spring Boot",
    "graphql": "GraphQL",
    "rest": "REST APIs",
    "restful": "REST APIs",
    "grpc": "gRPC",
    # Frontend
    "react": "React",
    "next.js": "Next.js",
    "nextjs": "Next.js",
    "vue": "Vue.js",
    "angular": "Angular",
    "tailwind": "Tailwind CSS",
    # Databases & Storage
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "redis": "Redis",
    "mongodb": "MongoDB",
    "dynamodb": "DynamoDB",
    "elasticsearch": "Elasticsearch",
    "pinecone": "Pinecone",
    "chromadb": "ChromaDB",
    # Infrastructure & Systems
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "terraform": "Terraform",
    "kafka": "Kafka",
    "rabbitmq": "RabbitMQ",
    "ci/cd": "CI/CD",
    "linux": "Linux",
    "distributed systems": "Distributed Systems",
    "concurrency": "Concurrency",
    # AI / ML
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "keras": "Keras",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "langchain": "LangChain",
    "rag": "RAG",
    "computer vision": "Computer Vision",
    "nlp": "NLP",
    "deep learning": "Deep Learning",
    "machine learning": "Machine Learning",
}


def clean_html(html_text: str) -> str:
    """Strips HTML tags to produce plain text."""
    if not html_text:
        return ""
    # Remove script and style tags
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br>, </p>, </li> with newlines
    clean = re.sub(r"<(br|/p|/li|/div|/h[1-6])[^>]*>", "\n", clean, flags=re.IGNORECASE)
    # Strip remaining tags
    clean = re.sub(r"<[^>]+>", " ", clean)
    # Decode the full HTML entity set (named and numeric), not a hand-picked
    # few: leftovers like &#8217; used to survive into descriptions AND into the
    # content hash, producing phantom "job updated" versions.
    clean = html.unescape(clean)
    # Normalize non-breaking spaces that unescape turns into U+00A0.
    clean = clean.replace(" ", " ")
    return clean


def extract_employment_type(title: str, content: str, raw_metadata: Dict[str, Any]) -> EmploymentType:
    """Deterministically extracts EmploymentType."""
    meta_val = str(raw_metadata.get("employmentType") or raw_metadata.get("commitment") or "").upper()
    if "INTERN" in meta_val:
        return EmploymentType.INTERNSHIP
    if "FULL" in meta_val:
        return EmploymentType.FULL_TIME
    if "PART" in meta_val:
        return EmploymentType.PART_TIME
    if "CONTRACT" in meta_val:
        return EmploymentType.CONTRACT

    text = f"{title} {clean_html(content)}".lower()
    if re.search(r"\b(intern|internship|summer intern|co-op)\b", text):
        return EmploymentType.INTERNSHIP
    if re.search(r"\b(full-time|full time|permanent)\b", text):
        return EmploymentType.FULL_TIME
    if re.search(r"\b(part-time|part time)\b", text):
        return EmploymentType.PART_TIME
    if re.search(r"\b(contract|contractor|freelance)\b", text):
        return EmploymentType.CONTRACT

    return EmploymentType.UNKNOWN


def extract_remote_type(title: str, location: Optional[str], content: str, raw_metadata: Dict[str, Any]) -> RemoteType:
    """Deterministically extracts RemoteType."""
    meta_workplace = str(raw_metadata.get("workplaceType") or "").upper()
    if "REMOTE" in meta_workplace:
        return RemoteType.REMOTE
    if "HYBRID" in meta_workplace:
        return RemoteType.HYBRID
    if "ONSITE" in meta_workplace or "ON_SITE" in meta_workplace:
        return RemoteType.ON_SITE

    combined = f"{title} {location or ''} {clean_html(content)[:1000]}".lower()
    if re.search(r"\b(remote|work from home|wfh|anywhere)\b", combined):
        if "hybrid" in combined:
            return RemoteType.HYBRID
        return RemoteType.REMOTE
    if re.search(r"\b(hybrid|flexible location)\b", combined):
        return RemoteType.HYBRID
    if re.search(r"\b(on-site|onsite|in-office|in office)\b", combined):
        return RemoteType.ON_SITE

    return RemoteType.UNKNOWN


def extract_experience_level(title: str, content: str) -> ExperienceLevel:
    """Deterministically extracts ExperienceLevel."""
    title_lower = title.lower()
    if re.search(r"\b(intern|internship|co-op)\b", title_lower):
        return ExperienceLevel.INTERN
    if re.search(r"\b(staff|principal|lead|director|manager)\b", title_lower):
        return ExperienceLevel.SENIOR
    if re.search(r"\b(senior|sr|sr\.)\b", title_lower):
        return ExperienceLevel.SENIOR
    if re.search(r"\b(junior|jr|jr\.|associate|entry|fresher|graduate)\b", title_lower):
        return ExperienceLevel.ENTRY_LEVEL

    text = clean_html(content).lower()
    if re.search(r"\b(0-1|0-2|1-2|0 to 2|1 to 2|entry level|new grad|recent graduate)\s*(years|yrs)?\b", text):
        return ExperienceLevel.ENTRY_LEVEL
    if re.search(r"\b(5\+|6\+|7\+|8\+|10\+|5 to 10|5-8)\s*(years|yrs)\b", text):
        return ExperienceLevel.SENIOR
    if re.search(r"\b(2-4|3-5|2 to 5|3\+)\s*(years|yrs)\b", text):
        return ExperienceLevel.MID

    return ExperienceLevel.UNKNOWN


_GRAD_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)\.?,?"
)
#: One year with an optional month or season: "2027", "December 2027", "Summer 2028".
_GRAD_YEAR = rf"(?:{_GRAD_MONTH}\s+)?20\d\d"
#: Several years: "2025 or 2026", "2026 & 2027", "2025, 2026, 2027", "2025/2026".
_GRAD_YEARS = rf"{_GRAD_YEAR}(?:\s*(?:,\s*(?:or|and)\s+|,|/|&|\bor\b|\band\b)\s*{_GRAD_YEAR})*"
_GRAD_BETWEEN_RE = re.compile(
    rf"(?:graduat\w+|class of|pass out)\s+(?:between|from)\s+(?:{_GRAD_MONTH}\s+)?(20\d\d)\s+(?:and|to|-)\s+(?:{_GRAD_MONTH}\s+)?(20\d\d)",
    re.IGNORECASE,
)
_GRAD_RANGE_RE = re.compile(r"\b(20\d\d)\s*[-–]\s*(20\d\d)\s*(?:batch|grads?|graduates?|passouts?|graduating)", re.IGNORECASE)
_GRAD_AFTER_RE = re.compile(rf"(?:graduat\w+|class of)\s+(?:in\s+)?(?:{_GRAD_MONTH}\s+)?(20\d\d)\s+or\s+(?:later|after)", re.IGNORECASE)
_GRAD_BEFORE_RE = re.compile(
    rf"(?:graduat\w+|expected graduation)\s+(?:by|before|on or before|no later than)\s+(?:the end of\s+)?(?:{_GRAD_MONTH}\s+)?(20\d\d)",
    re.IGNORECASE,
)
_GRAD_EXACT_RE = re.compile(
    rf"(?:graduat\w+\s+(?:in|by)?\s*|class of\s+|batch of\s+)(?P<a>{_GRAD_YEARS})\b"
    rf"|\b(?P<b>{_GRAD_YEARS})\s+(?:graduates?|grads?|passouts?|batch)\b"
    rf"|\bgraduate\s*\(\s*(?P<c>{_GRAD_YEARS})\s*\)",
    re.IGNORECASE,
)


def extract_graduation_requirement(content: str) -> Optional[GraduationRequirement]:
    """Deterministically extracts structured graduation year requirements.

    Handles patterns such as:
    - 'graduating in 2027' -> exact: [2027]
    - 'graduating between 2026 and 2028' -> min: 2026, max: 2028, exact: [2026, 2027, 2028]
    - '2026-2028 grads' -> min: 2026, max: 2028
    - 'expected graduation by 2028' / 'must graduate before December 2027' -> max
    - 'graduating 2027 or later' -> min: 2027
    - 'a 2025 or 2026 graduate' -> exact: [2025, 2026]

    Audit fix (2026-09-14, real boards): a list of years kept only one of them
    ("a 2025 or 2026 graduate" -> [2026], so a 2025 graduate was INELIGIBLE),
    and a month or season before the year hid the requirement entirely
    ("Must graduate before December 2027", "graduate in December 2026",
    "graduating by Spring 2027").
    """
    text = clean_html(content)

    between_match = _GRAD_BETWEEN_RE.search(text)
    if between_match:
        min_yr = int(between_match.group(1))
        max_yr = int(between_match.group(2))
        return GraduationRequirement(
            minimum_year=min_yr,
            maximum_year=max_yr,
            exact_years=list(range(min_yr, max_yr + 1)),
            original_text=between_match.group(0),
            extraction_confidence=0.95,
        )

    range_match = _GRAD_RANGE_RE.search(text)
    if range_match:
        min_yr = int(range_match.group(1))
        max_yr = int(range_match.group(2))
        return GraduationRequirement(
            minimum_year=min_yr,
            maximum_year=max_yr,
            exact_years=list(range(min_yr, max_yr + 1)),
            original_text=range_match.group(0),
            extraction_confidence=0.95,
        )

    after_match = _GRAD_AFTER_RE.search(text)
    if after_match:
        return GraduationRequirement(
            minimum_year=int(after_match.group(1)),
            original_text=after_match.group(0),
            extraction_confidence=0.90,
        )

    before_match = _GRAD_BEFORE_RE.search(text)
    if before_match:
        return GraduationRequirement(
            maximum_year=int(before_match.group(1)),
            original_text=before_match.group(0),
            extraction_confidence=0.90,
        )

    exact_match = _GRAD_EXACT_RE.search(text)
    if exact_match:
        listed = exact_match.group("a") or exact_match.group("b") or exact_match.group("c")
        years = sorted({int(year) for year in re.findall(r"20\d\d", listed)})
        return GraduationRequirement(
            minimum_year=years[0],
            maximum_year=years[-1],
            exact_years=years,
            original_text=exact_match.group(0),
            extraction_confidence=0.95,
        )

    return None


def extract_salary(content: str, raw_metadata: Dict[str, Any]) -> Optional[str]:
    """Deterministically extracts salary information as text."""
    # Check metadata first
    comp = raw_metadata.get("compensation") or raw_metadata.get("compensationTierSummary") or raw_metadata.get("salaryRange")
    if comp:
        if isinstance(comp, str):
            return comp
        if isinstance(comp, dict):
            min_val = comp.get("min") or comp.get("minValue")
            max_val = comp.get("max") or comp.get("maxValue")
            curr = comp.get("currency") or comp.get("currencyCode") or "$"
            if min_val and max_val:
                return f"{curr}{min_val:,} - {curr}{max_val:,}"

    # Search in text
    text = clean_html(content)
    # Pattern: $120,000 - $160,000 / $120k - $160k
    salary_match = re.search(
        r"(\$\s*[\d,]+(?:\s*k)?\s*(?:[-–]|to)\s*\$\s*[\d,]+(?:\s*k)?(?:\s*(?:per year|/yr|annually|/year))?)",
        text,
        re.IGNORECASE,
    )
    if salary_match:
        return salary_match.group(1).strip()

    # Pattern: ₹25-35 LPA / ₹25,00,000 - ₹35,00,000
    inr_match = re.search(
        r"([₹Rs\.]+\s*[\d\.,]+\s*(?:[-–]|to)\s*[\d\.,]+\s*(?:LPA|Lakhs?|Cr|per annum)?)",
        text,
        re.IGNORECASE,
    )
    if inr_match:
        return inr_match.group(1).strip()

    return None


def extract_skills_and_technologies(content: str) -> Tuple[List[str], List[str]]:
    """Extracts recognized canonical skills and technologies from content."""
    text = clean_html(content).lower()
    found_skills: Set[str] = set()

    for keyword, canonical in CANONICAL_SKILLS_AND_TECH.items():
        # Match as whole word / boundary
        escaped = re.escape(keyword)
        if re.search(rf"\b{escaped}\b", text):
            found_skills.add(canonical)

    skills_list = sorted(list(found_skills))
    # Separate into primary skills and technologies
    return skills_list, skills_list


def extract_bullets_and_sections(content: str) -> Tuple[List[str], List[str]]:
    """Extracts responsibilities and qualifications from bullet points or paragraphs."""
    responsibilities: List[str] = []
    qualifications: List[str] = []

    # Extract <li> items from HTML
    li_items = re.findall(r"<li[^>]*>(.*?)</li>", content, flags=re.DOTALL | re.IGNORECASE)
    cleaned_items = [clean_html(item).strip() for item in li_items if clean_html(item).strip()]

    # If no <li> found, look for markdown bullets (- or *)
    if not cleaned_items:
        bullet_items = re.findall(r"^\s*[-*•]\s+(.+)$", content, flags=re.MULTILINE)
        cleaned_items = [b.strip() for b in bullet_items if b.strip()]

    for item in cleaned_items:
        item_lower = item.lower()
        if any(w in item_lower for w in ["build", "design", "develop", "maintain", "collaborate", "deliver", "lead", "implement", "work with"]):
            if len(responsibilities) < 10:
                responsibilities.append(item)
        elif any(w in item_lower for w in ["experience", "degree", "proficien", "knowledge", "bachelor", "master", "ability to", "strong"]):
            if len(qualifications) < 10:
                qualifications.append(item)

    return responsibilities, qualifications
