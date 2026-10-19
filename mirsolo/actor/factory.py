"""实体工厂 —— 唯一按数据表"造人/造怪"的地方。

好处很实在：属性怎么算、组件挂哪些，只有这一处说了算。
测试里造一只怪不会因为某个系统多加了个组件而悄悄变形。
"""

from __future__ import annotations

from allin_engine import World

from mirsolo.components import (
    AiState,
    Attributes,
    Bag,
    Body,
    Cooldowns,
    Identity,
    Loadout,
    Position,
    Progression,
    Skills,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth


def spawn_player(
    world: World,
    data: GameData,
    *,
    class_id: str = "warrior",
    name: str = "无名",
    x: float = 0.0,
    y: float = 0.0,
    eid: str = "player",
):
    """创建玩家实体（1 级，裸装，按职业成长表初始化）。"""
    if class_id not in data.classes["classes"]:
        raise KeyError(f"未知职业 {class_id!r}")
    class_spec = data.classes["classes"][class_id]

    entity = world.create_entity(eid)
    entity.add(Identity(name=name, kind="player", faction=0, species=class_id))
    entity.add(Position(x=x, y=y))
    entity.add(Vitals())
    entity.add(Attributes())
    entity.add(Progression(level=1, exp=0, class_id=class_id))
    entity.add(Body())
    entity.add(Wallet())
    entity.add(Bag())
    entity.add(Loadout())
    entity.add(Skills())
    entity.add(Cooldowns())
    apply_growth(entity, class_spec)
    return entity


def spawn_monster(
    world: World,
    data: GameData,
    monster_id: str,
    *,
    x: float = 0.0,
    y: float = 0.0,
    eid: str | None = None,
):
    """创建怪物实体。

    eid 默认 ``"<species>#<序号>"`` —— 必须唯一（``World.entities`` 以 eid 为键），
    数据表查表一律走 ``Identity.species``。
    """
    spec = data.monsters["monsters"].get(monster_id)
    if spec is None:
        raise KeyError(f"未知怪物 {monster_id!r}")

    unique_eid = eid or f"{monster_id}#{world.entity_count() + 1}"
    entity = world.create_entity(unique_eid)
    entity.add(Identity(name=spec["name"], kind="monster", faction=1, species=monster_id))
    entity.add(Position(x=x, y=y))
    hp = int(spec["hp"])
    entity.add(Vitals(hp=hp, max_hp=hp, mp=0, max_mp=0))
    entity.add(Attributes(
        dc=tuple(spec["dc"]),
        ac=tuple(spec["ac"]),
        mac=tuple(spec.get("mac", (0, 0))),
        accuracy=int(spec["accuracy"]),
        agility=int(spec["agility"]),
    ))
    entity.add(AiState(
        home=(x, y),
        leash=float(spec.get("leash", 12.0)),
        vision=float(spec.get("vision", 10.0)),
    ))
    entity.add(Cooldowns())
    return entity
