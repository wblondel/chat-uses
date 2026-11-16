from pathlib import Path

import pytest

from chatuses.referee import laws
from chatuses.referee.laws import DEFAULT_TIERS, Laws, RulesetError, Tier

RULESETS = Path(__file__).resolve().parents[2] / "rulesets"


def test_the_shipped_ruleset_is_plans() -> None:
    ruleset = laws.load(RULESETS, "roguelike")
    assert ruleset.name == "roguelike" and ruleset.target_profile == "debian-xfce"
    assert ruleset.features == laws.FEATURES and ruleset.disabled == frozenset()
    assert ruleset.laws.democracy_window_s == 12 and ruleset.laws.auto_snapshot_min == 15
    assert ruleset.laws.tier("rewind") == Tier(0.66, 0.25, 3, 30, 30, 10)
    assert ruleset.laws.tier("newrun") == Tier(0.75, 0.35, 4, 45, 60, 60)
    assert ruleset.laws.tier("reboot") == DEFAULT_TIERS["reboot"]  # left out: PLAN.md 6.4's
    assert laws.available(RULESETS) == ["roguelike"]


def test_quorum_is_a_share_of_the_active_users_with_a_floor() -> None:
    rewind = DEFAULT_TIERS["rewind"]  # 25 %, at least 3
    assert [rewind.quorum(n) for n in (0, 4, 12, 13, 40)] == [3, 3, 3, 4, 10]


def test_a_partial_tier_keeps_the_rest_of_the_default() -> None:
    ruleset = laws.parse("tiers:\n  reboot: {window_s: 20}\n", "custom")
    assert ruleset.laws.tier("reboot") == Tier(0.5, 0.15, 2, 20, 0, 2)
    assert ruleset.laws.tier("reset") == DEFAULT_TIERS["reset"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("laws: {democracy_windw_s: 12}", "no law called 'democracy_windw_s'"),  # a typo isn't ignored
        ("laws: {auto_snapshot_min: 5}", "out of bounds (10 to 30)"),
        ("laws: {democracy_window_s: '12'}", "isn't a number"),
        ("laws: {democracy_window_s: true}", "isn't a number"),
        ("laws: {named_saves: 2.5}", "must be a whole number"),
        ("laws: {meter_anarchy_at: 40, meter_democracy_at: 51}", None),
        ("tiers: {rewnd: {window_s: 30}}", "no action called 'rewnd'"),
        ("tiers: {rewind: {window: 30}}", "no setting called 'window'"),
        ("tiers: {rewind: {threshold: 0.4}}", "out of bounds (0.5 to 1)"),
        ("features: [anarchy, telepathy]", "unknown telepathy"),
        ("commands: {disabled: [rm]}", "unknown rm"),
        ("commands: {enabled: [rewind]}", "commands: unknown keys enabled"),
        ("name: other", "isn't its file's"),
        ("cheats: true", "unknown keys: cheats"),
        ("- just\n- a list", "must be a mapping"),
        ("laws: {veto_min: [1, 2]}", "isn't a number"),
        ("laws: {a: b: c}", "not valid YAML"),
    ],
)
def test_a_bad_ruleset_is_refused_whole(text: str, error: str | None) -> None:
    if error is None:
        assert laws.parse(text, "custom").laws.meter_anarchy_at == 40
        return
    with pytest.raises(RulesetError, match=error.replace("(", r"\(").replace(")", r"\)")):
        laws.parse(text, "custom")


def test_disabled_commands_and_features() -> None:
    ruleset = laws.parse("features: [anarchy]\ncommands: {disabled: [newrun, rewind]}", "calm")
    assert ruleset.features == frozenset({"anarchy"})
    assert not ruleset.allows("newrun") and not ruleset.allows("rewind") and ruleset.allows("reboot")


def test_apply_changes_laws_and_tiers_within_bounds() -> None:
    changed = laws.apply(Laws(), {"democracy_window_s": 20, "tiers.rewind.window_s": 60})
    assert changed.democracy_window_s == 20 and changed.tier("rewind").window_s == 60
    assert Laws().tier("rewind").window_s == 30  # the original is untouched
    with pytest.raises(RulesetError, match="out of bounds"):
        laws.apply(Laws(), {"tiers.rewind.window_s": 3600})
    with pytest.raises(RulesetError, match=r"write tiers\.ACTION\.SETTING"):
        laws.apply(Laws(), {"tiers.rewind": 60})
    with pytest.raises(RulesetError, match="out of bounds"):  # the bounds keep anarchy below democracy
        laws.apply(Laws(), {"meter_anarchy_at": 50})
    assert laws.apply(Laws(), {"democracy_window_s": 20.0}).democracy_window_s == 20  # from the store's JSON


def test_loading_refuses_odd_names_missing_files_and_huge_ones(tmp_path: Path) -> None:
    for name in ("../etc/passwd", "", "a b", "x" * 41):
        with pytest.raises(RulesetError, match="isn't a ruleset name"):
            laws.load(tmp_path, name)
    with pytest.raises(RulesetError, match="no ruleset 'nope'"):
        laws.load(tmp_path, "nope")
    (tmp_path / "big.yaml").write_text("# " + "x" * laws.MAX_FILE)
    with pytest.raises(RulesetError, match="too big"):
        laws.load(tmp_path, "big")
    (tmp_path / "ok.yaml").write_text("description: fine\n")
    assert laws.load(tmp_path, "ok").laws == Laws()
