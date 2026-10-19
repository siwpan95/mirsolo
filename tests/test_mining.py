"""挖矿（P3-S4）—— 站在矿点上按 F，矿石进包、鹤嘴锄掉耐久。

钉死七件事：

1. **工具就是武器。** 鹤嘴锄占 ``weapon`` 槽、攻击 1-3，挖矿时手上必须握着它。
   "挖矿时打不动怪"不需要额外规则——它本来就是装备槽自己的语义。
2. **十刻一锄，每锄必磨 1 点。** 不是概率：生产成本与战斗损耗是两回事，
   概率磨损会让"锄头还能用多久"变成随机数。
3. **脚不在矿点上就自动收工**，不留"人在别处、矿在这边"的脏状态；
   换图时账本整体作废（矿点是**图内下标**，留着会指到别的地方去）。
4. **矿脉会挖空也会长回来**，但**累计锄数不清零**——一清零，
   同一个随机流名就被重用，长回来之后的前几锄会挖出和上次一模一样的矿石。
5. **挖矿的随机流与战斗/掉落完全分开**，先挖矿还是先打架互不挪动读数。
6. **空锄不回音**：十刻一锄，每锄都弹一行的屏幕没法看。
7. **矿石只进不出**：不上货架、不寄卖，出手只能整笔卖回商店。

字面量原则：期望值写死（12 / 3 / 3500 / 10000 …），不从数据表推导。
"""

from __future__ import annotations

from mirsolo.actor import penalty
from mirsolo.actor.factory import spawn_player
from mirsolo.actor.systems import KEY_LAST_ORE, KEY_MINE_TOGGLE, KEY_MINING_VEINS
from mirsolo.bootstrap import build_session
from mirsolo.components import (
    Attributes,
    Bag,
    Body,
    Loadout,
    Mining,
    Position,
    Progression,
    Vitals,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.economy.shop import ore_value, sell_ores
from mirsolo.items import mining
from mirsolo.items.drops import roll_drops
from mirsolo.items.equipment import equip
from mirsolo.items.wear import points_of
from mirsolo.presentation.panels import shop_stock
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()

# ── 数据字面量（改表必须同步改这里）────────────────────────────
PICK = "hezuichu"
PICK_WEIGHT = 12        # 鹤嘴锄重量
PICK_PRICE = 500        # 买入价
PICK_DURABILITY = 100   # 耐久点数（每锄磨 1 点 = 一把锄头 100 锄）
PICK_ATK = (1, 3)       # 攻击 1-3：拿锄头就是打不动怪

MINING_TICKS = 10       # 十刻一锄
WEAR_PER_SWING = 1      # 每锄必磨 1 点（必然，不是概率）

WARRIOR_BASE_DC = (1, 2)   # 战士 1 级裸身 dc

# 矿石：五色 + 黑铁四档（重量 / 价格 / 纯度）
ORE_WEIGHTS = {"ore_copper": 3, "ore_iron": 4, "ore_silver": 5, "ore_gold": 6,
               "ore_blackiron_1": 8, "ore_blackiron_2": 8,
               "ore_blackiron_3": 8, "ore_blackiron_4": 8}
ORE_PRICES = {"ore_copper": 30, "ore_iron": 60, "ore_silver": 120,
              "ore_gold": 300, "ore_blackiron_1": 400, "ore_blackiron_2": 800,
              "ore_blackiron_3": 1600, "ore_blackiron_4": 3200}
#: 纯度只长在黑铁矿石上；普通矿石没有纯度
ORE_PURITY = {"ore_blackiron_1": 30, "ore_blackiron_2": 50,
              "ore_blackiron_3": 70, "ore_blackiron_4": 100}
ORE_COUNT = 8

# 矿脉：出矿率（万分比）/ 储量 / 恢复刻数
VEIN_YIELD = {"vein_copper": 3500, "vein_iron": 3200,
              "vein_gold": 2800, "vein_black": 1800}
VEIN_CAPACITY = 20
VEIN_REGROW = 600
VEIN_WEIGHT_SUM = 10000

#: 比奇矿洞一层的四个矿点（铜×2 铁×2），坐标写死
MINE_POINTS = [([14, 20], "vein_copper"), ([46, 12], "vein_copper"),
               ([10, 48], "vein_iron"), ([50, 40], "vein_iron")]
#: 另一张图（换图测试用）：蜈蚣洞一层的第一个矿点
OTHER_POINT = [18, 30]

SELL_DIVISOR = 3        # 回收价 = 买入价 // 3

#: 这一种子下，比奇矿洞一层第一个铜矿点前五锄的出矿判定点（改流名必红）
SWING_ROLLS_0 = (2552, 6690, 7422, 6999, 791)
SWING_ORES_0 = ("ore_silver", None, None, None, "ore_silver")
#: 同一锄换个矿点，读数必须完全不同（流名里带矿点下标）
SWING_ROLL_VEIN_1 = 1228


def case(fn):
    CASES.append(fn)
    return fn


def _session(map_id: str = "biqi_mine_1", seed: int = 20261020):
    # 不传 ``data=`` 会拿到另一份 GameData，测试里改表就白改（踩过）
    session = build_session(data=DATA, seed=seed, map_id=map_id)
    player = spawn_player(session.world, DATA, class_id="warrior")
    return session, player


def _at_level(player, level: int):
    """提等级并走一遍成长（负重跟着等级走，先提级再穿装备）。"""
    prog = player.get(Progression)
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"]["warrior"])
    return player


def _with_pick(player):
    """手握鹤嘴锄（走 equip 真路径：槽位与负重两道门都在）。"""
    player.get(Bag).add(PICK, 1)
    equip(player, DATA, PICK)
    return player


def _stand_on(player, x: float, y: float):
    pos = player.get(Position)
    pos.x, pos.y = float(x), float(y)
    return player


def _map_spec(map_id: str = "biqi_mine_1") -> dict:
    return DATA.maps["maps"][map_id]


def _ledger(world, index: int = 0) -> dict:
    return (world.get_data(KEY_MINING_VEINS) or {}).get(str(index)) or {}


def _ore_counts(bag: Bag) -> dict[str, int]:
    return {k: v for k, v in bag.items.items() if k.startswith("ore_")}


# ── 数据：工具与矿石 ──────────────────────────────────

@case
def test_the_pick_is_a_weapon_not_a_trinket():
    """鹤嘴锄占 weapon 槽、攻击 1-3：拿它的那一刻就打不动怪。"""
    spec = DATA.items["items"][PICK]
    assert spec["kind"] == "equip", spec
    assert spec["slot"] == "weapon", spec
    assert tuple(spec["atk"]) == PICK_ATK, spec
    assert spec["weight"] == PICK_WEIGHT
    assert spec["price"] == PICK_PRICE
    assert spec["durability"] == PICK_DURABILITY
    assert spec["tool"] == "pick"

    session, player = _session()
    try:
        _with_pick(player)
        # 穿上就是 1-3 的攻击：dc 从 (1,2) 变成 (2,5)，与木剑比就是个笑话
        assert tuple(player.get(Attributes).dc) == (
            WARRIOR_BASE_DC[0] + PICK_ATK[0], WARRIOR_BASE_DC[1] + PICK_ATK[1])
        assert player.get(Loadout).slots["weapon"] == PICK
    finally:
        session.shutdown()


@case
def test_every_ore_is_sell_only():
    """矿石不上架、不寄卖、不占槽、没有耐久——它是资源不是装备。"""
    for item_id in ORE_WEIGHTS:
        spec = DATA.items["items"][item_id]
        assert spec["kind"] == "ore", item_id
        assert spec["weight"] == ORE_WEIGHTS[item_id], item_id
        assert spec["price"] == ORE_PRICES[item_id], item_id
        # 不占槽 = 穿不上；不进拍卖 = 寄卖不了；没有耐久 = 磨不了
        assert spec.get("slot") is None, item_id
        assert spec.get("auction") is None, item_id
        assert spec["durability"] == 0, item_id
    assert len(mining.ore_ids(DATA)) == ORE_COUNT
    assert all(not mining.is_ore(DATA, i) for i in shop_stock(DATA)), "矿石上架了"


@case
def test_purity_lives_on_the_item_id():
    """纯度按 id 分档（背包是堆叠的，没有实物实例 id 可用）。"""
    for item_id, purity in ORE_PURITY.items():
        assert mining.purity_of(DATA, item_id) == purity, item_id
    # 普通矿石没有纯度
    assert mining.purity_of(DATA, "ore_copper") == 0

    session, player = _session()
    try:
        bag = player.get(Bag)
        bag.add("ore_blackiron_1", 1)
        bag.add("ore_blackiron_3", 1)
        bag.add("ore_copper", 5)
        # 挑最高的就是按 purity 扫一遍，不需要给每块矿石发实例号
        assert mining.highest_purity(DATA, bag) == "ore_blackiron_3"
        bag.remove("ore_blackiron_3", 1)
        assert mining.highest_purity(DATA, bag) == "ore_blackiron_1"
    finally:
        session.shutdown()


@case
def test_vein_tables_sum_to_ten_thousand():
    """矿脉表一律万分比（与 drops 同尺度，避 P2 踩过的两套尺度混用）。"""
    veins = DATA.mines["veins"]
    assert len(veins) == 4, sorted(veins)
    for vein_id, yield_bp in VEIN_YIELD.items():
        spec = veins[vein_id]
        assert spec["yield_bp"] == yield_bp, vein_id
        assert spec["capacity"] == VEIN_CAPACITY, vein_id
        assert spec["regrow_ticks"] == VEIN_REGROW, vein_id
        total = sum(int(e["bp"]) for e in spec["entries"])
        assert total == VEIN_WEIGHT_SUM, (vein_id, total)


@case
def test_mine_points_are_written_in_the_map_data():
    spec = _map_spec()
    assert [(m["at"], m["vein"]) for m in spec["mines"]] == MINE_POINTS
    # 矿点不压传送点：踩在传送点上挖矿，站稳的那一 tick 就会被换走
    portals = {tuple(p["at"]) for p in spec["portals"]}
    for mine in spec["mines"]:
        assert tuple(mine["at"]) not in portals, mine["at"]
    for map_id, spec in DATA.maps["maps"].items():
        portals = {tuple(p["at"]) for p in spec.get("portals", [])}
        for mine in spec.get("mines", []):
            assert tuple(mine["at"]) not in portals, (map_id, mine["at"])


# ── 规则：能不能挖 ────────────────────────────────────

@case
def test_no_pick_no_mining():
    """手上没握锄头就挖不了（包里有一把不算——只看 weapon 槽）。"""
    session, player = _session()
    try:
        _stand_on(player, 14, 20)
        player.get(Bag).add(PICK, 1)   # 只在包里，没穿
        assert mining.holding_pick(player, DATA) is False
        assert mining.can_mine(player, DATA, _map_spec(), 14, 20) == "no_pick"

        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        session.step(1)
        got = world.get_data(KEY_LAST_ORE)
        assert got is not None and got["ok"] is False and got["code"] == "no_pick"
        assert player.get(Mining).active is False
    finally:
        session.shutdown()


@case
def test_pressing_f_off_the_vein_says_so():
    """脚下没矿点就按 F，必须回一句——不然玩家只会以为键坏了。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 30, 30)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        session.step(1)
        got = world.get_data(KEY_LAST_ORE)
        assert got is not None and got["code"] == "no_vein", got
        assert player.get(Mining).active is False, "没矿点也开工了"
    finally:
        session.shutdown()


@case
def test_a_downed_player_cannot_mine():
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        session.step(1)          # 先开工
        assert player.get(Mining).active is True
        player.get(Vitals).hp = 0
        penalty.knock_down(player, tick=int(session.tick))
        for _ in range(MINING_TICKS):
            session.step(1)
        got = world.get_data(KEY_LAST_ORE)
        assert got is not None and got["code"] == "downed", got
        assert player.get(Mining).active is False
    finally:
        session.shutdown()


# ── 规则：节奏、磨损、走开 ────────────────────────────

@case
def test_one_swing_every_ten_ticks():
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)

        session.step(1)
        # 按下去的那一刻就出第一锄（响应感），不是先干等十刻
        assert _ledger(world)["swings"] == 1
        for _ in range(MINING_TICKS - 1):
            session.step(1)
        assert _ledger(world)["swings"] == 1, "十刻没到就出第二锄了"
        session.step(1)
        assert _ledger(world)["swings"] == 2
    finally:
        session.shutdown()


@case
def test_each_swing_costs_exactly_one_durability_point():
    """每锄必磨 1 点（必然，不是概率）：生产成本，玩家得能规划。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        for _ in range(1 + MINING_TICKS * 4):
            session.step(1)
        assert _ledger(world)["swings"] == 5
        # 5 锄必磨 5 点，哪怕这 5 锄里只挖到 2 块——空锄也磨
        assert points_of(player.get(Wear), PICK) == 5 * WEAR_PER_SWING
    finally:
        session.shutdown()


@case
def test_walking_off_the_vein_stops_the_pick():
    """走开自动收工：不留"人在别处、矿在这边"的脏状态。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        session.step(1)
        assert player.get(Mining).active is True
        assert player.get(Mining).vein == 0

        _stand_on(player, 14, 26)
        session.step(1)
        assert player.get(Mining).active is False, "人走了还在挖"
        assert player.get(Mining).vein == -1
    finally:
        session.shutdown()


# ── 规则：产出与随机流 ────────────────────────────────

@case
def test_swing_readings_are_pinned_per_vein():
    """同一锄换个矿点，读数必须完全不同（流名里带矿点下标）。"""
    for index, roll in enumerate(SWING_ROLLS_0):
        got = mining.swing(DATA, seed=20261020, map_id="biqi_mine_1",
                           vein_index=0, swing_index=index, vein_id="vein_copper")
        assert got["roll"] == roll, (index, got)
        assert got["ore"] == SWING_ORES_0[index], (index, got)
    got = mining.swing(DATA, seed=20261020, map_id="biqi_mine_1",
                       vein_index=1, swing_index=0, vein_id="vein_copper")
    assert got["roll"] == SWING_ROLL_VEIN_1, got
    assert got["roll"] != SWING_ROLLS_0[0]


@case
def test_mining_does_not_shift_the_drop_streams():
    """先挖矿再打怪，与直接打怪，掉落读数必须一模一样。"""
    table = DATA.drops["drops"]["bande"]
    before = roll_drops(table, seed=20261020, monster_id="bande",
                        kill_index=0).as_dict()

    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        session.world.set_data(KEY_MINE_TOGGLE, True)
        for _ in range(60):
            session.step(1)
        after = roll_drops(table, seed=20261020, monster_id="bande",
                           kill_index=0).as_dict()
        assert after == before, (after, before)
    finally:
        session.shutdown()


@case
def test_empty_swings_stay_silent():
    """空锄不回音：十刻一锄，每锄都弹一行的屏幕没法看。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        echoes = 0
        blocked = 0
        for _ in range(250):
            session.step(1)
            got = world.get_data(KEY_LAST_ORE)
            if not got:
                continue
            world.set_data(KEY_LAST_ORE, None)   # 模拟表现层读走
            if got.get("ok"):
                echoes += 1
            else:
                blocked += 1
        got_ores = sum(_ore_counts(player.get(Bag)).values())
        # 回音次数 = 挖到的块数，一锄都不多弹
        assert echoes == got_ores, (echoes, got_ores)
        # 20 锄挖空，之后只挡下**一次**（挡完就收工了）
        assert _ledger(world)["total"] == VEIN_CAPACITY
        assert blocked == 1, blocked
    finally:
        session.shutdown()


@case
def test_a_depleted_vein_grows_back_with_its_swing_count_intact():
    """长回来之后储量清零，但**累计锄数不清零**——否则流名被重用。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        for _ in range(250):
            session.step(1)
        ledger = _ledger(world)
        assert ledger["swings"] == VEIN_CAPACITY
        assert int(ledger["until"]) > 0, "挖空了却没记恢复时间"

        for _ in range(VEIN_REGROW):
            session.step(1)
        world.set_data(KEY_MINE_TOGGLE, True)   # 再按一次 F 开工
        session.step(1)
        ledger = _ledger(world)
        assert ledger["swings"] == 1, "长回来了储量没清零"
        assert ledger["total"] == VEIN_CAPACITY + 1, "累计锄数被清零了"
        assert int(ledger["until"]) == 0
        assert player.get(Mining).active is True
    finally:
        session.shutdown()


@case
def test_carrying_too_much_stops_the_pick_without_giving_ore():
    """背不动了就停：宁可空挥，也不给"挖到了却捡不起来"的挫败。"""
    session, player = _session()          # 1 级战士，负重只有 30
    try:
        _with_pick(player)                # 锄头 12，剩 18
        assert player.get(Body).max_weight == 30
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        gained = 0
        reason = ""
        for _ in range(200):
            session.step(1)
            got = world.get_data(KEY_LAST_ORE)
            if not got:
                continue
            world.set_data(KEY_LAST_ORE, None)
            if got.get("ok"):
                gained += 1
            else:
                reason = got["code"]
                break
        # 挖到过东西（否则这条测的就不是"背满"），然后被"背不动"挡下
        assert gained >= 1, "一锄都没挖到，这条测不到东西"
        assert reason == "overweight", reason
        assert player.get(Mining).active is False
        # 18 点余量装最轻的铜（3）也只够 6 块：负重真在限速，不是摆设
        assert sum(_ore_counts(player.get(Bag)).values()) <= 6
    finally:
        session.shutdown()


@case
def test_mining_loads_the_bag_with_ores_and_wear():
    """一次完整出行：矿石进包、锄头掉耐久，两笔账都对得上。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        for _ in range(200):
            session.step(1)
        # 这一种子下 200 刻里挖到的东西（改矿脉表/流名必红）
        assert _ore_counts(player.get(Bag)) == {
            "ore_copper": 5, "ore_silver": 3, "ore_iron": 1}
        assert _ledger(world)["total"] == 20
        assert points_of(player.get(Wear), PICK) == 20
    finally:
        session.shutdown()


@case
def test_changing_map_voids_the_vein_ledger():
    """图内矿点是下标，换图必须整体作废，否则会指到别的地方去。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _with_pick(player)
        _stand_on(player, 14, 20)
        world = session.world
        world.set_data(KEY_MINE_TOGGLE, True)
        for _ in range(60):
            session.step(1)
        assert _ledger(world)["total"] >= 1

        session.travel_to("wugong_cave_1")
        session.step(1)
        assert world.get_data(KEY_MINING_VEINS) == {}, "账本留到新图去了"
        assert player.get(Mining).active is False
        assert player.get(Mining).vein == -1
    finally:
        session.shutdown()


# ── 出手：卖矿 ────────────────────────────────────────

@case
def test_selling_ores_is_one_lump_sum():
    """整笔结算：要么全卖，要么一块不卖。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        bag = player.get(Bag)
        bag.add("ore_copper", 3)          # 30 // 3 = 10，三块 30
        bag.add("ore_blackiron_2", 1)     # 800 // 3 = 266
        bag.add("mujian", 1)              # 不是矿石，不跟着卖
        assert ore_value(DATA, player) == 30 + 266
        got = sell_ores(player, DATA)
        assert got["earned"] == 296, got
        assert sorted(got["items"]) == ["ore_blackiron_2", "ore_copper"]
        assert bag.items.get("ore_copper", 0) == 0
        assert bag.items.get("ore_blackiron_2", 0) == 0
        assert bag.items.get("mujian") == 1, "把装备一起卖了"
        assert ore_value(DATA, player) == 0
        try:
            sell_ores(player, DATA)
        except Exception as exc:                # noqa: BLE001
            assert type(exc).__name__ == "TradeError" and exc.code == "nothing_to_sell"
        else:
            raise AssertionError("没矿石还能卖")
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
    print(f"test_mining: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
