"""Progressive pipeline foundation (blueprint §4, §5, §8).

Shared market identity (``opportunities``) is kept apart from candidate state
(``candidate_opportunities``); eligibility, fit and priority are three separate
persisted concepts; the application queue is a Postgres/SQLite table keyed by
``(tenant, opportunity, action)``.
"""
