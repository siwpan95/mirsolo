"""祝福油（P3-S5）—— 给武器加幸运，代价是可能加出诅咒。

祝福油是**唯一**能动"幸运"的消耗品：武器自带的 ``luck`` 写在数据表里
（赤月系 7 点、普通武器 0 点），祝福等级则是在此之上**加减**的一层。
两者相加再钳进 ``[-CURSE_CAP, LUCK_CAP]``，所以：

- 普通武器（自带 0）祝福到 +7，配幸运项链 2 点就能凑满 9——**木剑也能刀刀上限**，
  这正是祝福油存在的意义：让幸运不再是终局武器的专利；
- 自带 7 点的赤月魔剑不用项链就能到 9，等于**省下一个项链槽**；
- 失败会降 1，降成负数就是诅咒。诅咒不是"祝福为 0"，它在
  ``formulas.lucky_range`` 里**对称地压低上限**（幸运抬下限、诅咒压上限），
  所以一把被诅咒的刀是真的会变钝。

成功率随当前等级递减（越高越难），但**负等级会把成功率抬回去**：
诅咒越深越容易拉回来，否则连续失败会把人锁死在 -7，那不是风险、
是纯粹的劝退。

单机化取舍：只作用于**当前穿着的武器**（与升级同一条口径）；
原版祝福油有"武器祝福等级不能超过武器等级""祝福会掉持久"等附加约束，
本作一律不取——它们各自都要多一套表，而收益只是让系统更难懂。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Bag, Blessing
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng, splitmix64
from mirsolo.items import upgrade

#: 祝福油在 ``items.json`` 里的 id（数据驱动，代码只认 ID）
OIL = "blessing_oil"


class BlessError(ValueError):
    """用祝福油被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData) -> dict:
    return data.upgrades.get("blessing") or {}


def cap(data: GameData) -> int:
    """祝福等级上限（数据表口径：+7）。"""
    return int(_spec(data).get("max", 0))


def floor(data: GameData) -> int:
    """祝福等级下限（数据表口径：-7，负值即诅咒）。"""
    return int(_spec(data).get("min", 0))


def level_of(book, item_id: str) -> int:
    """这把武器现在的祝福等级（没有账本 = 0）。"""
    if book is None:
        return 0
    return int(book.levels.get(item_id, 0))


def attempts_of(book, item_id: str) -> int:
    """这把武器累计用过多少瓶祝福油（**只增不减**）。"""
    if book is None:
        return 0
    return int(book.attempts.get(item_id, 0))


def success_bp(data: GameData, level: int) -> int:
    """这一瓶的成功率（万分比）：``base_bp − 当前等级 × step_bp``，夹进 [min_bp, 10000]。

    ``level`` 为负时减法变加法——**诅咒越深越容易拉回来**。
    """
    spec = _spec(data)
    base = int(spec.get("base_bp", 0))
    bp = base - int(level) * int(spec.get("step_bp", 0))
    return max(int(spec.get("min_bp", 0)), min(10000, bp))


def is_cursed(book, item_id: str) -> bool:
    """这把武器是不是被诅咒了（祝福等级为负）。"""
    return level_of(book, item_id) < 0


def plan(data: GameData, entity: Entity) -> dict:
    """"再抹一瓶"这件事的全貌（面板数据源 + :func:`bless` 的前置校验共用）。

    ``ok`` 为假时 ``code`` 给原因（``no_weapon`` / ``not_upgradable`` /
    ``no_oil`` / ``maxed`` / ``floored``）。``maxed`` 与 ``floored`` 都不是错误，
    只是"这一瓶抹不下去"——面板据此把按钮置灰并写清楚原因。
    """
    item_id = upgrade.wielded_weapon(entity)
    if not item_id:
        return {"ok": False, "code": "no_weapon", "item": "", "level": 0}
    if not upgrade.is_upgradable(data, item_id):
        return {"ok": False, "code": "not_upgradable", "item": item_id, "level": 0}
    book = entity.get(Blessing)
    level = level_of(book, item_id)
    bag = entity.get(Bag)
    oils = int(bag.items.get(OIL, 0)) if bag is not None else 0
    if oils <= 0:
        return {"ok": False, "code": "no_oil", "item": item_id, "level": level}
    if level >= cap(data):
        return {"ok": False, "code": "maxed", "item": item_id, "level": level}
    if level <= floor(data):
        return {"ok": False, "code": "floored", "item": item_id, "level": level}
    return {
        "ok": True,
        "code": "",
        "item": item_id,
        "level": level,
        "oils": oils,
        "bp": success_bp(data, level),
    }


def bless(entity: Entity, data: GameData, seed: int) -> dict:
    """给当前穿着的武器抹一瓶祝福油（成功 +1 / 失败 −1）。

    与升级同一条纪律：**先扣油、再掷点**；尝试次数**只增不减**
    （流名 ``bless:{item_id}:{attempt}``，读档重来掷出的是同一个数）。
    """
    info = plan(data, entity)
    if not info["ok"]:
        raise BlessError(str(info["code"]), _message(data, info))
    item_id = str(info["item"])
    book = entity.get(Blessing)
    bag = entity.get(Bag)
    level = int(info["level"])

    bag.remove(OIL, 1)
    attempt = attempts_of(book, item_id)
    if book is not None:
        book.attempts[item_id] = attempt + 1

    stream = f"bless:{item_id}:{attempt}"
    rng = Rng(seed ^ splitmix64(1), stream)
    rolled = rng.rand_int(1, 10000)
    won = rolled <= int(info["bp"])
    after = min(cap(data), level + 1) if won else max(floor(data), level - 1)
    if book is not None:
        book.levels[item_id] = after
    # 与升级同一条：改完账本立刻重算，否则幸运变了但面板还写着旧数。
    from mirsolo.items.equipment import recompute

    recompute(entity, data)
    return {
        "ok": won,
        "item": item_id,
        "level": after,
        "from": level,
        "cursed": after < 0,
        "left": int(bag.items.get(OIL, 0)),
        "bp": int(info["bp"]),
        "roll": rolled,
    }


def _message(data: GameData, info: dict) -> str:
    name = (data.items["items"].get(info.get("item", "")) or {}).get(
        "name", info.get("item", ""))
    return {
        "no_weapon": "手上没有武器",
        "not_upgradable": f"{name} 不能抹祝福油",
        "no_oil": "背包里没有祝福油",
        "maxed": f"{name} 的祝福已经到顶",
        "floored": f"{name} 的诅咒已经到底",
    }.get(str(info.get("code")), "现在抹不了祝福油")
