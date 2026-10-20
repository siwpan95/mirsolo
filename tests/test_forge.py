"""武器升级与祝福油（P3-S5）—— 黑铁矿石顶等级，祝福油赌幸运。

钉死八件事：

1. **只有真武器能升级。** 鹤嘴锄占 ``weapon`` 槽，但它带 ``tool`` 标记——
   给它加攻击力，"挖矿要腾出武器槽"这个代价就凭空消失了。
2. **纯度就是成功率。** 30/50/70/100 → 4000/6000/8000/10000（万分比），
   每升一级再扣 600，最后夹在 500 的地板上：永远留一线。
3. **先扣材料、再掷点。** 失败也收矿石与金币——不然"一直点到成功为止"
   是最优解，升级就退化成一个没有代价的按钮。
4. **失败降一级，+0 不降**（没有 -1 这种等级，+0 已经是地板）。
5. **尝试次数只增不减。** 流名 ``upgrade:{item_id}:{attempt}`` /
   ``bless:{item_id}:{attempt}``，读档重来掷出的是**同一个**数，刷不出来。
6. **祝福油改的是武器幸运**，成功 +1 / 失败 −1；负值是**诅咒**，
   它在 ``lucky_range`` 里对称地压上限（幸运抬下限、诅咒压上限）。
7. **只作用于穿着的那把**：按 ``item_id`` 记账，包里另一把同款不受影响。
8. **改完账本立刻重算属性**：``recompute`` 是唯一出口，漏一步就是
   "点完了但攻击力没动"。

字面量原则：期望值写死（7 / (1,2) / 4000 / 600 / 7993 …），不从数据表推导。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_player
from mirsolo.bootstrap import build_session
from mirsolo.combat.formulas import LUCK_CAP, lucky_range
from mirsolo.components import (
    Attributes,
    Bag,
    Blessing,
    Progression,
    Upgrade,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.items import blessing, potion, upgrade
from mirsolo.items.equipment import equip, recompute
from mirsolo.presentation.panels import build_forge_panel, shop_stock
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

# ── 数据字面量（改表必须同步改这里）────────────────────────────
MAX_LEVEL = 7               # +7 封顶
BONUS = (1, 2)              # 每级：下限 +1、上限 +2（区间会变宽）
STEP_BP = 600               # 每级从成功率里扣掉的万分比
MIN_SUCCESS_BP = 500        # 成功率地板

#: 纯度 → 成功率基准（万分比）；普通矿石没有纯度，purity 记 0
PURITY_BP = {30: 4000, 50: 6000, 70: 8000, 100: 10000}
#: 七档：(from, to, min_purity, gold_permille)；矿石数恒为 1
LEVEL_ROWS = ((0, 1, 30, 50), (1, 2, 30, 100), (2, 3, 50, 150),
              (3, 4, 50, 200), (4, 5, 70, 250), (5, 6, 70, 300),
              (6, 7, 100, 350))

BLESS_BASE_BP = 7000        # 祝福油基础成功率
BLESS_STEP_BP = 700         # 每级递减
BLESS_MIN_BP = 1000
BLESS_MAX = 7               # 祝福上限
BLESS_MIN = -7              # 祝福下限（负值即诅咒）

OIL_PRICE = 3000
OIL_WEIGHT = 2

MUJIAN = "mujian"                   # 木剑：atk 2-5，120 金
MUJIAN_ATK = (2, 5)
MUJIAN_PRICE = 120
ZHANMADAO = "zhanmadao"             # 斩马刀：atk 7-15
ZHANMADAO_ATK = (7, 15)
PICK = "hezuichu"                   # 鹤嘴锄：带 tool 标记
NECKLACE = "xingyun_xianglian"      # 幸运项链：luck 2，44 级
NECKLACE_LUCK = 2
NECKLACE_LEVEL_REQ = 44

WARRIOR_BASE_DC_30 = (8, 16)        # 战士 30 级裸身 dc
WARRIOR_BASE_DC_44 = (11, 23)       # 战士 44 级裸身 dc（幸运项链要 44 级）
SEED = 20261020

#: 木剑 +0/+1/+2 三连（纯度 100）的读数与结果：成、成、败
UP_ROLLS = (7993, 3580, 9906)
UP_BPS = (10000, 9400, 8800)
UP_LEVELS = (1, 2, 1)               # 第三次失败，从 +2 掉回 +1
#: 每一档的服务费（120 × 千分比 // 1000）
UP_GOLDS = (6, 12, 18)
#: 连抹五瓶的读数与结果：败(-1)、成(0)、败(-1)、成(0)、成(+1)
BLESS_ROLLS = (7676, 941, 7560, 2807, 2059)
BLESS_BPS = (7000, 7700, 7000, 7700, 7000)
BLESS_LEVELS = (-1, 0, -1, 0, 1)
#: 账本 attempts 直接拨到 7（等价于读档回到第 7 次）时的读数
RELOAD_UP_ROLL = 7921
RELOAD_BLESS_ROLL = 6474

#: 锻造面板的可点文案（ore 名/瓶数/成功率都写死）
PANEL_UP_DETAIL = "黑铁矿石(纯度100) ×1 +6金 · 100%"
PANEL_BLESS_DETAIL = "持有 3 瓶 · 70% · 失败降 1"


def case(fn):
    CASES.append(fn)
    return fn


def _session(map_id: str = "biqi_mine_1", seed: int = SEED):
    # 不传 ``data=`` 会拿到另一份 GameData，测试里改表就白改（踩过）
    session = build_session(data=DATA, seed=seed, map_id=map_id)
    player = spawn_player(session.world, DATA, class_id="warrior")
    return session, player


def _at_level(player, level: int):
    prog = player.get(Progression)
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"]["warrior"])
    return player


def _wield(player, item_id: str):
    """穿上（走 equip 真路径：槽位/职业/等级/负重四道门都在）。"""
    player.get(Bag).add(item_id, 1)
    equip(player, DATA, item_id)
    return player


def _rich(player, gold: int = 1000):
    player.get(Wallet).gold = gold
    return player


def _forger(level: int = 30, weapon: str = MUJIAN, gold: int = 1000,
            ore: str = "ore_blackiron_4", ores: int = 3, oils: int = 0):
    """一个站在锻造台前的战士：等级到位、手握武器、兜里有钱。"""
    session, player = _session()
    _at_level(player, level)
    _wield(player, weapon)
    if ores:
        player.get(Bag).add(ore, ores)
    if oils:
        player.get(Bag).add(blessing.OIL, oils)
    player.get(Wallet).gold = gold
    return session, player


# ── 数据：升级表 ──────────────────────────────────────

@case
def test_the_upgrade_table_is_seven_contiguous_steps():
    """七档连号、末档到 +7、每档只要一块矿石。"""
    assert upgrade.max_level(DATA) == MAX_LEVEL
    assert upgrade.bonus_per_level(DATA) == BONUS
    assert upgrade.step_bp(DATA) == STEP_BP
    assert upgrade.min_success_bp(DATA) == MIN_SUCCESS_BP

    rows = upgrade.levels(DATA)
    assert len(rows) == MAX_LEVEL, len(rows)
    for row, (f, t, mp, gp) in zip(rows, LEVEL_ROWS):
        assert (row["from"], row["to"]) == (f, t), row
        assert row["min_purity"] == mp, row
        assert row["gold_permille"] == gp, row
        assert row["ore"] == 1, row
    assert rows[-1]["to"] == MAX_LEVEL
    # 满级之后没有档位可走
    assert upgrade.row_for(DATA, MAX_LEVEL) is None


@case
def test_purity_is_the_whole_success_rate():
    """纯度决定成败，等级负责把成功率一层层削下去。"""
    for purity, bp in PURITY_BP.items():
        assert upgrade.purity_bp_of(DATA, purity) == bp, purity
    assert upgrade.purity_bp_of(DATA, 0) == 0, "普通矿石没有纯度"
    assert upgrade.purity_bp_of(DATA, 55) == 0, "表里没有的纯度一律 0"

    assert upgrade.success_bp(DATA, 0, 100) == 10000   # +0 配纯度 100 必成
    assert upgrade.success_bp(DATA, 6, 100) == 6400    # 顶 +7 只剩 64%
    assert upgrade.success_bp(DATA, 6, 30) == MIN_SUCCESS_BP   # 夹到地板
    assert upgrade.success_bp(DATA, 0, 0) == 0         # 拿错矿石 = 0%


@case
def test_only_real_weapons_are_upgradable():
    """法杖、道剑都能升（加的是 mc/sc）；鹤嘴锄与项链不能。"""
    for item_id in (MUJIAN, "wumujian", ZHANMADAO, "chiyue_mojian",
                    "chiyue_mozhang", "chiyue_daojian", "niumo_zhanfu"):
        assert upgrade.is_upgradable(DATA, item_id), item_id
    assert not upgrade.is_upgradable(DATA, PICK), "鹤嘴锄是工具不是武器"
    assert not upgrade.is_upgradable(DATA, NECKLACE)
    assert not upgrade.is_upgradable(DATA, blessing.OIL)


@case
def test_every_weapon_has_exactly_one_main_stat():
    """加在哪一条由数据决定（atk/mc/sc 三者互斥，门禁钉着）。"""
    for item_id in (MUJIAN, ZHANMADAO, "chiyue_mojian", "niumo_zhanfu"):
        assert upgrade.main_stat(DATA, item_id) == "atk", item_id
    assert upgrade.main_stat(DATA, "chiyue_mozhang") == "mc"
    assert upgrade.main_stat(DATA, "chiyue_daojian") == "sc"
    assert upgrade.main_stat(DATA, NECKLACE) is None


@case
def test_blessing_oil_is_not_a_potion():
    """祝福油自成一类 kind：不是 consumable，就不会被当药灌、也不算补货余量。"""
    spec = ITEMS[blessing.OIL]
    assert spec["kind"] == "blessing", spec
    assert spec["weight"] == OIL_WEIGHT
    assert spec["price"] == OIL_PRICE
    assert spec.get("slot") is None
    assert spec.get("auction") is None, "祝福油不进拍卖行"
    assert potion.is_potion(DATA, blessing.OIL) is False, "祝福油被当成药水了"
    # 商店有售：掉落之外的第二条腿，不刷 BOSS 的人也得有路子
    assert blessing.OIL in shop_stock(DATA)


@case
def test_blessing_odds_fall_as_luck_rises_and_curse_pulls_them_back():
    """越高越难，但诅咒越深越容易拉回来——否则连续失败会把人锁死在 -7。"""
    assert blessing.cap(DATA) == BLESS_MAX
    assert blessing.floor(DATA) == BLESS_MIN
    assert blessing.success_bp(DATA, 0) == 7000
    assert blessing.success_bp(DATA, 6) == 2800
    assert blessing.success_bp(DATA, BLESS_MAX) == 2100
    assert blessing.success_bp(DATA, -1) == 7700
    assert blessing.success_bp(DATA, BLESS_MIN) == 10000   # 诅咒到底反而必成


# ── 规则：升级 ────────────────────────────────────────

@case
def test_bare_hands_cannot_be_upgraded():
    session, player = _session()
    try:
        assert upgrade.plan(DATA, player) == {
            "ok": False, "code": "no_weapon", "item": "", "level": 0}
        try:
            upgrade.upgrade(player, DATA, SEED)
        except upgrade.UpgradeError as exc:
            assert exc.code == "no_weapon"
        else:
            raise AssertionError("空手也能升级")
    finally:
        session.shutdown()


@case
def test_the_pick_cannot_be_upgraded():
    """鹤嘴锄握在手里也升不了：挖矿的代价不能被升级抵掉。"""
    session, player = _session()
    try:
        _at_level(player, 30)
        _wield(player, PICK)
        info = upgrade.plan(DATA, player)
        assert info["code"] == "not_upgradable", info
        assert info["item"] == PICK
    finally:
        session.shutdown()


@case
def test_the_purity_gate_turns_the_button_off():
    """+6 那一档只认纯度 100，揣着 70 的矿石就是点不动。"""
    session, player = _forger(ore="ore_blackiron_3", ores=1)
    try:
        player.get(Upgrade).levels[MUJIAN] = 6
        info = upgrade.plan(DATA, player)
        assert info["code"] == "purity_too_low", info
        assert info["gate"] == 100, info
        # 换一块够格的立刻可点
        player.get(Bag).add("ore_blackiron_4", 1)
        assert upgrade.plan(DATA, player)["ok"] is True
    finally:
        session.shutdown()


@case
def test_pick_ore_takes_the_highest_purity_that_clears_the_gate():
    """挑够门槛里最高的——与药水"挑最小的"相反：矿石没有合成出口，省着没用。"""
    session, player = _session()
    try:
        bag = player.get(Bag)
        bag.add("ore_blackiron_1", 1)
        bag.add("ore_blackiron_3", 1)
        bag.add("ore_copper", 5)
        assert upgrade.pick_ore(DATA, bag, 30) == "ore_blackiron_3"
        assert upgrade.pick_ore(DATA, bag, 50) == "ore_blackiron_3"
        assert upgrade.pick_ore(DATA, bag, 100) is None, "没有够格的就该是 None"
        # 挑走之后退回次高的
        bag.remove("ore_blackiron_3", 1)
        assert upgrade.pick_ore(DATA, bag, 30) == "ore_blackiron_1"
    finally:
        session.shutdown()


@case
def test_gold_is_a_service_fee_charged_either_way():
    """金币买的是"这一次尝试"，不是"这一次成功"——失败也收。"""
    assert upgrade.gold_cost(DATA, MUJIAN, 0) == 6      # 120 × 50‰
    assert upgrade.gold_cost(DATA, MUJIAN, 6) == 42     # 120 × 350‰
    assert upgrade.gold_cost(DATA, ZHANMADAO, 0) == 160  # 3200 × 50‰

    # 纯度 30 在 +0 只有 40%，这一种子掷出 7993 —— 必败
    session, player = _forger(ore="ore_blackiron_1", ores=1)
    try:
        got = upgrade.upgrade(player, DATA, SEED)
        assert got["ok"] is False, got
        assert got["roll"] == 7993 and got["bp"] == 4000, got
        assert got["paid"] == 6, got                    # 服务费照收
        assert player.get(Bag).items.get("ore_blackiron_1", 0) == 0, "矿石没扣"
        assert player.get(Wallet).gold == 1000 - 6
    finally:
        session.shutdown()


@case
def test_three_upgrades_in_a_row_are_pinned():
    """+0→+1→+2 然后掉回 +1：三次读数、三笔服务费、一块矿石都不剩。"""
    session, player = _forger()
    try:
        bag = player.get(Bag)
        for index, (roll, bp, level, gold) in enumerate(
                zip(UP_ROLLS, UP_BPS, UP_LEVELS, UP_GOLDS)):
            got = upgrade.upgrade(player, DATA, SEED)
            assert got["roll"] == roll, (index, got)
            assert got["bp"] == bp, (index, got)
            assert got["level"] == level, (index, got)
            assert got["paid"] == gold, (index, got)
            assert got["item"] == MUJIAN
        # 三块矿石全扣光，服务费合计 36
        assert bag.items.get("ore_blackiron_4", 0) == 0
        assert player.get(Wallet).gold == 1000 - 36
        # 等级掉回去了，但尝试次数没有跟着掉
        assert upgrade.level_of(player.get(Upgrade), MUJIAN) == 1
        assert upgrade.attempts_of(player.get(Upgrade), MUJIAN) == 3
    finally:
        session.shutdown()


@case
def test_failing_at_zero_stays_at_zero():
    """+0 失败不降：没有 -1 这种等级，+0 已经是地板。"""
    session, player = _forger(ore="ore_blackiron_1", ores=1)
    try:
        got = upgrade.upgrade(player, DATA, SEED)
        assert got["ok"] is False and got["from"] == 0, got
        assert got["level"] == 0, got
        assert upgrade.level_of(player.get(Upgrade), MUJIAN) == 0
        # 但代价一分不少：材料全损 + 服务费照收
        assert player.get(Bag).items.get("ore_blackiron_1", 0) == 0
        assert player.get(Wallet).gold == 994
        assert upgrade.attempts_of(player.get(Upgrade), MUJIAN) == 1
    finally:
        session.shutdown()


@case
def test_reloading_replays_the_same_reading():
    """账本 attempts 拨回同一个数 = 掷出同一个数：读档刷不了。"""
    readings = []
    for _ in range(2):
        session, player = _forger(ores=1)
        try:
            player.get(Upgrade).attempts[MUJIAN] = 7   # 等价于读档回到第 7 次
            got = upgrade.upgrade(player, DATA, SEED)
            readings.append(got["roll"])
            assert got["level"] == 1, got
        finally:
            session.shutdown()
    assert readings == [RELOAD_UP_ROLL, RELOAD_UP_ROLL], readings
    # 换一个次数必须是另一个数（否则"同数"就只是常量而已）
    session, player = _forger(ores=1)
    try:
        assert upgrade.upgrade(player, DATA, SEED)["roll"] == UP_ROLLS[0]
    finally:
        session.shutdown()


@case
def test_upgrading_recomputes_the_damage_immediately():
    """+1 之后 dc 立刻变：下限 +1、上限 +2（区间变宽，与幸运 9 互补）。"""
    session, player = _forger(ores=1)
    try:
        assert tuple(player.get(Attributes).dc) == (
            WARRIOR_BASE_DC_30[0] + MUJIAN_ATK[0],
            WARRIOR_BASE_DC_30[1] + MUJIAN_ATK[1])
        upgrade.upgrade(player, DATA, SEED)
        # (10, 21) → (11, 23)：不重算的话这里还是 (10, 21)
        assert tuple(player.get(Attributes).dc) == (11, 23)
        assert upgrade.bonus_of(DATA, player.get(Upgrade), MUJIAN) == BONUS
    finally:
        session.shutdown()


# ── 规则：祝福油 ──────────────────────────────────────

@case
def test_five_oils_curse_then_pull_themselves_out():
    """败 → 成 → 败 → 成 → 成：诅咒不是死局，深了反而更容易拉回来。"""
    session, player = _forger(ores=0, oils=5)
    try:
        book = player.get(Blessing)
        for index, (roll, bp, level) in enumerate(
                zip(BLESS_ROLLS, BLESS_BPS, BLESS_LEVELS)):
            got = blessing.bless(player, DATA, SEED)
            assert got["roll"] == roll, (index, got)
            assert got["bp"] == bp, (index, got)
            assert got["level"] == level, (index, got)
            assert got["cursed"] is (level < 0), (index, got)
        assert player.get(Bag).items.get(blessing.OIL, 0) == 0
        assert blessing.attempts_of(book, MUJIAN) == 5
        assert player.get(Attributes).luck == 1, "祝福 +1 应该体现在总幸运上"
    finally:
        session.shutdown()


@case
def test_there_is_no_weapon_without_a_blessing_book():
    """没穿武器就抹油，与升级同一句回绝。"""
    session, player = _session()
    try:
        player.get(Bag).add(blessing.OIL, 2)
        info = blessing.plan(DATA, player)
        assert info["code"] == "no_weapon", info
        try:
            blessing.bless(player, DATA, SEED)
        except blessing.BlessError as exc:
            assert exc.code == "no_weapon"
        else:
            raise AssertionError("空手也能抹油")
    finally:
        session.shutdown()


@case
def test_the_curse_floor_shuts_the_oil_off():
    """-7 是地板：到底了再抹也没用，面板要写清楚而不是默默吞掉。"""
    session, player = _forger(ores=0, oils=1)
    try:
        player.get(Blessing).levels[MUJIAN] = BLESS_MIN
        info = blessing.plan(DATA, player)
        assert info["code"] == "floored", info
        assert info["level"] == BLESS_MIN
    finally:
        session.shutdown()


@case
def test_a_cursed_weapon_loses_its_upper_end():
    """诅咒压上限（与幸运抬下限对称）：(15,31) 在 -7 时只剩 (15,18)。"""
    session, player = _forger(weapon=ZHANMADAO, ores=0)
    try:
        dc = (WARRIOR_BASE_DC_30[0] + ZHANMADAO_ATK[0],
              WARRIOR_BASE_DC_30[1] + ZHANMADAO_ATK[1])
        assert tuple(player.get(Attributes).dc) == dc
        assert lucky_range(dc, 0) == dc

        player.get(Blessing).levels[ZHANMADAO] = BLESS_MIN
        recompute(player, DATA)
        assert player.get(Attributes).luck == BLESS_MIN
        assert blessing.is_cursed(player.get(Blessing), ZHANMADAO) is True
        assert lucky_range(dc, BLESS_MIN) == (15, 18)
        # 反过来幸运 7 把下限抬到 28：两端各走各的，不是同一件事
        assert lucky_range(dc, 7) == (28, 31)
    finally:
        session.shutdown()


@case
def test_a_wooden_sword_can_reach_luck_nine():
    """木剑 +7 配幸运项链 = 9：幸运不再是终局武器的专利。"""
    session, player = _forger(level=NECKLACE_LEVEL_REQ, ores=0)
    try:
        player.get(Upgrade).levels[MUJIAN] = MAX_LEVEL
        player.get(Blessing).levels[MUJIAN] = BLESS_MAX
        _wield(player, NECKLACE)
        attr = player.get(Attributes)
        assert attr.luck == LUCK_CAP, attr.luck
        # +7 的加成：下限 +7、上限 +14 —— 44 级裸身 (11,23) + 木剑 (2,5) = (13,28)
        # → (20,42)。dc 写死，写活就等于把公式抄一遍、改错了也发现不了。
        assert tuple(attr.dc) == (20, 42), tuple(attr.dc)
        # 幸运 9 = 刀刀上限
        assert lucky_range(tuple(attr.dc), attr.luck) == (42, 42)
    finally:
        session.shutdown()


@case
def test_blessing_attempts_are_pinned_too():
    """祝福与升级同一条防线：同一个 attempts 掷出同一个数。"""
    readings = []
    for _ in range(2):
        session, player = _forger(ores=0, oils=1)
        try:
            player.get(Blessing).attempts[MUJIAN] = 7
            readings.append(blessing.bless(player, DATA, SEED)["roll"])
        finally:
            session.shutdown()
    assert readings == [RELOAD_BLESS_ROLL, RELOAD_BLESS_ROLL], readings


@case
def test_only_the_wielded_weapon_is_touched():
    """按 item_id 记账：包里那把斩马刀一点不受影响。"""
    session, player = _forger(ores=1)
    try:
        player.get(Bag).add(ZHANMADAO, 1)
        upgrade.upgrade(player, DATA, SEED)          # 木剑 +1
        assert upgrade.level_of(player.get(Upgrade), MUJIAN) == 1
        assert upgrade.level_of(player.get(Upgrade), ZHANMADAO) == 0

        equip(player, DATA, ZHANMADAO)               # 换刀（自动脱下木剑）
        assert tuple(player.get(Attributes).dc) == (15, 31), "斩马刀跟着涨了"
        assert upgrade.wielded_weapon(player) == ZHANMADAO
        # 再升一次，涨的是斩马刀，木剑仍停在 +1
        player.get(Bag).add("ore_blackiron_4", 1)
        got = upgrade.upgrade(player, DATA, SEED)
        assert got["item"] == ZHANMADAO and got["level"] == 1, got
        assert upgrade.level_of(player.get(Upgrade), MUJIAN) == 1
    finally:
        session.shutdown()


# ── 面板 ──────────────────────────────────────────────

@case
def test_the_forge_panel_writes_the_price_and_the_odds():
    """可点时把矿石、金币、成功率一起写出来——决策全在这三个数里。"""
    session, player = _forger(ores=2, oils=3)
    try:
        panel = build_forge_panel(player=player, data=DATA)
        row = next(r for r in panel.rows if r.action == "upgrade")
        assert row.enabled is True
        assert row.text == "升到 +1", row.text
        assert row.detail == PANEL_UP_DETAIL, row.detail

        brow = next(r for r in panel.rows if r.action == "bless")
        assert brow.enabled is True
        assert brow.text == "抹一瓶祝福油", brow.text
        assert brow.detail == PANEL_BLESS_DETAIL, brow.detail
    finally:
        session.shutdown()


@case
def test_the_forge_panel_says_why_the_button_is_off():
    """点不动时照样画出来并写清原因：整行消失的话玩家只会以为按钮坏了。"""
    session, player = _forger(ores=0, oils=0, gold=0)
    try:
        panel = build_forge_panel(player=player, data=DATA)
        row = next(r for r in panel.rows if r.action == "upgrade")
        assert row.enabled is False
        assert row.text == "纯度不够", row.text
        assert row.detail == "需要纯度不低于 30 的黑铁矿石", row.detail

        brow = next(r for r in panel.rows if r.action == "bless")
        assert brow.text == "没有祝福油", brow.text
        assert brow.detail == "商店有售，BOSS 也掉", brow.detail

        # 有了矿石但没钱：换成"还差多少金"
        player.get(Bag).add("ore_blackiron_4", 1)
        row = next(r for r in build_forge_panel(player=player, data=DATA).rows
                   if r.action == "upgrade")
        assert row.text == "金币不够", row.text
        assert row.detail == "还差 6 金", row.detail
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
    print(f"test_forge: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
