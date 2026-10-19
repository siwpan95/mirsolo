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
from mirsolo.components import Attributes, Identity, Vitals
from mirsolo.determinism import Rng

EVT_HIT = "combat:hit"
EVT_DEATH = "combat:death"


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

    def _apply(self, attacker: Entity, defender: Entity, outcome, kind: str) -> None:
        """把结算结果落到 Vitals 上并发事件。"""
        if outcome.hit and outcome.damage > 0:
            vitals = defender.get(Vitals)
            if vitals is not None:
                dealt = vitals.take(outcome.damage)
                if self.ctx.bus is not None:
                    self.ctx.bus.emit(
                        EVT_HIT,
                        {
                            "attacker": attacker.id,
                            "defender": defender.id,
                            "kind": kind,
                            "damage": dealt,
                            "hp_left": vitals.hp,
                            "rolls": outcome.rolls,
                            "tick": self.ctx.tick,
                        },
                    )
                if not vitals.alive and self.ctx.bus is not None:
                    self.ctx.bus.emit(
                        EVT_DEATH,
                        {
                            "attacker": attacker.id,
                            "defender": defender.id,
                            "killer_eid": attacker.eid,
                            "victim_eid": defender.eid,
                            "tick": self.ctx.tick,
                        },
                    )
        elif not outcome.hit and self.ctx.bus is not None:
            self.ctx.bus.emit(
                "combat:miss",
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
        )
        self._apply(attacker, defender, outcome, "taoist")
        return outcome

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
                "combat:heal",
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
