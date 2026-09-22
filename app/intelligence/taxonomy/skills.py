"""Curated skill taxonomy with aliases and disambiguation.

Two problems with the previous ~60-keyword dictionary this replaces:

1. **Coverage** — anything outside the list vanished from the pipeline.
2. **False positives** — matching ``\\bgo\\b`` flagged "we *go* fast" as the Go
   language, ``\\brest\\b`` flagged "the *rest* of the team", ``\\bspring\\b``
   flagged "*Spring* semester". Those bogus skills then became REQUIRED
   requirements downstream, so the fit score was measuring noise.

Ambiguous tokens therefore carry an explicit ``context`` pattern and are only
recognized when that pattern matches. Everything else matches on word
boundaries against the canonical name and its aliases.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class SkillEntry:
    """One canonical skill and the surface forms that mean it."""

    canonical: str
    category: str
    aliases: Sequence[str] = field(default_factory=tuple)
    # Regex that must match for an ambiguous token to count as this skill.
    context: Optional[str] = None
    # Related canonical skills, used as weak ("adjacent") matching evidence.
    related: Sequence[str] = field(default_factory=tuple)


def _e(canonical, category, aliases=(), context=None, related=()) -> SkillEntry:
    return SkillEntry(canonical, category, tuple(aliases), context, tuple(related))


# --- Disambiguation patterns for tokens that are also ordinary English -------
_OTHER_LANGS = r"(?:python|java|rust|c\+\+|c#|node(?:\.js)?|typescript|javascript|ruby|kotlin|scala|elixir)"
_GO_CONTEXT = (
    r"\bgolang\b"
    r"|\bgo\s+(?:programming|language|developer|engineer|services?|microservices?|"
    r"routines?|modules?|codebase|backend)\b"
    r"|\b(?:in|with|using|written\s+in)\s+go\b"
    # "Python and Go", "Go, Java", "Go/Rust" - a language list is unambiguous
    # context, unlike a bare "go" verb.
    rf"|\bgo\s*(?:[/,]|\bor\b|\band\b)\s*{_OTHER_LANGS}\b"
    rf"|\b{_OTHER_LANGS}\s*(?:[/,]|\bor\b|\band\b)\s*go\b"
)
_REST_CONTEXT = r"\brest(?:ful)?\s+(?:api|apis|service|services|endpoint|endpoints|web\s+service)\b|\brestful\b"
_SPRING_CONTEXT = r"\bspring\s+(?:boot|framework|mvc|cloud|security|data)\b|\bspring\b[^.]{0,40}\bjava\b"
_RAG_CONTEXT = (
    r"\bretrieval[\s-]augmented\s+generation\b"
    r"|\brag\b[^.]{0,60}\b(?:llm|llms|retrieval|vector|embedding|embeddings|pipeline)\b"
    r"|\b(?:llm|llms|retrieval|vector|embedding|embeddings)\b[^.]{0,60}\brag\b"
)
_R_CONTEXT = r"\br\s+(?:programming|language|scripts?|studio)\b|\brstudio\b|\b(?:python|sas|matlab)\s*[/,]\s*r\b"
_C_CONTEXT = r"\bc\s+(?:programming|language)\b|\bc\s*/\s*c\+\+|\bansi\s+c\b|\bembedded\s+c\b"
_SWIFT_CONTEXT = r"\bswift(?:ui)?\b[^.]{0,60}\b(?:ios|xcode|apple|objective-c|mobile|macos)\b|\bswiftui\b|\b(?:ios|xcode|objective-c)\b[^.]{0,60}\bswift\b"

_RAW_TAXONOMY: Tuple[SkillEntry, ...] = (
    # --- Programming languages ------------------------------------------
    _e("Python", "language", ["python3", "py3"], related=["FastAPI", "Django", "Flask"]),
    _e("Go", "language", ["golang"], context=_GO_CONTEXT, related=["gRPC", "Kubernetes"]),
    _e("Java", "language", ["java8", "java 11", "java 17"], related=["Spring Boot"]),
    _e("JavaScript", "language", ["js", "ecmascript", "es6"], related=["Node.js", "React"]),
    _e("TypeScript", "language", ["ts"], related=["JavaScript", "React"]),
    _e("C++", "language", ["cpp", "c plus plus"]),
    _e("C#", "language", ["csharp", "c sharp"], related=[".NET"]),
    _e("C", "language", context=_C_CONTEXT),
    _e("Rust", "language", ["rustlang"]),
    _e("Kotlin", "language", [], related=["Android"]),
    _e("Swift", "language", ["swiftui"], context=_SWIFT_CONTEXT, related=["iOS"]),
    _e("Ruby", "language", [], related=["Ruby on Rails"]),
    _e("PHP", "language", ["php8"], related=["Laravel"]),
    _e("Scala", "language", [], related=["Spark"]),
    _e("R", "language", ["rstudio"], context=_R_CONTEXT),
    _e("MATLAB", "language", []),
    _e("Perl", "language", []),
    _e("Shell Scripting", "language", ["bash", "shell script", "zsh", "shell scripting"]),
    _e("SQL", "language", ["ansi sql", "t-sql", "pl/sql"]),
    _e("Dart", "language", [], related=["Flutter"]),
    _e("Elixir", "language", []),
    _e("Objective-C", "language", ["objective c", "objc"]),
    # --- Backend frameworks ----------------------------------------------
    _e("FastAPI", "backend", ["fast api"], related=["Python", "REST APIs"]),
    _e("Django", "backend", ["django rest framework", "drf"], related=["Python"]),
    _e("Flask", "backend", [], related=["Python"]),
    _e("Node.js", "backend", ["nodejs", "node js", "node"], related=["JavaScript", "Express"]),
    _e("Express", "backend", ["express.js", "expressjs"], related=["Node.js"]),
    _e("Spring Boot", "backend", ["spring framework", "spring mvc", "springboot"], context=_SPRING_CONTEXT, related=["Java"]),
    _e("Ruby on Rails", "backend", ["rails", "ror"], related=["Ruby"]),
    _e("Laravel", "backend", [], related=["PHP"]),
    _e(".NET", "backend", ["dotnet", ".net core", "asp.net"], related=["C#"]),
    _e("NestJS", "backend", ["nest.js"], related=["TypeScript"]),
    _e("gRPC", "backend", ["grpc"], related=["Protocol Buffers"]),
    _e("GraphQL", "backend", ["graph ql", "apollo graphql"]),
    _e("REST APIs", "backend", ["rest api", "restful api", "restful"], context=_REST_CONTEXT),
    _e("Protocol Buffers", "backend", ["protobuf", "proto3"]),
    _e("WebSockets", "backend", ["websocket", "web sockets"]),
    _e("Microservices", "architecture", ["micro-services", "microservice architecture"]),
    _e("Event-Driven Architecture", "architecture", ["event driven", "event-driven", "pub/sub", "pubsub"]),
    _e("Distributed Systems", "architecture", ["distributed system", "distributed computing"]),
    _e("Concurrency", "architecture", ["concurrent programming", "multithreading", "multi-threading", "parallelism"]),
    _e("System Design", "architecture", ["systems design", "software architecture"]),
    _e("API Design", "architecture", ["api development", "api design"]),
    # --- Frontend ---------------------------------------------------------
    _e("React", "frontend", ["react.js", "reactjs", "react js"], related=["JavaScript", "TypeScript"]),
    _e("Next.js", "frontend", ["nextjs", "next js"], related=["React"]),
    _e("Vue.js", "frontend", ["vue", "vuejs", "vue 3"]),
    _e("Angular", "frontend", ["angularjs", "angular 2+"]),
    _e("Svelte", "frontend", ["sveltekit"]),
    _e("HTML", "frontend", ["html5"]),
    _e("CSS", "frontend", ["css3", "scss", "sass"]),
    _e("Tailwind CSS", "frontend", ["tailwind", "tailwindcss"]),
    _e("Redux", "frontend", [], related=["React"]),
    _e("React Native", "mobile", ["react-native"], related=["React"]),
    _e("Flutter", "mobile", [], related=["Dart"]),
    _e("Android", "mobile", ["android sdk", "android development"], related=["Kotlin", "Java"]),
    _e("iOS", "mobile", ["ios development"], related=["Swift"]),
    # --- Databases / storage ---------------------------------------------
    _e("PostgreSQL", "database", ["postgres", "psql", "postgresql"]),
    _e("MySQL", "database", ["mariadb"]),
    _e("SQLite", "database", []),
    _e("MongoDB", "database", ["mongo"]),
    _e("Redis", "database", []),
    _e("DynamoDB", "database", ["dynamo db"]),
    _e("Cassandra", "database", ["apache cassandra"]),
    _e("Elasticsearch", "database", ["elastic search", "opensearch"]),
    _e("Neo4j", "database", ["graph database"]),
    _e("Snowflake", "database", []),
    _e("BigQuery", "database", ["big query"]),
    _e("ClickHouse", "database", ["click house"]),
    _e("Pinecone", "vector", []),
    _e("ChromaDB", "vector", ["chroma db", "chroma"]),
    _e("Weaviate", "vector", []),
    _e("FAISS", "vector", []),
    _e("pgvector", "vector", ["pg vector"], related=["PostgreSQL"]),
    _e("Vector Databases", "vector", ["vector database", "vector store", "vector search"]),
    _e("Database Design", "database", ["schema design", "data modeling", "data modelling"]),
    _e("Query Optimization", "database", ["query tuning", "sql optimization", "indexing strategy"]),
    _e("ORM", "database", ["sqlalchemy", "hibernate", "prisma", "typeorm"]),
    # --- Infrastructure / DevOps -----------------------------------------
    _e("Docker", "infra", ["containerization", "docker compose", "containers"]),
    _e("Kubernetes", "infra", ["k8s", "eks", "gke", "aks"]),
    _e("AWS", "cloud", ["amazon web services", "ec2", "s3", "lambda"]),
    _e("GCP", "cloud", ["google cloud", "google cloud platform"]),
    _e("Azure", "cloud", ["microsoft azure"]),
    _e("Terraform", "infra", ["infrastructure as code", "iac"]),
    _e("Ansible", "infra", []),
    _e("CI/CD", "infra", ["ci cd", "continuous integration", "continuous delivery", "continuous deployment"]),
    _e("GitHub Actions", "infra", ["github action"], related=["CI/CD"]),
    _e("Jenkins", "infra", []),
    _e("Linux", "infra", ["unix", "ubuntu", "debian"]),
    _e("Git", "infra", ["version control", "github", "gitlab"]),
    _e("Nginx", "infra", []),
    _e("Kafka", "infra", ["apache kafka"], related=["Event-Driven Architecture"]),
    _e("RabbitMQ", "infra", ["rabbit mq", "amqp"]),
    _e("Celery", "infra", [], related=["Python"]),
    _e("Airflow", "data", ["apache airflow"]),
    _e("Spark", "data", ["apache spark", "pyspark"]),
    _e("dbt", "data", ["data build tool"]),
    _e("ETL", "data", ["etl pipeline", "elt", "data pipeline", "data pipelines"]),
    _e("Observability", "infra", ["monitoring", "prometheus", "grafana", "opentelemetry", "datadog"]),
    _e("Load Testing", "infra", ["k6", "jmeter", "locust", "performance testing"]),
    _e("Caching", "infra", ["cache layer", "caching strategy"]),
    _e("Scalability", "architecture", ["scalable systems", "high availability", "horizontal scaling"]),
    # --- AI / ML ----------------------------------------------------------
    _e("Machine Learning", "ai", ["ml", "machine-learning"]),
    _e("Deep Learning", "ai", ["neural networks", "neural network"]),
    _e("PyTorch", "ai", ["torch"], related=["Deep Learning", "Python"]),
    _e("TensorFlow", "ai", ["tf2"], related=["Deep Learning"]),
    _e("Keras", "ai", [], related=["TensorFlow"]),
    _e("scikit-learn", "ai", ["sklearn", "scikit learn"], related=["Machine Learning"]),
    _e("Pandas", "ai", ["pandas dataframe"], related=["Python"]),
    _e("NumPy", "ai", ["numpy"], related=["Python"]),
    _e("Computer Vision", "ai", ["cv", "opencv", "image classification", "object detection"]),
    _e("NLP", "ai", ["natural language processing", "text classification"]),
    _e("LLMs", "ai", ["large language models", "large language model", "llm", "gpt", "transformers"]),
    _e("LangChain", "ai", ["lang chain"], related=["LLMs"]),
    _e("RAG", "ai", ["retrieval augmented generation", "retrieval-augmented generation"], context=_RAG_CONTEXT, related=["LLMs", "Vector Databases"]),
    _e("Prompt Engineering", "ai", ["prompting", "prompt design"], related=["LLMs"]),
    _e("Embeddings", "ai", ["sentence transformers", "text embeddings", "embedding models"]),
    _e("MLOps", "ai", ["ml ops", "model deployment", "model serving"]),
    _e("Feature Engineering", "ai", ["feature extraction"]),
    _e("Model Evaluation", "ai", ["model validation", "cross validation", "cross-validation"]),
    _e("Recommendation Systems", "ai", ["recommender systems", "recsys"]),
    _e("Time Series", "ai", ["time-series", "forecasting"]),
    _e("Reinforcement Learning", "ai", ["rl"]),
    _e("Data Analysis", "data", ["data analytics", "exploratory data analysis", "eda"]),
    _e("Data Visualization", "data", ["matplotlib", "seaborn", "plotly", "tableau", "power bi"]),
    _e("Statistics", "data", ["statistical analysis", "probability"]),
    # --- Practices --------------------------------------------------------
    _e("Testing", "practice", ["unit testing", "unit tests", "pytest", "jest", "test automation", "tdd"]),
    _e("Code Review", "practice", ["peer review", "code reviews"]),
    _e("Agile", "practice", ["scrum", "kanban", "sprint planning"]),
    _e("Debugging", "practice", ["troubleshooting", "root cause analysis"]),
    _e("Documentation", "practice", ["technical writing", "technical documentation"]),
    _e("Security", "practice", ["application security", "appsec", "secure coding", "owasp"]),
    _e("Authentication", "practice", ["oauth", "oauth2", "jwt", "sso", "authorization"]),
    _e("Web Scraping", "practice", ["scraping", "crawler", "crawling", "beautifulsoup", "scrapy", "playwright", "selenium"]),
    _e("Data Structures", "cs", ["data structures and algorithms", "dsa"]),
    _e("Algorithms", "cs", ["algorithm design", "algorithmic"]),
    _e("Operating Systems", "cs", ["os internals"]),
    _e("Computer Networks", "cs", ["networking", "tcp/ip", "http protocols"]),
    _e("Compilers", "cs", ["compiler design"]),
    # --- Soft / collaboration --------------------------------------------
    _e("Communication", "soft", ["written communication", "verbal communication", "communication skills"]),
    _e("Collaboration", "soft", ["teamwork", "cross-functional", "cross functional"]),
    _e("Problem Solving", "soft", ["problem-solving", "analytical skills"]),
    _e("Ownership", "soft", ["end-to-end ownership", "self-starter", "autonomy"]),
    _e("Mentorship", "soft", ["mentoring", "coaching"]),
    _e("Leadership", "soft", ["technical leadership", "team lead"]),
)

SKILL_TAXONOMY: Dict[str, SkillEntry] = {entry.canonical: entry for entry in _RAW_TAXONOMY}


def _boundary_pattern(term: str) -> str:
    """Word-boundary regex for a term containing regex-significant characters.

    ``\\b`` does not work next to ``+``/``#``/``.``, so boundaries are asserted
    with lookarounds on "word-ish" characters instead.
    """
    escaped = re.escape(term)
    # The left boundary also excludes '.', '-' and '_' so the alias "js" cannot
    # match inside "React.js" (which is a React mention, not a JavaScript one).
    return rf"(?<![A-Za-z0-9._-]){escaped}(?![A-Za-z0-9])"


def _build_index() -> Tuple[Dict[str, str], List[Tuple[str, re.Pattern, Tuple[str, ...]]]]:
    """Build the alias→canonical map and the ordered match patterns.

    Each pattern carries the lower-cased literal surfaces it can match. A
    surface-only pattern can only match text that contains one of those
    literals, so :func:`find_skills_in_text` checks that with a substring test
    before paying for the regex. Context patterns (custom disambiguation
    regexes) carry an empty tuple and are always searched.
    """
    alias_map: Dict[str, str] = {}
    patterns: List[Tuple[str, re.Pattern, Tuple[str, ...]]] = []

    for entry in _RAW_TAXONOMY:
        surfaces = [entry.canonical, *entry.aliases]
        for surface in surfaces:
            alias_map[surface.lower()] = entry.canonical

        if entry.context:
            patterns.append((entry.canonical, re.compile(entry.context, re.IGNORECASE), ()))
        else:
            joined = "|".join(_boundary_pattern(s) for s in sorted(surfaces, key=len, reverse=True))
            literals = tuple(sorted({s.lower() for s in surfaces}))
            patterns.append((entry.canonical, re.compile(joined, re.IGNORECASE), literals))

    # Longest canonical names first so "Spring Boot" wins over a shorter match.
    patterns.sort(key=lambda item: len(item[0]), reverse=True)
    return alias_map, patterns


_ALIAS_MAP, _PATTERNS = _build_index()


def canonicalize(term: str) -> Optional[str]:
    """Map a surface form to its canonical skill name, if known.

    Handles the ``React.js``/``ReactJS``/``React`` family by normalizing
    punctuation and spacing before lookup.
    """
    if not term:
        return None

    raw = term.strip().lower()
    if raw in _ALIAS_MAP:
        return _ALIAS_MAP[raw]

    # Strip common suffixes/punctuation: "react.js" -> "reactjs" -> "react"
    squashed = re.sub(r"[\s._-]+", "", raw)
    for alias, canonical in _ALIAS_MAP.items():
        if re.sub(r"[\s._-]+", "", alias) == squashed:
            return canonical

    trimmed = re.sub(r"(js|framework|library)$", "", squashed)
    if trimmed and trimmed != squashed:
        for alias, canonical in _ALIAS_MAP.items():
            if re.sub(r"[\s._-]+", "", alias) == trimmed:
                return canonical

    return None


def find_skills_in_text(text: str) -> List[str]:
    """Return the canonical skills mentioned in ``text``.

    Ambiguous tokens only count when their disambiguation context matches, so
    ordinary English ("we go fast", "the rest of the team") no longer produces
    phantom skills.
    """
    if not text:
        return []

    lowered = text.lower()
    found: Set[str] = set()
    for canonical, pattern, literals in _PATTERNS:
        # Cheap necessary condition first: a surface pattern cannot match
        # unless one of its literal surfaces occurs in the text.
        if literals and not any(literal in lowered for literal in literals):
            continue
        if pattern.search(text):
            found.add(canonical)
    return sorted(found)


def related_skills(canonical: str) -> Sequence[str]:
    """Adjacent skills for weak evidence resolution."""
    entry = SKILL_TAXONOMY.get(canonical)
    return entry.related if entry else ()


def category_of(canonical: str) -> Optional[str]:
    entry = SKILL_TAXONOMY.get(canonical)
    return entry.category if entry else None


class TaxonomyProvider:
    """Seam for swapping in an ESCO/O*NET/Lightcast-backed taxonomy later.

    The default implementation is the curated local table above: zero
    dependencies, zero network calls, works offline on any free tier.
    """

    def canonicalize(self, term: str) -> Optional[str]:
        return canonicalize(term)

    def find_in_text(self, text: str) -> List[str]:
        return find_skills_in_text(text)

    def related(self, canonical: str) -> Sequence[str]:
        return related_skills(canonical)

    def category(self, canonical: str) -> Optional[str]:
        return category_of(canonical)

    def all_skills(self) -> Iterable[str]:
        return SKILL_TAXONOMY.keys()


_DEFAULT = TaxonomyProvider()


def default_taxonomy() -> TaxonomyProvider:
    return _DEFAULT
