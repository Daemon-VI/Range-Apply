"""Blueprint Phase 6: the execution foundation.

Consumes READY attempts (a READY preparation attached to a reserved
application attempt) and drives them through a guarded, idempotent,
auditable submission workflow. No browser automation lives here; executors
plug into :mod:`app.execution.executors.base`.
"""
