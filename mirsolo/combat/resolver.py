"""战斗结算器 —— 把纯函数公式接到 ECS 上（唯一允许"改血"的地方）。

设计取舍：**公式与施加分离**。

- ``combat/formulas.py`` 负责"该打多少"，纯函数、无副作用；
- 本模块负责"打到谁身上、发什么事件"，是公式在 ECS 上的唯一出口。

这样单测可以只测公式（不建 world），而端到端测试只测"血掉没掉"，
两边都不必互相将就。
"""

from __future__ import annotations

from dataclasses import dataclass

from allin_engine import Entity, EventBus, World

from mirsolo.combat import formulas
from mirsolo.components import (
    AiState,
    Attributes,
    Identity,
    Invisible,
    Poison,
    Shield,
    SiegeObjective,
    Vitals,
)
from mirsolo.determinism import Rng

EVT_HIT = "combat:hit"
EVT_MISS = "combat:miss"
EVT_DEATH = "combat:death"
EVT_HEAL = "combat:heal"
EVT_POISON = "combat:poison"


@dataclass
class CombatContext:
    """结算器依赖的最小环境：种子 + 世界 + 事件总线。"""

    seed: int
    world: World
    bus: EventBus | None = None
    tick: int = 0


def _stream(prefix: str, attacker: Entity, defender: Entity, tick: int) -> str:
    """随机流名：包含双方实体 id 与刻度 → 结果与调用顺序无关。"""
    return f"{prefix}:{attacker.id}->{defender.id}@{tick}"


class CombatResolver:
    """物理/魔法/道术三种攻击的统一入口。

    用法::

        resolver = CombatResolver(ctx)
        outcome = resolver.physical(player, mob, power_permille=1200)
    """

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx

    # ── 内部 ────────────────────────────────────────

    def _damage(self, source: Entity | None, defender: Entity, amount: int,
                kind: str, rolls: dict | None = None) -> int:
        """统一的掉血入口：**先扣盾、再扣血**，并只在血归零时发死亡。

        ``source`` 为 ``None`` 表示无归属伤害（例如施毒者已离场），
        此时仍会发死亡事件，但 ``attacker`` 为 ``None``——
        ``DeathRewardSystem`` 会跳过无归属者，不给经验。
        """
        vitals = defender.get(Vitals)
        if vitals is None or amount <= 0:
            return 0
        # 城门/旗台在非当前波次不吃伤害：没有这条，玩家可以绕过外城直接砍旗台，
        # 三波布防形同虚设
        objective = defender.get(SiegeObjective)
        if objective is not None and objective.invulnerable:
            return 0
        shield = defender.get(Shield)
        eaten = 0
        if shield is not None:
            eaten, amount = shield.absorb(amount)
        dealt = vitals.take(amount)
        # 挨打即锁定攻击者：passive 怪靠这条路还手，elite 也能被远程激怒
        if source is not None:
            ai = defender.get(AiState)
            if ai is not None:
                ai.target_id = source.id
        if self.ctx.bus is not None:
            self.ctx.bus.emit(
                EVT_HIT,
                {
                    "attacker": source.id if source is not None else None,
                    "defender": defender.id,
                    "kind": kind,
                    "damage": dealt,
                    "absorbed": eaten,
                    "hp_left": vitals.hp,
                    "rolls": dict(rolls or {}),
                    "tick": self.ctx.tick,
                },
            )
            if not vitals.alive:
                self.ctx.bus.emit(
                    EVT_DEATH,
                    {
                        "attacker": source.id if source is not None else None,
                        "defender": defender.id,
                        "killer_eid": source.eid if source is not None else None,
                        "victim_eid": defender.eid,
                        "tick": self.ctx.tick,
                    },
                )
        return dealt

    def _apply(self, attacker: Entity, defender: Entity, outcome, kind: str) -> None:
        """把结算结果落到 Vitals 上并发事件。"""
        # 主动出手即破除隐身（原版：一打就现形）
        attacker.remove(Invisible)
        if outcome.hit and outcome.damage > 0:
            self._damage(attacker, defender, outcome.damage, kind,
                         rolls=dict(outcome.rolls))
        elif not outcome.hit and self.ctx.bus is not None:
            self.ctx.bus.emit(
                EVT_MISS,
                {
                    "attacker": attacker.id,
                    "defender": defender.id,
                    "rolls": outcome.rolls,
                    "tick": self.ctx.tick,
                },
            )

    # ── 公共面 ──────────────────────────────────────

    def physical(self, attacker: Entity, defender: Entity, *, power_permille: int = 1000):
        """物理攻击（普攻 / 战士技能）。"""
        atk_attr = attacker.get(Attributes)
        def_attr = defender.get(Attributes)
        if atk_attr is None or def_attr is None:
            raise ValueError("物理攻击需要双方都有 Attributes 组件")
        rng = Rng(self.ctx.seed, _stream("physical", attacker, defender, self.ctx.tick))
        outcome = formulas.resolve_physical(
            dc=atk_attr.dc,
            accuracy=atk_attr.accuracy,
            ac=def_attr.ac,
            agility=def_attr.agility,
            rng=rng,
            power_permille=power_permille,
            luck=atk_attr.luck,
        )
        self._apply(attacker, defender, outcome, "physical")
        return outcome

    def magic(self, attacker: Entity, defender: Entity, *, power_permille: int = 1000,
              ignore_mac: bool = False):
        """魔法攻击（法师技能，必中，吃魔御）。"""
        atk_attr = attacker.get(Attributes)
        def_attr = defender.get(Attributes)
        if atk_attr is None or def_attr is None:
            raise ValueError("魔法攻击需要双方都有 Attributes 组件")
        rng = Rng(self.ctx.seed, _stream("magic", attacker, defender, self.ctx.tick))
        outcome = formulas.resolve_magic(
            mc=atk_attr.mc,
            ac=def_attr.ac,
            mac=def_attr.mac,
            rng=rng,
            power_permille=power_permille,
            ignore_mac=ignore_mac,
            luck=atk_attr.luck,
        )
        self._apply(attacker, defender, outcome, "magic")
        return outcome

    def taoist(self, attacker: Entity, defender: Entity, *, power_permille: int = 1000,
               ignore_mac: bool = False):
        """道术攻击（灵魂火符 / 施毒）。"""
        atk_attr = attacker.get(Attributes)
        def_attr = defender.get(Attributes)
        if atk_attr is None or def_attr is None:
            raise ValueError("道术攻击需要双方都有 Attributes 组件")
        rng = Rng(self.ctx.seed, _stream("taoist", attacker, defender, self.ctx.tick))
        outcome = formulas.resolve_taoist(
            sc=atk_attr.sc,
            mac=def_attr.mac,
            rng=rng,
            power_permille=power_permille,
            ignore_mac=ignore_mac,
            luck=atk_attr.luck,
        )
        self._apply(attacker, defender, outcome, "taoist")
        return outcome

    # ── 状态类（P1-S2） ─────────────────────────────

    def apply_poison(self, caster: Entity, target: Entity, *,
                     damage_per_tick: int, ticks: int,
                     interval: int = 10) -> Poison:
        """施毒：给目标挂持续伤害状态。

        毒本身不 roll（原版"红绿毒"是固定掉血/掉防），所以这里直接取表值；
        刷新策略：新的毒更强或来源不同就直接覆盖，避免叠多层互相打架。
        """
        current = target.get(Poison)
        if current is None:
            current = Poison()
            target.add(current)
        if (damage_per_tick >= current.damage_per_tick
                or current.remaining_ticks <= 0
                or current.source != caster.id):
            current.damage_per_tick = max(1, int(damage_per_tick))
            current.remaining_ticks = max(0, int(ticks))
            current.interval = max(1, int(interval))
            current.source = caster.id
        if self.ctx.bus is not None:
            self.ctx.bus.emit(EVT_POISON, {
                "caster": caster.id,
                "target": target.id,
                "damage_per_tick": current.damage_per_tick,
                "ticks": current.remaining_ticks,
                "tick": self.ctx.tick,
            })
        return current

    def apply_shield(self, caster: Entity, *, power_permille: int = 1000) -> Shield:
        """魔法盾：护盾池 = roll(魔法力) × 倍率；再结盾取较大值，不叠加。"""
        attr = caster.get(Attributes)
        shield = caster.get(Shield)
        if shield is None:
            shield = Shield()
            caster.add(shield)
        mc = attr.mc if attr is not None else (0, 0)
        rng = Rng(self.ctx.seed, f"shield:{caster.id}@{self.ctx.tick}")
        value = formulas.roll_damage_range(mc, rng) * power_permille // 1000
        shield.refresh(max(1, value))
        return shield

    def apply_invisible(self, caster: Entity, *, ticks: int) -> Invisible:
        """隐身：``until_tick = 当前刻 + ticks``；出手破隐（见 ``_apply``）。"""
        buff = caster.get(Invisible)
        if buff is None:
            buff = Invisible()
            caster.add(buff)
        buff.until_tick = int(self.ctx.tick) + max(0, int(ticks))
        return buff

    def tick_poison(self, defender: Entity) -> int:
        """跳一次毒：返回本跳造成的伤害（走 ``_damage``，因此过护盾）。"""
        poison = defender.get(Poison)
        if poison is None or poison.remaining_ticks <= 0:
            return 0
        source = self.ctx.world.get_entity(poison.source) if poison.source is not None else None
        dealt = self._damage(source, defender, poison.damage_per_tick, "poison")
        poison.remaining_ticks -= 1
        return dealt

    def heal(self, caster: Entity, target: Entity, *, power_permille: int = 1000) -> int:
        """治疗（道士治愈术）：返回实际恢复量。"""
        attr = caster.get(Attributes)
        vitals = target.get(Vitals)
        if attr is None or vitals is None:
            raise ValueError("治疗需要施法者 Attributes 与目标 Vitals")
        rng = Rng(self.ctx.seed, _stream("heal", caster, target, self.ctx.tick))
        amount = formulas.heal_amount(attr.sc, rng, power_permille)
        healed = vitals.heal(amount)
        if self.ctx.bus is not None and healed:
            self.ctx.bus.emit(
                EVT_HEAL,
                {"caster": caster.id, "target": target.id, "amount": healed,
                 "tick": self.ctx.tick},
            )
        return healed


def name_of(entity: Entity) -> str:
    """取显示名（缺 Identity 时回退 eid/id）。"""
    ident = entity.get(Identity)
    if ident is not None and ident.name:
        return ident.name
    return entity.eid or f"entity#{entity.id}"
