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
    Blessing,
    Body,
    Codex,
    Cooldowns,
    DeathLedger,
    Identity,
    Loadout,
    Mercenary,
    Mining,
    Position,
    PotionState,
    Progression,
    Skills,
    SummonOwner,
    Upgrade,
    Vitals,
    Wallet,
    Warehouse,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth

#: AI 档位参数（``monsters.json`` 的 ``ai`` 字段选用；单项可用表里同名字段覆盖）
#: ``vision`` 索敌视野（切比雪夫格），``leash_mult`` 脱战距离倍率。
AI_TIERS: dict[str, dict[str, float]] = {
    "passive": {"vision": 4.0, "leash_mult": 0.5},
    "aggressive": {"vision": 10.0, "leash_mult": 1.0},
    "elite": {"vision": 16.0, "leash_mult": 1.6},
}
DEFAULT_TIER = "aggressive"
BASE_LEASH = 12.0

#: 实体序号存在 world data 里的键（只增不减）
KEY_ENTITY_SERIAL = "entity_serial"


def next_entity_id(world: World, prefix: str) -> str:
    """生成一个**永不重复**的 eid。

    为什么不能用 ``entity_count() + 1``：``World.create_entity`` 遇到已存在的
    eid 会**静默覆盖**（实测：既不抛异常，实体数也不变），而实体数会**倒退**——
    召唤物尸体会被清掉、换图会整体重置。计数一倒退，新怪就直接顶替掉一只
    活着的旧怪，表现成"怪凭空少了一只"，还查不出痕迹。

    所以序号只增不减，落盘在 ``world`` data 里，最后再兜一次存在性检查。
    """
    while True:
        serial = int(world.get_data(KEY_ENTITY_SERIAL, 0)) + 1
        world.set_data(KEY_ENTITY_SERIAL, serial)
        candidate = f"{prefix}#{serial}"
        if not world.has_entity(candidate):
            return candidate


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
    """创建玩家实体（1 级，裸装，按职业成长表初始化）。

    eid 固定为 ``"player"``，所以**同 world 只能有一个**：重复创建时
    ``create_entity`` 会静默返回旧实体、把组件覆盖一遍（血量/背包/等级全丢）。
    换图请用 :func:`mirsolo.world.travel.travel` 搬位置，而不是重建玩家。
    """
    if class_id not in data.classes["classes"]:
        raise KeyError(f"未知职业 {class_id!r}")
    if world.has_entity(eid):
        raise ValueError(f"eid {eid!r} 已存在，不能重复创建玩家（换图请用 travel）")
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
    entity.add(Warehouse())
    entity.add(Loadout())
    entity.add(Wear())
    entity.add(Upgrade())
    entity.add(Blessing())
    entity.add(DeathLedger())
    entity.add(Mining())
    entity.add(PotionState())
    entity.add(Skills())
    entity.add(Cooldowns())
    entity.add(Codex())
    apply_growth(entity, class_spec)
    return entity


def _build_combatant(
    world: World,
    spec: dict,
    *,
    name: str,
    kind: str,
    faction: int,
    species: str,
    x: float,
    y: float,
    eid: str | None = None,
):
    """按一份"战斗单位"数据造实体 —— 怪物与佣兵共用的**唯一**构造路径。

    抽这一层的理由很实际：佣兵的属性字段与怪物同构（``hp``/``dc``/``ac``/``mac``/
    ``accuracy``/``agility``/``attack_speed``/``ai``），复制一份构造代码就等于
    给自己留一个"改了怪物没改佣兵"的坑——比如以后给怪物加个新组件，
    佣兵会静默少一样东西。

    ``faction`` 是唯一能把友军与敌人分开的东西：索敌按 faction 判定，
    所以佣兵（0）会自己去找怪（1）打，怪也会反过来打佣兵，不必另写 AI。
    """
    tier = spec.get("ai", DEFAULT_TIER)
    if tier not in AI_TIERS:
        tier = DEFAULT_TIER  # 数据写错档位就退回常规，不让造单位直接崩

    unique_eid = eid or next_entity_id(world, species)
    entity = world.create_entity(unique_eid)
    entity.add(Identity(name=name, kind=kind, faction=faction, species=species))
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
        leash=float(spec.get("leash", BASE_LEASH)) * AI_TIERS[tier]["leash_mult"],
        vision=float(spec.get("vision", AI_TIERS[tier]["vision"])),
        tier=tier,
    ))
    entity.add(Cooldowns())
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

    eid 默认 ``"<species>#<序号>"`` —— 必须唯一（``World.entities`` 以 eid 为键）。
    序号走 :func:`next_entity_id`（只增不减 + 存在性兜底），**不要**改成
    ``entity_count()``：那个值会因为清尸体/换图而倒退，撞上已有 eid 会静默覆盖。
    数据表查表一律走 ``Identity.species``。
    """
    spec = data.monsters["monsters"].get(monster_id)
    if spec is None:
        raise KeyError(f"未知怪物 {monster_id!r}")
    return _build_combatant(world, spec, name=spec["name"], kind="monster",
                            faction=1, species=monster_id, x=x, y=y, eid=eid)


def spawn_mercenary(
    world: World,
    data: GameData,
    merc_id: str,
    *,
    x: float = 0.0,
    y: float = 0.0,
    owner_id: int | None = None,
    tick: int = 0,
):
    """创建佣兵实体（P2-S6）：友军阵营的召唤物 + 雇佣到期刻。

    走 :func:`_build_combatant` 与怪物同一条构造路径，只额外挂两样东西：

    * ``SummonOwner`` —— 击杀归属（经验/掉落记到雇主头上，否则凭空消失）；
    * ``Mercenary`` —— 雇佣来源与到期刻（到期由 ``MercenarySystem`` 解散）。

    ``kind="summon"`` 而不是新造一个 "merc" 种类：死亡清理、尸体移除、
    "不被玩家误伤"这三条现有判定都按 summon 处理，多一个种类就要改三处。
    """
    spec = data.mercenaries["mercenaries"].get(merc_id)
    if spec is None:
        raise KeyError(f"未知佣兵 {merc_id!r}")
    entity = _build_combatant(world, spec, name=spec["name"], kind="summon",
                              faction=0, species=merc_id, x=x, y=y)
    if owner_id is not None:
        entity.add(SummonOwner(owner_id=owner_id))
    entity.add(Mercenary(merc_id=merc_id,
                         until_tick=int(tick) + int(spec["duration_ticks"])))
    return entity
