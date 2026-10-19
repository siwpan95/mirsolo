"""ECS 系统 —— 把战斗/成长/掉落接到 ``World.update()`` 的固定步长上。

系统清单（按 priority 从小到大执行）：

| priority | 系统 | 职责 |
|---:|---|---|
| 10 | ``CooldownSystem`` | 每帧递减技能冷却 |
| 11 | ``AiTargetSystem`` | 给主动怪选目标（同阵营跳过、超脱战距离放弃） |
| 12 | ``MovementSystem`` | 玩家八向网格移动（读 move_intent，按刻落格） |
| 14 | ``MobChaseSystem`` | 怪物追击（切比雪夫，八向一格） |
| 28 | ``MobAttackSystem`` | 相邻怪按自身攻速排队出刀 |
| 30 | ``AttackSystem`` | 消费攻击意图队列 → 调 ``CombatResolver`` 结算 |
| 40 | ``DeathRewardSystem`` | 结算死亡奖励：经验 + 掉落 + 金币 |
| 90 | ``RespawnSystem`` | 按地图刷新表补刷（P0 用固定 tick 间隔） |

**意图队列**是这一层的核心设计：AI/输入只往 ``world.get_data("attack_intents")``
里追加意图，由 ``AttackSystem`` 统一结算。好处是"谁先动手"不影响结果——
结算顺序由队列顺序 + tick 决定，回放可复算。
"""

from __future__ import annotations

from allin_engine import Entity, System, World

from mirsolo.combat.resolver import EVT_DEATH, CombatContext, CombatResolver
from mirsolo.components import (
    AiState,
    Attributes,
    Bag,
    Cooldowns,
    Identity,
    Invisible,
    Poison,
    Position,
    Progression,
    SummonOwner,
    Vitals,
    Wallet,
)
from mirsolo.determinism import Rng
from mirsolo.items.drops import roll_drops
from mirsolo.progression.exp import add_exp, apply_growth

KEY_TICK = "tick"
KEY_INTENTS = "attack_intents"
KEY_KILL_COUNTS = "kill_counts"
KEY_REWARDS = "reward_queue"
KEY_MOVE_INTENT = "move_intent"


def facing_from_delta(dx: int, dy: int) -> int:
    """八向朝向编号：0=北(-Y)，顺时针每 45° 加 1。

    与传奇客户端一致（北 = 0），因此渲染里"人物面向"可以直接用这个值。
    """
    if dx == 0 and dy == 0:
        return 0
    if dy > 0 and dx == 0:
        return 0
    if dy > 0 and dx > 0:
        return 1
    if dx > 0 and dy == 0:
        return 2
    if dx > 0 and dy < 0:
        return 3
    if dy < 0 and dx == 0:
        return 4
    if dy < 0 and dx < 0:
        return 5
    if dx < 0 and dy == 0:
        return 6
    return 7


class MovementSystem(System):
    """玩家八向网格移动：每 ``step_ticks`` 刻走一格。

    输入不直接改坐标，而是写进 ``world.set_data("move_intent", (dx, dy))``，
    由本系统按刻度统一落格——这样"按住方向键"不会因为帧率不同而走得更快，
    headless 与窗口版也永远一致。
    """

    priority = 12

    def __init__(self, step_ticks: int = 4) -> None:
        self.step_ticks = max(1, step_ticks)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.step_ticks:
            return
        intent = world.get_data(KEY_MOVE_INTENT) or (0, 0)
        dx, dy = int(intent[0]), int(intent[1])
        if dx == 0 and dy == 0:
            return
        player = world.get_entity("player")
        if player is None:
            return
        pos = player.get(Position)
        if pos is None:
            return
        map_id = world.get_data("current_map")
        data = world.get_data("data")
        spec = data.maps["maps"].get(map_id or "", {}) if data is not None else {}
        width, height = spec.get("size", (10**9, 10**9))
        pos.x = max(0, min(width - 1, pos.x + dx))
        pos.y = max(0, min(height - 1, pos.y + dy))
        pos.facing = facing_from_delta(dx, dy)


class MobChaseSystem(System):
    """怪物追击：锁定目标后每 ``step_ticks`` 刻走一格（切比雪夫，八向）。

    与原版一致：怪不会穿墙（P0 地图无墙，P1 接入地牢时这里换成 A*）。
    """

    priority = 14

    def __init__(self, step_ticks: int = 6) -> None:
        self.step_ticks = max(1, step_ticks)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.step_ticks:
            return
        for mob in world.each(AiState, Position):
            state = mob.get(AiState)
            pos = mob.get(Position)
            goal: Position | None = None
            if state.target_id is not None:
                target = world.get_entity(state.target_id)
                if target is not None:
                    goal = target.get(Position)
                else:
                    state.target_id = None
            # 脱战回巢：没有目标就走回出生点，否则怪会"追一半卡在原地"
            if goal is None:
                hx, hy = state.home
                if int(hx) == int(pos.x) and int(hy) == int(pos.y):
                    continue
                goal = Position(hx, hy)
            dx = int(max(-1, min(1, goal.x - pos.x)))
            dy = int(max(-1, min(1, goal.y - pos.y)))
            if dx == 0 and dy == 0:
                continue
            pos.x += dx
            pos.y += dy
            pos.facing = facing_from_delta(dx, dy)


class MobAttackSystem(System):
    """怪物反击：相邻即按自身攻速排队出刀（与玩家共用 ``AttackSystem`` 结算）。

    为什么怪物也要走意图队列而不是直接调 resolver：
    这样"同一刻玩家和怪同时出手"的先后顺序由队列决定，可回放、可复算。
    """

    priority = 28

    def __init__(self, data) -> None:
        self.data = data

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0:
            return
        intents = list(world.get_data(KEY_INTENTS, []))
        for mob in world.each(AiState, Position, Identity, Vitals):
            ident = mob.get(Identity)
            if ident.kind not in ("monster", "summon"):
                continue
            state = mob.get(AiState)
            if state.target_id is None:
                continue
            target = world.get_entity(state.target_id)
            if target is None:
                continue
            tpos = target.get(Position)
            pos = mob.get(Position)
            if max(abs(tpos.x - pos.x), abs(tpos.y - pos.y)) > 1:
                continue
            spec = self.data.monsters["monsters"].get(ident.species, {})
            interval = max(2, 12 - int(spec.get("attack_speed", 3)))
            if tick - state.last_attack_tick < interval:
                continue
            state.last_attack_tick = tick
            intents.append({"attacker": mob, "defender": target,
                            "kind": "physical", "power_permille": 1000})
        world.set_data(KEY_INTENTS, intents)


class RegenSystem(System):
    """自然回复：存活时每 ``interval`` 刻回 1 点 HP / MP。

    传奇的回血是按固定间隔跳的（不是每秒百分比），所以这里也用整数刻度，
    不做 ``dt`` 插值——插值会让"同一 tick 的存档"出现小数血，回放不干净。
    """

    priority = 5

    def __init__(self, interval: int = 20) -> None:
        self.interval = max(1, interval)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.interval:
            return
        for entity in world.each(Vitals):
            vitals = entity.get(Vitals)
            if not vitals.alive:
                continue
            if vitals.hp < vitals.max_hp:
                vitals.hp += 1
            if vitals.mp < vitals.max_mp:
                vitals.mp += 1


class CooldownSystem(System):
    """冷却递减（最便宜的系统，先跑）。"""

    priority = 10

    def update(self, world: World, dt: float) -> None:
        for entity in world.each(Cooldowns):
            entity.get(Cooldowns).tick(dt)


class AiTargetSystem(System):
    """极简选敌：锁定视野内最近的**敌对阵营**实体，超过脱战距离回巢。

    priority 必须**小于** ``MobChaseSystem``：先选目标再走位，
    否则追击永远慢一拍（每刻都在追上一刻的目标）。

    三个门控缺一不可：

    * ``vision``（切比雪夫）——**视野外不建立仇恨**。少了它，怪会盯住半张图外的
      玩家，走到脱战边界→丢目标→回巢→重新锁定，来回折返永不归位。
    * ``leash``（欧氏，相对出生点）——超过即放弃目标，由 ``MobChaseSystem`` 送回巢。
    * **隐身**——``Invisible.until_tick`` 未过期者一律不可选为目标。

    索敌按 ``faction`` 判定而非 ``kind``：这样道士的召唤骷髅（faction 0）
    能主动打怪，怪也会反过来打召唤物，不必为随从另写一套 AI。
    """

    priority = 11

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        candidates = [
            e for e in world.each(Identity, Position, Vitals)
            if e.get(Vitals).alive and not _is_invisible(e, tick)
        ]
        for mob in world.each(AiState, Identity, Position):
            state = mob.get(AiState)
            ident = mob.get(Identity)
            pos = mob.get(Position)
            if ident.kind not in ("monster", "summon"):
                continue
            home_x, home_y = state.home
            if (pos.x - home_x) ** 2 + (pos.y - home_y) ** 2 > state.leash ** 2:
                state.target_id = None
                continue
            best: Entity | None = None
            best_dist = float("inf")
            for other in candidates:
                if other is mob or other.get(Identity).faction == ident.faction:
                    continue
                opos = other.get(Position)
                if max(abs(opos.x - pos.x), abs(opos.y - pos.y)) > state.vision:
                    continue  # 视野外不索敌
                dist = (opos.x - pos.x) ** 2 + (opos.y - pos.y) ** 2
                if dist < best_dist:
                    best, best_dist = other, dist
            state.target_id = best.id if best is not None else None
            if best is not None:
                state.aggro = min(1000, state.aggro + 1)


def _is_invisible(entity: Entity, tick: int) -> bool:
    buff = entity.get(Invisible)
    return buff is not None and buff.until_tick > tick


class StatusSystem(System):
    """状态跳伤：施毒的每一跳都走 ``CombatResolver``（因此过护盾、能致死）。

    为什么单独一个系统而不是塞进 ``RegenSystem``：
    回血是"每个实体都跑"，跳毒是"只有中毒者跑"，且必须按各自 ``interval``
    错开——混在一起会让"回血 1 点 / 跳毒 3 点"的净变化变得不可预测。
    """

    priority = 13

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0:
            return
        for entity in world.each(Poison, Vitals):
            poison = entity.get(Poison)
            if poison.remaining_ticks <= 0:
                continue
            if tick % max(1, poison.interval):
                continue
            if not entity.get(Vitals).alive:
                poison.remaining_ticks = 0
                continue
            self.ctx.tick = tick
            self.resolver.tick_poison(entity)


class AttackSystem(System):
    """消费攻击意图并结算。

    意图格式::

        {"attacker": <Entity>, "defender": <Entity>, "kind": "physical",
         "power_permille": 1000}
    """

    priority = 30

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    def update(self, world: World, dt: float) -> None:
        intents: list[dict] = world.get_data(KEY_INTENTS, [])
        if not intents:
            return
        self.ctx.tick = int(world.get_data(KEY_TICK, 0))
        ready: list[dict] = []
        for intent in intents:
            attacker = intent["attacker"]
            defender = intent["defender"]
            vitals = defender.get(Vitals)
            atk_vitals = attacker.get(Vitals)
            if vitals is None or atk_vitals is None or not vitals.alive or not atk_vitals.alive:
                continue
            kind = intent.get("kind", "physical")
            power = int(intent.get("power_permille", 1000))
            if kind == "magic":
                self.resolver.magic(attacker, defender, power_permille=power)
            elif kind == "taoist":
                self.resolver.taoist(attacker, defender, power_permille=power)
            else:
                self.resolver.physical(attacker, defender, power_permille=power)
            ready.append(intent)
        # 消费已处理意图（保留未处理的，例如死亡目标）
        handled = {id(i) for i in ready}
        world.set_data(KEY_INTENTS, [i for i in intents if id(i) not in handled])


class DeathRewardSystem(System):
    """死亡奖励：经验按 ``monsters.json``、掉落按 ``drops.json``、金币入包。

    为什么订阅事件而不是每帧扫血量：死亡是**一次性事件**，
    用 :class:`allin_engine.EventBus` 订阅可以保证"只结算一次"，
    也避免系统之间靠轮询血量互相猜测。
    """

    priority = 40

    def __init__(self, *, data, seed: int) -> None:
        self.data = data
        self.seed = seed
        self._pending: list[dict] = []

    def bind(self, world: World) -> "DeathRewardSystem":
        """挂到世界的总线上（由 bootstrap 调用）。"""
        bus = world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_DEATH, self._on_death)
        self._world = world
        return self

    def _on_death(self, event: dict) -> None:
        self._pending.append(event)

    def update(self, world: World, dt: float) -> None:
        if not self._pending:
            return
        events, self._pending = self._pending, []
        for event in events:
            killer = world.get_entity(event.get("attacker"))
            victim = world.get_entity(event.get("defender"))
            if victim is None:
                continue
            ident = victim.get(Identity)
            species = ident.species if ident is not None else ""
            spec = self.data.monsters["monsters"].get(species, None)

            # 召唤物击杀：奖励记到主人头上（随从没背包没钱包，记它身上等于吞掉落）
            if killer is not None:
                owner_ref = killer.get(SummonOwner)
                if owner_ref is not None and owner_ref.owner_id is not None:
                    owner = world.get_entity(owner_ref.owner_id)
                    if owner is not None:
                        killer = owner

            # 召唤物尸体：没有怪表条目、也不发奖励，但必须清出场，否则越积越多
            if ident is not None and ident.kind == "summon":
                world.remove_entity(victim.id)
                continue

            if killer is None or ident is None or ident.kind != "monster" or spec is None:
                continue

            # 1) 经验
            prog = killer.get(Progression)
            if prog is not None:
                result = add_exp(self.data, prog, int(spec.get("exp", 0)))
                if result.levels_gained:
                    class_spec = self.data.classes["classes"][prog.class_id]
                    apply_growth(killer, class_spec)
                    world.set_data("last_level_up", {
                        "entity": killer.id,
                        "level": prog.level,
                        "tick": event.get("tick"),
                    })

            # 2) 掉落（kill_index 从世界状态取，存档后重打不刷）
            counts: dict = world.get_data(KEY_KILL_COUNTS, {})
            kill_index = int(counts.get(species, 0)) + 1
            counts[species] = kill_index
            world.set_data(KEY_KILL_COUNTS, counts)

            table = self.data.drops["drops"].get(species)
            if table:
                drop = roll_drops(table, seed=self.seed, monster_id=species,
                                  kill_index=kill_index)
                wallet = killer.get(Wallet)
                if wallet is not None:
                    wallet.gold += drop.gold
                bag = killer.get(Bag)
                if bag is not None:
                    for item_id, count in drop.items:
                        bag.add(item_id, count)
                world.set_data(KEY_REWARDS, list(world.get_data(KEY_REWARDS, [])) + [{
                    "monster": species,
                    "kill_index": kill_index,
                    "gold": drop.gold,
                    "items": list(drop.items),
                    "tick": event.get("tick"),
                }])

            # 3) 尸体移除（P0 不留尸体）
            world.remove_entity(victim.id)


class RespawnSystem(System):
    """按地图刷新表补刷怪物（P0 简化：固定 tick 间隔、原地附近随机点）。"""

    priority = 90

    def __init__(self, *, data, seed: int, tick_interval: int = 300) -> None:
        self.data = data
        self.seed = seed
        self.tick_interval = tick_interval
        self._serial = 0

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.tick_interval:
            return
        map_id = world.get_data("current_map")
        spec = self.data.maps["maps"].get(map_id or "")
        if spec is None:
            return
        for spawn in spec.get("spawns", []):
            monster_id = spawn["monster"]
            alive = 0
            corpses: list[int] = []
            for entity in list(world.each(Identity, Vitals)):
                ident = entity.get(Identity)
                if ident.kind != "monster" or ident.species != monster_id:
                    continue
                if entity.get(Vitals).alive:
                    alive += 1
                else:
                    corpses.append(entity.id)
            # 清掉没有走过死亡事件的尸体（例如测试里直接置零血），否则永远占着名额
            for entity_id in corpses:
                world.remove_entity(entity_id)
            want = int(spawn.get("count", 0))
            for _ in range(max(0, want - alive)):
                self._serial += 1
                rng = Rng(self.seed, f"spawn:{map_id}:{monster_id}:{self._serial}")
                x = rng.rand_int(0, int(spec["size"][0]) - 1)
                y = rng.rand_int(0, int(spec["size"][1]) - 1)
                from mirsolo.actor.factory import spawn_monster

                spawn_monster(world, self.data, monster_id, x=float(x), y=float(y))
