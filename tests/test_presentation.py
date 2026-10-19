"""场景构建：实例数量/颜色确定性/排序稳定性 + HUD 布局。"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster, spawn_player
from mirsolo.bootstrap import build_session
from mirsolo.components import Bag, Position, Progression, Vitals, Wallet
from mirsolo.data.loader import GameData
from mirsolo.presentation.hud import build_hud, clamp_ratio
from mirsolo.presentation.scene import (
    PLAYER_COLOR,
    SPECIES_COLOR,
    build_instances,
    species_color,
    tile_color,
)

CASES = []
DATA = GameData.load()
SKILLS = DATA.skills["skills"]


def case(fn):
    CASES.append(fn)
    return fn


# ── 场景 ────────────────────────────────────────────

@case
def test_instances_match_alive_entities():
    session = build_session(data=DATA, seed=1)
    try:
        player = spawn_player(session.world, DATA)
        mob_a = spawn_monster(session.world, DATA, "ji", x=1, y=1)
        mob_b = spawn_monster(session.world, DATA, "kulou", x=2, y=2)
        instances = build_instances(session.world, DATA)
        assert len(instances) == 3, len(instances)

        mob_b.get(Vitals).hp = 0  # 尸体不画
        instances = build_instances(session.world, DATA)
        assert len(instances) == 2
        assert all(i.kind in ("player", "monster") for i in instances)

        player_inst = next(i for i in instances if i.kind == "player")
        assert player_inst.color == PLAYER_COLOR
        assert player_inst.x == player.get(Position).x

        mob_inst = next(i for i in instances if i.eid == mob_a.eid)
        assert mob_inst.color == (*SPECIES_COLOR["ji"], 1.0)
    finally:
        session.shutdown()


@case
def test_species_color_is_deterministic_and_level_boosted():
    a = species_color("jiangshi", 12)
    b = species_color("jiangshi", 12)
    assert a == b
    boosted = species_color("jiangshi", 30)
    assert boosted[3] == 1.0 and all(0 <= c <= 1.0 for c in boosted)
    # 白名单物种不受哈希影响
    assert species_color("ji", 1) == (*SPECIES_COLOR["ji"], 1.0)


@case
def test_tile_color_is_deterministic():
    for tx, ty in ((0, 0), (17, 33), (63, 63)):
        assert tile_color("biqi_field", "field", tx, ty) == \
               tile_color("biqi_field", "field", tx, ty)
    samples = {tile_color("biqi_field", "field", tx, 0) for tx in range(30)}
    assert len(samples) > 5, "30 个格子颜色完全相同，微扰失效"


@case
def test_instances_sorted_far_to_near():
    session = build_session(data=DATA, seed=1)
    try:
        spawn_player(session.world, DATA)
        for i in range(8):
            spawn_monster(session.world, DATA, "ji", x=float(i * 3), y=float((7 - i) * 4))
        instances = build_instances(session.world, DATA)
        keys = [i.sort_key for i in instances]
        assert keys == sorted(keys)
    finally:
        session.shutdown()


# ── HUD ────────────────────────────────────────────

def _layout(**kw):
    defaults = dict(
        vitals=Vitals(hp=15, max_hp=30, mp=2, max_mp=10),
        progression=Progression(level=3, exp=150),
        wallet=Wallet(gold=1234),
        bag=Bag(items={"mujian": 2, "buyi": 1}),
        data=DATA,
        kills=7,
        map_name="比奇省外围",
        width=1280,
        height=720,
    )
    defaults.update(kw)
    return build_hud(**defaults)


@case
def test_bar_ratios_and_fill_width():
    layout = _layout()
    assert layout.hp.ratio == 0.5
    assert abs(layout.hp.fill.w - layout.hp.frame.w * 0.5) < 1e-9
    assert layout.mp.ratio == 0.2
    # 3→4 级需要 300（levels["3"]），别和 13 级的 15000 记混
    assert layout.exp.caption == "EXP 150/300", layout.exp.caption


@case
def test_ratio_clamped():
    assert clamp_ratio(5, 0) == 0.0
    assert clamp_ratio(-1, 10) == 0.0
    assert clamp_ratio(99, 10) == 1.0


@case
def test_hud_stays_inside_viewport():
    layout = _layout(width=800, height=600)
    for bar in (layout.hp, layout.mp, layout.exp):
        assert bar.frame.x >= 0 and bar.frame.right <= 800
        assert bar.frame.y >= 0 and bar.frame.top <= 600
    for panel in layout.panels.values():
        assert 0 <= panel.x and panel.right <= 800
        assert 0 <= panel.y and panel.top <= 600


@case
def test_hud_counts_texts():
    layout = _layout()
    joined = " ".join(t.text for t in layout.texts)
    assert "Lv 3" in joined and "金币 1234" in joined
    assert "击杀 7" in joined and "背包 2/48" in joined
    assert "比奇省外围" in joined


@case
def test_hud_handles_max_level_caption():
    from mirsolo.components import Progression as P

    layout = _layout(progression=P(level=50, exp=0))
    assert layout.exp.caption == "MAX"
    assert layout.exp.ratio == 0.0


@case
def test_hud_skill_slots_order_and_readiness():
    """技能栏：顺序 = 传入序（键位 1..5），就绪态由冷却值决定。"""
    from mirsolo.components import Cooldowns as C

    known = {"zhaohuankulou": 1, "zhiyu": 2, "shidu": 1}
    layout = _layout(known_skills=known,
                     skill_names={s: SKILLS[s]["name"] for s in known},
                     cooldowns=C(remaining={"zhiyu": 0.4}).remaining)
    assert [s.key for s in layout.skills] == ["1", "2", "3"]
    # 插入序保留：技能栏顺序就是调用方排好的（窗口层按学习等级排）
    assert [s.name for s in layout.skills] == ["召唤骷髅", "治愈术", "施毒术"]
    by_name = {s.name: s for s in layout.skills}
    assert by_name["治愈术"].ready is False      # 还在冷却
    assert by_name["召唤骷髅"].ready is True
    assert by_name["治愈术"].proficiency == 2
    for slot in layout.skills:                   # 格子必须落在屏内
        rect = slot.rect
        assert 0 <= rect.x and rect.right <= 1280
        assert 0 <= rect.y and rect.top <= 720


@case
def test_hud_skill_slots_capped_and_absent():
    """技能栏最多 5 格；没学技能时整条栏不存在（不画 5 个空框）。"""
    known = {f"s{i}": 1 for i in range(8)}
    names = {f"s{i}": f"技能{i}" for i in range(8)}
    layout = _layout(known_skills=known, skill_names=names)
    assert len(layout.skills) == 5
    assert "skills" in layout.panels

    empty = _layout()
    assert empty.skills == [] and "skills" not in empty.panels


@case
def test_hud_shield_text_only_when_active():
    """护盾提示只在有盾时出现——常驻一行 0/0 是视觉噪音。"""
    off = _layout()
    assert not any(t.text.startswith("盾") for t in off.texts)
    on = _layout(shield=(12, 16))
    assert any(t.text == "盾 12/16" for t in on.texts)


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    return passed, failed


if __name__ == "__main__":
    p, f = run()
    print(f"test_presentation: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
