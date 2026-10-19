"""经济：商店买卖 + 仓库存取（P1 逻辑层，P2 再接 NPC 面板）。

三条硬规则，全部有拒绝码，绝不做"悄悄扣一半"这种模糊处理：

1. **钱要够**：买入前逐项核对金币，扣款与入包在同一次调用里完成；
2. **负重/容量要够**：负重 = 已穿装备 + 背包内物品；背包满/超重直接拒；
3. **卖出价远低于买入价**（原版回收价约 1/3），防止"低买高卖"刷钱。

仓库与背包是两套容量，存取都不允许"溢出丢弃"。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Bag, Body, Loadout, Warehouse, Wallet
from mirsolo.data.loader import GameData

#: 回收价 = 买入价 // 该系数（原版卖掉装备大约只值三分之一）
SELL_DIVISOR = 3


class TradeError(ValueError):
    """买卖/存取被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, item_id: str) -> dict:
    spec = data.items["items"].get(item_id)
    if spec is None:
        raise TradeError("unknown_item", f"未知物品 {item_id!r}")
    return spec


def buy_price(data: GameData, item_id: str) -> int:
    """买入价（商店挂牌价）。"""
    return max(1, int(_spec(data, item_id).get("price", 1)))


def sell_price(data: GameData, item_id: str) -> int:
    """回收价。至少 1 金币，避免"卖东西一分钱不给"的挫败感。"""
    return max(1, buy_price(data, item_id) // SELL_DIVISOR)


def carried_weight(entity: Entity, data: GameData) -> int:
    """负重 = 已穿装备 + 背包内物品（原版背包也占负重）。"""
    total = 0
    loadout = entity.get(Loadout)
    if loadout is not None:
        for item_id in loadout.slots.values():
            total += int(data.items["items"].get(item_id, {}).get("weight", 0))
    bag = entity.get(Bag)
    if bag is not None:
        for item_id, count in bag.items.items():
            total += int(data.items["items"].get(item_id, {}).get("weight", 0)) * int(count)
    return total


def buy(entity: Entity, data: GameData, item_id: str, count: int = 1) -> dict:
    """买入：校验金币 → 容量 → 负重，然后扣钱入包。"""
    if count <= 0:
        raise TradeError("bad_count", "数量必须为正")
    spec = _spec(data, item_id)
    wallet = entity.get(Wallet)
    bag = entity.get(Bag)
    body = entity.get(Body)
    if wallet is None or bag is None:
        raise TradeError("missing_components", "缺少 Wallet/Bag 组件")

    cost = buy_price(data, item_id) * count
    if wallet.gold < cost:
        raise TradeError("no_gold", f"金币不足：需要 {cost}，身上 {wallet.gold}")
    if bag.full and item_id not in bag.items:
        raise TradeError("bag_full", "背包已满")
    if body is not None:
        incoming = int(spec.get("weight", 0)) * count
        after = carried_weight(entity, data) + incoming
        if after > int(body.max_weight):
            raise TradeError("overweight", f"负重不足：{after} > {body.max_weight}")

    wallet.gold -= cost
    bag.add(item_id, count)
    return {"item": item_id, "count": count, "paid": cost, "gold": wallet.gold}


def sell(entity: Entity, data: GameData, item_id: str, count: int = 1) -> dict:
    """卖出：校验持有量，然后扣物品加金币。"""
    if count <= 0:
        raise TradeError("bad_count", "数量必须为正")
    spec = _spec(data, item_id)
    wallet = entity.get(Wallet)
    bag = entity.get(Bag)
    if wallet is None or bag is None:
        raise TradeError("missing_components", "缺少 Wallet/Bag 组件")
    have = bag.items.get(item_id, 0)
    if have < count:
        raise TradeError("not_enough", f"背包里 {spec.get('name', item_id)} 只有 {have} 件")
    income = sell_price(data, item_id) * count
    bag.remove(item_id, count)
    wallet.gold += income
    return {"item": item_id, "count": count, "earned": income, "gold": wallet.gold}


def deposit(entity: Entity, data: GameData, item_id: str, count: int = 1) -> dict:
    """存仓：背包 → 仓库。仓库满则整笔拒绝（不部分存入）。"""
    if count <= 0:
        raise TradeError("bad_count", "数量必须为正")
    spec = _spec(data, item_id)
    bag = entity.get(Bag)
    stash = entity.get(Warehouse)
    if bag is None or stash is None:
        raise TradeError("missing_components", "缺少 Bag/Warehouse 组件")
    have = bag.items.get(item_id, 0)
    if have < count:
        raise TradeError("not_enough", f"背包里 {spec.get('name', item_id)} 只有 {have} 件")
    if stash.full and item_id not in stash.items:
        raise TradeError("stash_full", "仓库已满")
    bag.remove(item_id, count)
    stash.items[item_id] = stash.items.get(item_id, 0) + count
    return {"item": item_id, "count": count, "stash_used": stash.used}


def withdraw(entity: Entity, data: GameData, item_id: str, count: int = 1) -> dict:
    """取仓：仓库 → 背包。背包满则整笔拒绝。"""
    if count <= 0:
        raise TradeError("bad_count", "数量必须为正")
    spec = _spec(data, item_id)
    bag = entity.get(Bag)
    stash = entity.get(Warehouse)
    if bag is None or stash is None:
        raise TradeError("missing_components", "缺少 Bag/Warehouse 组件")
    have = stash.items.get(item_id, 0)
    if have < count:
        raise TradeError("not_enough", f"仓库里 {spec.get('name', item_id)} 只有 {have} 件")
    if bag.full and item_id not in bag.items:
        raise TradeError("bag_full", "背包已满")
    left = have - count
    if left:
        stash.items[item_id] = left
    else:
        stash.items.pop(item_id, None)
    bag.add(item_id, count)
    return {"item": item_id, "count": count, "bag_used": bag.used}
