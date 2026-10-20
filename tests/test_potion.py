"""药水与续航（P3-S3）—— 让"打怪 → 掉药 → 喝药"这条链真的转起来。

钉死的六件事：

1. **九档药水都在表里**，回复量 / 重量 / 价格按字面量对账（改表必须改这里）；
2. **选药不浪费**：够用的里面挑最小的，都不够才用最大的；
3. **公共冷却**：手动与自动共用一个冷却账，一个 tick 里不可能连灌两瓶；
4. **倒地喝不了**：能喝的话死亡惩罚就只剩掉东西了；
5. **自动喝药的阈值**是整数千分比比较（HP < 50% / MP < 30%），且战士
   （``max_mp == 0``）永远不会自动灌蓝药；
6. **药水组追加在掉落表末尾**，既有装备组的随机流下标一个都没挪。

字面量原则：期望值写死（20 / 50 / 100 / 200 / 700 / 136 / 186 …），
不从数据表推导，也不自指调生产函数来算期望值。
"""

from __future__ import annotations

from mirsolo.actor import penalty
from mirsolo.actor.factory import spawn_player
from mirsolo.actor.systems import KEY_LAST_POTION, KEY_POTION_INTENT
from mirsolo.bootstrap import build_session
from mirsolo.components import Bag, Body, PotionState, Progression, Vitals, Wallet
from mirsolo.data.loader import GameData
from mirsolo.economy.shop import buy_price, resupply
from mirsolo.items import blessing, potion
from mirsolo.items.drops import roll_drops
from mirsolo.presentation.panels import shop_stock
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()

# ── 数据字面量（改表/改常量必须同步改这里）──────────────────
COOLDOWN = 10          # 公共冷却（刻）：10 刻 = 1 秒
AUTO_HP_PERMILLE = 500 # HP 低于 50% 自动喝红
AUTO_MP_PERMILLE = 300 # MP 低于 30% 自动喝蓝
BUNDLE = 10            # 商店一次买几瓶

SMALL = 20    # 金创药/魔法药(小) 回复量
MEDIUM = 50   # (中)
LARGE = 100   # (大)
SUPER = 200   # 超级
SUN = 50      # 太阳水（红蓝各 50）

PRICE_HP_L = 700       # 金创药(大) 单价
PRICE_MP_L = 840       # 魔法药(大) 单价
PRICE_HP_M = 300       # 金创药(中) 单价

W_HP_L = 3             # 金创药(大) 重量
W_MP_L = 3
W_HP_M = 2

WARRIOR_30_HP = 136    # 战士 30 级 max_hp（20 + 29×4）
WARRIOR_30_WEIGHT = 88 # 战士 30 级负重（30 + 29×2）
MAGE_30_HP = 72        # 法师 30 级 max_hp（14 + 29×2）
MAGE_30_MP = 186       # 法师 30 级 max_mp（12 + 29×6）
MAGE_30_WEIGHT = 49    # 法师 30 级负重（20 + 29×1）


def case(fn):
    CASES.append(fn)
    return fn


def _session(seed: int = 20261019):
    session = build_session(data=DATA, seed=seed, map_id="biqi_field")
    player = spawn_player(session.world, DATA, class_id="warrior")
    return session, player


def _at_level(player, class_id: str, level: int):
    """提等级并走一遍成长（会把血蓝补满，所以要先提级再改血量）。"""
    prog = player.get(Progression)
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"][class_id])
    return player


def _stock(player, *item_ids: str):
    for item_id in item_ids:
        player.get(Bag).add(item_id, 1)
    return player


# ── 数据表 ──────────────────────────────────────────

@case
def test_nine_potions_are_shipped():
    """九档：红/蓝各小中大 + 超级红/蓝 + 太阳水。"""
    kinds = [spec.get("kind") for spec in DATA.items["items"].values()]
    assert kinds.count("consumable") == 9, kinds.count("consumable")
    assert DATA.ok, DATA.errors


@case
def test_the_potion_table_numbers_are_pinned():
    """回复量 / 重量 / 价格 / 冷却逐项对账——价格是"每点回复量单价递增"自洽的。"""
    rows = {
        "hp_potion_s": (SMALL, 1, 100, 1),
        "hp_potion_m": (MEDIUM, 2, 300, 1),
        "hp_potion_l": (LARGE, 3, PRICE_HP_L, 1),
        "hp_potion_x": (SUPER, 4, 1600, 20),
        "mp_potion_s": (SMALL, 1, 120, 1),
        "mp_potion_m": (MEDIUM, 2, 360, 1),
        "mp_potion_l": (LARGE, 3, PRICE_MP_L, 1),
        "mp_potion_x": (SUPER, 4, 1920, 20),
        "sunwater": (SUN, 3, 900, 1),
    }
    for item_id, (amount, weight, price, level_req) in rows.items():
        spec = DATA.items["items"][item_id]
        consume = spec["consume"]
        assert consume["amount"] == amount, (item_id, consume)
        assert consume["cooldown_ticks"] == COOLDOWN, (item_id, consume)
        assert spec["weight"] == weight, (item_id, spec["weight"])
        assert spec["price"] == price, (item_id, spec["price"])
        assert spec["level_req"] == level_req, (item_id, spec["level_req"])


@case
def test_gains_split_by_effect():
    """hp → (amount, 0)；mp → (0, amount)；both → (amount, amount)。"""
    assert potion.gains(DATA, "hp_potion_s") == (SMALL, 0)
    assert potion.gains(DATA, "mp_potion_s") == (0, SMALL)
    assert potion.gains(DATA, "sunwater") == (SUN, SUN)


@case
def test_equipment_is_not_a_potion():
    """木剑喝了没用：``consume_of`` 空、``gains`` 全 0、``drink`` 直接拒。"""
    assert potion.consume_of(DATA, "mujian") == {}
    assert potion.gains(DATA, "mujian") == (0, 0)
    session, player = _session()
    try:
        try:
            potion.drink(player, DATA, "mujian", tick=0)
        except potion.PotionError as exc:
            assert exc.code == "not_a_potion", exc.code
        else:
            raise AssertionError("木剑不该能喝")
    finally:
        session.shutdown()


@case
def test_potions_are_not_auctionable():
    """药水不进拍卖行（量产物，挂出去也是噪音）——面板因此显示"不可寄卖"。

    ``auction-book`` 门禁只查 equip/book，这条是它的补充：万一有人给药水
    补上 ``auction`` 段，拍卖行的"底价高于回收价"那条约束会被一堆 100 金的
    小药瓶拉歪。
    """
    for item_id, spec in DATA.items["items"].items():
        if spec.get("kind") == "consumable":
            assert spec.get("auction") is None, item_id


# ── 选药策略 ────────────────────────────────────────

@case
def test_pick_chooses_the_smallest_that_covers_the_gap():
    """够用的里面挑最小的：缺 5 喝小的、缺 45 喝中的、缺 60 喝大的。"""
    session, player = _session()
    try:
        bag = _stock(player, "hp_potion_s", "hp_potion_m",
                     "hp_potion_l", "hp_potion_x").get(Bag)
        assert potion.pick(bag, DATA, "hp", 5) == "hp_potion_s"
        assert potion.pick(bag, DATA, "hp", 45) == "hp_potion_m"
        assert potion.pick(bag, DATA, "hp", 60) == "hp_potion_l"
        assert potion.pick(bag, DATA, "hp", 150) == "hp_potion_x"
    finally:
        session.shutdown()


@case
def test_pick_falls_back_to_the_biggest_when_nothing_covers():
    """都不够就用最大的（缺 500 也先把最大的灌下去，比什么都不做强）。"""
    session, player = _session()
    try:
        bag = _stock(player, "hp_potion_s", "hp_potion_l").get(Bag)
        assert potion.pick(bag, DATA, "hp", 500) == "hp_potion_l"
    finally:
        session.shutdown()


@case
def test_pick_ignores_potions_that_cannot_help():
    """想回血的时候不会抓一瓶魔法药。"""
    session, player = _session()
    try:
        bag = _stock(player, "mp_potion_l").get(Bag)
        assert potion.pick(bag, DATA, "hp", 10) is None
        assert potion.pick(bag, DATA, "mp", 10) == "mp_potion_l"
    finally:
        session.shutdown()


@case
def test_sunwater_counts_for_both():
    """太阳水两边都算候选——它是"一瓶顶两瓶"的应急品。"""
    session, player = _session()
    try:
        bag = _stock(player, "sunwater").get(Bag)
        assert potion.pick(bag, DATA, "hp", 10) == "sunwater"
        assert potion.pick(bag, DATA, "mp", 10) == "sunwater"
    finally:
        session.shutdown()


@case
def test_pick_skips_empty_stacks():
    """数量为 0 的条目不算候选（背包用 remove 后可能留下 0）。"""
    session, player = _session()
    try:
        bag = player.get(Bag)
        bag.add("hp_potion_s", 2)
        bag.remove("hp_potion_s", 2)
        assert bag.items.get("hp_potion_s", 0) == 0
        assert potion.pick(bag, DATA, "hp", 1) is None
    finally:
        session.shutdown()


# ── 喝下去 ──────────────────────────────────────────

@case
def test_drinking_heals_and_consumes_exactly_one():
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        assert vitals.max_hp == WARRIOR_30_HP
        vitals.hp = 40
        player.get(Bag).add("hp_potion_m", 2)
        got = potion.drink(player, DATA, "hp_potion_m", tick=0)
        assert got["hp"] == MEDIUM, got
        assert vitals.hp == 40 + MEDIUM, vitals.hp
        assert got["left"] == 1, got
    finally:
        session.shutdown()


@case
def test_healing_never_overshoots_the_maximum():
    """回血夹在上限：40 + 大药 100 = 140，但 30 级战士上限是 136。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 40
        player.get(Bag).add("hp_potion_l", 1)
        got = potion.drink(player, DATA, "hp_potion_l", tick=0)
        assert vitals.hp == WARRIOR_30_HP, vitals.hp
        assert got["hp"] == WARRIOR_30_HP - 40, got
    finally:
        session.shutdown()


@case
def test_mage_mp_potions_restore_and_clamp():
    session = build_session(data=DATA, seed=3, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        vitals = player.get(Vitals)
        assert vitals.max_mp == MAGE_30_MP
        vitals.mp = 0
        player.get(Bag).add("mp_potion_l", 3)
        first = potion.drink(player, DATA, "mp_potion_l", tick=0)
        assert first["mp"] == LARGE, first
        assert vitals.mp == LARGE, vitals.mp
        second = potion.drink(player, DATA, "mp_potion_l", tick=COOLDOWN)
        assert vitals.mp == MAGE_30_MP, vitals.mp  # 100 + 100 = 200 → 夹到 186
        assert second["mp"] == MAGE_30_MP - LARGE, second
    finally:
        session.shutdown()


@case
def test_sunwater_restores_both_at_once():
    session = build_session(data=DATA, seed=4, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        vitals = player.get(Vitals)
        vitals.hp = 10
        vitals.mp = 10
        player.get(Bag).add("sunwater", 1)
        got = potion.drink(player, DATA, "sunwater", tick=0)
        assert got["hp"] == SUN and got["mp"] == SUN, got
        assert vitals.hp == 10 + SUN and vitals.mp == 10 + SUN
    finally:
        session.shutdown()


@case
def test_drinking_sets_the_shared_cooldown():
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Vitals).hp = 40
        player.get(Bag).add("hp_potion_s", 2)
        got = potion.drink(player, DATA, "hp_potion_s", tick=7)
        assert got["ready_at"] == 7 + COOLDOWN, got
        assert potion.is_cooling(player.get(PotionState), 16)
        assert not potion.is_cooling(player.get(PotionState), 17)
    finally:
        session.shutdown()


@case
def test_a_second_drink_inside_the_cooldown_is_refused():
    """冷却没走完，第二瓶喝不下去：血不回、瓶不少。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 40
        player.get(Bag).add("hp_potion_s", 5)
        potion.drink(player, DATA, "hp_potion_s", tick=0)
        try:
            potion.drink(player, DATA, "hp_potion_s", tick=COOLDOWN - 1)
        except potion.PotionError as exc:
            assert exc.code == "cooling", exc.code
        else:
            raise AssertionError("冷却中不该能喝")
        assert vitals.hp == 40 + SMALL, vitals.hp
        assert player.get(Bag).items["hp_potion_s"] == 4
    finally:
        session.shutdown()


@case
def test_a_downed_player_cannot_drink():
    """倒地期间喝不了药——能喝的话死亡惩罚就只剩掉东西了。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 0
        player.get(Bag).add("hp_potion_l", 3)
        try:
            potion.drink(player, DATA, "hp_potion_l", tick=0)
        except potion.PotionError as exc:
            assert exc.code == "dead", exc.code
        else:
            raise AssertionError("倒地不该能喝")
        assert player.get(Bag).items["hp_potion_l"] == 3
    finally:
        session.shutdown()


@case
def test_drinking_a_potion_you_do_not_have_is_refused():
    session, player = _session()
    try:
        try:
            potion.drink(player, DATA, "hp_potion_l", tick=0)
        except potion.PotionError as exc:
            assert exc.code == "none_left", exc.code
        else:
            raise AssertionError("没有的东西不该能喝")
    finally:
        session.shutdown()


@case
def test_the_level_gate_does_not_lock_a_dropped_super_potion():
    """超级档的 level_req 只管商店购买，**不**管喝：BOSS 掉一瓶就该能喝。"""
    session, player = _session()
    try:
        player.get(Vitals).hp = 1
        player.get(Bag).add("hp_potion_x", 1)
        got = potion.drink(player, DATA, "hp_potion_x", tick=0)
        assert got["hp"] == 19, got  # 1 级战士上限 20，1 + 200 → 夹到 20，实回 19
    finally:
        session.shutdown()


# ── 自动喝药 ────────────────────────────────────────

@case
def test_auto_want_asks_for_hp_below_half():
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 60            # 60×1000 = 60000 < 136×500 = 68000
        assert potion.auto_want(vitals) == "hp"
        vitals.hp = 68            # 68000 不小于 68000 → 刚好不触发
        assert potion.auto_want(vitals) is None
    finally:
        session.shutdown()


@case
def test_auto_want_never_asks_for_mp_when_the_pool_is_empty():
    """战士 max_mp 恒为 0：永远不该自动灌蓝药（灌了也是 0）。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        assert vitals.max_mp == 0
        vitals.hp = 100           # 高于阈值，只剩 mp 那一档可判定
        assert potion.auto_want(vitals) is None
    finally:
        session.shutdown()


@case
def test_auto_want_asks_for_mp_when_the_pool_is_low():
    session = build_session(data=DATA, seed=5, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        vitals = player.get(Vitals)
        vitals.mp = 20            # 20×1000 = 20000 < 186×300 = 55800
        assert potion.auto_want(vitals) == "mp"
        vitals.mp = 56            # 56000 ≥ 55800 → 不触发
        assert potion.auto_want(vitals) is None
    finally:
        session.shutdown()


@case
def test_hp_is_checked_before_mp():
    """血见底的时候先补血：两路都低于阈值也只报 hp。"""
    session = build_session(data=DATA, seed=6, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        vitals = player.get(Vitals)
        vitals.hp = 10
        vitals.mp = 10
        assert potion.auto_want(vitals) == "hp"
    finally:
        session.shutdown()


@case
def test_auto_pick_sized_the_bottle_to_the_gap():
    """法师 30 级血 30/72，缺 42 → 中瓶(50)够用就不浪费大瓶(100)。"""
    session = build_session(data=DATA, seed=7, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        vitals = player.get(Vitals)
        vitals.hp = 30
        vitals.mp = MAGE_30_MP
        _stock(player, "hp_potion_s", "hp_potion_m", "hp_potion_l", "hp_potion_x")
        assert potion.auto_pick(player, DATA) == "hp_potion_m"
    finally:
        session.shutdown()


# ── PotionSystem ────────────────────────────────────

@case
def test_the_manual_intent_is_consumed_exactly_once():
    """按一次喝一口：意图消费即清，不会在每个 tick 里各来一瓶。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 40
        player.get(Bag).add("hp_potion_m", 3)
        session.world.set_data(KEY_POTION_INTENT, "hp")
        session.step(1)
        assert vitals.hp == 40 + MEDIUM, vitals.hp
        assert player.get(Bag).items["hp_potion_m"] == 2
        assert session.world.get_data(KEY_POTION_INTENT) is None, "意图必须消费即清"
        # 再跑到冷却结束之后（10 刻）：意图若是留着的，这里就会再灌一瓶
        for _ in range(15):
            session.step(1)
        assert player.get(Bag).items["hp_potion_m"] == 2, "意图没清，冷却一过就多喝了"
    finally:
        session.shutdown()


@case
def test_manual_drink_reports_back_once():
    """手动喝成功写一次 ``KEY_LAST_POTION``，读完即清（表现层据此弹提示）。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Vitals).hp = 40
        player.get(Bag).add("hp_potion_s", 2)
        session.world.set_data(KEY_POTION_INTENT, "hp")
        session.step(1)
        got = session.world.get_data(KEY_LAST_POTION)
        assert got and got["ok"] and got["item"] == "hp_potion_s", got
        assert got["hp"] == SMALL, got
    finally:
        session.shutdown()


@case
def test_a_refused_manual_drink_says_why():
    """没药时按 Q 要有回音（"背包里没有…"），而不是悄无声息。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Vitals).hp = 40
        session.world.set_data(KEY_POTION_INTENT, "hp")
        session.step(1)
        got = session.world.get_data(KEY_LAST_POTION)
        assert got and not got["ok"] and got["code"] == "none_left", got
    finally:
        session.shutdown()


@case
def test_auto_drink_kicks_in_without_any_input():
    """不按任何键，血掉到阈值以下也会自己喝（单机没有外挂，这一档常开）。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 60
        player.get(Bag).add("hp_potion_l", 2)
        session.step(1)
        assert vitals.hp == WARRIOR_30_HP, vitals.hp   # 60 + 100 → 夹到 136
        assert player.get(Bag).items["hp_potion_l"] == 1
    finally:
        session.shutdown()


@case
def test_automatic_drinks_are_deliberately_silent():
    """自动喝药不写 ``KEY_LAST_POTION``：它每 tick 都可能发生，刷提示就是噪音。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Vitals).hp = 60
        player.get(Bag).add("hp_potion_l", 2)
        session.world.set_data(KEY_LAST_POTION, None)
        session.step(1)
        assert player.get(Bag).items["hp_potion_l"] == 1, "自动喝药没生效"
        assert not session.world.get_data(KEY_LAST_POTION)
    finally:
        session.shutdown()


@case
def test_the_cooling_ledger_blocks_automatic_drinks_too():
    """手动与自动共用一本冷却账：手动刚喝完，自动那口必须等到期。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        vitals = player.get(Vitals)
        vitals.hp = 40
        player.get(Bag).add("hp_potion_s", 5)
        session.world.set_data(KEY_POTION_INTENT, "hp")
        session.step(1)
        assert player.get(Bag).items["hp_potion_s"] == 4
        session.step(1)  # 仍在冷却里（10 刻），血还是低于阈值也不该再喝
        assert player.get(Bag).items["hp_potion_s"] == 4
    finally:
        session.shutdown()


@case
def test_a_downed_player_gets_no_automatic_potions():
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Bag).add("hp_potion_l", 2)
        vitals = player.get(Vitals)
        vitals.hp = 0
        penalty.knock_down(player, tick=int(session.tick))
        # 规则层这一关先单独钉住：倒地期间压根不该进入"想喝什么"的判定，
        # 而不是指望 drink 那道 dead 兜住（两道都在才叫双保险，各管各的）
        assert potion.auto_want(vitals) is None, "倒地不该进入自动喝药判定"
        for _ in range(5):
            session.step(1)
        assert player.get(Bag).items["hp_potion_l"] == 2, "倒地期间被自动喂药了"
    finally:
        session.shutdown()


# ── 商店与掉落 ──────────────────────────────────────

@case
def test_the_shelf_lists_every_potion():
    stock = shop_stock(DATA)
    names = [i for i in stock if potion.is_potion(DATA, i)]
    assert len(names) == 9, names
    # 最便宜的那几行就是药水：小瓶 100 金，比最便宜的装备（布衣 80）只高一点
    assert buy_price(DATA, names[0]) <= 120, names[0]
    # 祝福油（P3-S5）也上架，但它**不是药**：口径必须走 is_potion，
    # 否则"药水负重 / 补货余量 / 自动喝药"三条都会把它算进去。
    assert blessing.OIL in stock, "祝福油没上架（幸运只剩掉落一条腿）"
    assert not potion.is_potion(DATA, blessing.OIL), "祝福油被当成药水了"
    # 光有一条"祝福油不是药"不够：真正的口径是"带一条合法的 consume.effect"。
    # 表里扫一遍——每一个 consumable 都必须被判成药，而且 **consume 段只能长在
    # consumable 身上**。后面这条是硬约束：一旦有人给装备/祝福油补了个 consume
    # 段，`is_potion` 里"先看 kind"那道闸就是唯一的拦截，闸一松就漏进药水清单。
    all_consumables = [
        i for i, spec in DATA.items["items"].items()
        if spec.get("kind") == "consumable"
    ]
    assert len(all_consumables) == 9, all_consumables
    assert sorted(names) == sorted(all_consumables), (names, all_consumables)
    strays = sorted(
        i for i, spec in DATA.items["items"].items()
        if spec.get("kind") != "consumable" and "consume" in spec
    )
    assert not strays, f"consume 段只能长在 consumable 身上，这些不是：{strays}"
    assert not any(
        potion.is_potion(DATA, i)
        for i, spec in DATA.items["items"].items()
        if spec.get("kind") != "consumable"
    ), "非 consumable 的物品被算成药水了"


@case
def test_resupply_buys_a_bundle_sized_for_the_player():
    """战士 30 级：上限 136 → 半血 68 → 挑大药(100) ×10 = 7000 金；max_mp 0 不买蓝。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Wallet).gold = 10 ** 6
        got = resupply(player, DATA)
        assert got["items"] == [{"item": "hp_potion_l", "count": BUNDLE}], got
        assert got["paid"] == PRICE_HP_L * BUNDLE == 7000, got
        assert player.get(Bag).items["hp_potion_l"] == BUNDLE
    finally:
        session.shutdown()


@case
def test_resupply_clamps_to_the_weight_ceiling():
    """法师 30 级负重只有 49：红 10 瓶（20）+ 蓝 9 瓶（27）= 47，不能超。

    负重是**物理**上限，所以这里按余量夹瓶数（红优先）而不是整笔拒绝——
    否则法师一次也补不了货。
    """
    session = build_session(data=DATA, seed=8, map_id="biqi_field")
    try:
        player = spawn_player(session.world, DATA, class_id="mage")
        _at_level(player, "mage", 30)
        assert player.get(Body).max_weight == MAGE_30_WEIGHT
        player.get(Wallet).gold = 10 ** 6
        got = resupply(player, DATA)
        assert got["items"][0] == {"item": "hp_potion_m", "count": BUNDLE}, got
        assert got["items"][1] == {"item": "mp_potion_l", "count": 9}, got
        assert got["paid"] == PRICE_HP_M * BUNDLE + PRICE_MP_L * 9 == 10560, got
        carried = (W_HP_M * BUNDLE + W_MP_L * 9)
        assert carried <= MAGE_30_WEIGHT, carried
    finally:
        session.shutdown()


@case
def test_resupply_refuses_when_gold_is_short():
    """钱不够整笔拒绝——本模块从第一天起就不做"悄悄扣一半"。"""
    session, player = _session()
    try:
        _at_level(player, "warrior", 30)
        player.get(Wallet).gold = PRICE_HP_L * BUNDLE - 1
        try:
            resupply(player, DATA)
        except Exception as exc:  # noqa: BLE001 - TradeError
            assert getattr(exc, "code", None) == "no_gold", exc
        else:
            raise AssertionError("钱不够不该能补货")
        assert player.get(Bag).items.get("hp_potion_l", 0) == 0
    finally:
        session.shutdown()


@case
def test_every_monster_can_drop_potions():
    """23 个怪都有一组药水：只靠商店买，穷的时候会直接卡死推进节奏。

    补给组（药水 / 祝福油）一律追加在末尾，所以"有没有药水"要往末尾那几组里找，
    不能只看最后一组——P3-S5 起 BOSS 的末组是祝福油，药水在它前面。
    """
    for monster_id, table in DATA.drops["drops"].items():
        supply: list[set[str]] = []
        for group in reversed(table["groups"]):
            kinds = {DATA.items["items"][e["item"]]["kind"] for e in group["entries"]}
            if kinds <= {"consumable", "blessing"}:
                supply.append(kinds)
            else:
                break
        assert {"consumable"} in supply, (monster_id, supply)


@case
def test_the_potion_group_does_not_shift_the_equipment_streams():
    """补给组追加在末尾，既有组的随机流一个都没挪。

    ``roll_drops`` 的流名是 ``...:group{index}``，插到前面会把下标整体挪位，
    等于把已经沉淀下来的掉落结果全部洗牌——这条就是那次洗牌的报警器。
    这里砍掉的是**末组**（P3-S5 起就是祝福油组），砍掉它之后前 60 次掉落
    必须逐位不变，等于证明"新加的组没有洗掉任何既有掉落"。
    """
    table = DATA.drops["drops"]["shiwang"]
    trimmed = {"gold": table["gold"], "groups": table["groups"][:-1]}
    for index in range(60):
        full = roll_drops(table, seed=11, monster_id="shiwang", kill_index=index)
        old = roll_drops(trimmed, seed=11, monster_id="shiwang", kill_index=index)
        assert old.items == full.items[:len(old.items)], (index, old.items, full.items)
        assert old.gold == full.gold


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
    print(f"test_potion: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
