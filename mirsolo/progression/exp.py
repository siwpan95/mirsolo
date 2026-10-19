"""成长系统 —— 经验曲线查表 + 升级结算 + 属性成长。

三个要点：

1. **曲线只查表不拟合**：经验值是手录的离散表（1.76 口径），
   不在代码里写公式，避免"公式看着像但其实不是原版"的隐性漂移。
2. **溢出经验连续结转**：一次击杀拿到的经验可能连升多级，
   ``add_exp`` 用循环扣除本级阈值，返回实际升了几级。
3. **升级即重算**：属性由 ``level`` 派生（见 ``classes.json`` 的成长字段），
   升级后调用 :func:`apply_growth` 覆盖面，不做增量累加——增量累加会被
   "穿了装备再升级"这类操作搞出漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from mirsolo.components import Attributes, Body, Progression, Vitals
from mirsolo.data.loader import GameData


@dataclass
class LevelUpResult:
    """一次加经验的完整结果（便于日志与单测）。"""

    levels_gained: int = 0
    new_level: int = 1
    exp_left: int = 0
    overflow_dropped: int = 0


def exp_to_next(data: GameData, level: int) -> int:
    """本级升级所需经验；封顶级返回 0。"""
    return data.exp_to_next(level)


def total_exp_span(data: GameData, level: int) -> int:
    """到 ``level`` 为止的累计经验（用于"总进度"显示与回归断言）。"""
    return sum(data.exp_to_next(lv) for lv in range(1, level))


def add_exp(data: GameData, prog: Progression, amount: int) -> LevelUpResult:
    """给角色加经验并处理连升。

    封顶级（``exp_to_next == 0``）之后经验不再累积，``overflow_dropped``
    记录被丢弃的量——单机里这样处理是为了避免"满级后经验条一直涨"的假象。
    """
    result = LevelUpResult(new_level=prog.level, exp_left=prog.exp)
    if amount <= 0:
        return result

    prog.exp += amount
    while True:
        need = data.exp_to_next(prog.level)
        if need <= 0:
            result.overflow_dropped = prog.exp
            prog.exp = 0
            break
        if prog.exp < need:
            break
        prog.exp -= need
        prog.level += 1
        result.levels_gained += 1

    result.new_level = prog.level
    result.exp_left = prog.exp
    return result


def grow_attributes(class_spec: dict, level: int) -> dict:
    """按职业成长字段算出该等级的基础属性（返回值可直接喂组件）。

    成长语义（字段名即可读）：``*_per_levels = N`` 表示**每升 N 级 +1**，
    步长固定 1。全部整数除法，与数据表字段一一对应：

    ``dc_min = dc_base[0] + (level - 1) // dc_min_per_levels``
    """
    n = max(0, level - 1)

    def step(base: int, field: str) -> int:
        per = class_spec.get(field)
        if not per:
            return base
        return base + n // int(per)

    dc = (
        step(int(class_spec["dc_base"][0]), "dc_min_per_levels"),
        step(int(class_spec["dc_base"][1]), "dc_max_per_levels"),
    )
    ac = (
        step(int(class_spec["ac_base"][0]), "ac_min_per_levels"),
        step(int(class_spec["ac_base"][1]), "ac_max_per_levels"),
    )
    return {
        "hp_max": int(class_spec["hp_base"]) + int(class_spec["hp_per_level"]) * n,
        "mp_max": int(class_spec["mp_base"]) + int(class_spec["mp_per_level"]) * n,
        "dc": dc,
        "mc": (0, 0),
        "sc": (0, 0),
        "ac": ac,
        "mac": tuple(class_spec.get("mac_base", (0, 0))),
        "accuracy": step(int(class_spec["accuracy_base"]), "accuracy_per_levels"),
        "agility": step(int(class_spec["agility_base"]), "agility_per_levels"),
        "max_weight": int(class_spec["weight_base"]) + int(class_spec["weight_per_level"]) * n,
    }


def apply_growth(entity, class_spec: dict, *, heal_on_level_up: bool = True) -> None:
    """把等级派生属性写回实体组件（升级后调用）。

    ``heal_on_level_up``：传奇升级回满血蓝；单机保留这个手感。
    装备加成不在这里算——它由 ``items/equipment.py`` 在穿戴时叠加，
    成长只负责"裸身基准值"，两者分离保证可重算。
    """
    prog = entity.get(Progression)
    vitals = entity.get(Vitals)
    attr = entity.get(Attributes)
    body = entity.get(Body)
    if prog is None or vitals is None or attr is None:
        raise ValueError("apply_growth 需要 Progression / Vitals / Attributes 组件")

    grown = grow_attributes(class_spec, prog.level)
    vitals.max_hp = int(grown["hp_max"])
    vitals.max_mp = int(grown["mp_max"])
    if heal_on_level_up:
        vitals.hp = vitals.max_hp
        vitals.mp = vitals.max_mp
    else:
        vitals.hp = min(vitals.hp, vitals.max_hp)
        vitals.mp = min(vitals.mp, vitals.max_mp)

    attr.dc = grown["dc"]
    attr.mc = grown["mc"]
    attr.sc = grown["sc"]
    attr.ac = grown["ac"]
    attr.mac = grown["mac"]
    attr.accuracy = int(grown["accuracy"])
    attr.agility = int(grown["agility"])
    if body is not None:
        body.max_weight = int(grown["max_weight"])
