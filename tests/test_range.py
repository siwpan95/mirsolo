"""射程门 —— 普攻够不着就不结算，技能按 ``skills.json`` 的 ``range`` 卡。

这三条是**修前必红**的用例（改不改都绿的用例等于没写）：

* ① 隔着 40 格普攻攻城城门 —— 修前照样掉血（``AttackSystem`` 根本没有距离门）；
* ② 法师站在 10 格外放火球（``range=8``） —— 修前已经会红，所以本模块同时
  钉住"走到 8 格内能放"的正向半边，防止哪天把 0 当成"必须贴身"；
* ③ ``range=0`` 的自身技（魔法盾 / 隐身 / 召唤）—— 防的是**过度收紧**：

距离一律用切比雪夫 ``max(|dx|, |dy|)``，与 :mod:`mirsolo.combat.resolver`
的 ``chebyshev`` 同一把尺子（移动是八向一格，斜着走与直着走同样花一刻，
换欧氏距离会让斜对角在射程上平白吃亏）。
"""

from __future__ import annotations

import inspect

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import KEY_INTENTS
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat import resolver as resolver_mod
from mirsolo.components import (
    AiState,
    Bag,
    Cooldowns,
    Invisible,
    Position,
    Progression,
    Shield,
    SiegeObjective,
    Skills,
    SummonOwner,
    Vitals,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth
from mirsolo.siege.campaign import spawn_objective
from mirsolo.skills import learn

CASES = []
DATA = GameData.load()
SKILLS = DATA.skills["skills"]

#: 普攻距离门被拦下时发回音的事件名（与 ``combat:hit`` 同一族）
EVT_ATTACK_REJECTED = resolver_mod.EVT_ATTACK_REJECTED
MELEE_RANGE = resolver_mod.MELEE_RANGE

#: 城门摆在这儿、玩家摆在那儿，切比雪夫正好 42 格 —— 就是"隔着半张图砍城门"
GATE_AT = (47.0, 47.0)
PLAYER_AT = (5.0, 5.0)


def case(fn):
    CASES.append(fn)
    return fn


def _session(map_id: str = "sabak", seed: int = 7):
    session = build_session(data=DATA, seed=seed, map_id=map_id)
    player = populate(session, with_spawns=False)["player"]
    return session, player


def _caster(session, class_id: str, level: int):
    """把玩家换成指定职业/等级（``apply_growth`` 是升级的同一入口）。"""
    player = session.world.get_entity("player")
    spec = DATA.classes["classes"][class_id]
    player.get(Skills).known.clear()
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    apply_growth(player, spec)
    return player


def _learn(player, skill_id: str) -> None:
    player.get(Bag).add(SKILLS[skill_id]["book"], 1)
    learn(player, DATA, skill_id)


def _still(mob) -> None:
    """让怪**站着不动**：射程用例要的是确定的距离，不是"它走过来了"。

    ``vision=0`` 走的是索敌自己的门（视野外不锁人），不是把 AiState 摘掉——
    摘掉会顺手关掉"挨打就还手"那条路，测的就不是同一件事了。
    """
    ai = mob.get(AiState)
    if ai is not None:
        ai.vision = 0
        ai.target_id = None


def _rejects(session) -> list[dict]:
    """订阅普攻拒绝事件，返回收集器。"""
    got: list[dict] = []
    session.bus.subscribe(EVT_ATTACK_REJECTED,
                          lambda p: got.extend(p.get("items") or []))
    return got


# ── ① 普攻距离门 ────────────────────────────────────

@case
def test_melee_cannot_hit_objective_40_tiles_away():
    """隔着 42 格砍城门：一滴血都不许掉，而且要留下回音。

    修前：``AttackSystem`` 只查"双方都活着 + 没被麻痹"，距离从来不看，
    所以站在地图这头能把那头的攻城城门砍掉（实测 hp 1200 → 1198）。
    """
    session, player = _session()
    try:
        gate = spawn_objective(session.world, DATA, DATA.sieges["sieges"]["sabak"],
                               0, eid="probe:gate")
        player.get(Position).x, player.get(Position).y = PLAYER_AT
        gate.get(Position).x, gate.get(Position).y = GATE_AT
        session.step(1)
        assert not gate.get(SiegeObjective).invulnerable, "前置条件不成立：城门锁着"

        rejects = _rejects(session)
        hp0 = gate.get(Vitals).hp
        session.queue_attack(player, gate)
        session.step(1)

        assert gate.get(Vitals).hp == hp0, (
            f"隔着 {int(max(abs(GATE_AT[0] - PLAYER_AT[0]), abs(GATE_AT[1] - PLAYER_AT[1])))} "
            f"格砍到了城门：{hp0} → {gate.get(Vitals).hp}")
        assert len(rejects) == 1, f"距离不够却没给回音：{rejects}"
        assert rejects[0]["code"] == "out_of_range", rejects[0]
        assert rejects[0]["distance"] == 42, rejects[0]
        assert rejects[0]["reach"] == MELEE_RANGE, rejects[0]
    finally:
        session.shutdown()


@case
def test_melee_adjacent_still_lands():
    """正向对照：贴身一刀照旧掉血（距离门不能把普攻整个关死）。"""
    session, player = _session(map_id="biqi_field")
    try:
        mob = spawn_monster(session.world, DATA, "shiwang",
                            x=player.get(Position).x + 1, y=player.get(Position).y)
        _still(mob)
        rejects = _rejects(session)
        hp0 = mob.get(Vitals).hp
        hits = []
        session.bus.subscribe("combat:hit", hits.append)
        session.queue_attack(player, mob)
        session.step(1)
        assert mob.get(Vitals).hp < hp0, "贴身一刀没掉血（距离门收过头了）"
        assert rejects == [], f"贴身出手被误拒：{rejects}"
        assert hits, "贴身一刀连命中事件都没有"
    finally:
        session.shutdown()


@case
def test_out_of_range_intent_is_dropped_not_retried():
    """够不着的那一下**作废**，不许排队等目标走近再自己生效。

    与技能拒绝同一条纪律：按下即结算，攒着的刀在若干刻后一起打出来，
    玩家看着像"卡了一下突然爆发"。
    """
    session, player = _session(map_id="biqi_field")
    try:
        mob = spawn_monster(session.world, DATA, "shiwang",
                            x=player.get(Position).x + 4, y=player.get(Position).y)
        _still(mob)
        session.step(1)
        hp0 = mob.get(Vitals).hp

        session.queue_attack(player, mob)
        session.step(1)
        assert mob.get(Vitals).hp == hp0, "4 格外的一刀打中了"
        assert session.world.get_data(KEY_INTENTS) == [], "被拒的意图还留在队列里"

        # 把玩家挪到贴身：如果上面那一刀排队等着，这里就会打出来
        player.get(Position).x = mob.get(Position).x - 1
        player.get(Position).y = mob.get(Position).y
        session.step(2)
        assert mob.get(Vitals).hp == hp0, "被拒的意图在走近之后自己生效了"
    finally:
        session.shutdown()


# ── ② 技能射程（range > 0） ──────────────────────────

@case
def test_fireball_blocked_at_10_then_lands_at_8():
    """火球 ``range=8``：10 格外被拒（不扣蓝、不进冷却），走到 8 格内能放。"""
    session, _ = _session(map_id="biqi_field")
    try:
        mage = _caster(session, "mage", 7)
        _learn(mage, "huozhu")
        px, py = mage.get(Position).x, mage.get(Position).y
        mob = spawn_monster(session.world, DATA, "shiwang", x=px + 10, y=py)
        _still(mob)

        hp0 = mob.get(Vitals).hp
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp == hp0, "10 格外的火球打中了（range=8 没生效）"
        assert mage.get(Vitals).mp == mp0, "被拒的火球扣了 MP"
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0, "被拒却进了冷却"

        # 走到**边界**上：切比雪夫 8 格，正好等于 range，应当放得出去
        mage.get(Position).x = mob.get(Position).x - SKILLS["huozhu"]["range"]
        session.step(1)
        assert resolver_mod.chebyshev(mage.get(Position), mob.get(Position)) == 8, (
            mage.get(Position), mob.get(Position))
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp < hp0, "走到 8 格内还是放不出去"
        assert mage.get(Vitals).mp < mp0, "施放成功却没扣 MP"
    finally:
        session.shutdown()


# ── ③ range = 0 的自身技 ────────────────────────────

@case
def test_range_zero_skills_cast_at_any_distance():
    """``range=0`` = 自身技：多远都能放（0 不是"必须贴身"，是"不看距离"）。

    这条防的是过度收紧——把 ``range`` 当"距离上限"直接套到 0 上，
    魔法盾/隐身/召唤就得贴着脸才放得出来。
    """
    for skill_id, class_id, level in (("mofadun", "mage", 31),
                                      ("yinshen", "taoist", 21),
                                      ("zhaohuankulou", "taoist", 19)):
        assert SKILLS[skill_id]["range"] == 0, SKILLS[skill_id]
        session, _ = _session(map_id="biqi_field", seed=11)
        try:
            caster = _caster(session, class_id, level)
            caster.get(Position).x, caster.get(Position).y = 2.0, 2.0
            caster.get(Vitals).mp = caster.get(Vitals).max_mp
            # 放一只 40 格外的怪当参照物：证明"有没有敌人/多远"都不影响
            far = spawn_monster(session.world, DATA, "shiwang", x=42.0, y=2.0)
            _still(far)
            assert resolver_mod.chebyshev(caster.get(Position), far.get(Position)) == 40

            _learn(caster, skill_id)
            session.cast_skill(caster, skill_id, caster)
            session.step(2)

            assert caster.get(Skills).known[skill_id] == 2, (
                f"{skill_id} 没施放成功（熟练度没涨）")
            if skill_id == "mofadun":
                shield = caster.get(Shield)
                assert shield is not None and shield.current > 0, "魔法盾没套上"
            elif skill_id == "yinshen":
                buff = caster.get(Invisible)
                assert buff is not None and buff.until_tick > session.tick, "隐身没生效"
            else:
                owned = [e for e in session.world.each(SummonOwner)
                         if e.get(SummonOwner).owner_id == caster.id]
                assert len(owned) == 1, f"召唤没出随从：{[e.eid for e in owned]}"
        finally:
            session.shutdown()


# ── 尺子与常量的单一来源 ────────────────────────────

@case
def test_melee_range_has_one_definition():
    """近战距离不许有第二份 ``1``：表现层与 headless 都得用结算器那一个。

    只验"值相等"是不够的（三处各写 ``1`` 也相等，改一处就分叉），
    所以验的是**对象来源**：两个模块拿到的必须是同一个名字。
    """
    from mirsolo.cli import smoke

    assert smoke.MELEE_RANGE is MELEE_RANGE, "冒烟 CLI 自己定义了一个近战射程"
    assert "MELEE_RANGE = 1" not in inspect.getsource(smoke), "冒烟 CLI 里还有字面量 1"

    # 表现层：pyglet 未必装得上，装不上就跳过（门禁只要求"装了就必须同源"）
    try:
        from mirsolo.presentation import app
    except Exception:  # noqa: BLE001 - 无 GL 环境下表现层不可导入属正常
        return
    assert app.MELEE_RANGE is MELEE_RANGE, "表现层自己定义了一个近战射程"
    assert "MELEE_RANGE = 1" not in inspect.getsource(app), "表现层里还有字面量 1"


@case
def test_distance_uses_chebyshev_not_euclidean():
    """尺子本身：斜对角 2 格在切比雪夫下是 **2**（欧氏会算成 2.8 从而超出半径 2 的圆）。

    这条钉住的是"换尺子"这类改动：移动是八向一格，斜着走与直着走同样花一刻，
    用欧氏距离斜对角就会平白吃亏。
    """
    from mirsolo.components import Position

    assert resolver_mod.chebyshev(Position(0, 0), Position(2, 2)) == 2
    assert resolver_mod.chebyshev(Position(0, 0), Position(0, 3)) == 3
    assert resolver_mod.chebyshev(Position(5, 5), Position(5, 5)) == 0
    # 普攻：斜对角相邻（差 1 格）够得着
    assert resolver_mod.chebyshev(Position(0, 0), Position(1, 1)) <= MELEE_RANGE


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
    print(f"test_range: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
