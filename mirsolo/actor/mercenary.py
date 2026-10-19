"""佣兵（P2-S6）—— 花金币雇一段时间的友军。

原版传奇里"雇用兵/虎卫/魔法侍卫"是盟重土城花钱雇的随从，跟着你打，
到点自动走人。单机里这套东西有三条约束，缺一条就不是佣兵、是外挂：

1. **要花金币，且价格不低**：佣兵是拿钱换战力，不是白送的宠物。价格按
   战力梯度拉开（8000 / 35000 / 150000），让"雇哪一种"本身是个决策。
2. **有等级门槛**：``level_req`` 卡住"1 级就雇魔法侍卫平推全图"。
   这一条是**唯一**挡住数值崩塌的东西——佣兵属性是按它自己的等级给的，
   不给门槛等于让低级号直接拿到 50 级战力。
3. **到点必走**：``duration_ticks`` 明确到期，不给永久随从。永久随从会让
   "佣兵"变成"第二个角色"，玩家从此不再需要考虑自己够不够强。

**为什么佣兵不是一种新实体种类**：它的身体（``Attributes``/``AiState``/
``Vitals``）与怪物**同构**，走 :func:`mirsolo.actor.factory._build_combatant`
同一条构造路径；只靠 ``Identity.kind="summon"`` + ``faction=0`` 就能让现有的
索敌/追击/出手/死亡清理全部生效。多造一个 "merc" 种类要改四处判定，
而四处里漏一处就表现成"佣兵不动手"或"尸体留在场上"。

**已知取舍（记在 docs/数据口径.md）**：换图会清掉除玩家外的所有实体
（``world.travel._clear_map_entities``），所以**佣兵与召唤物换图即消失**。
这是既有行为，不是佣兵引入的 bug——但面板上必须写清楚，否则玩家会以为
自己的 15 万金被吞了。
"""

from __future__ import annotations

from allin_engine import Entity, World

from mirsolo.components import (
    Identity,
    Mercenary,
    Position,
    Progression,
    SummonOwner,
    Wallet,
)
from mirsolo.data.loader import GameData

#: 同时带兵的**全局**上限（不论种类）。
#: 与数据表里的 ``max`` 是两件事：``max`` 卡"同一种别带一堆"（3 雇用兵 + 2 虎卫
#: 已经 5 个，各自都没超自己的 max），``SQUAD_LIMIT`` 卡"别带一个班"。
#: 没有它，理论上能站 3+2+1=6 个随从，画面上比怪还多。
SQUAD_LIMIT = 4

EVT_DISMISS = "mercenary:dismissed"
"""佣兵离队事件（到期解散）。载荷：``{entity, merc_id, name, tick}``。"""


class MercenaryError(ValueError):
    """雇佣被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, merc_id: str) -> dict:
    table = data.mercenaries.get("mercenaries", {})
    spec = table.get(merc_id)
    if spec is None:
        raise MercenaryError("unknown_merc", f"未知佣兵 {merc_id!r}")
    return spec


def squad(world: World, owner_id: int) -> list[Entity]:
    """列出 ``owner_id`` 名下**活着**的佣兵（含召唤物，两者共用归属组件）。

    按 eid 排序返回：遍历顺序依赖插入顺序会让"面板上第一行是谁"随雇佣顺序抖动，
    而同样的队伍在两次读档后显示成两排是很费解的。
    """
    found = []
    for entity in world.each(Mercenary, SummonOwner):
        ref = entity.get(SummonOwner)
        if ref.owner_id != owner_id:
            continue
        found.append(entity)
    found.sort(key=lambda e: e.eid)
    return found


def count_of_kind(world: World, owner_id: int, merc_id: str) -> int:
    """``owner_id`` 名下某一种佣兵已有几名。"""
    total = 0
    for entity in squad(world, owner_id):
        if entity.get(Mercenary).merc_id == merc_id:
            total += 1
    return total


def remaining_ticks(merc: Mercenary, tick: int) -> int:
    """还剩多少刻到期（已到期返回 0）。面板的倒计时读这一个函数。"""
    return max(0, int(merc.until_tick) - int(tick))


def hire(
    world: World,
    data: GameData,
    entity: Entity,
    merc_id: str,
    *,
    tick: int,
    dx: float = 1.0,
    dy: float = 0.0,
) -> dict:
    """雇佣：校验等级 → 名额 → 金币，然后扣钱并在雇主身旁造出佣兵。

    校验顺序刻意**先门槛后钱**：等级不够时说"等级不足"比说"金币不足"有用，
    因为前者是永久性的、后者攒攒就有了；反过来报会让人白刷钱。

    ``dx/dy`` 是出生点相对雇主的偏移（默认站在右边一格）。偏移而不是原地，
    是为了避免两个实体完全重合——重合时相邻判定与渲染都会变得难以解释。
    """
    spec = _spec(data, merc_id)
    wallet = entity.get(Wallet)
    prog = entity.get(Progression)
    if wallet is None or prog is None:
        raise MercenaryError("missing_components", "缺少 Wallet/Progression 组件")

    level_req = int(spec["level_req"])
    if int(prog.level) < level_req:
        raise MercenaryError(
            "level_too_low",
            f"等级不足：{spec['name']} 需要 {level_req} 级，你现在 {prog.level} 级",
        )

    owner_id = entity.id
    if len(squad(world, owner_id)) >= SQUAD_LIMIT:
        raise MercenaryError("squad_full", f"随从已达上限 {SQUAD_LIMIT} 名")
    kind_max = int(spec["max"])
    if count_of_kind(world, owner_id, merc_id) >= kind_max:
        raise MercenaryError("kind_full", f"{spec['name']} 最多同时带 {kind_max} 名")

    price = int(spec["price"])
    if wallet.gold < price:
        raise MercenaryError("no_gold", f"金币不足：需要 {price}，身上 {wallet.gold}")

    pos = entity.get(Position)
    x = float(pos.x) + dx if pos is not None else dx
    y = float(pos.y) + dy if pos is not None else dy

    from mirsolo.actor.factory import spawn_mercenary  # 延迟导入：打断 factory↔mercenary 的环

    wallet.gold -= price
    merc = spawn_mercenary(world, data, merc_id, x=x, y=y,
                           owner_id=owner_id, tick=tick)
    return {
        "merc": merc_id,
        "name": str(spec["name"]),
        "paid": price,
        "gold": wallet.gold,
        "until_tick": int(merc.get(Mercenary).until_tick),
        "eid": merc.eid,
    }


def dismiss_name(data: GameData, merc_id: str) -> str:
    """离队提示用的名字。查不到表就退回 id——提示宁可难看也不能抛异常。"""
    try:
        return str(_spec(data, merc_id)["name"])
    except MercenaryError:
        return merc_id


def roster(world: World, owner_id: int, tick: int) -> list[dict]:
    """队伍快照：给面板用的一行行字典（含剩余时间与种类名）。

    把"读组件 → 拼显示串"收在这里，面板就只负责排版；否则同一个剩余时间
    会在面板、提示、测试里算出三种写法。
    """
    rows = []
    for entity in squad(world, owner_id):
        merc = entity.get(Mercenary)
        ident = entity.get(Identity)
        rows.append({
            "eid": entity.eid,
            "merc": merc.merc_id,
            "name": ident.name if ident is not None else merc.merc_id,
            "remaining": remaining_ticks(merc, tick),
        })
    return rows
