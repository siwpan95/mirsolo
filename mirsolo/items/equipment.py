"""装备穿戴 —— 把 Loadout 里的 item_id 变成属性加成。

关键设计：**属性永远是"裸身基准 + 装备加成"重算出来的**，不做增量累加。

原因很实在：``apply_growth`` 每次升级都会用职业成长表覆盖面（这是对的，
否则"穿装备再升级"会漂移），所以升级后必须重算一次加成；如果穿戴走增量、
升级走覆盖，两套语义混用，属性能不能对全靠调用顺序——那就是等着出 bug。

因此本模块只提供两个动作（穿/脱）和一个纯函数（重算），
任何"改属性"的路径最后都落到 :func:`recompute`。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.combat.formulas import clamp_luck
from mirsolo.components import (
    Attributes,
    Bag,
    Body,
    Identity,
    Loadout,
    Progression,
    Vitals,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import grow_attributes


class EquipError(ValueError):
    """穿脱装备被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _item(data: GameData, item_id: str) -> dict:
    spec = data.items["items"].get(item_id)
    if spec is None:
        raise EquipError("unknown_item", f"未知物品 {item_id!r}")
    if spec.get("kind", "equip") != "equip":
        raise EquipError("not_equipment", f"{spec.get('name', item_id)} 不是装备")
    return spec


def equip(entity: Entity, data: GameData, item_id: str) -> dict:
    """穿上一件装备；同槽已有则自动替换（替换下来的回背包）。

    返回被替换下来的 item_id 或 ``{}``。
    """
    spec = _item(data, item_id)
    ident = entity.get(Identity)
    prog = entity.get(Progression)
    bag = entity.get(Bag)
    loadout = entity.get(Loadout)
    body = entity.get(Body)
    if ident is None or prog is None or bag is None or loadout is None:
        raise EquipError("missing_components", "缺少 Identity/Progression/Bag/Loadout 组件")

    allowed = spec.get("class") or []
    if allowed and prog.class_id not in allowed:
        raise EquipError("class_mismatch",
                         f"{spec['name']} 限 {'/'.join(allowed)} 使用")
    if prog.level < int(spec.get("level_req", 1)):
        raise EquipError("level_too_low",
                         f"{spec['name']} 需要 {spec['level_req']} 级，当前 {prog.level} 级")
    if bag.items.get(item_id, 0) <= 0:
        raise EquipError("not_in_bag", f"背包里没有 {spec['name']}")

    slot = spec["slot"]
    replaced = loadout.slots.get(slot)
    # 负重校验要把"替换掉的重量"先扣掉，否则换更好的装备会被旧装备重量卡住
    if body is not None:
        current = _gear_weight(data, loadout.slots)
        freed = int(_item(data, replaced)["weight"]) if replaced else 0
        incoming = int(spec.get("weight", 0))
        if current - freed + incoming > int(body.max_weight):
            raise EquipError("overweight",
                             f"负重不足：{current - freed + incoming} > {body.max_weight}")
    if replaced is not None:
        bag.add(replaced, 1)
    bag.remove(item_id, 1)
    loadout.slots[slot] = item_id
    recompute(entity, data)
    return {"slot": slot, "replaced": replaced} if replaced else {"slot": slot}


def unequip(entity: Entity, data: GameData, slot: str) -> str:
    """脱下某槽装备并放回背包，返回 item_id。"""
    bag = entity.get(Bag)
    loadout = entity.get(Loadout)
    if bag is None or loadout is None:
        raise EquipError("missing_components", "缺少 Bag/Loadout 组件")
    item_id = loadout.slots.pop(slot, None)
    if item_id is None:
        raise EquipError("slot_empty", f"{slot} 槽没有装备")
    if bag.full and item_id not in bag.items:
        loadout.slots[slot] = item_id  # 回滚，不能凭空消失
        raise EquipError("bag_full", "背包已满，无法卸下")
    bag.add(item_id, 1)
    recompute(entity, data)
    return item_id


def _gear_weight(data: GameData, slots: dict[str, str]) -> int:
    return sum(int(data.items["items"].get(iid, {}).get("weight", 0))
               for iid in slots.values())


def _add_range(base: tuple[int, int], bonus) -> tuple[int, int]:
    if not bonus:
        return base
    return (int(base[0]) + int(bonus[0]), int(base[1]) + int(bonus[1]))


def recompute(entity: Entity, data: GameData) -> None:
    """属性 = 职业成长基准 + 已穿装备加成（每次穿/脱/升级后调用）。"""
    prog = entity.get(Progression)
    attr = entity.get(Attributes)
    loadout = entity.get(Loadout)
    body = entity.get(Body)
    if prog is None or attr is None:
        raise EquipError("missing_components", "缺少 Progression/Attributes 组件")
    class_spec = data.classes["classes"][prog.class_id]
    base = grow_attributes(class_spec, prog.level)

    slots = loadout.slots if loadout is not None else {}
    dc, mc, sc, ac, mac = base["dc"], base["mc"], base["sc"], base["ac"], base["mac"]
    accuracy = int(base["accuracy"])
    # 幸运：只有 weapon / necklace 两个槽会给（由 verify.py 的 luck-sources 门禁保证），
    # 所以这里可以放心地把**所有槽**的 luck 加起来——加错了门禁会先红。
    luck = 0
    gear_weight = 0
    for item_id in slots.values():
        spec = data.items["items"].get(item_id)
        if spec is None:
            continue  # 数据被删时不要让存档崩，直接跳过
        gear_weight += int(spec.get("weight", 0))
        dc = _add_range(dc, spec.get("atk"))
        mc = _add_range(mc, spec.get("mc"))
        sc = _add_range(sc, spec.get("sc"))
        ac = _add_range(ac, spec.get("ac"))
        mac = _add_range(mac, spec.get("mac"))
        accuracy += int(spec.get("accuracy", 0) or 0)
        luck += int(spec.get("luck", 0) or 0)

    # 幸运在这里**只汇总、不生效**：真正消费它的是 ``formulas.lucky_range``。
    # 两者中间隔着 resolver，所以这条赋值被摘掉时必须有集成测试变红
    #（``tests/test_luck.py::test_resolver_feeds_luck_into_physical``）。
    attr.dc, attr.mc, attr.sc, attr.ac, attr.mac = dc, mc, sc, ac, mac
    attr.accuracy = accuracy
    attr.agility = int(base["agility"])
    attr.luck = clamp_luck(luck)
    if body is not None:
        body.max_weight = int(base["max_weight"])
        body.weight = gear_weight
        if body.weight > body.max_weight:
            body.weight = body.max_weight  # 显示钳制；真正的门在 equip()
    vitals = entity.get(Vitals)
    if vitals is not None:
        # 装备不加上限（原版衣服不加血上限），只把当前值钳进上限
        vitals.max_hp = int(base["hp_max"])
        vitals.max_mp = int(base["mp_max"])
        vitals.hp = min(vitals.hp, vitals.max_hp)
        vitals.mp = min(vitals.mp, vitals.max_mp)


def equipped_summary(entity: Entity, data: GameData) -> list[dict]:
    """已穿装备清单（HUD / 调试用）。"""
    loadout = entity.get(Loadout)
    if loadout is None:
        return []
    out = []
    for slot, item_id in sorted(loadout.slots.items()):
        spec = data.items["items"].get(item_id, {})
        out.append({"slot": slot, "item": item_id, "name": spec.get("name", item_id)})
    return out
