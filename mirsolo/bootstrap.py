"""装配层 —— 用 allin-engine 的 ``GameEngine`` 把引擎、ECS、总线、系统接起来。

这是本项目**唯一**允许 new 出 ``GameEngine`` 的地方。其余模块只接受
:class:`GameSession`，因此换实现（比如以后接 pyglet 表现层）只改这一处。

装配顺序即依赖顺序::

    GameEngine().add_core()          # Context → World / EventBus / FixedTimestepLoop / WorldLog
      └─ World.set_data(...)         # 种子、数据表、总线、当前地图、意图队列
      └─ add_system(TickSystem)      # 刻度自增（最优先，所有随机流都吃 tick）
      └─ add_system(...)             # 冷却 → 选敌 → 攻击 → 死亡奖励 → 刷怪

``GameSession`` 额外暴露 :meth:`GameSession.run_ticks` —— 单测与 ``--smoke``
都靠它按固定步长推演，不依赖真实时钟，因此可复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from allin_engine import EventBus, FixedTimestepLoop, GameEngine, System, World

from mirsolo.actor.systems import (
    KEY_INTENTS,
    KEY_KILL_COUNTS,
    KEY_MOVE_INTENT,
    KEY_REWARDS,
    KEY_TICK,
    AiTargetSystem,
    AttackSystem,
    CooldownSystem,
    DeathRewardSystem,
    MobAttackSystem,
    MobChaseSystem,
    MovementSystem,
    RegenSystem,
    RespawnSystem,
    StatusSystem,
)
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.data.loader import GameData
from mirsolo.skills.engine import KEY_SKILL_INTENTS, SkillSystem
from mirsolo.siege.campaign import SiegeSystem
from mirsolo.world.travel import PortalSystem

DEFAULT_SEED = 20261019
DEFAULT_TICK_DT = 1.0 / 10.0  # 战斗逻辑 10Hz：传奇动作节奏本来就不快，整数结算更稳


class TickSystem(System):
    """刻度自增。所有随机流名都含 tick，因此它必须最先跑。"""

    priority = 1

    def update(self, world: World, dt: float) -> None:
        world.set_data(KEY_TICK, int(world.get_data(KEY_TICK, 0)) + 1)


@dataclass
class GameSession:
    """一次游戏会话：引擎 + 世界 + 数据 + 种子。"""

    engine: GameEngine
    world: World
    bus: EventBus
    data: GameData
    seed: int = DEFAULT_SEED
    map_id: str = "biqi_field"
    tick_dt: float = DEFAULT_TICK_DT
    resolver: CombatResolver | None = None
    loop: FixedTimestepLoop | None = None
    extras: dict = field(default_factory=dict)

    # ── 推演 ────────────────────────────────────────

    def step(self, ticks: int = 1) -> None:
        """按固定步长推进 ``ticks`` 次（不依赖真实时钟）。"""
        for _ in range(max(0, ticks)):
            self.world.update(self.tick_dt)

    @property
    def tick(self) -> int:
        return int(self.world.get_data(KEY_TICK, 0))

    # ── 输入面（AI 与玩家共用，保证同一条结算路径） ───

    def queue_attack(self, attacker, defender, *, kind: str = "physical",
                     power_permille: int = 1000) -> None:
        """把一次攻击意图塞进队列，下一次 ``step`` 统一结算。"""
        intents = list(self.world.get_data(KEY_INTENTS, []))
        intents.append({
            "attacker": attacker,
            "defender": defender,
            "kind": kind,
            "power_permille": power_permille,
        })
        self.world.set_data(KEY_INTENTS, intents)

    def cast_skill(self, caster, skill_id: str, target=None) -> None:
        """投递一个技能意图（校验/扣费/结算由 ``SkillSystem`` 在下一步统一做）。"""
        from mirsolo.skills.engine import queue_skill

        queue_skill(self.world, caster, skill_id, target)

    def travel_to(self, target_map_id: str) -> dict:
        """直接换图（走传送点由 ``PortalSystem`` 自动触发，这里是显式入口）。"""
        from mirsolo.world.travel import travel

        return travel(self, target_map_id)

    # ── 观测面（给 CLI / 测试 / 以后的 HUD） ──────────

    def rewards(self) -> list[dict]:
        return list(self.world.get_data(KEY_REWARDS, []))

    def kill_counts(self) -> dict:
        return dict(self.world.get_data(KEY_KILL_COUNTS, {}))

    def drain_rewards(self) -> list[dict]:
        """取走并清空奖励流水（表现层用它弹"获得物品"）。"""
        rewards = self.rewards()
        self.world.set_data(KEY_REWARDS, [])
        return rewards

    def shutdown(self) -> None:
        self.engine.shutdown()


def build_session(
    *,
    data: GameData | None = None,
    seed: int = DEFAULT_SEED,
    map_id: str = "biqi_field",
    tick_dt: float = DEFAULT_TICK_DT,
) -> GameSession:
    """装配一个可推演的会话（零 GL、零窗口，headless 友好）。"""
    game_data = data or GameData.load()
    if not game_data.ok:
        raise ValueError("数据表校验未通过:\n  - " + "\n  - ".join(game_data.errors))

    engine = GameEngine().add_core()
    world = engine.world
    bus = engine.bus
    assert world is not None and bus is not None and engine.loop is not None

    world.set_data("seed", seed)
    world.set_data("data", game_data)
    world.set_data("bus", bus)
    world.set_data(KEY_TICK, 0)
    world.set_data(KEY_INTENTS, [])
    world.set_data(KEY_SKILL_INTENTS, [])
    world.set_data(KEY_KILL_COUNTS, {})
    world.set_data(KEY_REWARDS, [])
    world.set_data(KEY_MOVE_INTENT, (0, 0))
    world.set_data("current_map", map_id)
    world.set_data("tick_dt", tick_dt)

    ctx = CombatContext(seed=seed, world=world, bus=bus, tick=0)

    death_system = DeathRewardSystem(data=game_data, seed=seed).bind(world)
    world.add_system(TickSystem())
    world.add_system(RegenSystem())
    world.add_system(CooldownSystem())
    world.add_system(StatusSystem(ctx))
    world.add_system(MovementSystem())
    world.add_system(SkillSystem(game_data, ctx))
    world.add_system(MobChaseSystem())
    world.add_system(AiTargetSystem())
    world.add_system(MobAttackSystem(game_data))
    world.add_system(AttackSystem(ctx))
    world.add_system(death_system)
    world.add_system(RespawnSystem(data=game_data, seed=seed, tick_dt=tick_dt))
    world.add_system(SiegeSystem(game_data, seed=seed, ctx=ctx))  # p50：伤害结算之后推进
    world.add_system(PortalSystem())  # 最后跑：先结算完这一 tick 的移动/攻击

    session = GameSession(
        engine=engine,
        world=world,
        bus=bus,
        data=game_data,
        seed=seed,
        map_id=map_id,
        tick_dt=tick_dt,
        resolver=CombatResolver(ctx),
        loop=engine.loop,
    )
    # 换图系统要拿会话（换图会改 session.map_id），但系统构造时会话还不存在，
    # 所以装配完再注入。headless 与窗口都走这条，不会分叉。
    world.set_data("session", session)
    return session


def populate(session: GameSession, *, player_class: str = "warrior",
             with_spawns: bool = True) -> dict:
    """按地图刷新表铺一遍怪，并放一个玩家。

    返回 ``{"player": Entity, "monsters": [Entity, ...]}``。
    刷怪走 :func:`mirsolo.world.spawn.spawn_map_monsters`（与换图同一条路径），
    否则"开局进去"与"换图进去"的怪会悄悄长成两个样。
    """
    from mirsolo.actor.factory import spawn_player
    from mirsolo.world.spawn import spawn_map_monsters

    map_spec = session.data.maps["maps"][session.map_id]
    player = spawn_player(session.world, session.data, class_id=player_class,
                          name="无名", x=float(map_spec["size"][0] // 2),
                          y=float(map_spec["size"][1] // 2))
    monsters = (spawn_map_monsters(session.world, session.data, session.map_id,
                                   session.seed) if with_spawns else [])
    return {"player": player, "monsters": monsters}
