"""The vendored wire codec answers the shared conformance cases.

``src/enterprise_worlds/platform/wire.py`` is copied verbatim from the platform repository
(``task-generation/bridge/wire.py``, mirroring the authoritative ``gym-contract/src/wire.ts``), and
``tests/data/wire-conformance.json`` is the shared case file both of those are held to. Running the
copy against the same cases is what proves it faithful — and keeps it so if someone edits it here
instead of re-copying.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from enterprise_worlds.platform import wire

_CONFORMANCE = Path(__file__).parent / "data" / "wire-conformance.json"


def _cases():
    return json.loads(_CONFORMANCE.read_text(encoding="utf-8"))


def _kind(msg: dict) -> str:
    if wire.is_request(msg):
        return "request"
    return "failure" if wire.is_failure(msg) else "success"


@pytest.mark.parametrize("case", _cases()["accept"], ids=lambda c: c["why"])
def test_accepts_the_shared_cases(case):
    msg, err = wire.decode(case["line"])
    assert err is None, "expected to decode: %s" % case["line"]
    assert _kind(msg) == case["kind"]
    assert msg.get("id") == case["id"]
    if "method" in case:
        assert msg["method"] == case["method"]
    if "code" in case:
        assert msg["error"]["code"] == case["code"]


@pytest.mark.parametrize("case", _cases()["reject"], ids=lambda c: c["why"])
def test_rejects_the_shared_cases(case):
    msg, err = wire.decode(case["line"])
    assert msg is None, "expected to reject: %s" % case["line"]
    assert err["code"] == case["code"]


def test_rejects_the_json_constants_python_alone_accepts():
    """NaN and the infinities are Python-only spellings; JavaScript's JSON.parse refuses them."""
    for line in ('{"jsonrpc":"2.0","id":1,"result":NaN}',
                 '{"jsonrpc":"2.0","id":1,"result":Infinity}',
                 '{"jsonrpc":"2.0","id":1,"result":-Infinity}'):
        msg, err = wire.decode(line)
        assert msg is None
        assert err["code"] == wire.WireErrorCode.PARSE_ERROR


def test_encode_refuses_a_non_finite_float():
    """Bare NaN on the wire is unreadable by a JavaScript peer — a hang, not an error."""
    with pytest.raises(ValueError):
        wire.encode(wire.success(1, float("nan")))


def test_encode_keeps_a_message_on_one_line():
    payload = {"text": "a\nb\r\ncd e f"}
    line = wire.encode(wire.success(1, payload))
    assert line.endswith("\n")
    assert len(line[:-1].splitlines()) == 1
    msg, err = wire.decode(line)
    assert err is None
    assert msg["result"] == payload


def test_encode_output_decodes_back_to_the_same_message():
    for msg in (wire.request(1, wire.WireMethod.DESCRIBE),
                wire.request(2, wire.WireMethod.INITIALIZE, {"protocolVersion": 1, "role": "platform"}),
                wire.success(3, {"ok": True}),
                wire.success(4, None),
                wire.failure(5, wire.WireErrorCode.NOT_INITIALIZED, "not yet", kind="not_initialized"),
                wire.failure(None, wire.WireErrorCode.PARSE_ERROR, "bad json")):
        decoded, err = wire.decode(wire.encode(msg))
        assert err is None, msg
        assert decoded == msg
