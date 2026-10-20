"""道士普攻吃道术 —— 曾经是**已知缺口**，现在是修好之后的钉子。

缺口本尊：``combat/resolver.py:physical`` 对三个职业一律取 ``atk_attr.dc``，
而道士武器（银蛇剑 ``sc[6,11]``、无极剑 ``sc[9,15]``）给的是 **sc**。结果：

* 道士 L34 的 ``dc`` 只有 (8, 17)（职业成长 0~1 + 每 4 级 +1 / 每 2 级 +1）；
* 沃玛段实测每次挥刀只打 **5.1** 点，每杀要 **173~226 刀**（战士 34~48 刀）；
* 不修的话道士 L33→47 要 9182 小时，是战士（5730h）的 1.6 倍。

修法在 ``resolver.physical_power``：普攻伤害源按 ``Progression.class_id``
查 ``PHYSICAL_SOURCE_BY_CLASS``（战士 dc / 法师 mc / 道士 sc），没有
``Progression`` 的实体（怪物 / 佣兵 / 召唤物）一律回落 ``dc``。

本模块把"已经修好"这件事钉成**双向**的：

* ``test_taoist_physical_should_use_sc`` —— 必须绿。道士压 dc(1,1)、抬 sc(15,24)，
  普攻最高伤害必须 >= 15；退回"一律吃 dc"就只剩 1。
* ``test_higher_sc_weapon_raises_taoist_damage`` —— 必须绿。换更高 ``sc`` 的剑，
  普攻均伤必须跟着涨。**这条比"不红了"有说服力**：把扁成一个固定值也能让上一条
  变绿，跟着 sc 涨才是真的换了字段。
* ``test_warrior_physical_still_only_uses_dc`` —— 修前修后都必须绿，
  是"修道士不许顺手改战士"的护栏：战士戴着 sc(15,24) 也只打得出 dc(1,1)。
* ``test_taoist_weapons_carry_sc_not_atk`` —— 数据面前提：道士武器只有 sc。
  如果哪天数据表给道士武器加了 atk，上面两条就不再描述真实世界了。

⚠️ 本模块**不**改任何生产代码，也不改 ``data/``。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.components import Attributes, Bag, Progression
from mirsolo.data.loader import GameData
from mirsolo.items.equipment import equip
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

#: 缺口已在 ``resolver.physical_power`` 里修掉（普攻按职业取 战士 dc / 法师 mc /
#: 道士 sc）→ ``False``。上面两条用例现在是**必须通过**的：
#: 谁把修复退回，它们立刻变红（退回的方式见 ``tmp_q1/p3_redcheck.py``）。
KNOWN_GAP = False

#: 缺口"醒了"的时候必须红的用例名（修好后必须全绿）。
#: 两条一起钉：第一条证明"道士吃得到 sc"，第二条证明"吃的是 sc 而不是别的"——
#: 只有第一条的话，把 physical() 改成随机一个数也能变绿。
GAP_CASES = frozenset({
    "test_taoist_physical_should_use_sc",
    "test_higher_sc_weapon_raises_taoist_damage",
})


def case(fn):
    CASES.append(fn)
    return fn


def _dummy(session):
    """站着不动的沙包：0 防、0 敏，命中与伤害都不受目标影响。"""
    mob = spawn_monster(session.world, DATA, "shiwang", eid="sc_dummy")
    attr = mob.get(Attributes)
    attr.ac, attr.mac, attr.agility = (0, 0), (0, 0), 0
    return mob


def _max_physical(player, target, session, ticks: int = 200) -> int:
    """打 ``ticks`` 刀，返回命中时出现过的最高伤害。"""
    best = 0
    for tick in range(ticks):
        ctx = CombatContext(seed=session.seed, world=session.world,
                            bus=session.bus, tick=tick)
        outcome = CombatResolver(ctx).physical(player, target)
        if outcome.hit:
            best = max(best, outcome.rolls["atk"])
    return best


def _avg_physical(player, target, session, ticks: int = 400) -> float:
    """打 ``ticks`` 刀，返回命中的**平均伤害**（沙包 0 防，等于 roll 出来的攻击值）。"""
    total = hits = 0
    for tick in range(ticks):
        ctx = CombatContext(seed=session.seed, world=session.world,
                            bus=session.bus, tick=tick)
        outcome = CombatResolver(ctx).physical(player, target)
        if outcome.hit:
            total += outcome.damage
            hits += 1
    return total / max(1, hits)


def _player_of(session, class_id: str, level: int = 34):
    player = populate(session, player_class=class_id, with_spawns=False)["player"]
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"][class_id])
    return player


@case
def test_taoist_physical_should_use_sc():
    """道士挥刀应当吃 ``sc``（道术），不是赤手空拳的 ``dc``。

    做法：把道士的 dc 压到 (1, 1)、sc 抬到 (15, 24)，再打 200 刀看最高伤害。
    现状（吃 dc）最高只能到 1；修好后应当能到 15 以上。
    """
    session = build_session(data=DATA, seed=1, map_id="biqi_field")
    try:
        player = _player_of(session, "taoist")
        player.get(Bag).add("yinshe_jian", 1)
        equip(player, DATA, "yinshe_jian")
        target = _dummy(session)
        attr = player.get(Attributes)
        attr.dc, attr.sc = (1, 1), (15, 24)
        best = _max_physical(player, target, session)
        assert best >= 15, (
            f"道士普攻最高只打出 {best} 点：physical() 吃的是 dc(1)，"
            f"没吃 sc(15~24)。这就是每杀 173~226 刀的根因。")
    finally:
        session.shutdown()


@case
def test_higher_sc_weapon_raises_taoist_damage():
    """同一等级的道士换更高 ``sc`` 的剑，普攻必须真的更高（不是"不再打 1 点"就算数）。

    为什么还要这一条：换字段之后"能打出 15 点"只证明值不再来自 dc(1)，
    **伤害跟着 sc 一起涨**才证明新来源是 sc 本身——
    两把剑（``xiangmo_jian`` sc[4,8] / ``wuji_jian`` sc[9,15]）都**没有** ``atk``，
    只要还在吃 dc，两次打出来的均值会一模一样。
    """
    session = build_session(data=DATA, seed=1, map_id="biqi_field")
    try:
        player = _player_of(session, "taoist", level=44)
        player.get(Bag).add("xiangmo_jian", 1)
        player.get(Bag).add("wuji_jian", 1)
        target = _dummy(session)
        equip(player, DATA, "xiangmo_jian")
        low_avg = _avg_physical(player, target, session)
        equip(player, DATA, "wuji_jian")
        high_avg = _avg_physical(player, target, session)
        assert high_avg > low_avg + 1, (
            f"道士换 wuji_jian(sc 9~15) 后均伤 {high_avg:.1f}，没有明显高于 "
            f"xiangmo_jian(sc 4~8) 的 {low_avg:.1f} —— 普攻没有跟着 sc 走")
    finally:
        session.shutdown()


@case
def test_warrior_physical_still_only_uses_dc():
    """对照组：战士的武器只有 ``atk``，把 sc 抬到天上也不该影响他的普攻。

    这条修前修后都绿——它就是"修道士不许顺手改战士"的护栏。
    """
    session = build_session(data=DATA, seed=1, map_id="biqi_field")
    try:
        player = _player_of(session, "warrior")
        target = _dummy(session)
        attr = player.get(Attributes)
        attr.dc, attr.sc = (1, 1), (15, 24)
        assert _max_physical(player, target, session) <= 1, (
            "战士普攻吃到了 sc —— 三个职业的物理通路不该都被改成吃 sc")
    finally:
        session.shutdown()


@case
def test_taoist_weapons_carry_sc_not_atk():
    """数据面：道士武器给的是 ``sc``，一件带 ``atk`` 的都没有。

    这条是第一条的前提——如果哪天数据表给道士武器加上 ``atk``，
    第一条 xfail 就不再描述真实世界了。
    """
    taoist_weapons = [iid for iid, spec in ITEMS.items()
                      if spec.get("slot") == "weapon"
                      and "taoist" in (spec.get("class") or [])
                      and int(spec.get("level_req", 1) or 1) >= 10]
    assert taoist_weapons, "数据表里没有 10 级以上的道士武器"
    for iid in taoist_weapons:
        spec = ITEMS[iid]
        assert spec.get("sc"), f"{iid} 没有 sc，道士拿什么打"
        assert not spec.get("atk"), f"{iid} 带 atk，会绕过 sc 缺口"


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        expect_fail = KNOWN_GAP and fn.__name__ in GAP_CASES
        try:
            fn()
        except AssertionError as exc:
            if expect_fail:
                passed += 1
                print(f"  ok   {fn.__name__}（已知缺口，按预期失败）: {exc}")
            else:
                failed += 1
                print(f"  FAIL {fn.__name__}: {exc}")
        else:
            if expect_fail:
                failed += 1
                print(f"  FAIL {fn.__name__}: 缺口已经不存在了？请把 KNOWN_GAP 改成 False"
                      "（并确认是 resolver 真改了，不是这条用例被改空了）")
            else:
                passed += 1
                print(f"  ok   {fn.__name__}")
    return passed, failed


if __name__ == "__main__":
    p, f = run()
    print(f"test_taoist_sc: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
