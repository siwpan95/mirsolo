"""面板：布局几何 + 命中测试 + 面板动作真的落到规则层。

面板最容易出的两类 bug 都是"看起来没问题"：

1. **行与数据错位**——滚动之后点了第 3 行，实际作用于第 1 屏的第 3 件物品；
2. **面板自己算一套价格/等级门槛**——和 ``shop``/``equipment`` 的判定分叉，
   玩家能点、点了报错，或者反过来点不动其实买得起。

所以这里既断言几何（行必须落在面板内、互不重叠），也断言"面板给出的
动作 → 规则函数 → 金币/背包的实际变化"，且期望值写手算字面量。
"""

from __future__ import annotations

from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Bag, Loadout, Position, Progression, Wallet, Warehouse
from mirsolo.data.loader import SLOTS, GameData
from mirsolo.economy.shop import buy, buy_price, sell_price
from mirsolo.items.equipment import equip
from mirsolo.presentation.panels import (
    BUILDERS,
    SLOT_NAMES,
    build_character_panel,
    build_panel,
    build_shop_panel,
    build_warehouse_panel,
    max_rows,
    shop_stock,
)

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

#: 货架上架的四种 kind + 上架件数（79 件里 71 件；矿石不在此列）
STOCK_KINDS = ("equip", "book", "consumable", "blessing")
STOCK_COUNT = 71
#: 全部物品件数（改表必须改这里）
ITEM_COUNT = 79
#: 矿石件数：五色 + 黑铁四档纯度
ORE_COUNT = 8


def case(fn):
    CASES.append(fn)
    return fn


def _session():
    session = build_session(data=DATA, seed=3)
    player = populate(session, with_spawns=False)["player"]
    return session, player


def _apply_row(player, row):
    """把面板行动作转成规则调用——与窗口层 ``_do_row`` 同一套映射。"""
    if row.action == "equip":
        return equip(player, DATA, row.item)
    if row.action == "unequip":
        from mirsolo.items.equipment import unequip

        return unequip(player, DATA, row.item)
    if row.action == "buy":
        return buy(player, DATA, row.item)
    if row.action == "sell":
        from mirsolo.economy.shop import sell

        return sell(player, DATA, row.item)
    if row.action == "deposit":
        from mirsolo.economy.shop import deposit

        return deposit(player, DATA, row.item)
    if row.action == "withdraw":
        from mirsolo.economy.shop import withdraw

        return withdraw(player, DATA, row.item)
    raise AssertionError(f"未处理的动作 {row.action}")


# ── 几何 ────────────────────────────────────────────

@case
def test_all_panels_stay_inside_viewport():
    session, player = _session()
    try:
        for width, height in ((1280, 720), (960, 540), (640, 360)):
            for key in ("character", "shop", "warehouse"):
                panel = build_panel(key, player=player, data=DATA,
                                    width=width, height=height)
                rect = panel.rect
                assert 0 <= rect.x and rect.right <= width, (key, width, rect)
                assert 0 <= rect.y and rect.top <= height, (key, height, rect)
                for row in panel.rows:
                    assert rect.x <= row.rect.x and row.rect.right <= rect.right, \
                        (key, width, row.text)
                    assert rect.y <= row.rect.y and row.rect.top <= rect.top, \
                        (key, height, row.text)
    finally:
        session.shutdown()


@case
def test_rows_do_not_overlap():
    """行互不重叠：否则命中测试会返回"上面那一行"，点哪都是错的。"""
    session, player = _session()
    try:
        player.get(Bag).items.update({"mujian": 1, "buyi": 1, "pihelmet": 1})
        for key in ("character", "shop", "warehouse"):
            panel = build_panel(key, player=player, data=DATA)
            rows = panel.rows
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i].rect, rows[j].rect
                    overlap = (a.x < b.right and b.x < a.right
                               and a.y < b.top and b.y < a.top)
                    assert not overlap, (key, rows[i].text, rows[j].text)
    finally:
        session.shutdown()


@case
def test_max_rows_matches_screen_capacity():
    """一屏 14 行（470 面板高 − 46 标题栏 − 46 页脚 − 24 边距 = 354，354 // 24 = 14）。

    页脚那 46 是给"金币/进度 + 操作提示"两行小字留的：不留的话整栏会铺到
    面板底边，最后一行正好压在统计文字上（图鉴 23 种怪铺满一栏时实测叠字）。
    这个数变了说明版式动了——刻意用字面量钉住，改版式必须显式改这里。
    """
    session, player = _session()
    try:
        panel = build_shop_panel(player=player, data=DATA)
        assert max_rows(panel.rect) == 14, max_rows(panel.rect)
    finally:
        session.shutdown()


# ── 角色面板 ────────────────────────────────────────

@case
def test_slot_names_match_loader_slots_exactly():
    """面板槽位必须与 ``loader.SLOTS`` 一一对应。

    以前两处各写一份（面板 8 个含腰带/靴子，loader 7 个含蜡烛），
    面板显示"腰带"但 loader 会拒绝任何 belt 装备。改成派生后由这条钉住。
    """
    assert set(SLOT_NAMES) == set(SLOTS), (sorted(SLOT_NAMES), sorted(SLOTS))
    assert len(SLOT_NAMES) == 7


@case
def test_character_panel_lists_every_slot_and_matches_loadout():
    session, player = _session()
    try:
        panel = build_character_panel(player=player, data=DATA)
        unequip_rows = [r for r in panel.rows if r.action == "unequip"]
        assert len(unequip_rows) == len(SLOT_NAMES) == 7
        assert all(not r.enabled for r in unequip_rows), "初始应为全空槽"
        # 动作负载是槽名（不是 item_id）——脱装备按槽定位
        assert {r.item for r in unequip_rows} == set(SLOT_NAMES)
        assert all("（空）" in r.text for r in unequip_rows)

        player.get(Bag).add("mujian", 1)
        equip(player, DATA, "mujian")
        panel = build_character_panel(player=player, data=DATA)
        weapon = next(r for r in panel.rows if r.item == "weapon")
        assert weapon.enabled and "木剑" in weapon.text, weapon.text
        # 穿上后从背包份的行里消失（背包里已无木剑），不会出现两行同物
        assert not [r for r in panel.rows if r.action == "equip" and r.item == "mujian"]
    finally:
        session.shutdown()


@case
def test_character_panel_hit_test_returns_the_clicked_row():
    session, player = _session()
    try:
        player.get(Bag).items.update({"mujian": 1, "buyi": 1})
        panel = build_character_panel(player=player, data=DATA)
        target = next(r for r in panel.rows if r.action == "equip" and r.item == "mujian")
        hit = panel.row_at(target.rect.x + target.rect.w / 2,
                           target.rect.y + target.rect.h / 2)
        assert hit is not None and hit.item == "mujian", hit
        # 点面板外、点标题区都不该命中
        assert panel.row_at(panel.rect.x + 2, panel.rect.top - 2) is None
        assert panel.row_at(5, 5) is None
    finally:
        session.shutdown()


@case
def test_character_panel_disables_wearable_it_cannot_use():
    """等级不够的装备要显灰并给出原因，而不是让玩家点了才报错。"""
    session, player = _session()
    try:
        player.get(Bag).add("zhanmadao", 1)  # 20 级斩马刀
        player.get(Progression).level = 1
        panel = build_character_panel(player=player, data=DATA)
        row = next(r for r in panel.rows if r.item == "zhanmadao")
        assert row.enabled is False
        assert "20" in row.detail, row.detail
    finally:
        session.shutdown()


# ── 商店面板 ────────────────────────────────────────

@case
def test_shop_stock_covers_all_equipment_and_books():
    """货架 = 全部装备 + 全部技能书 + 全部药水，且显示的是**买入价**。

    药水从 P3-S3 起上架（续航两条腿之一），祝福油从 P3-S5 起上架——它是
    掉落之外的第二条腿，不进商店就等于不刷 BOSS 的人一辈子摸不到幸运。
    不写死 22/16/9 这些数字：
    表里加了东西就该跟着变，写死只会把“新东西永远进不了货架”藏起来。
    """
    stock = shop_stock(DATA)
    kinds = [ITEMS[i]["kind"] for i in stock]
    # 数量写死（67 件物品里 59 件上架），改表必须改这几个数
    assert len(stock) == STOCK_COUNT, (len(stock), STOCK_COUNT)
    for kind in STOCK_KINDS:
        assert kinds.count(kind) == sum(1 for i in ITEMS.values() if i["kind"] == kind), kind
    # 矿石**不上架**：矿石是"只进不出"的资源，货架能卖就等于
    # 能用金币直接买到纯度 100 的黑铁矿石，#25 的升级成本当天崩盘
    assert "ore" not in kinds, sorted(set(kinds))
    prices = [buy_price(DATA, i) for i in stock]
    assert prices == sorted(prices), "货架应按价格升序，玩家一眼看到买得起的"

    session, player = _session()
    try:
        row = next(r for r in build_shop_panel(player=player, data=DATA).rows
                   if r.item == "mujian")
        # 木剑数据表价 120，回收价是 40——面板必须显示 120
        assert row.detail.startswith("120 金"), row.detail
        assert buy_price(DATA, "mujian") == 120
    finally:
        session.shutdown()


@case
def test_shop_panel_marks_unaffordable_rows():
    session, player = _session()
    try:
        player.get(Wallet).gold = 0
        panel = build_shop_panel(player=player, data=DATA)
        assert all(not r.enabled for r in panel.rows if r.action == "buy")
        assert all("钱不够" in r.detail for r in panel.rows if r.action == "buy")

        player.get(Wallet).gold = buy_price(DATA, "mujian")  # 正好 120
        panel = build_shop_panel(player=player, data=DATA)
        mujian = next(r for r in panel.rows if r.action == "buy" and r.item == "mujian")
        assert mujian.enabled, "钱刚好够就该能买"
        assert "钱不够" not in mujian.detail
    finally:
        session.shutdown()


@case
def test_shop_scroll_reaches_last_item_and_click_buys_that_item():
    """滚到底之后点第一行，买的必须是**末屏第一件**，不是货架第一件。"""
    session, player = _session()
    try:
        stock = shop_stock(DATA)
        player.get(Wallet).gold = 10**6
        limit = max_rows(build_shop_panel(player=player, data=DATA).rect)
        assert limit < len(stock), "商品数应多于一屏，否则这条测试没意义"
        last_page = build_shop_panel(player=player, data=DATA, scroll=len(stock))
        assert last_page.scroll_max == len(stock) - limit
        first_row = last_page.rows[0]
        assert first_row.item == stock[len(stock) - limit], first_row.item
        assert first_row.item != stock[0]

        hit = last_page.row_at(first_row.rect.x + 4, first_row.rect.y + 4)
        assert hit is not None and hit.item == first_row.item, hit
        player.get(Bag).items.clear()
        result = _apply_row(player, hit)
        assert result["item"] == first_row.item, result
        assert player.get(Bag).items.get(first_row.item) == 1
        assert player.get(Wallet).gold == 10**6 - buy_price(DATA, first_row.item)
    finally:
        session.shutdown()


@case
def test_shop_panel_sell_row_pays_recycle_price():
    """点一次卖**一件**（不是整叠）——误点一下卖掉 20 件药是灾难。"""
    session, player = _session()
    try:
        player.get(Bag).add("mujian", 2)
        player.get(Wallet).gold = 0
        panel = build_shop_panel(player=player, data=DATA)
        row = next(r for r in panel.rows if r.action == "sell" and r.item == "mujian")
        assert f"回收 {sell_price(DATA, 'mujian')} 金" in row.detail, row.detail
        assert sell_price(DATA, "mujian") == 40  # 120 // 3

        _apply_row(player, row)
        assert player.get(Wallet).gold == 40, player.get(Wallet).gold
        assert player.get(Bag).items.get("mujian") == 1, "只该卖出一件"

        # 再点一次才清空，钱累计到 80
        panel = build_shop_panel(player=player, data=DATA)
        _apply_row(player, next(r for r in panel.rows
                                if r.action == "sell" and r.item == "mujian"))
        assert player.get(Wallet).gold == 80, player.get(Wallet).gold
        assert "mujian" not in player.get(Bag).items
    finally:
        session.shutdown()


# ── 仓库面板 ────────────────────────────────────────

@case
def test_warehouse_panel_splits_bag_and_vault():
    session, player = _session()
    try:
        empty = build_warehouse_panel(player=player, data=DATA)
        assert empty.rows == [], "初始两边都空，不该有可点的行"

        player.get(Bag).items.update({"mujian": 2, "buyi": 1})
        panel = build_warehouse_panel(player=player, data=DATA)
        deposits = [r for r in panel.rows if r.action == "deposit"]
        assert {r.item for r in deposits} == {"mujian", "buyi"}

        _apply_row(player, next(r for r in deposits if r.item == "buyi"))
        assert player.get(Warehouse).items == {"buyi": 1}
        assert "buyi" not in player.get(Bag).items

        panel = build_warehouse_panel(player=player, data=DATA)
        withdraws = [r for r in panel.rows if r.action == "withdraw"]
        assert {r.item for r in withdraws} == {"buyi"}, withdraws
        _apply_row(player, withdraws[0])
        assert player.get(Warehouse).items == {}
        assert player.get(Bag).items.get("buyi") == 1
    finally:
        session.shutdown()


@case
def test_panel_texts_report_capacity_and_gold():
    session, player = _session()
    try:
        player.get(Wallet).gold = 777
        shop = " ".join(t.text for t in build_shop_panel(player=player, data=DATA).texts)
        assert "金币 777" in shop, shop
        player.get(Bag).items.update({"mujian": 2, "buyi": 1})
        vault = " ".join(t.text for t in build_warehouse_panel(player=player, data=DATA).texts)
        assert "背包 2/48" in vault, vault     # 2 种、容量 48
        assert "仓库 0/80" in vault, vault
    finally:
        session.shutdown()


@case
def test_unknown_panel_key_is_rejected():
    session, player = _session()
    try:
        try:
            # 用一个**永远不该成为面板名**的字符串：曾经这里写的是 "auction"，
            # 等拍卖行真的做出来之后，这条用例就变成"断言拍卖行是未知面板"了。
            build_panel("definitely_not_a_panel", player=player, data=DATA)
        except KeyError as exc:
            assert "definitely_not_a_panel" in str(exc), exc
        else:
            raise AssertionError("未知面板应报 KeyError")
    finally:
        session.shutdown()



@case
def test_right_edge_texts_are_right_anchored():
    """贴右边的文字必须显式 ``anchor_x="right"``：TextAnchor 默认左对齐，
    页脚「X 关闭」提示的 x 给的是右边界（rect.right - 8），左对齐会把
    整行甩到面板右边界之外——截图实测抓到过。"""
    session, player = _session()
    try:
        for key in BUILDERS:
            panel = build_panel(key, player=player, data=DATA)
            rect = panel.rect
            for text in panel.texts:
                if text.x < rect.x + rect.w * 0.6:
                    continue  # 左半边的文字本来就朝左，不管
                assert text.anchor_x == "right", (key, text.text, text.x)
    finally:
        session.shutdown()


# ── 40 档武器（三职业各一件） ─────────────────────
#
# 30 档到 48 档之间原本空 7 级（40~47）：L40 以后的经验占总量 92.4%，
# 这段空窗等于把整个后期交给 30 档武器。下面三条按"能换、能拿到、不越槽"
# 三个角度钉住它，改坏任意一件都会红。

#: 每个职业 40 档那一件
TIER40 = {"warrior": "caihong_mojian", "mage": "longwen_fazhang", "taoist": "wuji_jian"}
#: 这三件必须能从 40~47 级的两张图（石墓阵/封魔谷）的主刷怪身上掉出来
TIER40_SOURCES = ("heiming_zhanshi", "emo_gongjianshou")
#: 上一档（30）与下一档（48）的主属性，用来卡"40 档落在中间"
TIER30 = {"atk": [10, 20], "mc": [6, 11], "sc": [6, 11]}
TIER48 = {"atk": [22, 36], "mc": [12, 20], "sc": [12, 20]}


@case
def test_tier40_weapon_for_every_class():
    """三职业在 40 档各有一把本职业武器，且夹在 30 档与 48 档之间。

    这是"40 档空窗"的直接红证：把任意一件删掉或把 ``level_req`` 改到 48，
    对应职业 40 档就没有武器可用（上一档停在 30 级，落后 10 级）。
    """
    for class_id, item_id in TIER40.items():
        spec = ITEMS[item_id]
        assert spec["slot"] == "weapon", item_id
        assert spec["class"] == [class_id], (item_id, spec["class"])
        assert spec["level_req"] == 40, (item_id, spec["level_req"])
        # 主属性必须落在 30 档与 48 档之间：比 30 档强（上限更高），
        # 又不越过 48 档（下限不低于赤月、上限不高于赤月）
        key = {"warrior": "atk", "mage": "mc", "taoist": "sc"}[class_id]
        lo, hi = spec[key]
        prev, nxt = TIER30[key], TIER48[key]
        # 不许比上一档的**上限**更低，也不许越过下一档的**上限**
        assert hi > prev[1] and lo >= prev[0], (item_id, prev, spec[key])
        assert hi <= nxt[1], (item_id, spec[key], nxt)
        # 法道武器不带幸运（幸运只来自武器/项链，且有 luck-sources 门禁盯着）
        assert "luck" not in spec or not spec["luck"], item_id


@case
def test_tier40_weapons_droppable_in_40_47_band():
    """40 档三件都必须挂掉落，而且挂在 40~47 级那两张图的主刷怪身上。

    "数据表里有"不等于"打得出来"：掉落表漏挂一件，那件就是只活在文档里。
    同时钉住掉落组位置——新组只能插在补给组**之前**（``roll_drops`` 的流名
    带组下标，补给组必须留在连续尾部，否则既有掉落会被整片洗牌）。
    """
    maps = DATA.maps["maps"]
    band_monsters = set()
    for map_id in ("stone_tomb", "fengmo_valley"):
        lo, hi = maps[map_id]["level_range"]
        assert 40 <= lo and hi <= 47, (map_id, lo, hi)
        band_monsters |= {s["monster"] for s in maps[map_id]["spawns"]}
    for monster_id in TIER40_SOURCES:
        assert monster_id in band_monsters, (monster_id, sorted(band_monsters))
    # 怪本身也得落在这个等级带里，不然"挂上了也打不到"
    for monster_id in TIER40_SOURCES:
        assert 40 <= DATA.monsters["monsters"][monster_id]["level"] <= 47, monster_id

    for item_id in TIER40.values():
        sources = [mid for mid, table in DATA.drops["drops"].items()
                   if any(e.get("item") == item_id
                          for g in table.get("groups", []) for e in g.get("entries", []))]
        assert set(sources) >= set(TIER40_SOURCES), (item_id, sources)
        # 组位置：补给组（药水/祝福油）必须还在连续尾部
        for mid in sources:
            groups = DATA.drops["drops"][mid]["groups"]
            supply = 0
            for g in reversed(groups):
                kinds = {ITEMS[e["item"]]["kind"] for e in g["entries"]}
                if kinds <= {"consumable", "blessing"}:
                    supply += 1
                else:
                    break
            assert supply >= 1, (mid, "补给组不在连续尾部")
            assert all(ITEMS[g["entries"][0]["item"]]["kind"] != "consumable"
                       for g in groups[:len(groups) - supply]), mid


@case
def test_tier40_weapons_enter_trade_and_shop():
    """40 档武器要同时进商店货架和拍卖行（装备 100% 上架是本项目口径）。"""
    for item_id in TIER40.values():
        spec = ITEMS[item_id]
        assert spec["kind"] == "equip", item_id
        assert spec["auction"] is not None, f"{item_id} 不能寄卖"
        assert item_id in shop_stock(DATA), f"{item_id} 没进货架"


# ── 33~41 档防具（三职业各 4 件） ──────────────────
#
# ``armor`` 只有 buyi(1)/青铜(10)/中型(15)/赤月(48)，``helmet`` 只有皮(5)/
# 青铜(12)/牛魔(50)：15 级到 48 级之间**装备栏里没有任何防御可换**，战士
# L30~44 永远 AC 10、法道永远 AC 5，而 35+ 的怪单刀 26~40。下面按"能换、
# 拿得到、不抢赤月的饭碗"三个角度钉住这 12 件，改坏任意一件都会红。

#: 职业 → [(物品 id, 槽, 等级门槛)]，按档位从低到高排
#: ⚠️ 战士头盔是 **35** 不是 34——``data/items.json`` 的定稿值就是 35
#: （法/道头盔才是 34）。这里曾经写 34，导致
#: ``test_tier33_41_armor_for_every_class`` 红了一条。
ARMOR33_41 = {
    "warrior": [("tiekuijia", "armor", 33), ("tietoukui", "helmet", 35),
                ("jingtiekuijia", "armor", 40), ("jingtietoukui", "helmet", 41)],
    "mage": [("fashi_changpao", "armor", 33), ("fashi_toukui", "helmet", 34),
             ("monv_pao", "armor", 40), ("monv_toukui", "helmet", 41)],
    "taoist": [("daopao", "armor", 33), ("daoguan", "helmet", 34),
               ("tianzun_daopao", "armor", 40), ("tianzun_daoguan", "helmet", 41)],
}
#: 每一档「上一档 / 下一档」的 ac 上限，用来卡新甲必须**夹在中间**
#: （战士盔甲 6 → 8 → 10 → 13，头盔 4 → 5 → 6 → 8；法道同理）
AC_LADDER = {
    ("warrior", "armor"): (ITEMS["zhongxingkuijia"]["ac"][1], ITEMS["chiyue_zhanjia"]["ac"][1]),
    ("warrior", "helmet"): (ITEMS["qingtongtoukui"]["ac"][1], ITEMS["niumo_toukui"]["ac"][1]),
    ("mage", "armor"): (ITEMS["buyi"]["ac"][1], ITEMS["chiyue_zhanjia"]["ac"][1]),
    ("mage", "helmet"): (ITEMS["pihelmet"]["ac"][1], ITEMS["niumo_toukui"]["ac"][1]),
    ("taoist", "armor"): (ITEMS["buyi"]["ac"][1], ITEMS["chiyue_zhanjia"]["ac"][1]),
    ("taoist", "helmet"): (ITEMS["pihelmet"]["ac"][1], ITEMS["niumo_toukui"]["ac"][1]),
}
#: 按 level_req 分档 → 该档防具只能从这几张图的怪身上掉（逐档，不是总数检查）。
#: 沃玛寺庙 [33,40] 出 33/34/**35** 档；石墓阵 [40,44] + 封魔谷 [44,47] 连成 40~47 这一段，
#: 出 40/41 档。掉低级图里等于"15 级的图掉 40 级的甲"，掉高级图里等于越档。
ARMOR_BAND_MAPS = {
    33: ("woma_temple",),
    34: ("woma_temple",),
    35: ("woma_temple",),
    40: ("stone_tomb", "fengmo_valley"),
    41: ("stone_tomb", "fengmo_valley"),
}
#: ac 每 1 点 1.5k 金——新甲不按"感觉"定价，按防御收益定价，
#: 否则 40 档新甲（ac 7~10）会把 48 档赤月战甲（ac 9~13，30000 金）挤成废铁
GOLD_PER_AC = 1500


def _armor_sources(item_id):
    """该物品的所有掉落来源（怪 id 列表）。"""
    return [mid for mid, table in DATA.drops["drops"].items()
            if any(e.get("item") == item_id
                   for g in table.get("groups", []) for e in g.get("entries", []))]


def _band_of(level_req):
    """``(该档地图, 并集等级带)``——新甲只能从这些图的怪身上掉。

    沃玛寺庙 [33,40] / 石墓阵 [40,44] / 封魔谷 [44,47] 三张图首尾相接，
    所以"改坏一级"只要跨出档位表（34→35），来源怪就立刻不合规、这条就红。
    """
    assert level_req in ARMOR_BAND_MAPS, f"level_req {level_req} 不在 33~41 档位表内"
    map_ids = ARMOR_BAND_MAPS[level_req]
    monsters = set()
    lo, hi = 99, 0
    for map_id in map_ids:
        spec = DATA.maps["maps"][map_id]
        map_lo, map_hi = spec["level_range"]
        lo, hi = min(lo, map_lo), max(hi, map_hi)
        monsters |= {s["monster"] for s in spec["spawns"]}
    return monsters, lo, hi


@case
def test_tier33_41_armor_for_every_class():
    """三职业在 33/34/40/41 四档各有一件本职业盔甲与头盔，且 ac 夹在上下档之间。

    这是"15~48 级没有防御可换"的直接红证：缺任意一件，对应职业在该档
    的 AC 就停在上一档；把 ac 写到赤月之上，48 档就白做了。
    """
    for class_id, entries in ARMOR33_41.items():
        for item_id, slot, level_req in entries:
            assert item_id in ITEMS, f"items.json 里没有 {item_id}"
            spec = ITEMS[item_id]
            assert spec["kind"] == "equip", item_id
            assert spec["slot"] == slot, (item_id, spec["slot"])
            # 限职业：战士甲不给法师穿，否则"新甲 vs 赤月"的差价失去意义
            assert spec["class"] == [class_id], (item_id, spec["class"])
            assert spec["level_req"] == level_req, (item_id, spec["level_req"])
            lo, hi = spec["ac"]
            assert lo <= hi, (item_id, spec["ac"])
            prev_hi, next_hi = AC_LADDER[(class_id, slot)]
            # 同槽同职业内，等级越高 ac 上限越高；且不越过下一档
            ladder = [ITEMS[i]["ac"][1] for i, s, _ in entries if s == slot]
            assert ladder == sorted(ladder), (class_id, slot, ladder)
            assert ladder[0] > prev_hi, (item_id, ladder[0], prev_hi)
            assert ladder[-1] < next_hi, (item_id, ladder[-1], next_hi)


@case
def test_tier33_41_armor_drops_from_matching_level_band():
    """12 件都必须挂掉落，而且只挂在**该等级档那几张图**的怪身上。

    "数据表里有"不等于"打得出来"。这条按等级带逐档卡（不是总数检查）：
    把 ``tietoukui`` 的 ``level_req`` 从 35 改坏一级到 36，它就不在 33~41
    档位表里了，这条立刻红。
    """
    for class_id, entries in ARMOR33_41.items():
        for item_id, slot, level_req in entries:
            sources = _armor_sources(item_id)
            assert sources, f"{item_id} 没有掉落来源（只在数据表里活着）"
            assert len(sources) >= 2, f"{item_id} 只有一条掉落来源：{sources}"
            allowed, band_lo, band_hi = _band_of(level_req)
            illegal = sorted(set(sources) - allowed)
            assert not illegal, (item_id, level_req, illegal)
            # 掉它的怪必须落在这条档的等级带里：不许低档图掉高档甲，也不许越档
            for monster_id in sources:
                level = DATA.monsters["monsters"][monster_id]["level"]
                assert band_lo <= level <= band_hi, (item_id, monster_id, level)
                assert level >= level_req, (item_id, monster_id, level)
            # 新组只能插在补给组**之前**：``roll_drops`` 的流名带组下标，
            # 补给组必须留在连续尾部，否则既有掉落会被整片洗牌
            for mid in sources:
                groups = DATA.drops["drops"][mid]["groups"]
                supply = 0
                for g in reversed(groups):
                    kinds = {ITEMS[e["item"]]["kind"] for e in g["entries"]}
                    if kinds <= {"consumable", "blessing"}:
                        supply += 1
                    else:
                        break
                assert supply >= 1, (mid, "补给组不在连续尾部")


@case
def test_tier33_41_armor_price_follows_ac_not_vibes():
    """按 ac 每点 1.5k 金定价，且不得逼近赤月战甲（30000）。

    40 档新甲 ac 7~10 已经很接近赤月 ac 9~13：定价再高一点，赤月就变成
    "多花一倍钱买 3 点 ac"的笑话，48 档整档塌陷。
    """
    for class_id, entries in ARMOR33_41.items():
        for item_id, slot, _level in entries:
            assert item_id in ITEMS, f"items.json 里没有 {item_id}"
            spec = ITEMS[item_id]
            assert spec["price"] == spec["ac"][1] * GOLD_PER_AC, (item_id, spec["price"])
            assert spec["price"] > ITEMS["zhongxingkuijia"]["price"], item_id
            assert spec["price"] < ITEMS["chiyue_zhanjia"]["price"], item_id
            assert spec["auction"] is not None, f"{item_id} 不能寄卖"
            assert item_id in shop_stock(DATA), f"{item_id} 没进货架"
            assert spec.get("luck", 0) == 0, f"{item_id} 不该带幸运"


# ── 中后期怪物 dc / ac（P1 + P2） ────────────────────
#
# 依据 ``tmp_q1/BALANCE_REPORT.md`` §3.1：35+ 的怪单刀 26~40，玩家血量约
# 200（两刀半），沃玛段每样本死 141 次。下面把降完的数值逐条钉死。

#: P1：五只怪降完的 dc（顺序 [下限, 上限]）
P1_DC = {
    "woma_zhanshi": [12, 20],
    "woma_weishi": [14, 23],
    "woma_jiaozhu": [20, 33],
    "heiming_zhanshi": [17, 28],
    "emo_gongjianshou": [18, 31],
}
#: P2：只在 40+ 段动 ac（沃玛段的 ac 一个没碰，沃玛数字应当完全不变）
P2_AC = {"heiming_zhanshi": [7, 12], "emo_gongjianshou": [7, 13]}
#: 教主同步降血：1600 是同区怪（340~460）的 4 倍，打不掉又站不住
JIAOZHU_HP = 1300


@case
def test_p1_and_p2_are_applied_as_a_whole():
    """降 dc 与补甲必须**一起**落地——这是本批次最关键的一条护栏。

    REPORT §3.2 路 B 的实测是反直觉的：只补 12 件甲、怪的 dc 不动，战士
    只快 5%、法师 4%，**道士反而慢 1%**，死亡次数还全部上升（怪的 dc 区间
    太宽，AC 10→14 只抵掉上限的一点）。所以这里把"五只怪的 dc 到位"和
    "12 件甲到位"钉在同一条断言里：只做一半，这条就红，而不是默默上线。
    """
    monsters = DATA.monsters["monsters"]
    for monster_id, dc in P1_DC.items():
        assert list(monsters[monster_id]["dc"]) == dc, (monster_id, monsters[monster_id]["dc"], dc)
    for monster_id, ac in P2_AC.items():
        assert list(monsters[monster_id]["ac"]) == ac, (monster_id, monsters[monster_id]["ac"], ac)
    assert monsters["woma_jiaozhu"]["hp"] == JIAOZHU_HP, monsters["woma_jiaozhu"]["hp"]
    # 另一半：12 件甲必须同时在表里
    for class_id, entries in ARMOR33_41.items():
        for item_id, _slot, _level in entries:
            assert item_id in ITEMS, f"只降了 dc，没补甲：缺 {item_id}"
    # 沃玛段的 ac 是干净的归因对照：P2 不许碰它们
    untouched = {"woma_zhanshi": [6, 11], "woma_weishi": [7, 13], "woma_jiaozhu": [9, 17]}
    for monster_id, ac in untouched.items():
        assert list(monsters[monster_id]["ac"]) == ac, (monster_id, monsters[monster_id]["ac"])


@case
def test_woma_jiaozhu_drops_are_worth_the_fight():
    """教主降了数值（1600→1300、dc[26,44]→[20,33]），掉落必须同步补上。

    原来只掉 ``book_liehuo`` 12% + ``yinshen_jiezhi`` 3%：Boss 感被削弱
    之后如果掉落不变，"值得打"就没有替代品。补的是沃玛段的低档甲（33/34/
    35，同在沃玛寺庙 [33,40] 带内），不掉 40/41 档，免得越档。
    """
    jiaozhu = DATA.drops["drops"]["woma_jiaozhu"]
    dropped_items = {e["item"] for g in jiaozhu["groups"] for e in g["entries"]}
    for class_id in ARMOR33_41:
        # 低档 = 沃玛段（< 40）；战士头盔定稿在 35，所以这里不能用 34 当分界
        low_tier = [i for i, _s, lv in ARMOR33_41[class_id] if lv < 40]
        assert dropped_items >= set(low_tier), (class_id, sorted(set(low_tier) - dropped_items))
        high_tier = [i for i, _s, lv in ARMOR33_41[class_id] if lv >= 40]
        assert not dropped_items & set(high_tier), f"教主不该越档掉 {high_tier}"
    # 原有两件 Boss 掉落不许被挤掉（独立成组，不许改既有组的权重）
    assert "book_liehuo" in dropped_items and "yinshen_jiezhi" in dropped_items


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
    print(f"test_panels: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
