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
    MAX_KEY_LENGTH,
    ClaimLostWarning,
    FingerprintMismatchError,
    IdemKeyError,
    InflightTimeoutError,
    InvalidKeyError,
    MissingKeyError,
    OutcomeUnknownError,
    PreviousAttemptFailedError,
    ResultNotReplayableError,
    RetryableError,
    VerificationError,
    get_default_store,
    guard,
    namespaced_key,
)
from .keys import derive_key, fingerprint, is_derived, new_key
from .store import DedupStore, StoreCorruptedError

__version__ = "0.2.0"

__all__ = [
    "MAX_KEY_LENGTH",
    "ClaimLostWarning",
    "DedupStore",
    "FingerprintMismatchError",
    "IdemKeyError",
    "InflightTimeoutError",
    "InvalidKeyError",
    "MissingKeyError",
    "OutcomeUnknownError",
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
    "namespaced_key",
    "new_key",
]
