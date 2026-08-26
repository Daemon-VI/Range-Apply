# Truth Rules

**Hard constraints for every CareerOS agent.**

These rules must be enforced programmatically by the truth/validation layer and respected by all future agents.

**Last updated:** August 2026

---

## Core Rules

1. **Never fabricate experience.** No invented employment, internships, or job titles.
2. **Never fabricate employment.** Project work is not professional employment.
3. **Never fabricate skills.** Only claim skills with evidence.
4. **Never fabricate metrics.** Performance numbers require project documentation or test evidence.
5. **Never fabricate project ownership.** Accurately represent contribution level.
6. **Never fabricate certifications.** Certifications require explicit verification.
7. **Never fabricate achievements.** Hackathons, awards, and competitions require verification.
8. **Never convert project experience into professional experience.** Project-based engineering ≠ employment.
9. **Resume tailoring may rewrite verified facts but may not create new factual claims.**
10. **Application answers must come from verified facts, explicit preferences, or user-approved answers.**
11. **If an answer cannot be safely determined, require human review.**
12. **Conflicting information must be surfaced.** Never silently choose one value.
13. **The master career profile cannot be modified autonomously.**
14. **AI inference must never silently become a verified fact.**

---

## Verification Status Definitions

| Status | Meaning | Resume Safe | Application Safe |
|--------|---------|-------------|------------------|
| VERIFIED | Confirmed by source material or user | Yes | Yes |
| UNVERIFIED | Mentioned but not confirmed | No | No |
| INFERRED | AI interpretation | No | No |
| CONFLICT | Multiple sources disagree | No | No — surface conflict |
| NEEDS_REVIEW | Requires human decision | No | No |

---

## Promotion Rules

Never silently promote:

```text
INFERRED → VERIFIED
UNVERIFIED → VERIFIED
NEEDS_REVIEW → VERIFIED
```

Promotion to VERIFIED requires explicit user confirmation or authoritative source material.

---

## Resume Rules

- Master resume is the factual source
- Job-specific resumes may reorder and emphasize
- Job-specific resumes may NOT add new factual claims
- Metrics from projects require verification status check before inclusion
- Go experience is project-specific (Ticket Engine), not general professional experience

---

## Application Answer Rules

Safe sources for automatic answers:
- Verified education facts
- Verified graduation year and CGPA
- Verified skills with evidence
- Verified projects
- Explicit user preferences
- Previously approved standardized answers

Require human review for:
- Legal/financial/identity-sensitive questions
- Questions requiring interpretation
- Unsupported claims
- Conflicting information
- CAPTCHA, MFA, security challenges

---

## Metric Rules

| Metric Type | Policy |
|-------------|--------|
| Ticket Engine benchmarks | Project-reported; use only when supported by documentation/tests |
| ML model accuracy | UNVERIFIED until confirmed from current implementation |
| Dataset sizes | UNVERIFIED until confirmed from project source |
| DSA solved count | Do not claim without source of truth |
| Clinical/medical accuracy | Never fabricate; these are ML projects, not clinical products |

---

## Conflict Resolution

When information conflicts across sources:
1. Mark as CONFLICT
2. Surface both values
3. Do not silently choose
4. Require user resolution

---

## Agent Behavior

Agents must:
- Query CareerBrainService for structured data
- Check verification status before using facts
- Flag NEEDS_REVIEW items for human attention
- Pause for human intervention on ambiguous questions

Agents must NOT:
- Read dozens of markdown files and LLM-summarize
- Invent answers to fill gaps
- Bypass security mechanisms
- Modify the master career profile autonomously
