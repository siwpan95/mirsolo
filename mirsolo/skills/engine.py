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
    AiState,
    Bag,
    Cooldowns,
    Identity,
    Position,
    Progression,
    Skills,
    SummonOwner,
    Vitals,
)
from mirsolo.actor.systems import (
    KEY_INTENTS,
    mark_swing,
    player_attack_interval,
    swing_ready_at,
)
from mirsolo.actor.factory import spawn_monster
from mirsolo.combat.resolver import (
    KEY_ATTACK_PACE,
    CombatContext,
    CombatResolver,
    chebyshev,
)
from mirsolo.data.loader import SKILL_KINDS, GameData

KEY_SKILL_INTENTS = "skill_intents"

#: 引擎已实现的 kind（P1-S1 伤害/治疗 + P1-S2 施毒/护盾/隐身/召唤）
SUPPORTED_KINDS = frozenset({
    "physical", "physical_aoe", "magic", "magic_aoe", "taoist", "heal", "group_heal",
    "poison", "buff_shield", "buff_invis", "summon",
})
#: 数据表已落、行为待接的 kind —— P1-S2 后应为空；不为空说明数据跑在实现前面
PLANNED_KINDS = frozenset(SKILL_KINDS - SUPPORTED_KINDS)
#: 数据表允许的全部 kind = loader 里的数据面定义（单一事实来源）
ALL_KINDS = SKILL_KINDS

EVT_SKILL_CAST = "skill:cast"
EVT_SKILL_REJECTED = "skill:rejected"

#: 熟练度上限（1 起，练满 3 级；P2 再接伤害/命中加成）
MAX_PROFICIENCY = 3

#: 只能对自己施放的 kind（护盾 / 隐身 / 召唤）
SELF_KINDS = frozenset({"buff_shield", "buff_invis", "summon"})

#: **攻击动作**的 kind —— 与普攻共用同一个攻击窗口（``KEY_ATTACK_PACE``）。
#:
#: 分类依据是数据（``skills.json`` 的 ``kind``），不是技能 id 白名单：
#: 白名单每加一门技能都要人记一次，漏了就是静默绕过窗口。
#:
#: 这六类都是"朝敌人出手、结算伤害或挂伤害状态"：物/魔/道三条伤害路径
#: （含 AoE 变体）+ 施毒（它这一刻也是朝敌人投一次，杀伤在后续跳伤上）。
#: 修前它们**完全不过攻速门**：``gongsha``（cd 0、耗蓝 0 的 1.15 倍技）
#: 每刻可放，实测 60 刻 60 次——刚给普攻关上的门从技能这头原样敞开。
ATTACK_KINDS = frozenset({
    "physical", "physical_aoe", "magic", "magic_aoe", "taoist", "poison",
})
#: **不是攻击动作**的 kind —— 不占攻击窗口。
#:
#: 治疗 / 群体治疗 / 护盾 / 隐身 / 召唤。让它们占窗口会变成"放个魔法盾
#: 之后 10 刻不能普攻"——盾、药、召唤都不是挥刀，手感上说不通。
NON_ATTACK_KINDS = frozenset(SKILL_KINDS - ATTACK_KINDS)
# 两边必须正好把 ``SKILL_KINDS`` 分完（新增 kind 必须同时决定它算不算）。
# 写成跑时断言而不是测试里的一条：这条是**数据面**约束，loader 校验阶段
# 就该炸，而不是等到某个用例才发现。
assert ATTACK_KINDS | NON_ATTACK_KINDS == SKILL_KINDS
assert ATTACK_KINDS & NON_ATTACK_KINDS == frozenset()


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
    """学习技能：职业 → 等级 → **技能书**，三道门全过才习得。

    技能书是原版的核心约束（书要靠打/买），所以除了等级还必须真的持有
    对应道具；学会即消耗一本。已学会则幂等跳过（不重复吃书）。
    """
    spec = _spec(data, skill_id)
    ident = entity.get(Identity)
    prog = entity.get(Progression)
    skills = entity.get(Skills)
    if ident is None or prog is None or skills is None:
        raise ValueError("learn 需要 Identity / Progression / Skills 组件")
    if ident.kind != "player":
        raise SkillError("not_a_player", "只有玩家可主动学习技能")
    if skill_id in skills.known:
        return  # 幂等：不重复吃书、不覆盖熟练度
    if spec["class"] != prog.class_id:
        raise SkillError("class_mismatch",
                         f"{spec['name']} 是{spec['class']}技能，{prog.class_id} 无法学习")
    if prog.level < int(spec["level_req"]):
        raise SkillError("level_too_low",
                         f"{spec['name']} 需要 {spec['level_req']} 级，当前 {prog.level} 级")
    book_id = spec.get("book")
    if book_id:
        bag = entity.get(Bag)
        if bag is None:
            raise SkillError("no_bag", "没有背包，无法携带技能书")
        if bag.items.get(book_id, 0) <= 0:
            book_name = data.items["items"].get(book_id, {}).get("name", book_id)
            raise SkillError("missing_book", f"{spec['name']} 需要技能书 {book_name}")
        bag.remove(book_id, 1)
    skills.known[skill_id] = 1  # 熟练度 1 起


def can_cast(entity: Entity, data: GameData, skill_id: str) -> tuple[bool, str]:
    """施放前门控：``(是否可放, 拒绝码)``。拒绝码 ``ok`` 表示可放。

    这里只判**资源/学习**那几道门——不需要"现在第几刻"这个上下文。
    攻击窗口（第几刻、上一次出手是第几刻）由 :meth:`SkillSystem._process`
    在系统内用同一本 ``KEY_ATTACK_PACE`` 账判，读法与普攻共用
    :func:`mirsolo.actor.systems.swing_ready_at`。
    """
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


def reset_cooldowns(entity: Entity) -> None:
    """清空技能冷却账 —— 换图与读档**共用**这一处。

    语义是"进图即清"：冷却记的是"此刻正在发生什么"（上一次出手的余韵），
    而换图 / 读档都等于重新进图，进图之前攒下的那截冷却不该跟着人走。

    账本是 ``Cooldowns.remaining``（``skill_id -> 剩余秒``），存的是**相对
    剩余量**而不是"第几刻解锁"，所以不管换图后 ``tick`` 继续往前走、还是读档
    后 ``tick`` 回到存档里记的值，直接清零都成立——不需要挪刻度，也不存在
    "绝对刻号对不上"的问题（``PotionState.ready_at`` 那种绝对刻才是另一回事）。

    为什么必须是共享函数、而不是换图与读档各写一份 ``clear()``：铺怪已经踩过
    这同一个坑（见 :mod:`mirsolo.world.spawn` 顶部）——两条路径分叉出的差异
    只在很久以后才被发现。
    """
    comp = entity.get(Cooldowns)
    if comp is not None:
        comp.remaining.clear()


def reset_attack_pace(world: World, entity: Entity) -> None:
    """清掉这个实体在**攻击窗口**上的记录 —— 与 :func:`reset_cooldowns` 同一时机。

    为什么要清：攻击型技能与普攻共用 ``KEY_ATTACK_PACE`` 这本账，而"进图即清
    冷却"这句承诺本来就是**出手节奏**的承诺。只清技能冷却、不清窗口的话，
    换图 / 读档后仍然要等完旧图那截窗口——修前这条路径看着是通的，是因为
    技能压根不看窗口（门根本没接）；接上之后立刻暴露：换图后第一刻放不出
    火球（实测第 3 刻被挡）。

    为什么不干脆把整本账清空：这本账是按实体 id 记的，里面可能还有**别的**
    实体（同图其它玩家/召唤背后的主人）。只删自己那一格。
    """
    pace = world.get_data(KEY_ATTACK_PACE) or {}
    if entity.id in pace:
        pace = dict(pace)
        pace.pop(entity.id, None)
        world.set_data(KEY_ATTACK_PACE, pace)


def _within(pos_a: Position, pos_b: Position, rng: int) -> bool:
    """落点在射程内？与普攻距离门共用同一把尺子（``chebyshev``）。

    两把尺子各写一份的话，"技能 range=1" 与 "普攻贴身一格" 迟早会长成两个数。
    """
    return chebyshev(pos_a, pos_b) <= rng


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
        self.world = ctx.world
        self.resolver = CombatResolver(ctx)

    # ── 路由 ────────────────────────────────────────

    def _resolve_kind(self, kind: str, caster: Entity, victims: list[Entity],
                      power: int, spec: dict) -> list[dict]:
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
            elif kind == "poison":
                poison = self.resolver.apply_poison(
                    caster, victim,
                    damage_per_tick=int(spec.get("poison_damage", 1)),
                    ticks=int(spec.get("poison_ticks", 5)),
                    interval=int(spec.get("poison_interval", 10)),
                )
                outcome = {"damage_per_tick": poison.damage_per_tick,
                           "ticks": poison.remaining_ticks}
            elif kind == "buff_shield":
                shield = self.resolver.apply_shield(caster, power_permille=power)
                outcome = {"shield": shield.current, "shield_max": shield.max}
            elif kind == "buff_invis":
                buff = self.resolver.apply_invisible(
                    caster, ticks=int(spec.get("duration_ticks", 60)))
                outcome = {"until_tick": buff.until_tick}
            elif kind == "summon":
                outcome = {"summon": self._summon(caster, spec)}
            else:
                continue  # 未知 kind 在 loader 校验阶段已拦
            results.append({
                "target": victim.id,
                "kind": kind,
                **(outcome if isinstance(outcome, dict) else outcome.as_dict()),
            })
        return results

    # ── 召唤 ────────────────────────────────────────

    _summon_serial = 0

    def _summon(self, caster: Entity, spec: dict) -> str:
        """在施放者身旁召唤随从；超过 ``summon_max`` 时先清掉最旧的一只。"""
        monster_id = spec.get("summon_monster")
        if not monster_id:
            raise SkillError("bad_data", f"{spec.get('name')} 缺少 summon_monster")
        if monster_id not in self.data.monsters["monsters"]:
            raise SkillError("bad_data", f"召唤目标 {monster_id!r} 不在怪物表")
        keep = max(1, int(spec.get("summon_max", 1)))

        owned = [e for e in self.world.each(SummonOwner, Identity)
                 if e.get(SummonOwner).owner_id == caster.id]
        for extra in owned[keep - 1:]:  # 留 keep-1 只，再加下面这只
            self.world.remove_entity(extra.id)

        pos = caster.get(Position)
        faction = caster.get(Identity).faction
        type(self)._summon_serial += 1
        entity = spawn_monster(
            self.world, self.data, monster_id,
            x=float(int(pos.x) + 1), y=float(pos.y),
            eid=f"summon:{caster.id}:{type(self)._summon_serial}",
        )
        ident = entity.get(Identity)
        ident.kind = "summon"      # 既不算怪（不占刷新名额），也不占玩家位
        ident.faction = faction    # 与主人同阵营，索敌自动生效
        entity.add(SummonOwner(owner_id=caster.id))
        ai = entity.get(AiState)
        if ai is not None:
            ai.home = (float(int(pos.x) + 1), float(pos.y))
            ai.leash = max(ai.leash, 24.0)  # 随从要能跟着主人走远一点
        return entity.eid

    # ── 攻击窗口 ────────────────────────────────────

    @staticmethod
    def _drop_queued_melee(world: World, caster: Entity) -> None:
        """把这一 tick 队列里**属于施放者**的普攻意图作废。

        语义与攻击窗口里的"冷却中丢弃"完全一致：按下的那一下不该在若干刻
        之后自己生效，也不该和同一刻的技能一起结算。只动这一个实体的条目，
        别人的意图原样留在队列里（``AttackSystem`` 还会处理它们）。

        为什么不改成"让 ``AttackSystem`` 自己看窗口"：它**会**看（同一个
        ``swing_ready_at``），但本 tick 的账已经在前一刻结算前查过了——
        技能是在那之后才落的账，所以队列里那些意图此刻已经被认为"合法"。
        要在那里补一次复查就得让 ``AttackSystem`` 知道"技能先于自己跑"，
        那是把调用顺序固化成一条隐式依赖；在施放处清队列更直白。
        """
        intents = world.get_data(KEY_INTENTS, [])
        if not intents:
            return
        kept = [i for i in intents if i.get("attacker") is not caster]
        if len(kept) != len(intents):
            world.set_data(KEY_INTENTS, kept)

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

        # 攻击窗口：与普攻**同一把尺子**（同一本 ``KEY_ATTACK_PACE`` 账）。
        # 放在资源门之后、距离门之前——与 ``AttackSystem`` 的门序对齐
        # （麻痹 → 距离 → 攻速），而且被窗口挡下的施放不该先跑一遍选目标。
        if spec["kind"] in ATTACK_KINDS:
            ready_at = swing_ready_at(world, caster)
            if ready_at > self.ctx.tick:
                raise SkillError(
                    "cooldown",
                    f"出手冷却中（{player_attack_interval(world, caster)} 刻一次攻击动作，"
                    f"第 {ready_at} 刻才能出手）")

        caster_pos = caster.get(Position)
        if caster_pos is None:
            raise SkillError("missing_components", "施放者没有 Position")

        kind = spec["kind"]
        rng = int(spec.get("range", 0))
        radius = int(spec.get("aoe_radius", 0))
        power = int(spec.get("power_permille", 1000))

        # 目标与距离门（self 类技能目标就是自己；heal 的 target 是友方）
        if kind in SELF_KINDS:
            if target is not None and target is not caster:
                raise SkillError("bad_target", f"{spec['name']} 只能对自己施放")
            victims = [caster]
        elif kind in ("heal", "group_heal"):
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
        # 攻击动作占攻击窗口（与普攻**同一本账**）：记账点在扣费之后、
        # 结算之前——这一刻已经算是"出手了"，与普攻"挥出去了才记账"对齐。
        # 非攻击动作（治疗/盾/隐身/召唤）不记，见 ``ATTACK_KINDS``。
        if kind in ATTACK_KINDS:
            mark_swing(world, caster, self.ctx.tick)
            # 挤占是**即时的**，不只是"下一刻的普攻被挡"：同一个 tick 里
            # 已经排着的普攻意图必须当场作废。``SkillSystem`` 的优先级（15）
            # 高于 ``AttackSystem``（30），不清的话这一刻会先放技能、再
            # 把普攻也结算掉——一个窗口两个动作，正是"共用一把尺子"要堵的
            # 那种双份（实测 60 刻内技能 6 次 + 普攻 5 次，且 5 次撞在同一刻）。
            self._drop_queued_melee(world, caster)

        results = self._resolve_kind(kind, caster, victims, power, spec)

        # 熟练度：每成功施放 +1，练满 MAX_PROFICIENCY 为止（P2 再接加成）
        skills = caster.get(Skills)
        if skills is not None:
            skills.known[skill_id] = min(
                MAX_PROFICIENCY, max(1, skills.known.get(skill_id, 1)) + 1)

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
