"""Lightweight call budgets. Not a quota service: a counter per scope
(discovery run, preparation package) that the gateway consults before each
provider call. Cache hits are free and never counted."""

from collections import Counter
from typing import Optional


class CallBudget:
    def __init__(self, max_calls: Optional[int], name: str = "run"):
        self.max_calls = max_calls
        self.name = name
        self.calls = 0
        self.refused = 0
        self.by_operation: Counter = Counter()

    @property
    def unlimited(self) -> bool:
        return self.max_calls is None

    @property
    def remaining(self) -> Optional[int]:
        return None if self.max_calls is None else max(0, self.max_calls - self.calls)

    def can_call(self) -> bool:
        return self.max_calls is None or self.calls < self.max_calls

    def take(self, operation: str) -> bool:
        """Reserve one call; False (and counted as refused) when exhausted."""
        if not self.can_call():
            self.refused += 1
            return False
        self.calls += 1
        self.by_operation[operation] += 1
        return True

    def reset(self) -> None:
        self.calls = 0
        self.refused = 0
        self.by_operation.clear()

    def snapshot(self) -> dict:
        return {"name": self.name, "max_calls": self.max_calls, "calls": self.calls, "refused": self.refused, "remaining": self.remaining}
