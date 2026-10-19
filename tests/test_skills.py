"""技能系统：学习门槛、MP/冷却/距离门、伤害与治疗路由、AoE。

原则与公式测试一致：每条断言都能手算复核。
MP/冷却的数字直接来自 ``skills.json``，改表就要改这里的期望值。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster, spawn_player
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Cooldowns, Position, Progression, Skills, Vitals
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth
from mirsolo.skills import (
    PLANNED_KINDS,
    SUPPORTED_KINDS,
    SkillError,
    can_cast,
    learn,
)

CASES = []
DATA = GameData.load()
SKILLS = DATA.skills["skills"]

# 与 skills.json 同值的字面量（改表必须同步改）
HUOZHU_MP = SKILLS["huozhu"]["cost_mp"]          # 3
HUOZHU_CD = SKILLS["huozhu"]["cooldown"]          # 1.2
HUOZHU_RANGE = SKILLS["huozhu"]["range"]          # 8
ZHIYU_MP = SKILLS["zhiyu"]["cost_mp"]             # 4


def case(fn):
    CASES.append(fn)
    return fn


def _session():
    """空会话 + 玩家，玩家等级/职业可再调。"""
    session = build_session(data=DATA, seed=1)
    player = populate(session, with_spawns=False)["player"]
    return session, player


def _make_caster(session, class_id: str, level: int):
    """把玩家换成指定职业/等级（apply_growth 是升级的同一入口）。"""
    player = session.world.get_entity("player")
    spec = DATA.classes["classes"][class_id]
    player.get(Skills).known.clear()
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    apply_growth(player, spec)
    return player


@case
def test_learn_gates():
    """学习三道门：职业匹配、等级门槛、幂等。"""
    session, player = _session()
    try:
        warrior = _make_caster(session, "warrior", 7)
        learn(warrior, DATA, "gongsha")           # 7 级战士学攻杀：OK
        assert "gongsha" in warrior.get(Skills).known
        learn(warrior, DATA, "gongsha")           # 重复学习幂等
        assert warrior.get(Skills).known["gongsha"] == 1

        for sid in ("gongsha", "huozhu", "zhiyu"):
            try:
                learn(warrior, DATA, sid if sid != "gongsha" else "liehuo")
                raise AssertionError(f"{sid} 不该学得会")
            except SkillError as exc:
                assert exc.code in ("level_too_low", "class_mismatch"), exc.code

        mage = _make_caster(session, "mage", 7)
        learn(mage, DATA, "huozhu")               # 法师 7 级学火球：OK
        try:
            learn(mage, DATA, "zhiyu")            # 道士技能
            raise AssertionError("法师不该学得会治愈术")
        except SkillError as exc:
            assert exc.code == "class_mismatch"
    finally:
        session.shutdown()


@case
def test_cast_requires_learned():
    """没学过的技能放不出来，也不该掉 MP。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mage.get(Vitals).mp == mp0, "未学习也扣了 MP"
        assert mob.get(Vitals).hp == mob.get(Vitals).max_hp, "未学习却打出了伤害"
    finally:
        session.shutdown()


@case
def test_planned_kinds_are_rejected():
    """P1-S2 待实现 kind 必须被明确拒绝——数据先落、行为后接，不许静默失败。"""
    session, player = _session()
    try:
        for sid, spec in SKILLS.items():
            if spec["kind"] not in PLANNED_KINDS:
                continue
            caster = _make_caster(session, spec["class"], 35)  # 满足等级与职业
            learn(caster, DATA, sid)
            ok, code = can_cast(caster, DATA, sid)
            assert not ok and code == "kind_not_implemented", (sid, code)
        # 已实现 kind 全部可放（资源足够时）
        taoist = _make_caster(session, "taoist", 35)
        learn(taoist, DATA, "zhiyu")
        ok, code = can_cast(taoist, DATA, "zhiyu")
        assert ok, code
    finally:
        session.shutdown()


@case
def test_mp_deducted_and_damage_dealt():
    """火球术：扣 MP 精确等于 cost，目标掉血走魔法路径（无视 AC、吃 MAC）。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        learn(mage, DATA, "huozhu")
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mob_hp0 = mob.get(Vitals).hp
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mage.get(Vitals).mp == mp0 - HUOZHU_MP, (mp0, mage.get(Vitals).mp)
        assert mob.get(Vitals).hp < mob_hp0, "火球没造成伤害"
    finally:
        session.shutdown()


@case
def test_insufficient_mp_blocks_cast():
    """MP 不足：拒绝施放、不掉 MP、不掉血。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        learn(mage, DATA, "huozhu")
        mage.get(Vitals).mp = HUOZHU_MP - 1        # 刚好差 1 点
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mob_hp0 = mob.get(Vitals).hp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp == mob_hp0
        assert mage.get(Vitals).mp == HUOZHU_MP - 1, "MP 不足却被扣了"
    finally:
        session.shutdown()


@case
def test_cooldown_blocks_and_expires():
    """冷却期内再按无效；冷却走完（CooldownSystem 递减）后可再放。

    注意先后关系：CooldownSystem（p10）每刻先跑，SkillSystem（p15）后跑，
    所以第一发放完后立即读到的剩余值是 ``CD − 1 tick``，不是 CD 本身。
    """
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        learn(mage, DATA, "huozhu")
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)                             # 第一发放出
        left = mage.get(Cooldowns).remaining.get("huozhu", 0.0)
        assert 0.0 < left <= HUOZHU_CD, left         # 已进冷却且在递减

        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)                             # 冷却中
        assert mage.get(Vitals).mp == mp0, "冷却中施放不应扣 MP"

        session.step(int(HUOZHU_CD / session.tick_dt) + 2)  # 等冷却走完
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0

        session.cast_skill(mage, "huozhu", mob)     # 冷却结束应能再放
        session.step(2)
        releft = mage.get(Cooldowns).remaining.get("huozhu", 0.0)
        assert releft > 0, "冷却结束后再放没有重新进冷却"
    finally:
        session.shutdown()


@case
def test_range_gate():
    """超程施放必须被拒：不掉 MP、不掉血、不进冷却。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        learn(mage, DATA, "huozhu")
        far = HUOZHU_RANGE + 1
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + far, y=mage.get(Position).y)
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp == mob.get(Vitals).max_hp
        assert mage.get(Vitals).mp == mp0
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0, "超程却进了冷却"
    finally:
        session.shutdown()


@case
def test_heal_restores_by_sc_roll():
    """治愈术：恢复量 = roll(道术)，不超上限；MP 精确扣除。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 7)
        learn(taoist, DATA, "zhiyu")
        vitals = taoist.get(Vitals)
        vitals.hp = 5                               # 先打掉一批血
        mp0 = vitals.mp
        session.cast_skill(taoist, "zhiyu", taoist)  # 自疗
        session.step(2)
        assert vitals.hp > 5, "治愈术没回血"
        assert vitals.hp <= vitals.max_hp
        assert vitals.mp == mp0 - ZHIYU_MP
    finally:
        session.shutdown()


@case
def test_aoe_hits_all_in_radius():
    """半月弯刀：半径内两只怪各结算一次，半径外的没事。"""
    session, player = _session()
    try:
        warrior = _make_caster(session, "warrior", 22)
        learn(warrior, DATA, "banyue")
        px, py = warrior.get(Position).x, warrior.get(Position).y
        near1 = spawn_monster(session.world, DATA, "ji", x=px + 1, y=py)
        near2 = spawn_monster(session.world, DATA, "ji", x=px + 1, y=py + 1)  # 距目标 1 格
        far = spawn_monster(session.world, DATA, "ji", x=px + 5, y=py)
        session.cast_skill(warrior, "banyue", near1)  # 近战 AoE 以目标为中心 r=1
        session.step(2)
        assert near1.get(Vitals).hp < near1.get(Vitals).max_hp
        assert near2.get(Vitals).hp < near2.get(Vitals).max_hp, "半径内的怪没被打到"
        assert far.get(Vitals).hp == far.get(Vitals).max_hp, "半径外的怪被波及了"
    finally:
        session.shutdown()


@case
def test_magic_aoe_and_group_heal_shapes():
    """冰咆哮打敌方群体、集体治愈回友方群体——两个方向各自只影响本阵营。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 35)
        learn(mage, DATA, "bingpaoxiao")
        px, py = mage.get(Position).x, mage.get(Position).y
        mobs = [spawn_monster(session.world, DATA, "ji", x=px + dx, y=py)
                for dx in (1, 2)]
        before = [m.get(Vitals).hp for m in mobs]
        session.cast_skill(mage, "bingpaoxiao", mobs[0])
        session.step(2)
        for mob, hp0 in zip(mobs, before):
            assert mob.get(Vitals).hp < hp0, "冰咆哮没覆盖到半径内目标"

        taoist = _make_caster(session, "taoist", 26)
        learn(taoist, DATA, "jiti_zhiyu")
        taoist.get(Vitals).hp = 3
        session.cast_skill(taoist, "jiti_zhiyu", taoist)
        session.step(2)
        assert taoist.get(Vitals).hp > 3, "集体治愈没生效"
    finally:
        session.shutdown()


@case
def test_skill_kind_partition_is_pinned():
    """kind 分区钉死：数据表新增 kind 时，本测试会红——必须同时决定实现批次。"""
    kinds = {spec["kind"] for spec in SKILLS.values()}
    assert kinds <= (SUPPORTED_KINDS | PLANNED_KINDS), kinds - (SUPPORTED_KINDS | PLANNED_KINDS)
    assert SUPPORTED_KINDS & PLANNED_KINDS == frozenset()
    # 本批必须已覆盖三职业
    for class_id, expected in (("warrior", 4), ("mage", 5), ("taoist", 7)):
        owned = [s for s in SKILLS.values() if s["class"] == class_id]
        assert len(owned) == expected, (class_id, len(owned))
    assert len(SKILLS) >= 12


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
    print(f"test_skills: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
