"""技能子系统 —— 学习、施放门控与结算路由。

分层与战斗一致：**本包不做"该打多少"的计算**（那在 ``combat/formulas``），
只负责把"谁、在什么条件下、以哪个技能、打到哪些人"翻译成一次
``CombatResolver`` 调用，并管好三道门：

1. **学习门**：职业匹配 + 等级门槛（``learn``）；
2. **资源门**：MP 消耗 + 技能冷却（``can_cast`` / 系统内扣除）；
3. **距离门**：切比雪夫距离 ≤ ``range``（超放是 bug，原版根本够不着）。

技能走与普攻同构的**意图队列**（``skill_intents``）：施放方只管投递，
:class:`SkillSystem` 统一校验、扣费、结算——顺序由队列 + tick 决定，
可回放、可复算，与"谁先按键"无关。
"""

from __future__ import annotations

from allin_engine import Entity, System, World

from mirsolo.components import (
    Cooldowns,
    Identity,
    Position,
    Progression,
    Skills,
    Vitals,
)
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.data.loader import SKILL_KINDS, GameData

KEY_SKILL_INTENTS = "skill_intents"

#: 引擎已实现的 kind（本批 P1-S1）
SUPPORTED_KINDS = frozenset({
    "physical", "physical_aoe", "magic", "magic_aoe", "taoist", "heal", "group_heal",
})
#: 数据表已落、行为待接的 kind（P1-S2：施毒/魔法盾/隐身/召唤）
PLANNED_KINDS = frozenset(SKILL_KINDS - SUPPORTED_KINDS)
#: 数据表允许的全部 kind = loader 里的数据面定义（单一事实来源）
ALL_KINDS = SKILL_KINDS

EVT_SKILL_CAST = "skill:cast"


class SkillError(ValueError):
    """技能相关拒绝。``code`` 是稳定的机器可读原因（测试/表现层据此提示）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, skill_id: str) -> dict:
    try:
        return data.skills["skills"][skill_id]
    except KeyError:
        raise SkillError("unknown_skill", f"未知技能 {skill_id!r}") from None


def learn(entity: Entity, data: GameData, skill_id: str) -> None:
    """学习技能。不满足门槛抛 :class:`SkillError`；已学会则幂等跳过。"""
    spec = _spec(data, skill_id)
    ident = entity.get(Identity)
    prog = entity.get(Progression)
    skills = entity.get(Skills)
    if ident is None or prog is None or skills is None:
        raise ValueError("learn 需要 Identity / Progression / Skills 组件")
    if ident.kind != "player":
        raise SkillError("not_a_player", "只有玩家可主动学习技能")
    if spec["class"] != prog.class_id:
        raise SkillError("class_mismatch",
                         f"{spec['name']} 是{spec['class']}技能，{prog.class_id} 无法学习")
    if prog.level < int(spec["level_req"]):
        raise SkillError("level_too_low",
                         f"{spec['name']} 需要 {spec['level_req']} 级，当前 {prog.level} 级")
    skills.known.setdefault(skill_id, 1)  # 熟练度 1 起，重复学习不覆盖进度


def can_cast(entity: Entity, data: GameData, skill_id: str) -> tuple[bool, str]:
    """施放前门控：``(是否可放, 拒绝码)``。拒绝码 ``ok`` 表示可放。"""
    spec = _spec(data, skill_id)
    vitals = entity.get(Vitals)
    skills = entity.get(Skills)
    cooldowns = entity.get(Cooldowns)
    if vitals is None or skills is None or cooldowns is None:
        return False, "missing_components"
    if not vitals.alive:
        return False, "dead"
    if spec["kind"] in PLANNED_KINDS:
        return False, "kind_not_implemented"  # P1-S2 接行为
    if skill_id not in skills.known:
        return False, "not_learned"
    if vitals.mp < int(spec.get("cost_mp", 0)):
        return False, "no_mp"
    if not cooldowns.ready(skill_id):
        return False, "cooldown"
    return True, "ok"


def _within(pos_a: Position, pos_b: Position, rng: int) -> bool:
    return max(abs(pos_a.x - pos_b.x), abs(pos_a.y - pos_b.y)) <= rng


def _faction_entities(world: World, faction: int, center: Position,
                      radius: int) -> list[Entity]:
    """以 ``center`` 为中心、切比雪夫 ``radius`` 内某阵营的全部活体。"""
    out: list[Entity] = []
    for entity in world.each(Identity, Position, Vitals):
        ident = entity.get(Identity)
        if ident.faction != faction or not entity.get(Vitals).alive:
            continue
        if _within(entity.get(Position), center, radius):
            out.append(entity)
    return out


class SkillSystem(System):
    """消费 ``skill_intents``：校验 → 扣费 → 按 kind 路由到战斗结算。"""

    priority = 15  # 选敌/走位之后、怪物出刀之前

    def __init__(self, data: GameData, ctx: CombatContext) -> None:
        self.data = data
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    # ── 路由 ────────────────────────────────────────

    def _resolve_kind(self, kind: str, caster: Entity, victims: list[Entity],
                      power: int) -> list[dict]:
        """对 victims 逐个结算，返回每刀的摘要（进事件，也方便断言）。"""
        results: list[dict] = []
        for victim in victims:
            if kind in ("physical", "physical_aoe"):
                outcome = self.resolver.physical(caster, victim, power_permille=power)
            elif kind in ("magic", "magic_aoe"):
                outcome = self.resolver.magic(caster, victim, power_permille=power)
            elif kind == "taoist":
                outcome = self.resolver.taoist(caster, victim, power_permille=power)
            elif kind in ("heal", "group_heal"):
                amount = self.resolver.heal(caster, victim, power_permille=power)
                outcome = {"heal": amount}
            else:
                continue  # PLANNED kind 在 can_cast 已拦截
            results.append({
                "target": victim.id,
                "kind": kind,
                **(outcome if isinstance(outcome, dict) else outcome.as_dict()),
            })
        return results

    # ── 系统入口 ────────────────────────────────────

    def update(self, world: World, dt: float) -> None:
        intents: list[dict] = world.get_data(KEY_SKILL_INTENTS, [])
        if not intents:
            return
        self.ctx.tick = int(world.get_data("tick", 0))
        rejected: list[dict] = []
        for intent in intents:
            caster = intent["caster"]
            try:
                self._process(world, caster, intent["skill"], intent.get("target"))
            except SkillError as exc:
                # 拒绝即丢弃（不重试）：冷却中按的技能键不能等冷却一过自动生效。
                # 原因通过事件给表现层弹提示，测试也可据此断言拒绝码。
                rejected.append({
                    "skill": intent["skill"],
                    "code": exc.code,
                    "reason": str(exc),
                    "tick": self.ctx.tick,
                })
        world.set_data(KEY_SKILL_INTENTS, [])
        if rejected and self.ctx.bus is not None:
            self.ctx.bus.emit("skill:rejected", {"items": rejected})

    def _process(self, world: World, caster: Entity, skill_id: str,
                 target: Entity | None) -> None:
        spec = _spec(self.data, skill_id)
        ok, code = can_cast(caster, self.data, skill_id)
        if not ok:
            raise SkillError(code, f"技能 {spec['name']} 不可施放：{code}")

        caster_pos = caster.get(Position)
        if caster_pos is None:
            raise SkillError("missing_components", "施放者没有 Position")

        kind = spec["kind"]
        rng = int(spec.get("range", 0))
        radius = int(spec.get("aoe_radius", 0))
        power = int(spec.get("power_permille", 1000))

        # 目标与距离门（heal/group_heal 的 target 为友方中心，可为施放者自身）
        if kind in ("heal", "group_heal"):
            if target is None:
                target = caster
            center = target.get(Position)
            if center is None:
                raise SkillError("bad_target", "治疗目标没有 Position")
            if not _within(caster_pos, center, rng):
                raise SkillError("out_of_range", f"{spec['name']} 距离超过 {rng} 格")
            victims = [e for e in _faction_entities(
                world, caster.get(Identity).faction, center,
                radius if kind == "group_heal" else 0)]
        else:
            if target is None or target is caster:
                raise SkillError("bad_target", f"{spec['name']} 需要一个敌对目标")
            center = target.get(Position)
            if center is None:
                raise SkillError("bad_target", "目标没有 Position")
            if not _within(caster_pos, center, rng):
                raise SkillError("out_of_range", f"{spec['name']} 距离超过 {rng} 格")
            if kind.endswith("_aoe"):
                victims = [e for e in _faction_entities(
                    world, target.get(Identity).faction, center, radius)]
            else:
                victims = [target]

        if not victims:
            raise SkillError("no_targets", f"{spec['name']} 范围内没有目标")

        # 门全过了：扣费 + 进冷却（ miss 也扣，与原版一致——空放就是空放 ）
        vitals = caster.get(Vitals)
        cost = int(spec.get("cost_mp", 0))
        if cost:
            vitals.mp = max(0, vitals.mp - cost)
        cooldown = float(spec.get("cooldown", 0.0))
        if cooldown > 0:
            caster.get(Cooldowns).remaining[skill_id] = cooldown

        results = self._resolve_kind(kind, caster, victims, power)
        if self.ctx.bus is not None:
            self.ctx.bus.emit(EVT_SKILL_CAST, {
                "caster": caster.id,
                "skill": skill_id,
                "kind": kind,
                "mp_cost": cost,
                "targets": results,
                "tick": self.ctx.tick,
            })


def queue_skill(world: World, caster: Entity, skill_id: str,
                target: Entity | None = None) -> None:
    """投递一个技能意图（表现层 / 测试 / AI 共用入口）。"""
    intents = list(world.get_data(KEY_SKILL_INTENTS, []))
    intents.append({"caster": caster, "skill": skill_id, "target": target})
    world.set_data(KEY_SKILL_INTENTS, intents)
