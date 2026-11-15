"""Rate limits for input commands (PLAN.md 6.3): per user one command every
2 s with a burst of 3, and 8 inputs per second for everyone together.

Token buckets on an injected clock (seconds), so tests and the simulator
control time. A command takes a token from both buckets or from neither:
being refused by the global limit doesn't cost the user anything.
"""

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["ok", "user", "global"]


@dataclass
class TokenBucket:
    rate: float  # tokens per second
    burst: float  # bucket size
    tokens: float = field(init=False)
    stamp: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.burst
        self.stamp = float("-inf")

    def refill(self, now: float) -> None:
        if self.stamp != float("-inf"):
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def full(self, now: float) -> bool:
        self.refill(now)
        return self.tokens >= self.burst


@dataclass
class RateLimiter:
    user_rate: float = 0.5
    user_burst: float = 3
    global_rate: float = 8
    global_burst: float = 8
    max_users: int = 5000  # buckets kept before idle ones are pruned
    _users: dict[str, TokenBucket] = field(default_factory=dict[str, TokenBucket])
    _global: TokenBucket = field(init=False)
    _pruned_at: float = float("-inf")

    def __post_init__(self) -> None:
        self._global = TokenBucket(self.global_rate, self.global_burst)

    def allow(self, user_id: str, now: float) -> Verdict:
        bucket = self._users.get(user_id)
        if bucket is None:
            if len(self._users) >= self.max_users and now - self._pruned_at >= 1:
                self._prune(now)  # at most once a second: a raid of new accounts stays cheap
            bucket = self._users[user_id] = TokenBucket(self.user_rate, self.user_burst)
        bucket.refill(now)
        self._global.refill(now)
        if bucket.tokens < 1:
            return "user"
        if self._global.tokens < 1:
            return "global"
        bucket.tokens -= 1
        self._global.tokens -= 1
        return "ok"

    def _prune(self, now: float) -> None:
        """Forget users whose bucket has refilled: they'd start full anyway."""
        self._pruned_at = now
        for user_id in [u for u, b in self._users.items() if b.full(now)]:
            del self._users[user_id]
