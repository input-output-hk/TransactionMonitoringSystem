"""Reconnection resilience: exponential backoff with jitter and circuit breaker"""

import asyncio
import logging
import random
import time
from enum import Enum

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection"""

    def __init__(self, base_delay: float = 1.0, max_delay: float = 60.0, jitter_factor: float = 0.3):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter_factor = jitter_factor
        self.attempt = 0

    async def wait(self):
        """Wait with exponential backoff + jitter, then increment attempt"""
        delay = min(self.base_delay * (2 ** self.attempt), self.max_delay)
        jitter = random.uniform(0, delay * self.jitter_factor)
        total = delay + jitter
        logger.info(f"Backoff: waiting {total:.1f}s (attempt {self.attempt + 1})")
        await asyncio.sleep(total)
        self.attempt += 1

    def reset(self):
        """Reset attempt counter on successful connection"""
        self.attempt = 0


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Circuit breaker to prevent resource exhaustion during extended outages"""

    def __init__(self, failure_threshold: int = 5, failure_window: float = 300.0,
                 cooldown: float = 120.0):
        self.failure_threshold = failure_threshold
        self.failure_window = failure_window  # seconds
        self.cooldown = cooldown  # seconds
        self.state = CircuitState.CLOSED
        self._failures: list[float] = []
        self._opened_at: float = 0.0

    def can_attempt(self) -> bool:
        """Check if a connection attempt is allowed"""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.cooldown:
                logger.info("Circuit breaker: OPEN → HALF_OPEN (cooldown elapsed)")
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow single probe
        return True

    def record_failure(self):
        """Record a connection failure"""
        now = time.monotonic()
        self._failures.append(now)
        # Prune old failures outside the window
        self._failures = [t for t in self._failures if now - t <= self.failure_window]

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("Circuit breaker: HALF_OPEN → OPEN (probe failed)")
            self.state = CircuitState.OPEN
            self._opened_at = now
        elif len(self._failures) >= self.failure_threshold:
            logger.warning(f"Circuit breaker: CLOSED → OPEN ({len(self._failures)} failures in window)")
            self.state = CircuitState.OPEN
            self._opened_at = now

    def record_success(self):
        """Record a successful connection"""
        if self.state != CircuitState.CLOSED:
            logger.info(f"Circuit breaker: {self.state} → CLOSED (connection succeeded)")
        self.state = CircuitState.CLOSED
        self._failures.clear()
