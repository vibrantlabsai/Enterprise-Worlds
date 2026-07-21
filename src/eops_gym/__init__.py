"""Deprecated alias for :mod:`enterprise_worlds` (this package's former name).

Downstream consumers — notably the synthetic-enterprise-ops bridge, which imports
``eops_gym.*`` with ``PYTHONPATH=gym/src`` and monkeypatches module attributes
(e.g. ``eops_gym.user.user_simulator.generate``) by name — keep working because
every ``eops_gym.<submodule>`` entry in ``sys.modules`` is the *same object* as
its ``enterprise_worlds.<submodule>`` counterpart, so patching one patches both.

Delete this package (and its entry in ``pyproject.toml``) once nothing imports
``eops_gym``.
"""

import importlib
import pkgutil
import sys
import warnings

import enterprise_worlds as _ew

warnings.warn(
    "the 'eops_gym' package was renamed to 'enterprise_worlds'; "
    "update imports — this alias will be removed",
    DeprecationWarning,
    stacklevel=2,
)

# Import every submodule eagerly so the aliases below exist before any
# ``import eops_gym.x.y`` resolves. Optional extras (e.g. the gymnasium
# interface) are skipped if their dependencies are missing.
for _info in pkgutil.walk_packages(_ew.__path__, prefix="enterprise_worlds.", onerror=lambda _name: None):
    try:
        importlib.import_module(_info.name)
    except ImportError:
        continue

for _name, _module in list(sys.modules.items()):
    if _name == "enterprise_worlds" or _name.startswith("enterprise_worlds."):
        sys.modules["eops_gym" + _name[len("enterprise_worlds"):]] = _module
