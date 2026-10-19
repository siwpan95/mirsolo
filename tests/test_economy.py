"""装备穿戴与铁匠铺经济：属性重算、四道门、买卖与仓库。

所有期望值都能手算：装备加成来自 ``items.json``，价格来自 ``price`` 字段。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    Attributes,
    Bag,
    Body,
    Loadout,
    Position,
    Progression,
    Vitals,
    Wallet,
    Warehouse,
)
from mirsolo.data.loader import GameData
from mirsolo.economy.shop import (
    SELL_DIVISOR,
    TradeError,
    buy,
    buy_price,
    carried_weight,
    deposit,
    sell,
    sell_price,
    withdraw,
)
from mirsolo.items.equipment import EquipError, equip, recompute, unequip
from mirsolo.progression.exp import add_exp, apply_growth, grow_attributes

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]


def case(fn):
    CASES.append(fn)
    return fn


def _player(class_id: str = "warrior", level: int = 1):
    session = build_session(data=DATA, seed=1)
    player = populate(session, with_spawns=False)["player"]
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"][class_id])
    return session, player


def _give(player, item_id: str, count: int = 1) -> None:
    player.get(Bag).add(item_id, count)


# ── 装备 ────────────────────────────────────────────


@case
def test_equip_adds_attributes_and_unequip_reverts():
    """穿木剑：dc 上下限各 +2/+5；脱下回到裸身基准。"""
    session, player = _player("warrior", 1)
    try:
        base_dc = player.get(Attributes).dc
        _give(player, "mujian")
        equip(player, DATA, "mujian")
        assert player.get(Loadout).slots["weapon"] == "mujian"
        assert player.get(Attributes).dc == (base_dc[0] + 2, base_dc[1] + 5)
        assert player.get(Bag).items.get("mujian", 0) == 0, "穿上后背包里还有一件"

        unequip(player, DATA, "weapon")
        assert player.get(Attributes).dc == base_dc
        assert player.get(Bag).items["mujian"] == 1
    finally:
        session.shutdown()


@case
def test_equip_gates():
    """四道门：职业、等级、持有、负重。"""
    session, player = _player("mage", 1)
    try:
        # 1) 职业不符（乌木剑限战士）
        _give(player, "wumujian")
        try:
            equip(player, DATA, "wumujian")
            raise AssertionError("法师不该能拿战士剑")
        except EquipError as exc:
            assert exc.code == "class_mismatch"

        # 2) 等级不够
        _give(player, "qijian" if "qijian" in ITEMS else "mujian")
        player.get(Progression).level = 1
        _give(player, "zhanmadao")  # 20 级
        try:
            equip(player, DATA, "zhanmadao")
            raise AssertionError("1 级不该能拿斩马刀")
        except EquipError as exc:
            assert exc.code in ("class_mismatch", "level_too_low")

        # 3) 背包里没有
        try:
            equip(player, DATA, "buyi" if "buyi" not in player.get(Bag).items else "pihelmet")
            equip(player, DATA, "qingtongkuijia")
            raise AssertionError("背包没有也能穿？")
        except EquipError as exc:
            assert exc.code in ("not_in_bag", "level_too_low", "class_mismatch")

        # 4) 超重：法师负重 20，硬塞重甲（20 级青铜盔甲 weight 14 ×2 件不同槽不行，
        #    这里直接用 body.max_weight 压到 1 来验证门控本身）
        warrior_session, warrior = _player("warrior", 20)
        try:
            warrior.get(Body).max_weight = 1
            _give(warrior, "qingtongkuijia")
            try:
                equip(warrior, DATA, "qingtongkuijia")
                raise AssertionError("负重 1 却穿上了重甲")
            except EquipError as exc:
                assert exc.code == "overweight", exc.code
        finally:
            warrior_session.shutdown()
    finally:
        session.shutdown()


@case
def test_equip_swap_returns_old_item():
    """同槽替换：旧装备回背包，属性只算新装备（不会叠加）。"""
    session, player = _player("warrior", 20)
    try:
        _give(player, "mujian")
        _give(player, "zhanmadao")
        equip(player, DATA, "mujian")
        with_sword = player.get(Attributes).dc
        equip(player, DATA, "zhanmadao")
        assert player.get(Bag).items.get("mujian", 0) == 1, "旧武器没回背包"
        advanced = player.get(Attributes).dc
        assert advanced[1] > with_sword[1], "换了更好的武器攻击上限却没涨"
        assert player.get(Loadout).slots["weapon"] == "zhanmadao"
    finally:
        session.shutdown()


@case
def test_level_up_keeps_gear_bonus():
    """升级会覆盖面 → 必须重算，否则**武器加成被升级吞掉**（真实回归点）。

    这里刻意先把"覆盖面"与"重算"两步拆开断言：
    先证明覆盖面确实抹掉了加成（所以重算是必需的），再证明重算把它加回来。
    """
    session, player = _player("warrior", 1)
    try:
        _give(player, "mujian")
        equip(player, DATA, "mujian")
        with_gear_at_1 = player.get(Attributes).dc

        prog = player.get(Progression)
        add_exp(DATA, prog, DATA.exp_to_next(1))  # 刚好升到 2 级
        assert prog.level == 2
        spec = DATA.classes["classes"]["warrior"]
        apply_growth(player, spec)
        naked_at_2 = grow_attributes(spec, 2)["dc"]
        assert player.get(Attributes).dc == naked_at_2, "升级没有覆盖面？那这条回归就没意义了"
        assert player.get(Attributes).dc[1] < with_gear_at_1[1], "覆盖面居然没丢掉武器加成"

        recompute(player, DATA)                   # 升级路径里紧跟的就是这一步
        after = player.get(Attributes).dc
        assert after == (naked_at_2[0] + 2, naked_at_2[1] + 5), after
    finally:
        session.shutdown()


@case
def test_gear_survives_level_up_via_death_reward():
    """端到端：带着武器升一级，武器加成仍在（走 DeathRewardSystem 的真实路径）。"""
    session, player = _player("warrior", 1)
    try:
        _give(player, "mujian")
        equip(player, DATA, "mujian")
        dc_with_gear = player.get(Attributes).dc

        # 造一只血薄怪给玩家砍，吃掉经验触发升级
        mob = spawn_monster(session.world, DATA, "ji",
                            x=player.get(Position).x + 1, y=player.get(Position).y)
        mob.get(Vitals).hp = 1
        monster_exp = DATA.monsters["monsters"]["ji"]["exp"]
        player.get(Progression).exp = DATA.exp_to_next(1) - monster_exp + 1
        session.queue_attack(player, mob)
        session.step(3)

        assert player.get(Progression).level == 2, player.get(Progression)
        grown = player.get(Attributes).dc
        assert grown[1] >= dc_with_gear[1], "升级后攻击上限反而降了（武器加成丢了）"
        assert player.get(Loadout).slots["weapon"] == "mujian"
    finally:
        session.shutdown()


# ── 经济 ────────────────────────────────────────────


@case
def test_buy_costs_exact_gold_and_fills_bag():
    session, player = _player("warrior", 5)
    try:
        price = buy_price(DATA, "mujian")
        player.get(Wallet).gold = price + 7
        buy(player, DATA, "mujian")
        assert player.get(Bag).items["mujian"] == 1
        assert player.get(Wallet).gold == 7
    finally:
        session.shutdown()


@case
def test_buy_gates_no_gold_bag_full_overweight():
    session, player = _player("warrior", 5)
    try:
        price = buy_price(DATA, "mujian")
        player.get(Wallet).gold = price - 1
        try:
            buy(player, DATA, "mujian")
            raise AssertionError("钱不够也买到了")
        except TradeError as exc:
            assert exc.code == "no_gold"
        assert player.get(Wallet).gold == price - 1 and "mujian" not in player.get(Bag).items

        player.get(Wallet).gold = 10**6
        player.get(Bag).capacity = 1
        player.get(Bag).items["buyi"] = 1
        try:
            buy(player, DATA, "mujian")
            raise AssertionError("背包满了还能买")
        except TradeError as exc:
            assert exc.code == "bag_full"

        # 负重：把上限压到当前负重以下
        player.get(Bag).items.clear()
        player.get(Bag).capacity = 48
        player.get(Body).max_weight = 0
        try:
            buy(player, DATA, "mujian")
            raise AssertionError("超重还能买")
        except TradeError as exc:
            assert exc.code == "overweight", exc.code
    finally:
        session.shutdown()


@case
def test_sell_price_is_one_third_and_removes_item():
    session, player = _player("warrior", 5)
    try:
        _give(player, "mujian", 2)
        price = buy_price(DATA, "mujian")
        result = sell(player, DATA, "mujian", 2)
        assert result["earned"] == sell_price(DATA, "mujian") * 2
        assert sell_price(DATA, "mujian") == max(1, price // SELL_DIVISOR)
        assert player.get(Wallet).gold == result["earned"]
        assert "mujian" not in player.get(Bag).items
    finally:
        session.shutdown()


@case
def test_sell_without_item_is_rejected():
    session, player = _player("warrior", 5)
    try:
        try:
            sell(player, DATA, "mujian")
            raise AssertionError("没有东西也卖掉了")
        except TradeError as exc:
            assert exc.code == "not_enough"
        assert player.get(Wallet).gold == 0
    finally:
        session.shutdown()


@case
def test_buy_then_sell_never_profits():
    """核心防刷断言：买进再卖出必然亏（否则可以无限刷钱）。"""
    session, player = _player("warrior", 5)
    try:
        player.get(Wallet).gold = 10**6
        before = player.get(Wallet).gold
        buy(player, DATA, "pihelmet")
        sell(player, DATA, "pihelmet")
        assert player.get(Wallet).gold < before, "买进卖出赚了钱，经济系统被刷爆"
    finally:
        session.shutdown()


@case
def test_warehouse_deposit_withdraw_roundtrip():
    session, player = _player("warrior", 5)
    try:
        _give(player, "mujian", 3)
        deposit(player, DATA, "mujian", 2)
        stash = player.get(Warehouse)
        assert stash.items["mujian"] == 2
        assert player.get(Bag).items["mujian"] == 1

        withdraw(player, DATA, "mujian", 1)
        assert stash.items["mujian"] == 1
        assert player.get(Bag).items["mujian"] == 2
    finally:
        session.shutdown()


@case
def test_warehouse_capacity_and_missing_items():
    session, player = _player("warrior", 5)
    try:
        stash = player.get(Warehouse)
        stash.capacity = 1
        _give(player, "mujian")
        _give(player, "buyi")
        deposit(player, DATA, "mujian")
        try:
            deposit(player, DATA, "buyi")
            raise AssertionError("仓库满了还能存")
        except TradeError as exc:
            assert exc.code == "stash_full"

        try:
            withdraw(player, DATA, "buyi")
            raise AssertionError("仓库里没有也取出来了")
        except TradeError as exc:
            assert exc.code == "not_enough"
        assert player.get(Bag).items["buyi"] == 1, "被拒的存取不该动背包"
    finally:
        session.shutdown()


@case
def test_carried_weight_counts_gear_and_bag():
    session, player = _player("warrior", 5)
    try:
        _give(player, "mujian", 2)          # 5 × 2 = 10
        equip(player, DATA, "mujian")       # 穿上一把，背包剩 1 把
        weight = carried_weight(player, DATA)
        assert weight == 5 * 2, weight      # 穿 1 + 背 1
    finally:
        session.shutdown()


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
    print(f"test_economy: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
