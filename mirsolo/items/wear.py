"""装备磨损与修理（P3-S1）—— 把数据表里躺了两批的 ``durability`` 真正用起来。

三条硬规则：

1. **磨损只由"真的命中"产生**：挨打才记，Miss 不记（不然躲得越好坏得越快，反直觉）；
2. **磨穿了这件装备的属性就不算数**，装备还在、也还能脱下来 —— 玩家看得见
   "我为什么变弱了"，而不是东西凭空消失；
3. **洗掉磨损只能花钱**：要么修理，要么卖掉再买一件新的。

为什么按 ``item_id`` 而不是按"第几件实物"记：背包是堆叠的（``item_id -> 数量``），
没有实物实例 id，而按 id 记刚好让"打废了换一把同 id 的"洗不掉磨损——
详见 :class:`mirsolo.components.Wear` 的说明。

口径纪律（踩过 10 倍差的坑）：**概率用万分比**（``WEAR_BP_PER_HIT``），
**倍率用千分比**（``REPAIR_PERMILLE``）。

已知差异（原版对照）：原版是**每件实物**独立耐久、且武器持久见底会"消失/破碎"；
本作按 item_id 记账且不销毁装备（单机丢装备太惩罚，而且会与"读档重打"打架）。
见 docs/数据口径.md。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Loadout, Wear
from mirsolo.data.loader import GameData

#: 每次**命中**给装备 +1 点磨损的概率（万分比）。300 = 3%，
#: 配合 durability 8-30，一件装备大致撑 250-1000 次命中——够打一两张图，
#: 又不至于让人整局都不用管。
WEAR_BP_PER_HIT = 300

#: 修满一件装备的花费 = 买入价 × 该千分比 × 已磨损比例。
#: 300 = "修满一件顶全新价三成"，必须**明显低于买入价**，
#: 否则"卖掉再买一件"永远更划算、修理按钮形同虚设（由 verify.py 门禁钉住）。
REPAIR_PERMILLE = 300

#: 数据表缺 durability 时的兜底（正常不会发生，loader 已校验该字段）
DURABILITY_DEFAULT = 10

#: 出手时不参与磨损的槽：武器只在**出手**时磨，挨打磨的是防具。
#: 这样"挥刀把武器砍钝"和"挨揍把盔甲砸烂"是两条独立的账，符合直觉。
ARMOR_ONLY_EXCLUDE = ("weapon",)

EVT_BROKE = "wear:broke"


class WearError(ValueError):
    """磨损/修理被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, item_id: str) -> dict:
    spec = data.items["items"].get(item_id)
    if spec is None:
        raise WearError("unknown_item", f"未知物品 {item_id!r}")
    return spec


def durability_of(data: GameData, item_id: str) -> int:
    """该装备的耐久点数（磨损到这个数就"磨穿"）。"""
    return max(1, int(_spec(data, item_id).get("durability", DURABILITY_DEFAULT)))


def points_of(wear: Wear | None, item_id: str) -> int:
    """已磨损点数（没有账本 = 全新）。"""
    if wear is None:
        return 0
    return int(wear.points.get(item_id, 0))


def left_permille(data: GameData, wear: Wear | None, item_id: str) -> int:
    """剩余耐久（千分比）：1000 = 全新，0 = 已磨穿。"""
    total = durability_of(data, item_id)
    left = total - min(total, points_of(wear, item_id))
    return int(left * 1000 // total)


def is_broken(data: GameData, wear: Wear | None, item_id: str) -> bool:
    """这件装备是不是已经磨穿（**穿在身上也不给属性**）。"""
    return points_of(wear, item_id) >= durability_of(data, item_id)


def add_wear(data: GameData, wear: Wear | None, item_id: str, amount: int = 1) -> int:
    """累加磨损点数，夹在 [0, durability]；返回实际增加的点数。"""
    if wear is None or amount <= 0:
        return 0
    total = durability_of(data, item_id)
    before = int(wear.points.get(item_id, 0))
    after = min(total, before + int(amount))
    if after == before:
        return 0
    wear.points[item_id] = after
    return after - before


def reset(wear: Wear | None, item_id: str) -> None:
    """清零一件装备的磨损（**新入手**或**修理完成**时调用）。"""
    if wear is not None:
        wear.points.pop(item_id, None)


def roll_strike(data: GameData, wear: Wear | None, item_id: str, rng) -> bool:
    """按概率给一件装备 +1 点磨损，返回是否真的磨到了。

    ``rng`` 由调用方给（流名带自身标识 + tick），本模块不自己造流——
    造流的位置必须和别的效果错开，否则戴上戒指就挪动了主伤害流。
    """
    if rng.rand_int(1, 10000) > WEAR_BP_PER_HIT:
        return False
    return add_wear(data, wear, item_id, 1) > 0


def armor_slots(loadout: Loadout | None) -> list[str]:
    """当前穿着的防具槽（排好序：随机挑选时顺序必须稳定）。"""
    if loadout is None:
        return []
    slots = [slot for slot in loadout.slots
             if slot not in ARMOR_ONLY_EXCLUDE and loadout.slots[slot]]
    return sorted(slots)


def pick_armor_slot(loadout: Loadout | None, rng) -> str | None:
    """挨打时随机挑一件防具来磨（没穿防具就返回 ``None``）。"""
    slots = armor_slots(loadout)
    if not slots:
        return None
    return slots[rng.rand_int(0, len(slots) - 1)]


def repair_cost(data: GameData, wear: Wear | None, item_id: str) -> int:
    """修满这件装备要多少钱；全新（无磨损）返回 0。

    ``买入价 × REPAIR_PERMILLE × 已磨损比例``，向上取整、至少 1 金
    （磨了一点也要收钱，否则"每次磨 1 点就修一次"能白嫖）。
    """
    total = durability_of(data, item_id)
    points = min(total, points_of(wear, item_id))
    if points <= 0:
        return 0
    price = max(1, int(_spec(data, item_id).get("price", 1)))
    num = price * REPAIR_PERMILLE * points
    den = 1000 * total
    return max(1, (num + den - 1) // den)


def repair_rows(data: GameData, entity: Entity) -> list[dict]:
    """可修清单（面板数据源）：``已穿 + 背包 + 仓库``里所有**有磨损**的装备。

    按 item_id 排序（面板排序一律显式，字典序会在数据表改动后悄悄错位）。
    """
    from mirsolo.components import Bag, Warehouse  # 延迟导入：同上

    wear = entity.get(Wear)
    ids: set[str] = set()
    loadout = entity.get(Loadout)
    if loadout is not None:
        ids.update(loadout.slots.values())
    bag = entity.get(Bag)
    if bag is not None:
        ids.update(bag.items)
    stash = entity.get(Warehouse)
    if stash is not None:
        ids.update(stash.items)

    rows = []
    for item_id in sorted(ids):
        spec = data.items["items"].get(item_id)
        if spec is None or spec.get("kind", "equip") != "equip":
            continue
        if points_of(wear, item_id) <= 0:
            continue
        rows.append({
            "item": item_id,
            "name": str(spec.get("name", item_id)),
            "left_permille": left_permille(data, wear, item_id),
            "broken": is_broken(data, wear, item_id),
            "cost": repair_cost(data, wear, item_id),
        })
    return rows


def total_repair_cost(data: GameData, entity: Entity) -> int:
    """全部修好要多少钱（面板底部"修理全部"那一行显示的就是它）。"""
    return sum(row["cost"] for row in repair_rows(data, entity))
