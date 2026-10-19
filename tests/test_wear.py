"""装备磨损与修理（P3-S1）—— ``durability`` 这张嘴终于要吃饭了。

钉死的四件事：

1. **磨损只从"真的命中"来**：Miss 不磨、挨打磨防具、出手磨武器，两条账分开；
2. **磨穿了属性就真的没了**（不是面板写写而已）：``recompute`` 要把这件装备
   的加成整段摘掉，``dc`` 回到裸身值；
3. **洗掉磨损只能花钱**：修理费必须**明显低于买入价**（否则不如买新的、按钮白做），
   而"卖掉再买一件"因为按 item_id 记账，拿回来是全新的——两条路都得掏钱；
4. **磨损的随机流不许挪动主伤害流**：加一套新随机，最怕的就是把既有回放搅乱。

字面量原则：期望值写死（10 / 120 / 36 / (1, 2)），不从数据表推导。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_player
from mirsolo.bootstrap import build_session
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.components import Attributes, Bag, Body, Loadout, Vitals, Wallet, Wear
from mirsolo.data.loader import GameData
from mirsolo.economy.shop import buy, repair, repair_all, sell
from mirsolo.items import wear
from mirsolo.items.equipment import equip, recompute
from mirsolo.presentation.panels import build_character_panel, build_shop_panel

CASES = []
DATA = GameData.load()

# ── 数据字面量（改表必须同步改这里）────────────────────────────
MUJIAN_DURABILITY = 10   # 木剑耐久点数
MUJIAN_PRICE = 120       # 木剑买入价
BUYI_PRICE = 80          # 布衣买入价
MUJIAN_ATK = (2, 5)      # 木剑攻击加成
WARRIOR_BASE_DC = (1, 2)  # 战士 1 级裸身 dc
REPAIR_PERMILLE = 300    # 修满一件 = 买入价 × 30%

# 全废木剑的修理费 = 120 × 300‰ = 36 金（整除法，写死）
MUJIAN_FULL_REPAIR = 36
BUYI_FULL_REPAIR = 24
MUJIAN_HALF_REPAIR = 18  # 磨掉一半（5/10 点）


def case(fn):
    CASES.append(fn)
    return fn


def _session():
    # 不传 ``data=`` 会拿到另一份 GameData，测试里改表就白改（踩过）
    session = build_session(data=DATA)
    player = spawn_player(session.world, DATA, class_id="warrior")
    return session, player


def _dressed(player, *, gold: int = 10 ** 6):
    """穿上木剑与布衣，给一笔钱。"""
    player.get(Wallet).gold = gold
    player.get(Bag).add("mujian", 1)
    player.get(Bag).add("buyi", 1)
    equip(player, DATA, "mujian")
    equip(player, DATA, "buyi")
    return player


def _wear_to(player, item_id: str, points: int):
    """直接把磨损账写到指定点数（绕过随机，测规则本身）。"""
    player.get(Wear).points[item_id] = points
    recompute(player, DATA)


# ── 规则层 ──────────────────────────────────────────

@case
def test_new_gear_starts_at_full_durability():
    session, player = _session()
    try:
        player.get(Bag).add("mujian", 1)
        assert wear.points_of(player.get(Wear), "mujian") == 0
        assert wear.left_permille(DATA, player.get(Wear), "mujian") == 1000
        assert wear.is_broken(DATA, player.get(Wear), "mujian") is False
        assert wear.repair_cost(DATA, player.get(Wear), "mujian") == 0
    finally:
        session.shutdown()


@case
def test_broken_gear_gives_no_attribute_bonus():
    """磨穿 = 加成整段消失，dc 回到 1 级裸身 (1, 2)。"""
    session, player = _session()
    try:
        _dressed(player)
        assert tuple(player.get(Attributes).dc) == (
            WARRIOR_BASE_DC[0] + MUJIAN_ATK[0], WARRIOR_BASE_DC[1] + MUJIAN_ATK[1])
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        assert wear.is_broken(DATA, player.get(Wear), "mujian") is True
        assert tuple(player.get(Attributes).dc) == WARRIOR_BASE_DC, \
            "磨穿的武器还在给攻击加成"
    finally:
        session.shutdown()


@case
def test_broken_gear_still_counts_as_weight():
    """磨穿不能顺带减重（木剑 5 + 布衣 4 = 9）：不然"打废装备"反倒成了收益。"""
    session, player = _session()
    try:
        _dressed(player)
        assert player.get(Body).weight == 9, player.get(Body).weight
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        assert player.get(Body).weight == 9, "磨穿后重量变了"
    finally:
        session.shutdown()


@case
def test_repair_costs_gold_and_restores_the_bonus():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        assert tuple(player.get(Attributes).dc) == WARRIOR_BASE_DC

        result = repair(player, DATA, "mujian")
        assert result["paid"] == MUJIAN_FULL_REPAIR, result
        assert player.get(Wallet).gold == 1000 - MUJIAN_FULL_REPAIR
        # 修好了：属性要**立刻**回来（不是等下次穿脱）
        assert tuple(player.get(Attributes).dc) == (
            WARRIOR_BASE_DC[0] + MUJIAN_ATK[0], WARRIOR_BASE_DC[1] + MUJIAN_ATK[1])
    finally:
        session.shutdown()


@case
def test_repair_is_always_cheaper_than_buying_new():
    """修理存在的理由就是比买新的便宜；贵了这条就别做了。"""
    for item_id, price, full in (("mujian", MUJIAN_PRICE, MUJIAN_FULL_REPAIR),
                                 ("buyi", BUYI_PRICE, BUYI_FULL_REPAIR)):
        assert full < price, (item_id, full, price)


@case
def test_half_worn_costs_half_price():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        _wear_to(player, "mujian", MUJIAN_DURABILITY // 2)
        assert wear.repair_cost(DATA, player.get(Wear), "mujian") == MUJIAN_HALF_REPAIR
    finally:
        session.shutdown()


@case
def test_repair_all_fixes_everything_in_one_bill():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        _wear_to(player, "buyi", MUJIAN_DURABILITY)
        result = repair_all(player, DATA)
        assert result["paid"] == MUJIAN_FULL_REPAIR + BUYI_FULL_REPAIR, result
        assert sorted(result["items"]) == ["buyi", "mujian"]
        assert wear.points_of(player.get(Wear), "mujian") == 0
        assert wear.points_of(player.get(Wear), "buyi") == 0
        assert tuple(player.get(Attributes).dc) == (
            WARRIOR_BASE_DC[0] + MUJIAN_ATK[0], WARRIOR_BASE_DC[1] + MUJIAN_ATK[1])
    finally:
        session.shutdown()


@case
def test_repair_without_gold_is_rejected_and_changes_nothing():
    session, player = _session()
    try:
        _dressed(player, gold=1)
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        try:
            repair(player, DATA, "mujian")
        except Exception as exc:  # noqa: BLE001 - 只看拒绝码
            assert getattr(exc, "code", "") == "no_gold", exc
        else:
            raise AssertionError("钱不够却修成功了")
        assert player.get(Wallet).gold == 1
        assert wear.points_of(player.get(Wear), "mujian") == MUJIAN_DURABILITY
    finally:
        session.shutdown()


@case
def test_selling_a_wreck_and_buying_new_gives_full_durability():
    """卖掉再买一件 = 全新（这条必须成立，否则花 120 金买回一把废剑）。"""
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        # 先脱下来，不然卖的是穿着的那件之外的另一件
        from mirsolo.items.equipment import unequip
        unequip(player, DATA, "weapon")
        sell(player, DATA, "mujian", 1)
        buy(player, DATA, "mujian", 1)
        assert wear.points_of(player.get(Wear), "mujian") == 0, \
            "新买回来的木剑继承了旧木剑的磨损"
        assert wear.left_permille(DATA, player.get(Wear), "mujian") == 1000
    finally:
        session.shutdown()


# ── 结算器接线 ──────────────────────────────────────

def _duel(world, player, target, *, seed: int = 7, strikes: int = 60):
    """让 player 打 target ``strikes`` 次（tick 递增，保证每条流只用一次）。"""
    ctx = CombatContext(seed=seed, world=world, bus=None, tick=0, data=DATA)
    resolver = CombatResolver(ctx)
    for index in range(strikes):
        ctx.tick = index
        resolver.physical(player, target, power_permille=1000)
    return resolver


@case
def test_strikes_wear_the_weapon_down():
    session, player = _session()
    try:
        _dressed(player)
        target = spawn_player(session.world, DATA, class_id="mage", eid="dummy")
        target.get(Vitals).hp = 10 ** 6  # 打不死，纯粹当沙包
        target.get(Vitals).max_hp = 10 ** 6
        _duel(session.world, player, target, strikes=200)
        points = wear.points_of(player.get(Wear), "mujian")
        assert 0 < points <= MUJIAN_DURABILITY, points
    finally:
        session.shutdown()


@case
def test_getting_hit_wears_armor_not_weapon():
    """挨打磨的是防具：武器只在挥出去时磨。"""
    session, player = _session()
    try:
        _dressed(player)
        attacker = spawn_player(session.world, DATA, class_id="warrior", eid="attacker")
        attacker.get(Vitals).hp = 10 ** 6
        attacker.get(Vitals).max_hp = 10 ** 6
        player.get(Vitals).hp = 10 ** 6
        player.get(Vitals).max_hp = 10 ** 6
        _duel(session.world, attacker, player, strikes=200)
        assert wear.points_of(player.get(Wear), "buyi") > 0, "挨打没磨到防具"
        assert wear.points_of(player.get(Wear), "mujian") == 0, "挨打不该磨武器"
    finally:
        session.shutdown()


@case
def test_wear_rolls_never_shift_the_damage_stream():
    """磨损记账不许挪动主伤害流：关掉磨损后，同一场架的伤害序列必须一模一样。"""
    session, player = _session()
    try:
        _dressed(player)
        target = spawn_player(session.world, DATA, class_id="mage", eid="dummy2")
        target.get(Vitals).hp = 10 ** 6
        target.get(Vitals).max_hp = 10 ** 6

        seen: list[int] = []
        ctx = CombatContext(seed=11, world=session.world, bus=None, tick=0, data=DATA)
        resolver = CombatResolver(ctx)
        original_hp = int(target.get(Vitals).hp)
        for index in range(120):
            ctx.tick = index
            resolver.physical(player, target, power_permille=1000)
            seen.append(int(target.get(Vitals).hp))
        assert wear.points_of(player.get(Wear), "mujian") > 0, "这一场压根没磨到"

        # 关掉磨损（0 BP = 永不磨损），重打同一场
        saved = wear.WEAR_BP_PER_HIT
        wear.WEAR_BP_PER_HIT = 0
        try:
            player.get(Wear).points.clear()
            recompute(player, DATA)
            target.get(Vitals).hp = original_hp
            again: list[int] = []
            ctx2 = CombatContext(seed=11, world=session.world, bus=None, tick=0, data=DATA)
            resolver2 = CombatResolver(ctx2)
            for index in range(120):
                ctx2.tick = index
                resolver2.physical(player, target, power_permille=1000)
                again.append(int(target.get(Vitals).hp))
        finally:
            wear.WEAR_BP_PER_HIT = saved
        assert seen == again, "磨损的随机流挪动了主伤害流"
    finally:
        session.shutdown()


# ── 面板 ────────────────────────────────────────────

@case
def test_character_panel_shows_wear_and_marks_the_broken_one():
    session, player = _session()
    try:
        _dressed(player)
        before = build_character_panel(player=player, data=DATA)
        assert not [r for r in before.rows if "已损坏" in (r.detail or "")], \
            "崭新的装备不该显示已损坏"
        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        panel = build_character_panel(player=player, data=DATA)
        # 磨损写在装备行的 detail 上（不是页脚文本）：玩家盯的是那一行装备
        shown = " ".join(f"{r.text} {r.detail}" for r in panel.rows)
        assert "木剑" in shown, shown[:200]
        assert "已损坏" in shown, f"磨穿的木剑没被标出来：{shown[:200]}"
    finally:
        session.shutdown()


@case
def test_shop_panel_offers_repair_all_with_the_price():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        panel = build_shop_panel(player=player, data=DATA)
        assert not [r for r in panel.rows if r.action == "repair_all"], \
            "没磨损时不该出现修理行"

        _wear_to(player, "mujian", MUJIAN_DURABILITY)
        panel = build_shop_panel(player=player, data=DATA)
        row = [r for r in panel.rows if r.action == "repair_all"]
        assert len(row) == 1, "有磨损就必须给出修理入口"
        assert str(MUJIAN_FULL_REPAIR) in row[0].text, row[0].text

        # 钱不够要置灰但仍看得见（玩家需要看见自己差多少）
        player.get(Wallet).gold = 1
        panel = build_shop_panel(player=player, data=DATA)
        row = [r for r in panel.rows if r.action == "repair_all"]
        assert row[0].enabled is False, "钱不够的修理行必须置灰"
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
    print(f"test_wear: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
