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
    NOT_FOUND,
    ClaimLostError,
    ClaimLostWarning,
    FingerprintMismatchError,
    IdemKeyError,
    InflightTimeoutError,
    InvalidKeyError,
    MissingKeyError,
    OutcomeUnknownError,
    PreviousAttemptFailedError,
    ReconcileError,
    ResultNotReplayableError,
    RetryableError,
    VerificationError,
    downstream_key,
    get_default_store,
    guard,
    namespaced_key,
    run,
)
from .keys import derive_key, fingerprint, fingerprint2, is_derived, new_key
from .store import DedupStore, StoreCorruptedError

__version__ = "0.3.0"

__all__ = [
    "MAX_KEY_LENGTH",
    "NOT_FOUND",
    "ClaimLostError",
    "ClaimLostWarning",
    "DedupStore",
    "FingerprintMismatchError",
    "IdemKeyError",
    "InflightTimeoutError",
    "InvalidKeyError",
    "MissingKeyError",
    "OutcomeUnknownError",
    "PreviousAttemptFailedError",
    "ReconcileError",
    "ResultNotReplayableError",
    "RetryableError",
    "StoreCorruptedError",
    "VerificationError",
    "derive_key",
    "downstream_key",
    "fingerprint",
    "fingerprint2",
    "get_default_store",
    "guard",
    "is_derived",
    "mcp_tool",
    "namespaced_key",
    "new_key",
    "run",
]
