"""``eworlds serve`` — answer the gym wire protocol over stdio.

JSON-RPC 2.0, one message per line, UTF-8, as specified by ``gym-contract/src/wire.ts`` and
implemented for this side by the vendored ``wire.py``. The platform calls the gym; the gym answers
and never initiates. A gym that needs a model reaches it directly with configuration handed to it at
startup (litellm reads credentials from the environment), so there is no callback channel here.

Served today: ``gym.initialize``, ``gym.describe``, ``gym.health``. Every other contract method
answers ``CAPABILITY_UNSUPPORTED`` until it is implemented — and gains its ``capabilities`` entry in
the descriptor only then.

This module is imported before :func:`main` redirects ``sys.stdout``, so its module level must stay
free of gym imports: anything the gym prints at import time would land on the wire. Gym modules are
imported lazily inside handlers, which only ever run after the redirect.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from enterprise_worlds.platform import descriptor as _descriptor
from enterprise_worlds.platform import wire
from enterprise_worlds.platform.wire import PROTOCOL_VERSION, WireErrorCode, WireMethod

_REPO_ROOT = Path(__file__).resolve().parents[3]

_IMPLEMENTED = frozenset({WireMethod.INITIALIZE, WireMethod.DESCRIBE, WireMethod.HEALTH})
_KNOWN = frozenset(v for k, v in vars(WireMethod).items() if not k.startswith("_"))


class _Failure(Exception):
    """A handler outcome that is a protocol error rather than a result."""

    def __init__(self, code: int, message: str, *, kind: Optional[str] = None,
                 retryable: Optional[bool] = None, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.retryable = retryable
        self.details = details


_sha_cache: Optional[Dict[str, Optional[str]]] = None


def gym_commit_sha() -> Optional[str]:
    """The build actually answering, when it can be determined; resolved once per process.

    ``GYM_COMMIT_SHA`` (injected by a runner, or baked at image build) wins; otherwise the checkout
    is asked directly. A container built without ``.git`` or the arg simply has no sha, which the
    contract allows — the field records a fact, so it is resolved rather than declared.
    """
    global _sha_cache
    if _sha_cache is None:
        sha = os.environ.get("GYM_COMMIT_SHA") or None
        if sha is None:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                sha = proc.stdout.strip() or None if proc.returncode == 0 else None
            except Exception:  # noqa: BLE001 - no git, no checkout: the sha is simply unknown
                sha = None
        _sha_cache = {"sha": sha}
    return _sha_cache["sha"]


class Server:
    """A read-line / dispatch / write-line loop over two text streams.

    Streams are injected so tests can drive it in-process; :func:`main` hands it the real pipe ends.
    """

    def __init__(self, inp: TextIO, out: TextIO) -> None:
        self.inp = inp
        self.out = out
        self._initialized = False
        self._role: Optional[str] = None

    # ── wire ──────────────────────────────────────────────────────────────

    def _write(self, msg: dict) -> None:
        self.out.write(wire.encode(msg))
        self.out.flush()

    def serve_forever(self) -> None:
        """Answer requests until the stream closes."""
        while True:
            try:
                raw = self.inp.readline()
            except UnicodeDecodeError as e:
                # Undecodable bytes on a strict-errors stream. :func:`main` configures stdin with
                # surrogateescape so this does not happen on the real pipe; for a stream that raises
                # anyway, the wrapper's buffer state after the error is undefined, so continuing
                # risks answering garbage forever. Report once and end the connection instead.
                self._write(wire.failure(None, WireErrorCode.PARSE_ERROR,
                                         f"stdin is not decodable text: {e}"))
                return
            if not raw:
                return
            line = raw.strip()
            if not line:
                continue
            msg, err = wire.decode(line)
            if err is not None:
                # Reported with a null id — there is no request id to answer — then skipped, so one
                # unreadable line does not end a connection that is otherwise healthy.
                self._write(wire.failure(None, err["code"], err["message"]))
                continue
            if not wire.is_request(msg):
                # This side never initiates, so no reply can be owed to it. A failure with a null id
                # is the peer reporting a line *it* could not read — most often a stray write to
                # stdout from this process. Either way it answers nothing and is logged, not answered:
                # replying to a reply is how two peers start resolving each other's ghosts.
                if wire.is_failure(msg) and msg.get("id") is None:
                    sys.stderr.write("[serve] peer could not read a line we sent: %s\n"
                                     % msg["error"].get("message"))
                else:
                    sys.stderr.write("[serve] ignoring unexpected reply for id %r\n" % (msg.get("id"),))
                continue
            self._dispatch(msg)

    def _dispatch(self, req: dict) -> None:
        """Run one request and reply.

        A handler that raises becomes a failure reply rather than killing the process: the peer is
        blocked waiting for this id, so an unanswered request would hang it until the pipe closes.
        The success write sits inside the try so a result :func:`wire.encode` refuses (a non-finite
        float) also comes back as a failure naming the handler, not a hang.
        """
        rid = req["id"]
        try:
            self._write(wire.success(rid, self._handle(req["method"], req.get("params") or {})))
        except _Failure as f:
            self._write(wire.failure(rid, f.code, str(f), kind=f.kind,
                                     retryable=f.retryable, details=f.details))
        except Exception as e:  # noqa: BLE001 - surfaced to the peer instead of crashing this side
            self._write(wire.failure(
                rid, WireErrorCode.INTERNAL_ERROR, f"{type(e).__name__}: {e}",
                kind="handler_raised", retryable=False,
            ))

    # ── dispatch ──────────────────────────────────────────────────────────

    def _handle(self, method: str, params: dict) -> Any:
        if method == WireMethod.INITIALIZE:
            return self._initialize(params)
        if not self._initialized:
            raise _Failure(WireErrorCode.NOT_INITIALIZED,
                           "gym.initialize must be the first call on a connection",
                           kind="not_initialized", retryable=False)
        if method == WireMethod.DESCRIBE:
            return _descriptor.build_descriptor(gym_commit_sha())
        if method == WireMethod.HEALTH:
            return self._health()
        if method == WireMethod.CUSTOM:
            # A platform never calls `custom` (wire.ts `isCallableBy`); a miner may, but this gym
            # has agreed no custom operations with anyone.
            if self._role == "miner":
                raise _Failure(WireErrorCode.UNKNOWN_CUSTOM_OP,
                               "this gym defines no custom operations",
                               kind="unknown_custom_op", retryable=False)
            raise _Failure(WireErrorCode.CUSTOM_FORBIDDEN,
                           "custom operations are not available to a platform caller",
                           kind="custom_forbidden", retryable=False)
        if method in _KNOWN:
            raise _Failure(WireErrorCode.CAPABILITY_UNSUPPORTED,
                           f"this gym does not offer {method} yet; see gym.describe capabilities",
                           kind="capability_unsupported", retryable=False,
                           details={"capabilities": list(_descriptor.CAPABILITIES)})
        raise _Failure(WireErrorCode.METHOD_NOT_FOUND, f"no such method: {method}",
                       kind="method_not_found", retryable=False,
                       details={"known": sorted(_IMPLEMENTED)})

    # ── handlers ──────────────────────────────────────────────────────────

    def _initialize(self, params: dict) -> dict:
        client_max = _integral(params.get("protocolVersion"))
        if client_max is None:
            raise _Failure(WireErrorCode.INVALID_PARAMS, "protocolVersion must be an integer",
                           kind="invalid_params", retryable=False)
        client_min = client_max
        if "minProtocolVersion" in params:
            client_min = _integral(params["minProtocolVersion"])
            if client_min is None or client_min > client_max:
                raise _Failure(WireErrorCode.INVALID_PARAMS,
                               "minProtocolVersion must be an integer no greater than protocolVersion",
                               kind="invalid_params", retryable=False)
        role = params.get("role")
        if role not in ("platform", "miner"):
            raise _Failure(WireErrorCode.INVALID_PARAMS, 'role must be "platform" or "miner"',
                           kind="invalid_params", retryable=False)
        # Re-initialize is tolerated (a client that lost a reply may retry) but the role is fixed
        # for the connection's lifetime: what a caller may do — the `custom` gate — was decided by
        # the role this connection was opened with, and must not be shed by asking again. A failed
        # re-initialize (this error, or no version overlap below) leaves the established connection
        # exactly as it was.
        if self._initialized and role != self._role:
            raise _Failure(WireErrorCode.INVALID_PARAMS,
                           f"role cannot change after initialize; this connection is {self._role!r}",
                           kind="invalid_params", retryable=False)
        # This gym speaks exactly one version. The agreed version is the highest both sides support;
        # no overlap means refusing honestly rather than agreeing to something one side cannot honour.
        if not client_min <= PROTOCOL_VERSION <= client_max:
            raise _Failure(
                WireErrorCode.UNSUPPORTED_PROTOCOL,
                f"this gym speaks protocol version {PROTOCOL_VERSION}; "
                f"the client offered {client_min}..{client_max}",
                kind="unsupported_protocol", retryable=False,
                details={"supported": {"min": PROTOCOL_VERSION, "max": PROTOCOL_VERSION}},
            )
        self._initialized = True
        self._role = role
        result: Dict[str, Any] = {"protocolVersion": PROTOCOL_VERSION, "gymId": _descriptor.GYM_ID}
        sha = gym_commit_sha()
        if sha:
            result["gymCommitSha"] = sha
        return result

    def _health(self) -> dict:
        """Whether the gym can accept work. Reported, never raised: unhealthy is an answer."""
        try:
            from enterprise_worlds.domains.itsm.environment import ITSM_DB_PATH, ITSM_POLICY_PATH

            missing = [str(p) for p in (ITSM_DB_PATH, ITSM_POLICY_PATH) if not p.exists()]
            if missing:
                return {"ready": False, "detail": "missing data files: %s" % ", ".join(missing)}
            return {"ready": True}
        except Exception as e:  # noqa: BLE001 - a health probe reports faults, it does not throw them
            return {"ready": False, "detail": f"{type(e).__name__}: {e}"}


def _integral(value: Any) -> Optional[int]:
    """The value as an int when it is an integral JSON number (``1.0`` reads as ``1``), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def main() -> None:
    """Entry point for ``eworlds serve``.

    stdout is the wire, and nothing else may be written to it: a stray ``print`` — from here, from
    the gym, or from any library either imports — lands in the middle of the byte stream, and without
    a trailing newline it prefixes the next protocol message, which is then lost and its caller hangs.
    So the wire is taken here and ``sys.stdout`` is pointed at stderr for the rest of the process:
    anything that prints still prints, harmlessly, where it can be read. Gym modules are imported
    lazily by the handlers, after this redirect, so even their import-time noise is safe.
    """
    wire_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        # Whether undecodable stdin bytes raise depends on the invoking locale (strict under
        # en_US.UTF-8, surrogateescape under C.UTF-8): pin the lenient behaviour, so one byte of
        # garbage becomes a PARSE_ERROR reply on the next decode instead of a locale-dependent crash
        # that also swallows every valid line queued behind it.
        sys.stdin.reconfigure(errors="surrogateescape")
    except (AttributeError, ValueError):  # a redirected/exotic stdin; served as-is
        pass
    try:
        Server(sys.stdin, wire_out).serve_forever()
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
