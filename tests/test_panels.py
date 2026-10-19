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

#: 货架上架的三种 kind + 上架件数（56 件里 48 件；矿石不在此列）
STOCK_KINDS = ("equip", "book", "consumable")
STOCK_COUNT = 48
#: 全部物品件数（改表必须改这里）
ITEM_COUNT = 56
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

    药水从 P3-S3 起上架（续航两条腿之一）。不写死 22/16/9 这些数字：
    表里加了东西就该跟着变，写死只会把“新东西永远进不了货架”藏起来。
    """
    stock = shop_stock(DATA)
    kinds = [ITEMS[i]["kind"] for i in stock]
    # 数量写死（56 件物品里 48 件上架），改表必须改这三个数
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
