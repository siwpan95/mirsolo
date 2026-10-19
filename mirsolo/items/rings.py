"""特戒子系统 —— 传奇六枚戒指的单机化规则层。

原版六戒（麻痹/护身/复活/传送/隐身/火焰）依赖的东西单机大多没有：
目标 UI、**服务端权威**、随机传送卷、队伍。所以这里按**触发时机**重排一遍，
每条都写明"原版怎么算 / 单机改成什么"——否则照抄只会得到"按了没反应"的戒指。

| 戒指 | 触发 | 单机口径 | 需要的数据参数 |
|---|---|---|---|
| 麻痹 | 攻击命中 | 命中后掷万分比，中了给目标挂 :class:`Paralysis` | ``bp`` / ``ticks`` |
| 护身 | 自己挨伤害 | 按比例把 HP 损失换成 MP；蓝不够的部分照扣血 | ``permille`` |
| 复活 | 自己即将致死 | 致死那一下在 ``take()`` 之前拦下，回 ``hp_permille`` 并扣一次额度 | ``charges`` / ``hp_permille`` |
| 传送 | 主动（``T`` 键） | 数据驱动的固定回城点 + 冷却，走 ``world.travel.travel`` | ``map`` / ``cooldown_ticks`` |
| 隐身 | 持续 | 每刻刷新 :class:`Invisible`；出手破隐，过 ``recover_ticks`` 再结 | ``ticks`` / ``recover_ticks`` |
| 火焰 | 攻击命中 | 追加一次**不看防御**的火焰伤害 | ``power_permille`` |

**一个槽、只有一枚戒指**：``Loadout`` 的 ``ring`` 槽没有左右手之分，想换一枚
必须先摘下当前这枚。这是刻意的设计——护身 + 复活叠起来会让"这刀到底死没死"
变得无法讨论，而两两搭配的平衡工作在单机里收益极低。

本模块是**纯规则**：回答"该扣多少 / 能不能发动 / 传哪去"，负责改世界的那部分
（挂组件、发事件、每秒刷新）分别落在 ``combat/resolver.py`` 与
``actor/systems.py`` 的 ``RingEffectSystem``。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.combat import formulas
from mirsolo.components import (
    Attributes,
    Loadout,
    Paralysis,
    RingState,
    Vitals,
)
from mirsolo.data.loader import RING_EFFECTS, GameData

#: 只有这个槽能带 ``ring_effect``（``loader.validate`` 与 ring-effects 门禁同口径）
RING_SLOT = "ring"

#: 效果的中文名（UI 与拒绝提示共用一份，免得两处翻译不一致）
EFFECT_LABELS: dict[str, str] = {
    "paralysis": "麻痹",
    "protect": "护身",
    "revive": "复活",
    "teleport": "传送",
    "invis": "隐身",
    "flame": "火焰",
}

EVT_REVIVE = "ring:revive"
"""复活戒指发动。载荷：``{entity, item, charges_left, hp, tick}``。"""

EVT_PARALYSIS = "ring:paralysis"
"""麻痹戒指发动。载荷：``{attacker, defender, until_tick, tick}``。"""


class RingError(ValueError):
    """特戒发动被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── 查询：谁戴着什么戒指 ──────────────────────────────

def ring_effect(data: GameData, entity: Entity) -> tuple[str, str, dict] | None:
    """返回 ``(item_id, effect, param)``；没戴戒指或戒指无效则 ``None``。

    为什么容错到 ``None`` 而不是抛异常：战斗结算每秒十次调用它，
    "某个实体没有 Loadout"（怪物就是如此）是常态，不是错误。
    """
    loadout = entity.get(Loadout)
    if loadout is None:
        return None
    item_id = loadout.slots.get(RING_SLOT)
    if not item_id:
        return None
    spec = data.items["items"].get(item_id)
    if spec is None or spec.get("slot") != RING_SLOT:
        return None
    effect = spec.get("ring_effect")
    if effect not in RING_EFFECTS:
        return None
    return item_id, effect, dict(spec.get("ring_param") or {})


def _state(entity: Entity) -> RingState:
    """取（没有就建）记账组件。所有写 side-effect 的入口都从这里走。"""
    state = entity.get(RingState)
    if state is None:
        state = RingState()
        entity.add(state)
    return state


# ── 挨打类：护身 / 复活 ───────────────────────────────

def split_protect(data: GameData, defender: Entity, amount: int) -> tuple[int, int]:
    """护身戒指：把 ``amount`` 拆成 ``(MP 代偿, 真正落到 HP 的伤害)``。

    顺序由调用方保证：**先扣魔法盾，再由戒指代偿**。反过来会让"戴着护身还套着
    魔法盾"的玩家把两层吸收叠成同一个血池——两层变相加，耐久直接翻倍。

    MP 不够时按现有 MP 顶，剩下的照掉血（不是"要么全顶要么不顶"）。
    """
    found = ring_effect(data, defender)
    if found is None or found[1] != "protect" or amount <= 0:
        return 0, amount
    param = found[2]
    permille = int(param.get("permille", 0))
    if permille <= 0:
        return 0, amount
    vitals = defender.get(Vitals)
    if vitals is None:
        return 0, amount
    covered = amount * permille // 1000
    mp_cost = min(int(vitals.mp), covered)
    return mp_cost, amount - mp_cost


def try_revive(data: GameData, defender: Entity, *, bus=None, tick: int = 0) -> bool:
    """复活戒指：扣一次额度并把血回到 ``hp_permille``；没额度则返回 ``False``。

    必须在致死伤害**落到 HP 之前**调用（``resolver._damage`` 里正是这么排的）：
    一旦先扣成 0 血再救回来，"死过一次"的副作用——死亡事件、尸体清算、
    战役判负——已经发出去了，救不干净。
    """
    found = ring_effect(data, defender)
    if found is None or found[1] != "revive":
        return False
    item_id, _, param = found
    total = max(1, int(param.get("charges", 1)))
    state = _state(defender)
    left = state.charges.get(item_id, total)
    if left <= 0:
        return False

    vitals = defender.get(Vitals)
    if vitals is None:
        return False
    state.charges[item_id] = left - 1
    # 直接赋值而不是"取较大值"：致命那一刀确实被吃掉了，但代价是血量被压到回复线。
    # 若保留当前血，这枚戒指就等价于"一次免死"，hp_permille 这个参数也就没用了。
    target = max(1, int(vitals.max_hp) * int(param.get("hp_permille", 1000)) // 1000)
    vitals.hp = min(int(vitals.max_hp), target)
    if bus is not None:
        bus.emit(EVT_REVIVE, {
            "entity": defender.id,
            "item": item_id,
            "charges_left": left - 1,
            "hp": vitals.hp,
            "tick": tick,
        })
    return True


def charges_left(data: GameData, entity: Entity) -> int | None:
    """复活戒指剩余次数（没戴复活戒指返回 ``None``）——面板显示用。"""
    found = ring_effect(data, entity)
    if found is None or found[1] != "revive":
        return None
    item_id, _, param = found
    return int(_state(entity).charges.get(item_id, max(1, int(param.get("charges", 1)))))


# ── 出手类：麻痹 / 火焰 / 破隐 ────────────────────────

def roll_paralysis(data: GameData, attacker: Entity, defender: Entity,
                   rng, tick: int) -> bool:
    """麻痹戒指：命中后掷一次万分比，中了给 ``defender`` 挂定身并返回 ``True``。

    ``rng`` 由调用方给定（必须是一条**独立的、名字里带 ``ring:``** 的流）：
    这样戴上/摘下戒指不会挪动主伤害流的位置，回放才对得上。
    """
    found = ring_effect(data, attacker)
    if found is None or found[1] != "paralysis":
        return False
    bp = int(found[2].get("bp", 0))
    if bp <= 0 or rng.rand_int(1, 10000) > bp:
        return False
    buff = defender.get(Paralysis)
    if buff is None:
        buff = Paralysis()
        defender.add(buff)
    buff.until_tick = int(tick) + max(1, int(found[2].get("ticks", 1)))
    buff.source = attacker.id
    return True


def flame_damage(data: GameData, attacker: Entity, rng) -> int:
    """火焰戒指：追加伤害 = roll(佩戴者攻击力) × power_permille // 1000。

    刻意**不看目标的 ac/mac**：原版火焰是元素伤害，穿透防御正是它的卖点；
    代价则是倍率给得低（数据表里控制在 300‰ 级别），两边相抵。
    """
    found = ring_effect(data, attacker)
    if found is None or found[1] != "flame":
        return 0
    attr = attacker.get(Attributes)
    dc = attr.dc if attr is not None else (0, 0)
    rolled = int(formulas.roll_damage_range(dc, rng))
    return rolled * max(0, int(found[2].get("power_permille", 0))) // 1000


def mark_strike(data: GameData, attacker: Entity, tick: int) -> None:
    """出手标记：隐身戒指的佩戴者出手破隐后，要等 ``recover_ticks`` 才能重结。

    没有这段冷却的话，"戴隐身戒指站桩输出"就是永久且无代价的隐身——
    原版也不是这样：出手会现形。
    """
    found = ring_effect(data, attacker)
    if found is None or found[1] != "invis":
        return
    _state(attacker).cloak_at = int(tick) + max(0, int(found[2].get("recover_ticks", 0)))


def can_cloak(data: GameData, entity: Entity, tick: int) -> int | None:
    """隐身戒指此刻能不能结隐；能则返回持续刻数，不能返回 ``None``。"""
    found = ring_effect(data, entity)
    if found is None or found[1] != "invis":
        return None
    state = _state(entity)
    if int(tick) < int(state.cloak_at):
        return None
    return max(1, int(found[2].get("ticks", 1)))


# ── 主动类：传送 ──────────────────────────────────────

def try_travel(data: GameData, session, entity: Entity, tick: int) -> dict:
    """传送戒指：走 ``world.travel.travel`` 到 ``ring_param["map"]``。

    为什么目标写在数据里而不是"记忆上一次落脚点"：后者要一套 UI 让玩家确认坐标，
    单机里收益远低于成本；固定回城点则可以直接被 ring-effects 门禁校验
    （地图存在、冷却为正），不会出现"戒指指向一张不存在的图"。

    ``session`` 用鸭子类型（只取 ``map_id`` / ``travel_to``），
    避免规则层依赖装配层。
    """
    found = ring_effect(data, entity)
    if found is None:
        raise RingError("no_ring", "没有戴传送戒指")
    _item_id, effect, param = found
    if effect != "teleport":
        raise RingError("wrong_ring", f"当前戒指的效果是{EFFECT_LABELS.get(effect, effect)}")

    target = str(param.get("map") or "")
    if target not in data.maps["maps"]:
        raise RingError("unknown_map", f"传送目标 {target!r} 不存在")
    if target == session.map_id:
        raise RingError("same_map", "已经在这张图了")

    cooldown = max(0, int(param.get("cooldown_ticks", 0)))
    state = _state(entity)
    if cooldown and int(tick) - int(state.last_travel_tick) < cooldown:
        raise RingError("cooling", f"冷却还剩 {cooldown - (int(tick) - state.last_travel_tick)} 刻")

    vitals = entity.get(Vitals)
    if vitals is not None and not vitals.alive:
        raise RingError("dead", "死亡状态不能传送")

    result = session.travel_to(target)
    state.last_travel_tick = int(tick)
    return result
