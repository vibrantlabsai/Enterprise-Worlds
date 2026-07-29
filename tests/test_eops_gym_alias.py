"""The deprecated ``eops_gym`` alias must expose the same module objects.

Guards the downstream synthetic-enterprise-ops bridge contract: it imports
``eops_gym.*`` and monkeypatches module attributes by name, which only works if
the alias and ``enterprise_worlds`` share module objects (not copies).
"""

import warnings


def test_alias_modules_are_identical():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import eops_gym  # noqa: F401
        from eops_gym.evaluator.evaluator_env import _build_gold_env  # noqa: F401
        import eops_gym.evaluator.evaluator_nl
        import eops_gym.evaluator.text_match_strategy
        import eops_gym.user.user_simulator

    import enterprise_worlds.evaluator.evaluator_nl
    import enterprise_worlds.evaluator.text_match_strategy
    import enterprise_worlds.user.user_simulator

    assert eops_gym.user.user_simulator is enterprise_worlds.user.user_simulator
    assert eops_gym.evaluator.evaluator_nl is enterprise_worlds.evaluator.evaluator_nl
    assert eops_gym.evaluator.text_match_strategy is enterprise_worlds.evaluator.text_match_strategy


def test_alias_keeps_monkeypatch_seams():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import eops_gym.evaluator.evaluator_nl as n
        import eops_gym.evaluator.text_match_strategy as t
        import eops_gym.user.user_simulator as u

    assert all(hasattr(m, "generate") for m in (u, n, t))


def test_alias_patch_reaches_real_module(monkeypatch):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import eops_gym.user.user_simulator as old

    import enterprise_worlds.user.user_simulator as new

    sentinel = object()
    monkeypatch.setattr(old, "generate", sentinel)
    assert new.generate is sentinel
