"""idemkey — drop-in idempotency keys for agent tool calls.

Wrap any tool so retries reuse a stable key and the wrapper deduplicates::

    from idemkey import guard, new_key

    capture = guard(raw_capture_order, verify=order_exists)

    capture(order, idemkey="order-7f3a")   # first call executes
    capture(order, idemkey="order-7f3a")   # retry replays the stored result

At-least-once execution, exactly-once *effect* (as observed via the store).
"""

from .adapters import mcp_tool
from .guard import (
    FingerprintMismatchError,
    IdemKeyError,
    InflightTimeoutError,
    PreviousAttemptFailedError,
    ResultNotReplayableError,
    RetryableError,
    VerificationError,
    get_default_store,
    guard,
)
from .keys import derive_key, fingerprint, is_derived, new_key
from .store import DedupStore, StoreCorruptedError

__version__ = "0.1.1"

__all__ = [
    "DedupStore",
    "FingerprintMismatchError",
    "IdemKeyError",
    "InflightTimeoutError",
    "PreviousAttemptFailedError",
    "ResultNotReplayableError",
    "RetryableError",
    "StoreCorruptedError",
    "VerificationError",
    "derive_key",
    "fingerprint",
    "get_default_store",
    "guard",
    "is_derived",
    "mcp_tool",
    "new_key",
]
