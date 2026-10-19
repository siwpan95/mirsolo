"""ECS 系统 —— 把战斗/成长/掉落接到 ``World.update()`` 的固定步长上。

系统清单（按 priority 从小到大执行）：

| priority | 系统 | 职责 |
|---:|---|---|
| 10 | ``CooldownSystem`` | 每帧递减技能冷却 |
| 20 | ``AiTargetSystem`` | 给主动怪选目标（同阵营跳过、超脱战距离放弃） |
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
    Position,
    Progression,
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
    """极简选敌：主动怪锁定同图最近的敌对实体，超过脱战距离回巢。"""

    priority = 20

    def update(self, world: World, dt: float) -> None:
        players = [e for e in world.each(Identity, Position)
                   if e.get(Identity).kind == "player"]
        for mob in world.each(AiState, Identity, Position):
            state = mob.get(AiState)
            ident = mob.get(Identity)
            pos = mob.get(Position)
            if ident.kind != "monster":
                continue
            home_x, home_y = state.home
            if (pos.x - home_x) ** 2 + (pos.y - home_y) ** 2 > state.leash ** 2:
                state.target_id = None
                continue
            best: Entity | None = None
            best_dist = float("inf")
            for player in players:
                if player.get(Identity).faction == ident.faction:
                    continue
                ppos = player.get(Position)
                dist = (ppos.x - pos.x) ** 2 + (ppos.y - pos.y) ** 2
                if dist < best_dist:
                    best, best_dist = player, dist
            state.target_id = best.id if best is not None else None
            if best is not None:
                state.aggro = min(1000, state.aggro + 1)


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
            if killer is None or victim is None:
                continue
            ident = victim.get(Identity)
            species = ident.species if ident is not None else ""
            spec = self.data.monsters["monsters"].get(species, None)
            if ident is None or ident.kind != "monster" or spec is None:
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
