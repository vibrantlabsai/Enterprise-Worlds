"""``eworlds serve`` answers the gym wire protocol over stdio.

Two layers. The in-process tests drive :class:`Server` through StringIO streams and pin the protocol
semantics: the initialize gate, version negotiation, the error-code map, and one-bad-line-does-not-
end-the-connection. The subprocess tests are the handoff's acceptance checks verbatim — the real
``eworlds serve`` process answering over a real pipe, with nothing but protocol on stdout even when
a gym module prints at import time.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from enterprise_worlds.platform import wire
from enterprise_worlds.platform.server import Server

REPO_ROOT = Path(__file__).resolve().parents[1]

INITIALIZE = '{"jsonrpc":"2.0","id":1,"method":"gym.initialize","params":{"protocolVersion":1,"role":"platform"}}'


def drive(*lines: str) -> list[dict]:
    """Feed lines to an in-process Server and return its replies, decoded."""
    out = io.StringIO()
    Server(io.StringIO("".join(line + "\n" for line in lines)), out).serve_forever()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def req(rid: int, method: str, params: dict | None = None) -> str:
    return wire.encode(wire.request(rid, method, params)).rstrip("\n")


# ── protocol semantics, in process ────────────────────────────────────────────

def test_initialize_agrees_on_version_1_and_names_the_gym():
    (reply,) = drive(INITIALIZE)
    assert reply["id"] == 1
    result = reply["result"]
    assert result["protocolVersion"] == 1
    assert result["gymId"] == "itsm"


def test_anything_before_initialize_fails_not_initialized():
    for method in ("gym.health", "gym.describe", "session.create", "nonsense"):
        (reply,) = drive(req(7, method))
        assert reply["id"] == 7
        assert reply["error"]["code"] == wire.WireErrorCode.NOT_INITIALIZED, method


def test_no_version_overlap_is_refused_not_agreed_to():
    (reply,) = drive(req(1, "gym.initialize", {"protocolVersion": 3, "minProtocolVersion": 2, "role": "platform"}))
    assert reply["error"]["code"] == wire.WireErrorCode.UNSUPPORTED_PROTOCOL


def test_a_newer_client_that_still_speaks_v1_gets_v1():
    (reply,) = drive(req(1, "gym.initialize", {"protocolVersion": 3, "minProtocolVersion": 1, "role": "platform"}))
    assert reply["result"]["protocolVersion"] == 1


def test_initialize_rejects_a_bad_role_and_a_missing_version():
    for params in ({"protocolVersion": 1, "role": "wizard"}, {"role": "platform"},
                   {"protocolVersion": 1.5, "role": "platform"}):
        (reply,) = drive(req(1, "gym.initialize", params))
        assert reply["error"]["code"] == wire.WireErrorCode.INVALID_PARAMS, params


def test_describe_serves_the_descriptor_with_the_policy_text_inline():
    replies = drive(INITIALIZE, req(2, "gym.describe"))
    descriptor = replies[1]["result"]
    assert descriptor["protocolVersion"] == 1
    assert descriptor["gymId"] == "itsm"
    assert descriptor["displayName"] == "EnterpriseOps ITSM"
    # Nothing is implemented beyond the handshake yet, and an over-claim becomes a failure at the
    # call site — so the descriptor must claim nothing.
    assert descriptor["capabilities"] == []
    # The rules themselves, not a pointer to them.
    policy_on_disk = (REPO_ROOT / "data" / "itsmbench" / "policy.md").read_text(encoding="utf-8")
    assert descriptor["policy"] == policy_on_disk
    assert descriptor["reviewPrimer"].startswith("How the gym runs this task:")
    assert [s["id"] for s in descriptor["taskView"]["sections"]] == [
        "task", "sim", "persona", "scope", "setup", "gold", "assertions",
    ]
    aliases = {t["alias"]: t for t in descriptor["rolloutTargets"]}
    assert set(aliases) == {"opus", "sonnet", "sarvam"}
    assert aliases["sarvam"].get("isDefault") is True


def test_health_reports_ready():
    replies = drive(INITIALIZE, req(2, "gym.health"))
    assert replies[1]["result"] == {"ready": True}


def test_a_contract_method_not_served_yet_is_capability_unsupported():
    for method in ("gym.materializeState", "session.create", "session.callTool", "rollout.submit"):
        replies = drive(INITIALIZE, req(2, method))
        assert replies[1]["error"]["code"] == wire.WireErrorCode.CAPABILITY_UNSUPPORTED, method


def test_an_unknown_method_is_method_not_found():
    replies = drive(INITIALIZE, req(2, "gym.frobnicate"))
    assert replies[1]["error"]["code"] == wire.WireErrorCode.METHOD_NOT_FOUND


def test_custom_is_forbidden_to_the_platform_and_undefined_for_a_miner():
    replies = drive(INITIALIZE, req(2, "custom"))
    assert replies[1]["error"]["code"] == wire.WireErrorCode.CUSTOM_FORBIDDEN

    miner_init = req(1, "gym.initialize", {"protocolVersion": 1, "role": "miner"})
    replies = drive(miner_init, req(2, "custom"))
    assert replies[1]["error"]["code"] == wire.WireErrorCode.UNKNOWN_CUSTOM_OP


def test_reinitialize_is_idempotent_but_the_role_is_fixed_for_the_connection():
    # A retried initialize (same role) answers identically; one that tries to switch role fails and
    # changes nothing — a platform connection must not shed the `custom` gate by asking again.
    replies = drive(INITIALIZE, INITIALIZE,
                    req(3, "gym.initialize", {"protocolVersion": 1, "role": "miner"}),
                    req(4, "custom"))
    assert replies[0]["result"] == replies[1]["result"]
    assert replies[2]["error"]["code"] == wire.WireErrorCode.INVALID_PARAMS
    assert replies[3]["error"]["code"] == wire.WireErrorCode.CUSTOM_FORBIDDEN  # still "platform"


def test_a_failed_reinitialize_leaves_the_connection_serving():
    replies = drive(INITIALIZE,
                    req(2, "gym.initialize", {"protocolVersion": 99, "minProtocolVersion": 99, "role": "platform"}),
                    req(3, "gym.health"))
    assert replies[1]["error"]["code"] == wire.WireErrorCode.UNSUPPORTED_PROTOCOL
    assert replies[2]["result"] == {"ready": True}


def test_a_bad_line_is_reported_and_the_next_call_still_answers():
    replies = drive(INITIALIZE, "not json", req(3, "gym.health"))
    assert len(replies) == 3
    assert replies[1]["id"] is None
    assert replies[1]["error"]["code"] == wire.WireErrorCode.PARSE_ERROR
    assert replies[2] == {"jsonrpc": "2.0", "id": 3, "result": {"ready": True}}


def test_an_inbound_reply_is_ignored_not_answered():
    replies = drive(INITIALIZE, '{"jsonrpc":"2.0","id":9,"result":{"stray":true}}', req(2, "gym.health"))
    assert [r.get("id") for r in replies] == [1, 2]


# ── acceptance: the real process over a real pipe ─────────────────────────────

def _serve(stdin_text: str, extra_pythonpath: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (extra_pythonpath, src, env.get("PYTHONPATH", "")) if p)
    return subprocess.run(
        [sys.executable, "-m", "enterprise_worlds.cli", "serve"],
        input=stdin_text, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT, env=env,
    )


def test_the_server_answers_over_a_pipe_and_stdout_stays_clean():
    calls = "\n".join([INITIALIZE, req(2, "gym.describe"), req(3, "gym.health")]) + "\n"
    proc = _serve(calls)
    lines = proc.stdout.splitlines()
    assert len(lines) == 3, proc.stderr[-2000:]
    replies = [json.loads(line) for line in lines]  # every stdout line parses, or this raises
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["gymId"] == "itsm"
    assert replies[1]["result"]["policy"]
    assert replies[2]["result"] == {"ready": True}
    assert proc.returncode == 0


def test_a_print_in_the_gym_import_path_lands_on_stderr_not_the_wire(tmp_path):
    """The check that the stdout redirect works.

    A meta-path hook (installed via sitecustomize, before the server starts) prints to stdout the
    moment the gym's environment module is imported — which the server does lazily, inside the first
    handler. The noise must reach stderr, and the wire must still parse line for line.
    """
    (tmp_path / "sitecustomize.py").write_text(
        "import importlib.abc, sys\n"
        "class _Noisy(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'enterprise_worlds.domains.itsm.environment':\n"
        "            print('noise')  # deliberate stdout write at gym import time\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Noisy())\n",
        encoding="utf-8",
    )
    calls = "\n".join([INITIALIZE, req(2, "gym.describe"), req(3, "gym.health")]) + "\n"
    proc = _serve(calls, extra_pythonpath=str(tmp_path))
    assert "noise" in proc.stderr, "the hook never fired: the import path must have changed"
    lines = proc.stdout.splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_malformed_input_does_not_kill_the_real_process():
    calls = "\n".join([INITIALIZE, "not json", req(3, "gym.health")]) + "\n"
    proc = _serve(calls)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 3
    assert replies[1]["error"]["code"] == wire.WireErrorCode.PARSE_ERROR
    assert replies[2]["result"] == {"ready": True}
    assert proc.returncode == 0


def test_undecodable_bytes_do_not_kill_the_server_whatever_the_locale():
    """A strict-errors stdin (en_US.UTF-8 and friends) must not turn one garbage byte into a crash
    that also swallows every valid line queued behind it. PYTHONIOENCODING pins the strict behaviour
    the server has to override, so the test does not depend on the machine's locale."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")) if p)
    env["PYTHONIOENCODING"] = "utf-8:strict"
    proc = subprocess.run(
        [sys.executable, "-m", "enterprise_worlds.cli", "serve"],
        input=b"\xff\xfe{bad}\n" + INITIALIZE.encode() + b"\n",
        capture_output=True, timeout=120, cwd=REPO_ROOT, env=env,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert replies[0]["error"]["code"] == wire.WireErrorCode.PARSE_ERROR
    assert replies[1]["result"]["gymId"] == "itsm"
    assert proc.returncode == 0


def test_serve_help_prints_help_instead_of_starting_the_server():
    proc = _serve_argv(["--help"])
    assert proc.returncode == 0
    assert "usage: eworlds serve" in proc.stdout
    assert "jsonrpc" not in proc.stdout  # help, not wire traffic


def test_serve_rejects_stray_arguments_like_every_other_subcommand():
    proc = _serve_argv(["extra"])
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "usage: eworlds serve" in proc.stderr


def _serve_argv(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")) if p)
    return subprocess.run(
        [sys.executable, "-m", "enterprise_worlds.cli", "serve", *argv],
        input="", capture_output=True, text=True, timeout=120, cwd=REPO_ROOT, env=env,
    )
