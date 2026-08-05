"""The gym wire protocol, in Python.

A direct mirror of ``gym-contract/src/wire.ts``. That file is the specification; this one is an
implementation of it. The two must agree on what every message *means* — which method names exist,
which error codes, which shapes decode and which are rejected — rather than on exact bytes, since
the two languages escape non-ASCII text differently. ``test_wire.py`` holds them to that, using the
shared cases in ``gym-contract/test/wire-conformance.json`` that the TypeScript suite also reads.

Transport is JSON-RPC 2.0, one message per line, UTF-8. Nothing but protocol messages may be written
to stdout: a stray ``print`` corrupts the stream. Diagnostics belong on stderr, which is never
parsed.

Kept dependency-free and importable on its own so that a gym can vendor this single file and speak
the protocol without taking on anything else from this repository.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

# JSON-RPC 2.0 protocol identifier. Always this exact string.
JSONRPC_VERSION = "2.0"

# Highest contract version this module implements. An integer, bumped only on a breaking change.
PROTOCOL_VERSION = 1


class WireErrorCode:
    """Why a call failed.

    The negative range is JSON-RPC's own and its meanings are fixed by that specification. The
    positive range is the gym contract's. A caller decides what to do from ``data.retryable`` rather
    than by matching on messages, which are free to change.
    """

    # Message was not valid JSON.
    PARSE_ERROR = -32700
    # Message was JSON but not a valid request.
    INVALID_REQUEST = -32600
    # No such method.
    METHOD_NOT_FOUND = -32601
    # Method exists, but the arguments were wrong.
    INVALID_PARAMS = -32602
    # The gym failed unexpectedly.
    INTERNAL_ERROR = -32603

    # The client speaks a protocol version this gym cannot serve.
    UNSUPPORTED_PROTOCOL = 1001
    # Called before `gym.initialize`.
    NOT_INITIALIZED = 1002
    # The method exists but this gym does not offer it; see `capabilities`.
    CAPABILITY_UNSUPPORTED = 1003

    # No session with that id, or it has been closed.
    SESSION_NOT_FOUND = 2001
    # The session is already serving another call.
    SESSION_BUSY = 2002

    # No rollout job with that id.
    JOB_NOT_FOUND = 3001
    # The job has not finished, so there is no result to collect.
    JOB_NOT_COMPLETE = 3002
    # The job was cancelled; there will be no result.
    JOB_CANCELLED = 3003

    # No custom operation of that name.
    UNKNOWN_CUSTOM_OP = 4001
    # Custom operations are not available to this caller.
    CUSTOM_FORBIDDEN = 4002


class WireMethod:
    """Every method a gym may serve.

    Grouped by prefix, which is also how a caller knows whether a world is involved: ``gym.*`` is
    metadata and touches no world, ``session.*`` addresses one world, ``rollout.*`` runs episodes,
    and ``custom`` is whatever a gym and its miner agreed on.
    """

    INITIALIZE = "gym.initialize"
    DESCRIBE = "gym.describe"
    HEALTH = "gym.health"
    MATERIALIZE_STATE = "gym.materializeState"

    SESSION_CREATE = "session.create"
    SESSION_CLOSE = "session.close"
    SESSION_CALL_TOOL = "session.callTool"
    SESSION_QUERY_STATE = "session.queryState"
    SESSION_VERIFY = "session.verify"

    ROLLOUT_SUBMIT = "rollout.submit"
    ROLLOUT_STATUS = "rollout.status"
    ROLLOUT_RESULT = "rollout.result"
    ROLLOUT_CANCEL = "rollout.cancel"

    CUSTOM = "custom"


def request(rid: int, method: str, params: Optional[Dict[str, Any]] = None) -> dict:
    """Build a request.

    ``params`` is omitted entirely when absent rather than sent as null, matching what the
    TypeScript implementation puts on the wire for the same call.
    """
    msg: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def success(rid: int, result: Any) -> dict:
    """Build a successful reply."""
    return {"jsonrpc": JSONRPC_VERSION, "id": rid, "result": result}


def failure(
    rid: Optional[int],
    code: int,
    message: str,
    *,
    kind: Optional[str] = None,
    retryable: Optional[bool] = None,
    details: Any = None,
) -> dict:
    """Build a failed reply.

    ``rid`` is None only when the request could not be parsed well enough to recover its id, which
    JSON-RPC represents as a null id.
    """
    data: Dict[str, Any] = {}
    if kind is not None:
        data["kind"] = kind
    if retryable is not None:
        data["retryable"] = retryable
    if details is not None:
        data["details"] = details
    error: Dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": rid, "error": error}


def is_request(msg: dict) -> bool:
    """True when a decoded message is a request rather than a reply."""
    return isinstance(msg.get("method"), str)


def is_failure(msg: dict) -> bool:
    """True when a reply reports failure."""
    return "error" in msg


def decode(line: str) -> Tuple[Optional[dict], Optional[dict]]:
    """Read one line into a message.

    Returns ``(message, None)`` on success and ``(None, error)`` otherwise, where ``error`` is the
    error object of a :func:`failure` — the caller supplies the id, since a message that failed to
    decode may not have a usable one.

    Checking for ``method`` before looking at the id matters: a reply is matched by id, so a request
    arriving at a peer that was expecting only replies would otherwise be mistaken for one and
    resolve an unrelated call with nothing. Failing here turns that into an error rather than
    corrupted data.
    """
    def bad(message: str, code: int = WireErrorCode.INVALID_REQUEST):
        return None, {"code": code, "message": message}

    try:
        # `parse_constant` fires for NaN and ±Infinity, which Python accepts by default and
        # JavaScript's JSON.parse rejects. Left alone, the two implementations would disagree about
        # whether a message is valid at all.
        parsed = json.loads(line, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        return bad("message is not valid JSON", WireErrorCode.PARSE_ERROR)

    # Batches and bare values are JSON-RPC features this protocol does not use; rejecting them
    # outright is clearer than half-supporting them.
    if not _is_object(parsed):
        return bad("message must be a single JSON object")

    if parsed.get("jsonrpc") != JSONRPC_VERSION:
        return bad('jsonrpc must be "%s"' % JSONRPC_VERSION)

    rid = parsed.get("id")
    if isinstance(parsed.get("method"), str):
        if not _is_request_id(rid):
            return bad("request id must be a safe integer")
        # Notifications — a request with no id, expecting no reply — are a JSON-RPC feature this
        # protocol does not use. Every call is answered, so a missing id is a mistake.
        if "params" in parsed and not _is_object(parsed["params"]):
            return bad("params must be an object", WireErrorCode.INVALID_PARAMS)
        return parsed, None

    has_result = "result" in parsed
    has_error = "error" in parsed
    # JSON-RPC requires exactly one. Accepting both would silently discard the result; accepting
    # neither leaves nothing to resolve the call with.
    if has_result == has_error:
        return bad(
            "response must carry result or error, not both" if has_result
            else "response must carry result or error"
        )

    if has_error:
        # A null id is how a peer reports a message it could not parse well enough to answer. It is
        # meaningful only on a failure: a result belonging to no call cannot be delivered.
        if rid is not None and not _is_request_id(rid):
            return bad("response id must be a safe integer or null")
        # Validated rather than trusted: a caller that reads ``error["code"]`` after seeing an
        # ``error`` key is doing the obvious thing, so a malformed error here would turn the peer's
        # failure into a crash on this side.
        err = parsed["error"]
        if not _is_object(err):
            return bad("error must be an object")
        # Same rule as an id, and for the same reason: an error code is an integer, and the two
        # sides must agree on which spellings of one are acceptable.
        if not _is_integral_number(err.get("code")):
            return bad("error.code must be an integer")
        if not isinstance(err.get("message"), str):
            return bad("error.message must be a string")
        return parsed, None

    if not _is_request_id(rid):
        return bad("response id must be a safe integer")
    return parsed, None


def _reject_constant(name: str):
    """Refuse the non-standard JSON constants Python would otherwise accept."""
    raise ValueError("%s is not valid JSON" % name)


def _is_object(value: Any) -> bool:
    """True for a JSON object, which in Python is a dict and nothing else."""
    return isinstance(value, dict)


# Beyond 2**53 a JavaScript peer cannot represent an id exactly, so two of its distinct calls would
# arrive as one. Python has no such limit, which makes it the side that has to enforce the shared one.
_MAX_SAFE_INTEGER = 2 ** 53 - 1


def _is_request_id(value: Any) -> bool:
    """Request ids are integers within the range both languages represent exactly.

    JSON-RPC permits strings too, but allowing both means two implementations can disagree about
    whether ``1`` and ``"1"`` are the same call, and that mismatch presents as a hang rather than an
    error. ``bool`` is rejected explicitly because Python makes it an ``int``, and ``true`` is not an
    id.

    ``1.0`` is accepted, and read as ``1``. Rejecting it would be the stricter rule but not a shared
    one: JavaScript parses ``1`` and ``1.0`` to the same value and cannot tell them apart afterwards,
    so a rule this side could enforce and the other could not is how the two implementations come to
    disagree about whether a message is valid — which is the thing this module exists to prevent.
    A true fraction is still rejected, on both sides.
    """
    return _is_integral_number(value) and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER


def _is_integral_number(value: Any) -> bool:
    """True for a JSON number with no fractional part, however it was spelled.

    ``bool`` is excluded because Python makes it an ``int``. A non-finite float is excluded because
    ``float.is_integer()`` is false for it, which is the answer wanted here anyway.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def encode(message: dict) -> str:
    """Serialise a message to the single line that goes on the wire, terminator included.

    ``ensure_ascii`` keeps the line pure ASCII whatever the payload holds, so a pipe whose encoding
    is not UTF-8 cannot mangle it. That makes this differ from ``JSON.stringify`` for non-ASCII text,
    which is fine — the two must agree on what a message *means*, not on its bytes.

    ``allow_nan`` is off, and that one matters. Python would otherwise write bare ``NaN`` or
    ``Infinity``, which are not JSON: ``JSON.parse`` refuses them, so the message would be unreadable
    by the very peer it is addressed to, and the call it answers would never complete — a hang rather
    than an error. Raising instead turns a non-finite float into a failure reply naming the handler
    that produced it, since :meth:`Peer._dispatch` catches it like any other handler error.

    JSON escapes any newline inside a string, so an encoded message can never contain a raw ``\\n``
    and split itself across two lines.
    """
    return json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
