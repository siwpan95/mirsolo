"""沙巴克攻城战役 —— 单机转化后的规则层。

原版是"行会争沙城"（玩家互砍 + 城门耐久 + 旗台归属），单机没有对手，
所以转化成一件事：**攻城小队 vs 守军分层布防**。

- 三个波次 = 三道据点：外城城门 → 内城门 → 旗台；
- 每波有自己的守军配置（数量/位置/增援），守卫是普通 ``monster``，
  走的是现有 AI（视野/脱战/档位），不另写一套；
- **推进权**：只有当前波次的目标可被打。没打下城门就去砍旗台？
  目标根本不吃伤害（``invulnerable``），而不是"砍得动但不结算"——
  后者会让玩家以为自己砍错了怪。
- 旗台倒下 = 攻城成功；玩家倒下或超时 = 失败。没有"平局"。

三条纪律：

1. **守军也是怪**（``kind="monster"``）：死亡奖励、掉落、AI 全部复用现有系统，
   这样攻城战的守军和野外怪不会出现"两种怪两套规则"的裂缝。
2. **城门没有 AI 组件**：它是死物，``AiTargetSystem`` / ``MobAttackSystem`` /
   ``MobChaseSystem`` 都按 ``AiState`` 或 ``kind`` 过滤，所以不会追人。
3. **超时与失败都要有明确出口**：``FAILED`` 状态下系统停止投放增援，
   并清掉场上的守军——否则玩家死了以后回来会看到一堆残局。
"""

from __future__ import annotations

from dataclasses import dataclass

from allin_engine import System, World

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import KEY_TICK
from mirsolo.combat.resolver import CombatResolver
from mirsolo.components import (
    Attributes,
    Bag,
    Body,
    Cooldowns,
    Identity,
    Position,
    Progression,
    SiegeObjective,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng
from mirsolo.progression.exp import add_exp, apply_growth

EVT_SIEGE_STARTED = "siege:started"
EVT_SIEGE_WAVE = "siege:wave"
EVT_SIEGE_OBJECTIVE_DOWN = "siege:objective_down"
EVT_SIEGE_WON = "siege:won"
EVT_SIEGE_LOST = "siege:lost"

KEY_SIEGE = "siege_state"

RUNNING = "running"
WON = "won"
LOST = "lost"

#: 守军的 eid 前缀（清场时按前缀删，不必给每个守军再加组件）
GUARD_PREFIX = "siege_guard"


@dataclass
class SiegeState:
    """一次攻城的运行状态。

    ``wave`` 是**当前**波次序号；打完最后一波的目标就 ``WON``。
    ``objective_id`` 是当前目标的 eid，系统靠它判断"目标倒了没"。
    """

    siege_id: str
    map_id: str
    wave: int = 0
    started_tick: int = 0
    status: str = RUNNING
    objective_id: str | None = None
    last_reinforce_tick: int = 0
    reinforced: int = 0

    @property
    def finished(self) -> bool:
        return self.status != RUNNING


class SiegeError(ValueError):
    """攻城操作被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _wave(spec: dict, index: int) -> dict:
    waves = spec.get("waves") or []
    if not 0 <= index < len(waves):
        raise SiegeError("bad_wave", f"波次 {index} 不存在")
    return waves[index]


def spawn_objective(world: World, data: GameData, spec: dict, wave_index: int,
                    eid: str | None = None) -> object:
    """把某一波的目标（城门/旗台）放进世界。

    目标用 ``kind="guard"``（表现层按这个画城门）而不是 ``"monster"``：
    死亡奖励系统只认 ``kind == "monster"``，目标倒下不该掉经验/掉落——
    奖励由战役结算统一发。
    """
    wave = _wave(spec, wave_index)
    at = wave["objective_at"]
    hp = int(wave["objective_hp"])
    entity_id = eid or f"siege_obj:{spec.get('map')}:{wave_index}"
    entity = world.create_entity(entity_id)
    entity.add(Identity(name=wave["name"], kind="guard", faction=1,
                        species=wave["objective"]))
    entity.add(Position(x=float(at[0]), y=float(at[1])))
    entity.add(Vitals(hp=hp, max_hp=hp, mp=0, max_mp=0))
    entity.add(Attributes(dc=(1, 2), ac=(0, 0), mac=(0, 0), accuracy=5, agility=5))
    entity.add(SiegeObjective(
        siege_id=spec.get("map", ""), wave=wave_index,
        objective=wave["objective"], hp=hp, max_hp=hp,
    ))
    return entity


def spawn_wave_guards(world: World, data: GameData, spec: dict, wave_index: int,
                      *, seed: int) -> list:
    """铺一波守军，返回实体列表。

    坐标用 ``Rng(seed, f"siege:{map}:{wave}:{serial}")`` 抖开——
    同一场战役里同样的种子必须铺出同样的阵，否则"重开一次怪站的位置不一样"，
    玩家会以为地图随机生成。
    """
    wave = _wave(spec, wave_index)
    map_spec = data.maps["maps"][spec["map"]]
    width, height = map_spec["size"]
    guards = []
    serial = 0
    for group in wave.get("guards", []):
        base_x, base_y = group["at"]
        spread = int(group.get("spread", 0))
        for _ in range(int(group["count"])):
            serial += 1
            rng = Rng(seed, f"siege:{spec['map']}:{wave_index}:{serial}")
            dx = rng.rand_int(-spread, spread) if spread else 0
            dy = rng.rand_int(-spread, spread) if spread else 0
            x = max(0, min(int(width) - 1, int(base_x) + dx))
            y = max(0, min(int(height) - 1, int(base_y) + dy))
            guard = spawn_monster(world, data, group["monster"], x=float(x), y=float(y),
                                  eid=f"{GUARD_PREFIX}:{spec['map']}:{wave_index}:{serial}")
            guards.append(guard)
    return guards


def _clear_siege_entities(world: World, *, wave: int | None = None) -> int:
    """清掉所有守军与目标（按 eid 前缀 + SiegeObjective 双条件）。

    ``wave`` 给定时**只清该波次**（``siege_guard:{map}:{wave}:*``）——
    推进波次时用这个，让上一波的守军跟着城门一起退场，
    否则打到最后会发现"三道门的兵全堆在旗台下面"。
    """
    removed = 0
    for eid in list(world.list_entity_ids()):
        if not eid.startswith((f"{GUARD_PREFIX}:", "siege_obj:")):
            continue
        if wave is not None:
            # siege_guard:{map}:{wave}:{serial} / siege_obj:{map}:{wave}
            # 两种前缀之后都是 map，所以波次在下标 2
            parts = eid.split(":")
            if len(parts) <= 2 or parts[2] != str(wave):
                continue
        world.remove_entity(eid)
        removed += 1
    for entity in list(world.each(SiegeObjective)):
        if not world.has_entity(entity.eid):
            continue
        if wave is not None and entity.get(SiegeObjective).wave != wave:
            continue
        world.remove_entity(entity.eid)
        removed += 1
    return removed


def _retire_wave(world: World, state: "SiegeState") -> int:
    """一波打完：把这一波的守军与目标清出场。

    目标（城门/旗台）走 ``kind="guard"``，``DeathRewardSystem`` 会跳过它
    （不给奖励、也不移除尸体——见那里的 ``kind != "monster"`` 守卫），
    所以"目标倒了就该消失"这件事必须由战役自己负责。
    守军同理：不 retire 的话第二波会变成"上一波残兵 + 新一波"。
    """
    return _clear_siege_entities(world, wave=state.wave)


def start_siege(session, siege_id: str = "sabak") -> dict:
    """开始攻城：换图 → 铺第一波守军与目标 → 记状态。

    ``session`` 只用到 ``data`` / ``world`` / ``map_id``（鸭子类型，
    与 ``world.travel`` 同一套约定，避免与装配层循环依赖）。
    """
    data: GameData = session.data
    spec = data.sieges["sieges"].get(siege_id)
    if spec is None:
        raise SiegeError("unknown_siege", f"未知战役 {siege_id!r}")
    world = session.world

    current = world.get_data(KEY_SIEGE)
    if current is not None and current.status == RUNNING:
        raise SiegeError("already_running", f"{spec['name']} 正在进行")

    # 先清上一场残局，再换图（换图本身也会清实体，但目标 eid 是固定前缀，
    # 清两遍是幂等的，宁可多清也不留幽灵城门）
    _clear_siege_entities(world)
    world.set_data(KEY_SIEGE, None)

    from mirsolo.world.travel import travel

    if session.map_id != spec["map"]:
        travel(session, spec["map"], from_map=spec.get("base_map"))

    # 落点：数据表给的 player_spawn（换图落点是"回程口内侧"，攻城要从城外开始）
    player = world.get_entity("player")
    spawn_at = spec.get("player_spawn")
    if player is not None and spawn_at:
        pos = player.get(Position)
        pos.x, pos.y = float(spawn_at[0]), float(spawn_at[1])

    state = SiegeState(siege_id=siege_id, map_id=spec["map"],
                       started_tick=int(world.get_data(KEY_TICK, 0) or 0))
    world.set_data(KEY_SIEGE, state)

    objective = spawn_objective(world, data, spec, 0)
    guards = spawn_wave_guards(world, data, spec, 0, seed=session.seed)
    state.objective_id = objective.eid

    bus = world.get_data("bus")
    payload = {"siege": siege_id, "name": spec["name"], "wave": 0,
               "wave_name": _wave(spec, 0)["name"], "objective": _wave(spec, 0)["objective"],
               "guards": len(guards), "objective_hp": int(_wave(spec, 0)["objective_hp"])}
    if bus is not None:
        bus.emit(EVT_SIEGE_STARTED, payload)
        bus.emit(EVT_SIEGE_WAVE, payload)
    return {"siege": siege_id, "map": spec["map"], "wave": 0,
            "guards": len(guards), "objective": objective.eid,
            "objective_hp": objective.get(Vitals).max_hp}


def _advance_wave(world: World, data: GameData, spec: dict, state: SiegeState,
                  seed: int) -> dict | None:
    """目标倒下：进下一波；已经是最后一波就判胜。"""
    next_index = state.wave + 1
    waves = spec.get("waves") or []
    bus = world.get_data("bus")

    if next_index >= len(waves):
        state.status = WON
        _retire_wave(world, state)
        state.objective_id = None
        _grant_rewards(world, data, spec)
        payload = {"siege": state.siege_id, "name": spec["name"],
                   "tick": int(world.get_data(KEY_TICK, 0) or 0)}
        if bus is not None:
            bus.emit(EVT_SIEGE_WON, payload)
        return payload

    # 先让上一波退场（城门 + 守军），再铺下一波
    _retire_wave(world, state)
    state.wave = next_index
    state.last_reinforce_tick = int(world.get_data(KEY_TICK, 0) or 0)
    state.reinforced = 0
    objective = spawn_objective(world, data, spec, next_index)
    guards = spawn_wave_guards(world, data, spec, next_index, seed=seed)
    state.objective_id = objective.eid
    payload = {"siege": state.siege_id, "name": spec["name"], "wave": next_index,
               "wave_name": _wave(spec, next_index)["name"],
               "objective": _wave(spec, next_index)["objective"],
               "guards": len(guards), "objective_hp": int(_wave(spec, next_index)["objective_hp"])}
    if bus is not None:
        bus.emit(EVT_SIEGE_WAVE, payload)
    return payload


def _grant_rewards(world: World, data: GameData, spec: dict) -> None:
    """攻城奖励：金币 + 物品 + 经验（走 add_exp/apply_growth，与打怪同一条路径）。"""
    rewards = spec.get("rewards") or {}
    player = world.get_entity("player")
    if player is None:
        return
    wallet = player.get(Wallet)
    if wallet is not None:
        wallet.gold += int(rewards.get("gold", 0))
    bag = player.get(Bag)
    if bag is not None:
        for entry in rewards.get("items", []):
            bag.add(entry["item"], int(entry.get("count", 1)))
    body = player.get(Body)
    if body is not None:
        pass  # 负重上限由装备重算管，这里不动（避免与 recompute 抢职责）
    prog = player.get(Progression)
    exp = int(rewards.get("exp", 0))
    if prog is not None and exp > 0:
        result = add_exp(data, prog, exp)
        if result.levels_gained:
            apply_growth(player, data.classes["classes"][prog.class_id])
            from mirsolo.items.equipment import recompute

            recompute(player, data)


def _fail(world: World, spec: dict, state: SiegeState) -> dict:
    state.status = LOST
    state.objective_id = None
    _clear_siege_entities(world)
    payload = {"siege": state.siege_id, "name": spec["name"],
               "tick": int(world.get_data(KEY_TICK, 0) or 0)}
    bus = world.get_data("bus")
    if bus is not None:
        bus.emit(EVT_SIEGE_LOST, payload)
    return payload


class SiegeSystem(System):
    """战役推进：目标倒下 → 下一波；玩家倒下/超时 → 失败；增援按间隔投放。

    priority 50：**在死亡奖励（40）之后、补刷（90）之前**。目标倒下要等
    这一 tick 的伤害结算完才判定，否则会读到"打之前"的血量。
    """

    priority = 50

    def __init__(self, data: GameData, *, seed: int, ctx=None) -> None:
        self.data = data
        self.seed = seed
        self.ctx = ctx

    def update(self, world: World, dt: float) -> None:
        state: SiegeState | None = world.get_data(KEY_SIEGE)
        if state is None or state.status != RUNNING:
            return
        spec = self.data.sieges["sieges"].get(state.siege_id)
        if spec is None:
            return
        tick = int(world.get_data(KEY_TICK, 0) or 0)
        player = world.get_entity("player")

        # 0) 每 tick 刷一次"准入标记"：只有当前目标可打，其余目标锁住。
        #    在这里做而不是单开一个系统：推进和准入必须以**同一份波次状态**为准，
        #    两处各算一遍就会出现"砍得动但不结算"这种最让人困惑的中间态。
        for entity in world.each(SiegeObjective):
            objective = entity.get(SiegeObjective)
            current = (entity.eid == state.objective_id and objective.wave == state.wave)
            objective.invulnerable = not current

        # 1) 玩家倒下 → 失败（先判，别让"刚死又被打掉旗台"算成胜利）
        if player is None or not player.get(Vitals).alive:
            _fail(world, spec, state)
            return

        # 2) 超时 → 失败
        limit = int(spec.get("time_limit_ticks", 0))
        if limit and tick - state.started_tick >= limit:
            _fail(world, spec, state)
            return

        # 3) 目标是否倒下
        objective = world.get_entity(state.objective_id) if state.objective_id else None
        if objective is None:
            # 目标实体被清掉了（例如换图清场）：按失败处理，不留"无目标"的僵局
            _fail(world, spec, state)
            return
        obj = objective.get(SiegeObjective)
        vitals = objective.get(Vitals)
        if obj is not None:
            objective_hp = vitals.hp if vitals is not None else obj.hp
        else:
            objective_hp = vitals.hp if vitals is not None else 1
        if objective_hp <= 0:
            bus = world.get_data("bus")
            if bus is not None:
                bus.emit(EVT_SIEGE_OBJECTIVE_DOWN, {
                    "siege": state.siege_id, "wave": state.wave,
                    "objective": _wave(spec, state.wave)["objective"],
                    "tick": tick,
                })
            _advance_wave(world, self.data, spec, state, self.seed)
            return

        # 4) 增援：按波次配置的间隔投放，上限卡住，避免无限堆怪
        wave = _wave(spec, state.wave)
        reinforce = wave.get("reinforce")
        if reinforce:
            every = int(reinforce["every_ticks"])
            if tick - state.last_reinforce_tick >= every and state.reinforced < int(reinforce["max"]):
                state.last_reinforce_tick = tick
                state.reinforced += 1
                serial = state.reinforced + state.wave * 100
                rng = Rng(self.seed, f"siege:reinforce:{state.siege_id}:{serial}")
                map_spec = self.data.maps["maps"][spec["map"]]
                width, height = map_spec["size"]
                at = wave.get("objective_at", [0, 0])
                x = max(0, min(int(width) - 1, int(at[0]) + rng.rand_int(-4, 4)))
                y = max(0, min(int(height) - 1, int(at[1]) + rng.rand_int(-4, 4)))
                for index in range(int(reinforce["count"])):
                    spawn_monster(world, self.data, reinforce["monster"], x=float(x + index),
                                  y=float(y),
                                  eid=f"{GUARD_PREFIX}:{spec['map']}:r{state.wave}:{serial}:{index}")


def siege_status(world: World) -> dict | None:
    """给 HUD / 测试读的战役快照。"""
    state: SiegeState | None = world.get_data(KEY_SIEGE)
    if state is None:
        return None
    data: GameData | None = world.get_data("data")
    spec = data.sieges["sieges"].get(state.siege_id, {}) if data is not None else {}
    waves = spec.get("waves") or []
    wave = waves[state.wave] if 0 <= state.wave < len(waves) else {}
    objective_hp = 0
    objective_max = 0
    if state.objective_id:
        entity = world.get_entity(state.objective_id)
        if entity is not None:
            vitals = entity.get(Vitals)
            if vitals is not None:
                objective_hp, objective_max = vitals.hp, vitals.max_hp
    # 场上还剩几个守军：玩家判断"要不要先把杂兵清掉"的唯一依据
    guards_alive = 0
    for entity in world.each(Identity, Vitals):
        ident = entity.get(Identity)
        if ident.kind == "monster" and entity.get(Vitals).alive:
            guards_alive += 1
    return {
        "siege": state.siege_id,
        "name": spec.get("name", state.siege_id),
        "status": state.status,
        "wave": state.wave,
        "wave_count": len(waves),
        "wave_name": wave.get("name", ""),
        "objective": wave.get("objective", ""),
        "objective_name": {
            "gate": "外城城门", "inner_gate": "内城城门", "banner": "旗台",
        }.get(wave.get("objective", ""), wave.get("objective", "")),
        "objective_hp": objective_hp,
        "objective_max": objective_max,
        "guards_alive": guards_alive,
        "elapsed": int(world.get_data(KEY_TICK, 0) or 0) - state.started_tick,
        "limit": int(spec.get("time_limit_ticks", 0)),
        "finished": state.finished,
    }
