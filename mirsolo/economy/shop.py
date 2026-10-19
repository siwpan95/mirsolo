"""经济：商店买卖 + 仓库存取（P1 逻辑层，P2 再接 NPC 面板）。

三条硬规则，全部有拒绝码，绝不做"悄悄扣一半"这种模糊处理：

1. **钱要够**：买入前逐项核对金币，扣款与入包在同一次调用里完成；
2. **负重/容量要够**：负重 = 已穿装备 + 背包内物品；背包满/超重直接拒；
3. **卖出价远低于买入价**（原版回收价约 1/3），防止"低买高卖"刷钱。

仓库与背包是两套容量，存取都不允许"溢出丢弃"。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Bag, Body, Loadout, Vitals, Warehouse, Wallet, Wear
from mirsolo.data.loader import GameData
from mirsolo.items import mining, potion, wear
from mirsolo.items.equipment import recompute

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
    # 新入手的就是**全新**的：磨损账按 item_id 记，不重置的话
    # "打废一把、卖掉、再买一把同款的"拿回来还是废的（钱白花了还没修好）。
    wear.reset(entity.get(Wear), item_id)
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


def ore_value(data: GameData, entity: Entity) -> int:
    """背包里那些矿石**现在**能卖多少金（面板用它写按钮上的数）。

    与 ``sell_ores`` 共用 ``is_ore`` + ``sell_price`` 这两个口径，
    所以按钮上写的数和点下去真正进账的数不可能对不上。
    """
    bag = entity.get(Bag)
    if bag is None:
        return 0
    total = 0
    for item_id, count in bag.items.items():
        if int(count) > 0 and mining.is_ore(data, item_id):
            total += sell_price(data, item_id) * int(count)
    return total


def sell_ores(entity: Entity, data: GameData) -> dict:
    """一次性卖掉背包里的**全部矿石**（P3-S4）。

    挖一趟回来手上往往是五六种矿石各一堆；一种一种点过去的点击量，
    足以让人懒得再去挖——而挖矿的收益本来就不比打怪，再收一道操作税就不划算了。

    与 ``repair_all`` 同一条纪律：**整笔结算**。包里没有矿石就直接报错
    （面板据此把这行藏起来），绝不做"先卖掉一部分"。
    """
    bag = entity.get(Bag)
    wallet = entity.get(Wallet)
    if bag is None or wallet is None:
        raise TradeError("missing_components", "缺少 Bag/Wallet 组件")
    rows = [(item_id, int(count)) for item_id, count in sorted(bag.items.items())
            if int(count) > 0 and mining.is_ore(data, item_id)]
    if not rows:
        raise TradeError("nothing_to_sell", "背包里没有矿石")
    income = 0
    for item_id, count in rows:
        bag.remove(item_id, count)
        income += sell_price(data, item_id) * count
    wallet.gold += income
    return {"items": [item_id for item_id, _ in rows], "earned": income,
            "gold": wallet.gold}


def repair(entity: Entity, data: GameData, item_id: str) -> dict:
    """修好**一件**装备（磨损清零，P3-S1）。

    只在"真的有磨损"时收钱：全新装备不给修理按钮点（面板会置灰）。
    """
    spec = _spec(data, item_id)
    if spec.get("kind", "equip") != "equip":
        raise TradeError("not_equipment", f"{spec.get('name', item_id)} 不是装备")
    wear_book = entity.get(Wear)
    wallet = entity.get(Wallet)
    if wallet is None:
        raise TradeError("missing_components", "缺少 Wallet 组件")
    cost = wear.repair_cost(data, wear_book, item_id)
    if cost <= 0:
        raise TradeError("nothing_to_repair", f"{spec.get('name', item_id)} 还是好的")
    if wallet.gold < cost:
        raise TradeError("no_gold", f"金币不足：需要 {cost}，身上 {wallet.gold}")
    wallet.gold -= cost
    wear.reset(wear_book, item_id)
    recompute(entity, data)  # 修好了属性要立刻回来
    return {"item": item_id, "paid": cost, "gold": wallet.gold}


def repair_all(entity: Entity, data: GameData) -> dict:
    """一次性修好身上/背包/仓库里**所有有磨损**的装备。

    整笔结算、不做"钱不够就先修一部分"：本模块从第一天起就不做悄悄扣一半
    这种模糊处理，钱不够就是不够，面板会把按钮置灰并写明差多少。
    """
    rows = wear.repair_rows(data, entity)
    if not rows:
        raise TradeError("nothing_to_repair", "没有需要修理的装备")
    wallet = entity.get(Wallet)
    if wallet is None:
        raise TradeError("missing_components", "缺少 Wallet 组件")
    total = sum(int(row["cost"]) for row in rows)
    if wallet.gold < total:
        raise TradeError("no_gold", f"金币不足：需要 {total}，身上 {wallet.gold}")
    wear_book = entity.get(Wear)
    wallet.gold -= total
    for row in rows:
        wear.reset(wear_book, row["item"])
    recompute(entity, data)
    return {"items": [row["item"] for row in rows], "paid": total, "gold": wallet.gold}


def resupply(entity: Entity, data: GameData) -> dict:
    """一键补给：按当前血/蓝上限挑档位，红蓝各买 ``potion.BUY_BUNDLE`` 瓶。

    为什么是"一批"而不是"补满"：补满要一路买到负重见底或钱花光，一次点击
    就把背包和钱包同时清空——捡不到掉落、修不起装备，是个看着方便实则坑人
    的按钮。固定一批可预测，点几次就是几批。

    为什么整笔拒绝而不是"钱不够就少买点"：与 :func:`buy` 同一条规矩，
    本模块不做悄悄扣一半这种模糊处理。钱不够/装不下，面板把这一行置灰
    并写明差多少。
    """
    vitals = entity.get(Vitals)
    wallet = entity.get(Wallet)
    bag = entity.get(Bag)
    if vitals is None or wallet is None or bag is None:
        raise TradeError("missing_components", "缺少 Vitals/Wallet/Bag 组件")

    # 负重是**物理**上限，所以这里按余量夹瓶数（红优先），而不是整笔拒绝：
    # 法师 30 级裸装负重只有 49，红蓝各 10 瓶要 50，整笔拒绝会让人一次也补不了。
    # 钱不够才是整笔拒绝——那一条从第一天起就没破过例。
    free = potion.free_weight(entity, data, carried_weight(entity, data))
    plan: list[tuple[str, int]] = []
    for want, cap in (("hp", int(vitals.max_hp) // 2), ("mp", int(vitals.max_mp) // 2)):
        if cap <= 0:
            continue  # 战士 max_mp 恒为 0，这一档连挑都不用挑
        item_id = potion.stock_choice(data, want, cap)
        if item_id is None:
            continue
        weight = int(data.items["items"][item_id].get("weight", 1)) or 1
        count = min(potion.BUY_BUNDLE, free // weight)
        if count <= 0:
            continue
        free -= weight * count
        plan.append((item_id, count))
    if not plan:
        raise TradeError("overweight", "负重已经满了，一瓶也塞不进去")

    cost = sum(buy_price(data, item_id) * count for item_id, count in plan)
    if wallet.gold < cost:
        raise TradeError("no_gold", f"金币不足：需要 {cost}，身上 {wallet.gold}")
    new_kinds = sum(1 for item_id, _ in plan if item_id not in bag.items)
    if bag.used + new_kinds > int(bag.capacity):
        raise TradeError("bag_full", "背包放不下新种类的药水了")

    wallet.gold -= cost
    for item_id, count in plan:
        bag.add(item_id, count)
    return {"items": [{"item": i, "count": n} for i, n in plan],
            "paid": cost, "gold": wallet.gold}


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
