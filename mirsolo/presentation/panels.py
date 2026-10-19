"""面板布局 —— 角色/商店/仓库三个面板的**纯几何**计算，零 GL。

和 :mod:`mirsolo.presentation.hud` 同样的思路：把"哪一行该画在哪、点下去
意味着什么"算成数据，渲染层照着画、输入层照着命中测试。于是"点了背包第三行
是不是第 3 件物品""小窗口下会不会出屏""钱不够要不要变灰"全都能断言，
而不是靠人肉看截图。

**这里不写经济/装备规则**。面板只产出"动作 + 物品 id"，真正的
``equip`` / ``buy`` / ``sell`` / ``deposit`` / ``withdraw`` 由窗口层转手交给
``mirsolo.items.equipment`` 与 ``mirsolo.economy.shop`` —— 同一套规则，
headless 测试与鼠标点击不可能分叉。

排序一律显式（装备槽按槽名、背包/仓库按 item_id、商品按 (价, id)）：
字典序在 Python 里稳定，但"依赖插入顺序"会在某个数据表改动后悄悄错位。

**滚动**：一屏放不下的行不截断丢弃，而是记 ``scroll`` 偏移 + 在列的上下端
标出"还有几种"。商店有 24 种货（8 装备 + 16 技能书），一屏只有 17 行；
直接截断会让技能书永远买不到，而且玩家看不出少的是哪些。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from allin_engine import Entity

from mirsolo.combat.formulas import LUCK_CAP
from mirsolo.components import (
    Attributes,
    Bag,
    Loadout,
    Progression,
    Vitals,
    Wallet,
    Warehouse,
)
from mirsolo.data.loader import SLOTS, GameData
from mirsolo.economy.shop import buy_price, sell_price
from mirsolo.presentation.hud import Rect, TextAnchor

#: 面板尺寸与行高（行高决定"一个屏能显示多少行"，超出部分靠 scroll 翻）
PANEL_WIDTH = 560.0
PANEL_HEIGHT = 470.0
PANEL_MARGIN = 24.0
#: 顶部留白 = 标题行 + 栏头行（两行各占一半，行从这之下开始铺）
TITLE_HEIGHT = 46.0
ROW_HEIGHT = 22.0
ROW_GAP = 2.0
COLUMN_GAP = 12.0

#: 槽位的中文名（原版叫法，装备面板直接显示）
#: 槽位 → 中文名。**键必须覆盖 ``loader.SLOTS`` 全部、且不多不少**——
#: 以前这里是独立写的 8 个（腰带/靴子），而 loader 认的是 7 个（含蜡烛），
#: 结果面板显示"腰带"但 loader 会拒绝任何 belt 装备（双份事实来源，踩过）。
#: 现在由 ``SLOTS`` 派生，多一个/少一个都会 KeyError，由门禁
#: ``slot-names-match`` 钉住。
SLOT_LABELS: dict[str, str] = {
    "weapon": "武器",
    "armor": "衣服",
    "helmet": "头盔",
    "necklace": "项链",
    "bracelet": "手镯",
    "ring": "戒指",
    "candle": "蜡烛",
}

SLOT_NAMES: dict[str, str] = {slot: SLOT_LABELS[slot] for slot in SLOTS}


@dataclass(frozen=True)
class Row:
    """面板里一行可点的条目。

    ``action`` 是稳定的机器可读动作名（``equip``/``unequip``/``buy``/
    ``sell``/``deposit``/``withdraw``），``item`` 是动作对象。
    ``enabled=False`` 表示"看得见但点不动"（钱不够、槽是空的、等级不够），
    仍然画出来——玩家需要看到自己差多少钱，而不是整行消失。
    """

    rect: Rect
    text: str
    action: str
    item: str = ""
    detail: str = ""
    enabled: bool = True


@dataclass
class Panel:
    key: str
    title: str
    rect: Rect
    rows: list[Row] = field(default_factory=list)
    texts: list[TextAnchor] = field(default_factory=list)
    sections: list[TextAnchor] = field(default_factory=list)
    scroll: int = 0
    scroll_max: int = 0
    """本面板可滚动的最大偏移（由两栏里较长的一栏决定，窗口层钳制用）。"""

    def row_at(self, x: float, y: float) -> Row | None:
        """命中测试：返回落在 (x, y) 上的行（先命中先返回，行不重叠）。"""
        for row in self.rows:
            if row.rect.contains(x, y):
                return row
        return None


def _panel_rect(width: int, height: int) -> Rect:
    """面板居中并夹进视口（小窗口时缩到视口内，绝不出屏）。"""
    w = min(PANEL_WIDTH, max(160.0, width - 2 * PANEL_MARGIN))
    h = min(PANEL_HEIGHT, max(120.0, height - 2 * PANEL_MARGIN))
    return Rect((width - w) / 2.0, (height - h) / 2.0, w, h)


def _columns(panel: Rect) -> tuple[Rect, Rect]:
    """左右两栏（左：来源/装备，右：背包/仓库）。"""
    col_w = (panel.w - 2 * COLUMN_GAP) / 2.0
    left = Rect(panel.x + COLUMN_GAP, panel.y, col_w, panel.h)
    right = Rect(panel.x + COLUMN_GAP * 2 + col_w, panel.y, col_w, panel.h)
    return left, right


def max_rows(panel_rect: Rect) -> int:
    """一栏最多显示几行（窗口层算滚动步长也要用同一口径）。"""
    usable = panel_rect.h - TITLE_HEIGHT - 2 * COLUMN_GAP
    return max(1, int(usable // (ROW_HEIGHT + ROW_GAP)))


def _fill_rows(panel: Rect, column: Rect, entries: list[tuple[str, str, str, str, bool]],
               *, scroll: int = 0) -> tuple[list[Row], int, list[TextAnchor]]:
    """把 ``(action, item, text, detail, enabled)`` 铺成行。

    返回 ``(行, 本栏 scroll_max, 上下截断提示)``。
    """
    limit = max_rows(panel)
    scroll_max = max(0, len(entries) - limit)
    start = max(0, min(scroll, scroll_max))
    visible = entries[start:start + limit]

    rows: list[Row] = []
    y = column.y + column.h - TITLE_HEIGHT - ROW_HEIGHT
    for action, item, text, detail, enabled in visible:
        rows.append(Row(
            rect=Rect(column.x, y, column.w, ROW_HEIGHT),
            text=text, action=action, item=item, detail=detail, enabled=enabled,
        ))
        y -= ROW_HEIGHT + ROW_GAP

    notes: list[TextAnchor] = []
    # 滚动提示与栏头同一行、靠右对齐——不占行位，也不会和第一行叠
    if start > 0:
        notes.append(TextAnchor(f"↑ 还有 {start} 种", column.right - 4,
                                column.y + column.h - TITLE_HEIGHT + 6, 10,
                                (0.72, 0.72, 0.76, 1.0), anchor_x="right"))
    below = len(entries) - start - len(visible)
    if below > 0:
        notes.append(TextAnchor(f"↓ 还有 {below} 种（滚轮翻）", column.right - 4,
                                column.y + column.h - TITLE_HEIGHT + 6, 10,
                                (0.72, 0.72, 0.76, 1.0), anchor_x="right"))
    return rows, scroll_max, notes


def _item_label(data: GameData, item_id: str) -> str:
    return str(data.items["items"].get(item_id, {}).get("name", item_id))


def _stat_hint(spec: dict) -> str:
    """装备的一行摘要（右侧详情列）。没有加成的装备显示"无加成"。"""
    parts = []
    for key, label in (("atk", "攻"), ("mc", "魔"), ("sc", "道"),
                       ("ac", "防"), ("mac", "魔御")):
        value = spec.get(key)
        if value:
            parts.append(f"{label} {value[0]}-{value[1]}")
    if spec.get("accuracy"):
        parts.append(f"准确 +{spec['accuracy']}")
    if spec.get("luck"):
        # 幸运单独列出而不并入"无加成"：它是 P2-S3 的核心机制，
        # 看不见的话玩家很难理解"为什么同样的刀有时刀刀上限"
        parts.append(f"幸运 +{spec['luck']}")
    return "　".join(parts) or "无加成"


def build_character_panel(*, player: Entity, data: GameData,
                          width: int = 1280, height: int = 720,
                          scroll: int = 0) -> Panel:
    """角色面板：左 = 装备槽（点一下脱下），右 = 背包（点一下穿上）。

    属性行用的是**重算后的当前值**（``Attributes`` 已被 ``recompute`` 写过），
    不是数据表里的裸身值——玩家看到的必须就是打怪用的那个数。
    """
    rect = _panel_rect(width, height)
    left, right = _columns(rect)
    panel = Panel(key="character", title="角色", rect=rect, scroll=scroll)

    loadout = player.get(Loadout)
    bag = player.get(Bag)
    prog = player.get(Progression)
    wallet = player.get(Wallet)
    vitals = player.get(Vitals)
    attr = player.get(Attributes)

    slots = dict(loadout.slots) if loadout is not None else {}
    entries: list[tuple[str, str, str, str, bool]] = []
    for slot in sorted(SLOT_NAMES):
        item_id = slots.get(slot)
        name = _item_label(data, item_id) if item_id else "（空）"
        # 动作负载是**槽名**而不是 item_id：脱装备按槽定位，
        # 按 item_id 脱会把"同款两件穿在不同槽"搞混
        hint = _stat_hint(data.items["items"][item_id]) if item_id else "空槽"
        entries.append(("unequip", slot, f"{SLOT_NAMES[slot]}：{name}",
                        hint, bool(item_id)))
    gear_rows, gear_max, gear_notes = _fill_rows(rect, left, entries, scroll=scroll)
    panel.rows.extend(gear_rows)
    panel.sections.append(TextAnchor("装备（点击脱下）", left.x + 4, left.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))

    # 背包：只有装备能"点着穿上"，其他物品仍然列出（看得见才不迷惑）
    bag_entries: list[tuple[str, str, str, str, bool]] = []
    items = dict(bag.items) if bag is not None else {}
    for item_id in sorted(items):
        spec = data.items["items"].get(item_id, {})
        is_equip = spec.get("kind", "equip") == "equip"
        able, hint = True, f"×{items[item_id]}"
        if is_equip:
            if spec.get("class") and prog is not None and prog.class_id not in spec["class"]:
                able, hint = False, "职业不符"
            elif prog is not None and prog.level < int(spec.get("level_req", 1)):
                able, hint = False, f"需 {spec['level_req']} 级"
        bag_entries.append(("equip" if is_equip else "none", item_id,
                            _item_label(data, item_id), hint, able))
    bag_rows, bag_max, bag_notes = _fill_rows(rect, right, bag_entries, scroll=scroll)
    panel.rows.extend(bag_rows)
    panel.sections.append(TextAnchor("背包（点击穿戴）", right.x + 4, right.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))
    panel.scroll_max = max(gear_max, bag_max)

    if attr is not None:
        lines = [
            f"等级 {prog.level}　金币 {wallet.gold if wallet else 0}",
            f"攻 {attr.dc[0]}-{attr.dc[1]}　魔 {attr.mc[0]}-{attr.mc[1]}　道 {attr.sc[0]}-{attr.sc[1]}",
            f"防 {attr.ac[0]}-{attr.ac[1]}　魔御 {attr.mac[0]}-{attr.mac[1]}",
            f"准确 {attr.accuracy}　敏捷 {attr.agility}　幸运 {attr.luck}"
            + ("（刀刀上限）" if attr.luck >= LUCK_CAP else ""),
        ]
        if vitals is not None:
            lines.append(f"气血 {vitals.hp}/{vitals.max_hp}　魔法 {vitals.mp}/{vitals.max_mp}")
        for index, line in enumerate(reversed(lines)):
            panel.texts.append(TextAnchor(line, left.x + 4, left.y + 70 + index * 15,
                                          10, (0.85, 0.87, 0.90, 1.0)))
    panel.texts.extend(gear_notes)
    panel.texts.extend(bag_notes)
    panel.texts.append(TextAnchor("C 关闭　点击行操作　滚轮翻页",
                                  rect.x + rect.w - 8, rect.y + 8, 10,
                                  (0.6, 0.62, 0.66, 1.0)))
    return panel


def shop_stock(data: GameData) -> list[str]:
    """商店货架：``items.json`` 里的装备与技能书，按 (价, id) 排。

    单机版**随身商铺**（不占城镇位置）是刻意简化，记在 ``docs/数据口径.md``；
    价格仍走 ``shop.buy_price``，与卖出价共用一份表，不可能出现两套口径。
    """
    ids = [item_id for item_id, spec in data.items["items"].items()
           if spec.get("kind", "equip") in ("equip", "book")]
    return sorted(ids, key=lambda item_id: (buy_price(data, item_id), item_id))


def build_shop_panel(*, player: Entity, data: GameData,
                     width: int = 1280, height: int = 720,
                     scroll: int = 0) -> Panel:
    """商店面板：左 = 货架（点一下买入），右 = 背包（点一下卖出）。"""
    rect = _panel_rect(width, height)
    left, right = _columns(rect)
    panel = Panel(key="shop", title="商店", rect=rect, scroll=scroll)
    wallet = player.get(Wallet)
    gold = int(wallet.gold) if wallet is not None else 0
    prog = player.get(Progression)

    entries: list[tuple[str, str, str, str, bool]] = []
    for item_id in shop_stock(data):
        spec = data.items["items"][item_id]
        price = buy_price(data, item_id)
        hint = f"{price} 金"
        able = gold >= price
        if not able:
            hint += "（钱不够）"
        elif prog is not None and prog.level < int(spec.get("level_req", 1)):
            hint += f"（需 {spec['level_req']} 级）"
        entries.append(("buy", item_id, _item_label(data, item_id), hint, able))
    shop_rows, shop_max, shop_notes = _fill_rows(rect, left, entries, scroll=scroll)
    panel.rows.extend(shop_rows)
    panel.sections.append(TextAnchor("货架（点击买入）", left.x + 4, left.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))

    bag_entries: list[tuple[str, str, str, str, bool]] = []
    bag = player.get(Bag)
    items = dict(bag.items) if bag is not None else {}
    for item_id in sorted(items):
        bag_entries.append(("sell", item_id, _item_label(data, item_id),
                            f"回收 {sell_price(data, item_id)} 金 ×{items[item_id]}", True))
    sell_rows, sell_max, sell_notes = _fill_rows(rect, right, bag_entries, scroll=scroll)
    panel.rows.extend(sell_rows)
    panel.sections.append(TextAnchor("背包（点击卖出）", right.x + 4, right.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))
    panel.scroll_max = max(shop_max, sell_max)

    panel.texts.append(TextAnchor(f"金币 {gold}", left.x + 4, left.y + 24, 11,
                                  (0.95, 0.86, 0.55, 1.0)))
    panel.texts.extend(shop_notes)
    panel.texts.extend(sell_notes)
    panel.texts.append(TextAnchor("B 关闭　滚轮翻页", rect.x + rect.w - 8, rect.y + 8,
                                  10, (0.6, 0.62, 0.66, 1.0)))
    return panel


def build_warehouse_panel(*, player: Entity, data: GameData,
                          width: int = 1280, height: int = 720,
                          scroll: int = 0) -> Panel:
    """仓库面板：左 = 背包（点一下存入），右 = 仓库（点一下取出）。"""
    rect = _panel_rect(width, height)
    left, right = _columns(rect)
    panel = Panel(key="warehouse", title="仓库", rect=rect, scroll=scroll)

    bag = player.get(Bag)
    stash = player.get(Warehouse)

    bag_entries: list[tuple[str, str, str, str, bool]] = []
    items = dict(bag.items) if bag is not None else {}
    for item_id in sorted(items):
        bag_entries.append(("deposit", item_id, _item_label(data, item_id),
                            f"×{items[item_id]}", True))
    dep_rows, dep_max, dep_notes = _fill_rows(rect, left, bag_entries, scroll=scroll)
    panel.rows.extend(dep_rows)
    panel.sections.append(TextAnchor("背包（点击存入）", left.x + 4, left.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))

    stash_entries: list[tuple[str, str, str, str, bool]] = []
    stored = dict(stash.items) if stash is not None else {}
    for item_id in sorted(stored):
        stash_entries.append(("withdraw", item_id, _item_label(data, item_id),
                              f"×{stored[item_id]}", True))
    with_rows, with_max, with_notes = _fill_rows(rect, right, stash_entries, scroll=scroll)
    panel.rows.extend(with_rows)
    panel.sections.append(TextAnchor("仓库（点击取出）", right.x + 4, right.top - 40, 13,
                                     (0.95, 0.86, 0.55, 1.0)))
    panel.scroll_max = max(dep_max, with_max)

    bag_used = bag.used if bag is not None else 0
    bag_cap = bag.capacity if bag is not None else 0
    stash_used = stash.used if stash is not None else 0
    stash_cap = stash.capacity if stash is not None else 0
    panel.texts.append(TextAnchor(f"背包 {bag_used}/{bag_cap}", left.x + 4, left.y + 24, 11,
                                  (0.85, 0.87, 0.90, 1.0)))
    panel.texts.append(TextAnchor(f"仓库 {stash_used}/{stash_cap}", right.x + 4, right.y + 24,
                                  11, (0.85, 0.87, 0.90, 1.0)))
    panel.texts.extend(dep_notes)
    panel.texts.extend(with_notes)
    panel.texts.append(TextAnchor("V 关闭　滚轮翻页", rect.x + rect.w - 8, rect.y + 8,
                                  10, (0.6, 0.62, 0.66, 1.0)))
    return panel


BUILDERS = {
    "character": build_character_panel,
    "shop": build_shop_panel,
    "warehouse": build_warehouse_panel,
}


def build_panel(key: str, *, player: Entity, data: GameData,
                width: int = 1280, height: int = 720, scroll: int = 0) -> Panel:
    """按名字造面板（窗口层只认名字，不认构造函数）。"""
    builder = BUILDERS.get(key)
    if builder is None:
        raise KeyError(f"未知面板 {key!r}")
    return builder(player=player, data=data, width=width, height=height, scroll=scroll)
