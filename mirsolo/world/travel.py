"""换图 —— 传送点判定与地图切换。

传奇的传送点是"走上去就过"，没有确认框，所以三件事必须做对，否则表现成
玩家看不懂的怪现象：

1. **落点不能压在传送点上**。目标图通往本图的入口本身就是一个传送点，
   落在同一格会立刻被判定成"踩到了"，于是刚进去就被弹回来，来回刷屏。
   落点要向地图内侧偏移（:data:`PORTAL_ARRIVE_OFFSET` 格），并且另加一个
   极短的免疫窗口（:data:`KEY_PORTAL_GRACE`）兜住极端地图数据。
2. **不能重建玩家**。``spawn_player`` 的 eid 固定为 ``"player"``，重建等于
   把血量、背包、等级、已学技能全部清空。换图只搬位置，不搬实体。
3. **旧图残留实体要清干净**。旧图的怪与召唤物属于旧图，留着会跟着玩家
   一起出现在新图里，掉落也记错地方。

``session.map_id`` 与 ``world.get_data("current_map")`` **必须同步**：
前者给表现层/HUD 用，后者是各系统（补刷、传送）读的。``travel`` 两个都写。
"""

from __future__ import annotations

from allin_engine import System, World

from mirsolo.actor.systems import KEY_TICK
from mirsolo.components import Position, Vitals
from mirsolo.data.loader import GameData
from mirsolo.world.spawn import spawn_map_monsters

EVT_MAP_CHANGED = "world:map_changed"
"""换图事件（payload 同 :func:`travel` 的返回值），表现层用它弹提示。"""

#: 到达后落在传送点内侧几格
PORTAL_ARRIVE_OFFSET = 3

#: 到达后免疫传送的截止刻（存 world data）。1 刻 = ``tick_dt`` 秒
PORTAL_GRACE_TICKS = 2
KEY_PORTAL_GRACE = "portal_grace_until"


class TravelError(ValueError):
    """换图被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def find_portal(data: GameData, map_id: str, x: float, y: float) -> dict | None:
    """返回 (x, y) 所在格上的传送点；没有则 ``None``。

    判定是**同格**（切比雪夫距离 0），不是"附近"——否则站在传送点旁边
    也会被传走，玩家会觉得地图在抽风。
    """
    spec = data.maps["maps"].get(map_id)
    if spec is None:
        return None
    tx, ty = int(round(x)), int(round(y))
    for portal in spec.get("portals") or []:
        at = portal.get("at") or [None, None]
        if at[0] is None or at[1] is None:
            continue
        if int(at[0]) == tx and int(at[1]) == ty:
            return portal
    return None


def entry_point(data: GameData, map_id: str, *, from_map: str | None = None
                ) -> tuple[int, int]:
    """进入 ``map_id`` 时的落点：``from_map`` 对应的入口内侧。

    目标图里 ``to == from_map`` 的那个传送点就是"回程口"，玩家应该从它
    旁边出现（原版也是这样：从矿洞回比奇，你站在比奇那个洞口边上）。
    找不到匹配入口时退回地图中心——总比落在随机格子上好解释。
    """
    spec = data.maps["maps"][map_id]
    size = spec["size"]
    cx, cy = int(size[0]) // 2, int(size[1]) // 2
    if from_map is None:
        return cx, cy
    back = next((p for p in spec.get("portals") or [] if p.get("to") == from_map),
                None)
    if back is None or not back.get("at"):
        return cx, cy

    px, py = int(back["at"][0]), int(back["at"][1])
    step = PORTAL_ARRIVE_OFFSET
    ax = px + (step if cx > px else -step if cx < px else 0)
    ay = py + (step if cy > py else -step if cy < py else 0)
    ax = max(0, min(int(size[0]) - 1, ax))
    ay = max(0, min(int(size[1]) - 1, ay))
    if find_portal(data, map_id, ax, ay) is not None:
        # 偏移后还是踩在别的传送点上（数据把传送点排得很密）：退回中心
        return cx, cy
    return ax, ay


def _clear_map_entities(session, *, keep_eid: str) -> int:
    """清掉除 ``keep_eid`` 外的所有实体，返回清除数量。"""
    removed = 0
    for eid in list(session.world.list_entity_ids()):
        if eid == keep_eid:
            continue
        session.world.remove_entity(eid)
        removed += 1
    return removed


def travel(session, target_map_id: str, *, from_map: str | None = None) -> dict:
    """把玩家搬到 ``target_map_id``。

    参数 ``session`` 只用到 ``data`` / ``world`` / ``seed`` / ``map_id``
    （鸭子类型，不 import ``GameSession``，避免与装配层互相依赖）。

    返回 ``{"from", "to", "at", "removed", "spawned"}``。
    """
    data: GameData = session.data
    maps = data.maps["maps"]
    if target_map_id not in maps:
        raise TravelError("unknown_map", f"未知地图 {target_map_id!r}")
    origin = session.map_id
    if target_map_id == origin:
        raise TravelError("same_map", f"已经在 {maps[origin]['name']} 了")

    player = session.world.get_entity("player")
    if player is None:
        raise TravelError("no_player", "世界里没有玩家实体")
    vitals = player.get(Vitals)
    if vitals is not None and not vitals.alive:
        raise TravelError("dead", "死亡状态不能换图")

    removed = _clear_map_entities(session, keep_eid=player.eid)

    session.map_id = target_map_id
    session.world.set_data("current_map", target_map_id)

    x, y = entry_point(data, target_map_id, from_map=from_map or origin)
    pos = player.get(Position)
    pos.x, pos.y = float(x), float(y)

    spawned = spawn_map_monsters(session.world, data, target_map_id, session.seed)
    tick = int(session.world.get_data(KEY_TICK, 0) or 0)
    session.world.set_data(KEY_PORTAL_GRACE, tick + PORTAL_GRACE_TICKS)
    return {"from": origin, "to": target_map_id, "at": (x, y),
            "removed": removed, "spawned": len(spawned)}


def use_portal_if_any(session) -> dict | None:
    """玩家此刻踩在传送点上的话就换图，返回 :func:`travel` 的结果。

    每 tick 调一次；不在传送点上返回 ``None``（绝不做任何"附近也算"的宽容）。
    """
    player = session.world.get_entity("player")
    if player is None:
        return None
    tick = int(session.world.get_data(KEY_TICK, 0) or 0)
    grace = int(session.world.get_data(KEY_PORTAL_GRACE, 0) or 0)
    if tick < grace:
        return None
    pos = player.get(Position)
    portal = find_portal(session.data, session.map_id, pos.x, pos.y)
    if portal is None:
        return None
    return travel(session, portal["to"], from_map=session.map_id)


class PortalSystem(System):
    """踩传送点换图。

    **做成系统而不是"窗口每帧调一次"**：窗口走 ``engine.advance()`` →
    ``FixedTimestepLoop`` → ``world.update()``，与 headless 的 ``session.step()``
    是两条入口。若把换图写在窗口里，headless 就永远测不到这条规则，
    "看得见的版本"和"测过的版本"立刻分叉（本项目已经栽过一次）。

    priority 95 = 全场最后跑：先让玩家这一 tick 的移动/攻击结算完，
    再判断脚下是不是传送点。会话从 ``world.get_data("session")`` 取
    （装配层注入），这样系统构造时不需要会话已存在。
    """

    priority = 95

    def update(self, world: World, dt: float) -> None:
        session = world.get_data("session")
        if session is None:
            return
        result = use_portal_if_any(session)
        if result is None:
            return
        bus = world.get_data("bus")
        if bus is not None:
            bus.emit(EVT_MAP_CHANGED, result)
