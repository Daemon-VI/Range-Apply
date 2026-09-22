"""Deterministic signal classification (rules first, AI never required).

Every rule is a named regular expression on the subject and/or the body.
Categories have a fixed precedence for the case where rules of several
categories match one message (a rejection usually mentions the interview it
follows). When two *incompatible* categories both match with HIGH
confidence the result is downgraded to MEDIUM so a person (or, optionally,
the AI classifier) looks at it. ``CLASSIFIER_VERSION`` is bumped whenever
this file's rules change; it is stored on every classified signal.
"""

import re
from dataclasses import dataclass
from typing import Optional

from app.signals.models import (
    CLASSIFIER_VERSION,
    Classification,
    ClassificationSource,
    Confidence,
    SignalCategory,
)


@dataclass(frozen=True)
class Rule:
    id: str
    category: SignalCategory
    confidence: Confidence
    pattern: re.Pattern
    #: "subject", "body" or "any"
    field: str = "any"


def _r(id_: str, category: SignalCategory, confidence: Confidence, pattern: str, field: str = "any") -> Rule:
    return Rule(id_, category, confidence, re.compile(pattern, re.IGNORECASE | re.DOTALL), field)


C = SignalCategory
H, M, L = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW

RULES: tuple[Rule, ...] = (
    # --- rejection ---------------------------------------------------------
    _r("rej.not_moving_forward", C.REJECTION, H, r"\b(not|won't|will not|unable to|decided not to)\b.{0,40}\b(move|moving|proceed|proceeding|go|going)\s+(forward|ahead)"),
    _r("rej.other_candidates", C.REJECTION, H, r"\b(other|another|different)\s+(candidates?|applicants?)\b.{0,80}\b(more closely|better|whose|align|match|fit|selected|decided|proceed|pursue)|\b(pursue|proceed|move forward with|selected)\b.{0,40}\b(other|another)\s+(candidates?|applicants?)"),
    _r("rej.regret", C.REJECTION, H, r"\b(regret to inform|regret that|we are sorry to inform|sorry to let you know|it is with regret)\b"),
    _r("rej.not_selected", C.REJECTION, H, r"\b(not (been )?selected|no longer (under|in) consideration|not (been )?shortlisted|unsuccessful (on this occasion|this time)|will not be (progressing|proceeding|considered)|not be progressing)\b"),
    _r("rej.position_filled", C.REJECTION, H, r"\b(position|role|vacancy) (has|have) (now )?been filled\b"),
    _r("rej.unfortunately_soft", C.REJECTION, M, r"\bunfortunately\b.{0,120}\b(application|candidacy|profile|role|position)\b"),
    # --- withdrawal --------------------------------------------------------
    _r("wd.withdrawn", C.WITHDRAWAL, H, r"\b(application|candidacy)\b.{0,80}\b(has been|was|is) (withdrawn|cancelled|canceled)\b|\byou (have )?withdrawn\b|\bwithdraw(al|n) (of )?(your )?(application|candidacy)\b|\bconfirm(ing)? (your )?withdrawal\b"),
    # --- duplicate / closed -----------------------------------------------
    _r("dup.already_applied", C.DUPLICATE_OR_CLOSED, H, r"\b(already applied|duplicate application|application already (exists|received|on file)|previously applied)\b"),
    _r("dup.closed", C.DUPLICATE_OR_CLOSED, H, r"\b(no longer accepting applications|posting (has|is) (been )?closed|job (has been|is) closed|requisition (has been|is) (closed|cancelled|canceled)|position (has been|is) (closed|cancelled|canceled|put on hold|on hold))\b"),
    # --- assessment --------------------------------------------------------
    _r("asm.test", C.ASSESSMENT, H, r"\b(online assessment|coding (challenge|test|assessment|exercise)|take[- ]home (assignment|test|challenge|exercise)|technical (assessment|test|challenge)|aptitude test|skills? (test|assessment)|hackerrank|codility|codesignal|hackerearth|assessment (link|invitation|invite))\b"),
    _r("asm.complete", C.ASSESSMENT, M, r"\b(complete|finish|attempt) (the|this|your|an?) (assessment|test|challenge|assignment)\b"),
    # --- interview ---------------------------------------------------------
    _r("int.invite", C.INTERVIEW_INVITATION, H, r"\b(invite|inviting|invitation|like|love|want|pleased) (you )?(to )?(an? |the |your )?(interview|phone screen|screening call|technical round|hr round|discussion|chat|conversation)\b|\binterview (invitation|invite|request)\b|\bschedule (an? |your |the )?(interview|call|phone screen|screening|conversation|time to (talk|chat|speak))\b"),
    _r("int.next_round", C.INTERVIEW_INVITATION, H, r"\b(next (round|stage|step)s?\b.{0,60}\b(interview|call|conversation|discussion)|shortlisted for (an? )?(interview|the next round)|selected for (an? )?(interview|the next round)|interview (has been )?(scheduled|confirmed|booked)|your interview (with|at|on|is)|book a (time|slot))\b"),
    _r("int.speak", C.INTERVIEW_INVITATION, M, r"\b(would like to (speak|talk|chat|connect) with you|set up a (call|time|conversation)|availability (for|to) (a )?(call|chat|interview|conversation)|available for a (quick )?(call|chat))\b"),
    # --- information request ----------------------------------------------
    _r("info.provide", C.INFORMATION_REQUEST, H, r"\b(please (provide|send|share|upload|submit|attach|complete)|could you (please )?(provide|send|share|upload|submit|attach)|we (need|require|are missing)|missing (document|information|details)|additional (information|details|documents?) (is|are)? ?(required|needed)|required to complete your application)\b"),
    # --- application received / confirmation ------------------------------
    _r("conf.reference", C.APPLICATION_CONFIRMATION, H, r"\b(application|confirmation|reference) (id|number|no\.?|#|code)\s*[:#]?\s*[A-Za-z0-9][A-Za-z0-9\-_/]{3,}"),
    _r("conf.submitted", C.APPLICATION_CONFIRMATION, H, r"\b(application (has been|was) (successfully )?(submitted|received|recorded|logged)|successfully (submitted|applied)|application confirmation|confirmation of your application|confirm(s|ing)? (receipt of|that we (have )?received) your application)\b"),
    _r("recv.thanks", C.APPLICATION_RECEIVED, H, r"\b(thank(s| you) for (applying|your application|your interest in|submitting your application)|we('ve| have) received your application|application received|received your application|your application (to|for|at) .{2,80} (has been received|was received))\b"),
    _r("recv.review_soon", C.APPLICATION_RECEIVED, M, r"\b(will (review|be reviewing) your (application|profile|resume|cv)|review your application (and|shortly)|be in touch (if|should|when))\b"),
    # --- status update -----------------------------------------------------
    _r("stat.under_review", C.STATUS_UPDATE, H, r"\b(application (status|update)|status of your application|update on your application|(is|are) (currently )?(under|in) review|(currently |still )?(reviewing|being reviewed|being considered)|moved to the next stage|status (has )?changed)\b"),
    # --- recruiter contact -------------------------------------------------
    _r("rec.reaching_out", C.RECRUITER_CONTACT, M, r"\b(reaching out|i('m| am) (a )?(recruiter|talent partner|hiring manager|technical recruiter)|talent acquisition|came across your (profile|resume|cv)|your (profile|background|experience) (caught|stood out|looks)|(opportunity|opening|role) (at|with) .{2,60}(that|which|might|may|could) (be a )?(fit|match|interest))\b"),
    # --- other (marketing / alerts) ----------------------------------------
    _r("other.alerts", C.OTHER, H, r"\b(job alert|jobs? (you may|you might) (like|be interested in)|recommended (jobs|for you)|new jobs (matching|for you)|weekly digest|newsletter|unsubscribe from (these|this|our) (emails?|alerts?|notifications?)|based on your (search|profile) preferences)\b"),
)

#: Fixed precedence when several categories match.
PRECEDENCE: tuple[SignalCategory, ...] = (
    C.REJECTION,
    C.WITHDRAWAL,
    C.DUPLICATE_OR_CLOSED,
    C.ASSESSMENT,
    C.INTERVIEW_INVITATION,
    C.INFORMATION_REQUEST,
    C.APPLICATION_CONFIRMATION,
    C.APPLICATION_RECEIVED,
    C.STATUS_UPDATE,
    C.RECRUITER_CONTACT,
    C.OTHER,
)

#: Pairs that routinely co-occur in one message; the higher-precedence one wins at full confidence.
_COMPATIBLE: frozenset[frozenset] = frozenset(
    {
        frozenset({C.REJECTION, C.INTERVIEW_INVITATION}),
        frozenset({C.REJECTION, C.ASSESSMENT}),
        frozenset({C.REJECTION, C.APPLICATION_RECEIVED}),
        frozenset({C.REJECTION, C.APPLICATION_CONFIRMATION}),
        frozenset({C.REJECTION, C.STATUS_UPDATE}),
        frozenset({C.ASSESSMENT, C.INTERVIEW_INVITATION}),
        frozenset({C.ASSESSMENT, C.INFORMATION_REQUEST}),
        frozenset({C.ASSESSMENT, C.APPLICATION_RECEIVED}),
        frozenset({C.ASSESSMENT, C.APPLICATION_CONFIRMATION}),
        frozenset({C.INTERVIEW_INVITATION, C.APPLICATION_RECEIVED}),
        frozenset({C.INTERVIEW_INVITATION, C.APPLICATION_CONFIRMATION}),
        frozenset({C.INTERVIEW_INVITATION, C.RECRUITER_CONTACT}),
        frozenset({C.INTERVIEW_INVITATION, C.STATUS_UPDATE}),
        frozenset({C.INFORMATION_REQUEST, C.APPLICATION_RECEIVED}),
        frozenset({C.INFORMATION_REQUEST, C.APPLICATION_CONFIRMATION}),
        frozenset({C.INFORMATION_REQUEST, C.STATUS_UPDATE}),
        frozenset({C.APPLICATION_CONFIRMATION, C.APPLICATION_RECEIVED}),
        frozenset({C.APPLICATION_CONFIRMATION, C.STATUS_UPDATE}),
        frozenset({C.APPLICATION_RECEIVED, C.STATUS_UPDATE}),
        frozenset({C.APPLICATION_RECEIVED, C.RECRUITER_CONTACT}),
        frozenset({C.WITHDRAWAL, C.APPLICATION_RECEIVED}),
        frozenset({C.WITHDRAWAL, C.STATUS_UPDATE}),
        frozenset({C.DUPLICATE_OR_CLOSED, C.APPLICATION_RECEIVED}),
        frozenset({C.DUPLICATE_OR_CLOSED, C.STATUS_UPDATE}),
    }
)

_RANK = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.NONE: 0}


def _lower(conf: Confidence) -> Confidence:
    return {Confidence.HIGH: Confidence.MEDIUM, Confidence.MEDIUM: Confidence.LOW, Confidence.LOW: Confidence.LOW, Confidence.NONE: Confidence.NONE}[conf]


def classify_text(subject: Optional[str], body: Optional[str], marketing_sender: bool = False) -> Classification:
    """Pure: the same subject and body always give the same classification."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    both = f"{subject}\n{body}"
    matched: dict[SignalCategory, list[Rule]] = {}
    for rule in RULES:
        haystack = subject if rule.field == "subject" else body if rule.field == "body" else both
        if haystack and rule.pattern.search(haystack):
            matched.setdefault(rule.category, []).append(rule)
    if marketing_sender and C.OTHER not in matched and not any(c in matched for c in (C.REJECTION, C.INTERVIEW_INVITATION, C.ASSESSMENT, C.APPLICATION_CONFIRMATION)):
        matched.setdefault(C.OTHER, []).append(Rule("other.marketing_sender", C.OTHER, Confidence.HIGH, re.compile("")))
    if not matched:
        return Classification(category=C.UNKNOWN, confidence=Confidence.NONE, source=ClassificationSource.RULES, matched_rules=[])
    ordered = sorted(matched, key=lambda c: PRECEDENCE.index(c) if c in PRECEDENCE else 99)
    winner = ordered[0]
    rules = matched[winner]
    confidence = max((r.confidence for r in rules), key=lambda c: _RANK[c])
    competing = [c.value for c in ordered[1:]]
    if winner is C.OTHER and len(ordered) > 1:
        # Marketing wording next to a real category: let the real one win at reduced confidence.
        winner = ordered[1]
        rules = matched[winner]
        confidence = _lower(max((r.confidence for r in rules), key=lambda c: _RANK[c]))
        competing = [c.value for c in ordered if c is not winner]
    for other in ordered[1:]:
        if other is C.OTHER:
            continue
        other_conf = max((r.confidence for r in matched[other]), key=lambda c: _RANK[c])
        if other_conf is Confidence.HIGH and frozenset({winner, other}) not in _COMPATIBLE:
            confidence = _lower(confidence)
            break
    return Classification(
        category=winner,
        confidence=confidence,
        source=ClassificationSource.RULES,
        classifier_version=CLASSIFIER_VERSION,
        matched_rules=[r.id for c in ordered for r in matched[c]],
        competing=competing,
    )


__all__ = ["RULES", "PRECEDENCE", "classify_text", "Rule"]
