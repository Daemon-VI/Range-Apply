"""Blueprint Phase 5: deterministic scheduler and policy enforcement.

The scheduler decides *which* admitted work enters the queue and in *what
order*; it never submits anything. See ``docs/BLUEPRINT.md`` Phase 5 and
``app/scheduler/service.py`` for the exact semantics of caps, cool-down,
blocklist and duplicates.
"""
