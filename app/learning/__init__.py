"""Blueprint Phase 11 — the volume-neutral Outcome Learning Engine.

It reads the Phase 10 outcome history and produces *learning signals*:
observed rates per source / company / title / role family / fit band /
preparation lane and level / cover-letter mode / positioning variant /
execution method, response-time statistics, versioned snapshots and
phrased recommendations — every one with its sample size, evidence quality,
confidence and window.

It never writes a policy, a cap, a band, a threshold, a blocklist, an
eligibility rule, the Evidence Graph or a preparation. The only way a
learned signal reaches ordering is the pre-existing ``learned_prior``
priority component, and only when the candidate turns
``learning_settings.ordering_enabled`` on. Nothing here filters, and
nothing here is a top-N.
"""
