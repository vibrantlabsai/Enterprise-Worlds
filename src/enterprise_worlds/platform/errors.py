"""Protocol-failure exception shared by the server and its method modules.

Raising :class:`WireFailure` inside a handler becomes a JSON-RPC failure reply with the given code;
any other exception becomes ``INTERNAL_ERROR``. Kept in its own module so ``state``/``sessions``/
``rollout`` can raise typed failures without importing the server (and the server without importing
them at module level — they load lazily, after the stdout redirect).
"""
from __future__ import annotations

from typing import Any, Optional


class WireFailure(Exception):
    """A handler outcome that is a protocol error rather than a result."""

    def __init__(self, code: int, message: str, *, kind: Optional[str] = None,
                 retryable: Optional[bool] = None, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.retryable = retryable
        self.details = details
