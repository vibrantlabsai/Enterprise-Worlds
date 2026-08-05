"""The platform-facing side of the gym: the wire protocol and the stdio server.

``wire.py`` is vendored verbatim from ``synthetic-enterprise-ops`` (``task-generation/bridge/wire.py``,
itself a mirror of the authoritative ``gym-contract/src/wire.ts``) — do not edit it here; re-copy it.
``tests/data/wire-conformance.json`` is the shared conformance file that proves the copy faithful.

``server.py`` answers the protocol over stdin/stdout; ``descriptor.py`` is what it answers
``gym.describe`` with. Nothing in this package may write to stdout except through the wire handle
the server takes at startup.
"""
