"""攻击型技能与普攻**共用一把尺子** —— 放技能也占攻击窗口。

修前的事实：普攻走 ``attack_intents`` → ``AttackSystem``，那里有攻速门；
技能走 ``skill_intents`` → ``SkillSystem``，**根本不过那道门**。于是
``skills.json`` 里 ``gongsha``（攻杀剑术，``cooldown: 0 / cost_mp: 0``
的 1.15 倍主动技）可以**每刻放一次**——实测 60 刻放出 60 次，
等于把刚关进笼子的普攻从一个侧门原样放了出来（技能流 17.5 刻/杀
vs 按普攻窗口打 58.8 刻/杀）。

修法是**不另配技能冷却**，而是让攻击动作共用同一个攻击窗口：
普攻与攻击型技能都记账在 ``KEY_ATTACK_PACE`` 这一本上，互相挤占。
"一道门一个真相"——两套节奏迟早长成两个数。

分类**按数据**、不列白名单：``data/skills.json`` 的 ``kind`` 字段。
``ATTACK_KINDS`` 只收"对敌人造成伤害的出手"；治疗 / 增益 / 召唤不是攻击
动作，占窗口会变成"放个魔法盾之后 10 刻不能普攻"。这四类 kind 的集合
在本模块里被钉住（新增一个 kind 而没决定它算不算攻击动作，测试立刻红）。

本模块的用例全部是"修前必红"：

* ① 攻杀剑术（cd=0）连续 N 刻只放出 N/interval 次，不是 N 次；
* ② 治疗 / 盾 / 隐身 / 召唤**不受影响**：窗口被占着也照放；
* ③ 放出技能后立刻按普攻 → 普攻被窗口挡（挤占真的生效，且是双向的）；
* ④ 窗口记的是**攻击动作**，不是"最近一次施法"：放完魔法盾不影响普攻；
* ⑤ 技能自己的 cooldown 与窗口**两者都要满足**，不是取 max 以外的语义
   （火球 cd 12 刻 > 窗口 10 刻 → 卡它的是自己的 cd）。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import KEY_ATTACK_PACE, player_attack_interval
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat import resolver as resolver_mod
from mirsolo.components import (
    AiState,
    Bag,
    Cooldowns,
    Position,
    Progression,
    Shield,
    Skills,
    SummonOwner,
    Vitals,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth
from mirsolo.skills import ATTACK_KINDS, NON_ATTACK_KINDS, learn

CASES = []
DATA = GameData.load()
SKILLS = DATA.skills["skills"]

EVT_SKILL_CAST = "skill:cast"
EVT_SKILL_REJECTED = "skill:rejected"
EVT_ATTACK_REJECTED = resolver_mod.EVT_ATTACK_REJECTED

#: 连续放技能的刻数。战士 5 档 → 10 刻/窗口：60 刻里第一发立刻出，
#: 之后每 10 刻一发 → 第 1/11/21/31/41/51 刻共 6 发（修前是 60 发）。
HOLD_TICKS = 60
WARRIOR_WINDOW = 10


def case(fn):
    CASES.append(fn)
    return fn


def _setup(class_id: str = "warrior", level: int = 50):
    """空会话 + 指定职业/等级的玩家（打不死的血，省得被怪反杀打断观察）。"""
    session = build_session(data=DATA, seed=1, map_id="biqi_field")
    player = populate(session, player_class=class_id, with_spawns=False)["player"]
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    player.get(Skills).known.clear()
    apply_growth(player, DATA.classes["classes"][class_id])
    vitals = player.get(Vitals)
    vitals.max_hp = vitals.hp = 10 ** 6
    return session, player


def _learn(player, skill_id: str) -> None:
    book = SKILLS[skill_id].get("book")
    if book:
        player.get(Bag).add(book, 1)
    learn(player, DATA, skill_id)


def _punch_bag(session, player, skill_id: str, *, distance: int | None = None):
    """放一只站着不动的沙包（10^6 血，打不死）。

    ``distance`` 缺省时摆到技能 ``range`` 的边缘（够得着的最远格）——
    远程技能不这样摆就会因为够不着在距离门上被拒，测不到窗口。
    """
    rng = max(1, int(SKILLS[skill_id].get("range", 1)))
    step = rng if distance is None else distance
    mob = spawn_monster(session.world, DATA, "shiwang",
                        x=player.get(Position).x + step,
                        y=player.get(Position).y)
    ai = mob.get(AiState)
    if ai is not None:
        ai.vision = 0
        ai.target_id = None
    mv = mob.get(Vitals)
    mv.max_hp = mv.hp = 10 ** 6
    return mob


def _casts(session, player) -> list[int]:
    """玩家每一次**真正放出**的技能刻（同一刻多条事件按刻去重）。"""
    seen: list[int] = []
    session.bus.subscribe(EVT_SKILL_CAST,
                          lambda p: seen.append(int(p.get("tick", 0)))
                          if p.get("caster") == player.id else None)
    return seen


def _spam(session, player, skill_id: str, target, ticks: int) -> list[int]:
    """每刻把蓝补满、投一个技能意图、走一刻——等同于"按住技能键不放"。"""
    seen = _casts(session, player)
    vitals = player.get(Vitals)
    for _ in range(ticks):
        vitals.mp = vitals.max_mp
        session.cast_skill(player, skill_id, target)
        session.step(1)
    return sorted(set(seen))


# ── 分类：按数据分，不列白名单 ───────────────────────

@case
def test_kinds_partition_is_complete_and_pinned():
    """每个 ``kind`` 必须**明确**属于"攻击动作"或"不是攻击动作"，不许漏。

    修前的隐患不是"分类错了"，而是"根本没有分类"——技能这一侧压根不看窗口。
    这条钉住三件事：
    ① 两个集合互斥、并集正好是 ``SKILL_KINDS``（loader 的数据面定义）；
    ② 数据表里出现的每个 kind 都在其中（新增 kind 必须同时决定它算不算）；
    ③ 逐字钉住两侧成员——把 ``heal`` 挪进攻击侧、或把 ``magic`` 挪出去，
       都必须是一次**有意识的**修改（顺手改这里 = 承认行为变了）。
    """
    from mirsolo.data.loader import SKILL_KINDS

    assert ATTACK_KINDS & NON_ATTACK_KINDS == frozenset(), (
        f"两边都算：{ATTACK_KINDS & NON_ATTACK_KINDS}")
    assert ATTACK_KINDS | NON_ATTACK_KINDS == SKILL_KINDS, (
        f"kind 没分完：{SKILL_KINDS - (ATTACK_KINDS | NON_ATTACK_KINDS)}")
    assert ATTACK_KINDS == frozenset({
        "physical", "physical_aoe", "magic", "magic_aoe", "taoist", "poison",
    }), ATTACK_KINDS
    assert NON_ATTACK_KINDS == frozenset({
        "heal", "group_heal", "buff_shield", "buff_invis", "summon",
    }), NON_ATTACK_KINDS
    # 数据表里出现的每个 kind 都已被分类
    used = {spec["kind"] for spec in SKILLS.values()}
    assert used <= (ATTACK_KINDS | NON_ATTACK_KINDS), used - (ATTACK_KINDS | NON_ATTACK_KINDS)
    # 数据口径上"攻击型技能"必须真的是往敌人身上结算伤害的那几类：
    # physical* / magic* / taoist 三类能一眼看出；poison 挂的是敌方状态
    for sid, spec in SKILLS.items():
        if spec["kind"] in ATTACK_KINDS:
            assert spec.get("range", 0) >= 1, f"{sid} 算攻击动作却没有射程"
        if spec["kind"] in NON_ATTACK_KINDS:
            assert spec["kind"] not in ("physical", "physical_aoe", "magic",
                                        "magic_aoe", "taoist"), sid


# ── ① 攻杀剑术：cd=0 也不能每刻放 ────────────────────

@case
def test_zero_cooldown_attack_skill_shares_the_attack_window():
    """攻杀剑术 cd=0、耗蓝 0：修前 60 刻放 60 次（等于每刻一刀的 DPS）。

    修后它和其他出手一样占窗口：60 刻只放出 6 次，间隔 ≥ 10 刻。
    """
    session, player = _setup("warrior")
    try:
        _learn(player, "gongsha")
        mob = _punch_bag(session, player, "gongsha")
        ticks = _spam(session, player, "gongsha", mob, HOLD_TICKS)
        assert len(ticks) == HOLD_TICKS // WARRIOR_WINDOW, (
            f"攻杀剑术 {HOLD_TICKS} 刻放出 {len(ticks)} 次"
            f"（应当 {HOLD_TICKS // WARRIOR_WINDOW} 次）：{ticks}")
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert gaps and min(gaps) >= WARRIOR_WINDOW, f"两次之间比窗口还密：{ticks}"
        # 也不能收紧到"放不出来"
        assert ticks and ticks[0] == 1, f"第一发没有立刻放出：{ticks}"
    finally:
        session.shutdown()


@case
def test_window_is_the_same_one_as_melee():
    """窗口取自 ``classes.json`` 的攻速档，与普攻**同一个数**（不是一个新常数）。

    法师 4 档 = 11 刻，火球自己的 cd 1.2s = 12 刻（更严）：60 刻里应放出 5 次
    （第 1/14/27/40/53 刻）。间隔 13 刻是"先按窗口 11 刻冷却、cd 还没走完，
    再过 1 刻才出手"的合成结果——写死 10 或干脆不查窗口，数字都会变。
    """
    session, player = _setup("mage")
    try:
        _learn(player, "huozhu")
        vitals = player.get(Vitals)
        # 蓝池放大：法师 7 级只有十几点蓝，按住火球会先撞"缺蓝"而不是撞节奏。
        # ⚠️ 必须在回蓝时**接着补**（``_spam`` 把蓝补满），因为
        # ``RegenSystem``（每 20 刻 +1）会盖掉一次性的大蓝池。
        vitals.max_mp = vitals.mp = 10 ** 4
        mob = _punch_bag(session, player, "huozhu")
        window = player_attack_interval(session.world, player)
        assert window == 11, f"法师窗口不是 11 刻：{window}"
        ticks = _spam(session, player, "huozhu", mob, HOLD_TICKS)
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert gaps and min(gaps) >= window, f"火球比普攻窗口还快：{ticks}"
        assert len(ticks) == 5, f"60 刻放出 {len(ticks)} 发火球：{ticks}"
        # 火球自己的 cd 是 1.2s = 12 刻 > 窗口 11：**两者都要满足**，
        # 实际节奏由更严的那个（自己的 cd）决定 → 1/14/27/40/53。
        assert min(gaps) >= 12, f"火球比它自己的 cd 还快：{ticks}"
        expect = [1, 14, 27, 40, 53]
        assert ticks == expect, f"火球节奏不对：{ticks} != {expect}"
    finally:
        session.shutdown()


@case
def test_skill_own_cooldown_still_binds():
    """技能自己的 ``cooldown`` 仍然生效：窗口只是**另一道**必须满足的门。

    刺客剑 cd 1.5s = 15 刻 > 窗口 10 刻 → 卡它的是自己那份 cd（15 刻），
    修后 60 刻 4 次，与修前一致（这条不该被窗口这层"顺手放宽"）。
    """
    session, player = _setup("warrior")
    try:
        _learn(player, "cisha")
        mob = _punch_bag(session, player, "cisha")
        ticks = _spam(session, player, "cisha", mob, HOLD_TICKS)
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert gaps and min(gaps) >= 15, f"刺杀剑术比自己的 cd 还快：{ticks}"
        assert len(ticks) == 4, f"60 刻放出 {len(ticks)} 次刺杀：{ticks}"
        # 自己的 cd 账仍在（不是被窗口顶掉了）
        assert player.get(Cooldowns).remaining.get("cisha", 0.0) > 0.0
    finally:
        session.shutdown()


# ── ② 非攻击技能不受影响（最容易被误伤，必须单独钉住） ──

@case
def test_heal_is_not_an_attack_action():
    """治愈术不占窗口：窗口刚被普攻占住的同一刻，治疗照放。

    修前放得出来只是因为"根本不过门"；修后仍然放得出来，必须是因为
    **治疗不算攻击动作**，而不是因为门没接——所以这条同时验另一半：
    同一刻的普攻仍然被窗口挡着（窗口确实在，只是治疗不交这笔账）。
    """
    session, player = _setup("taoist", level=7)
    try:
        _learn(player, "zhiyu")
        mob = _punch_bag(session, player, "zhiyu", distance=1)
        # 先用普攻占住窗口（第 1 刻出刀）
        session.queue_attack(player, mob)
        session.step(1)
        pace = session.world.get_data(KEY_ATTACK_PACE) or {}
        assert pace.get(player.id) == 1, f"普攻没占窗口：{pace}"

        vitals = player.get(Vitals)
        vitals.hp = 10
        session.cast_skill(player, "zhiyu", player)
        session.step(1)                                    # 第 2 刻：窗口内
        assert vitals.hp > 10, "窗口期内治疗被误伤了（治疗不该占攻击窗口）"
        # 窗口确实还在：这一刻的普攻要被挡
        session.queue_attack(player, mob)
        session.step(1)
        assert (session.world.get_data(KEY_ATTACK_PACE) or {}).get(player.id) == 1, (
            "治疗把攻击窗口顶掉了（它不该占窗口）")
    finally:
        session.shutdown()


@case
def test_shield_invis_summon_do_not_consume_the_window():
    """魔法盾 / 隐身 / 召唤：连放多次都不推进窗口，放完立刻能普攻。

    "放个魔法盾之后 10 刻不能普攻"是不合理的手感——它们不是攻击动作。
    这里逐个 check：放完之后窗口账上**没有**该玩家的记录。
    """
    cases = (
        ("mage", 31, "mofadun"),
        ("taoist", 21, "yinshen"),
        ("taoist", 19, "zhaohuankulou"),
    )
    for class_id, level, skill_id in cases:
        session, player = _setup(class_id, level=level)
        try:
            _learn(player, skill_id)
            session.cast_skill(player, skill_id, player)
            session.step(1)
            pace = session.world.get_data(KEY_ATTACK_PACE) or {}
            assert player.id not in pace, f"{skill_id} 占用了攻击窗口：{pace}"
            if skill_id == "mofadun":
                assert player.get(Shield) is not None, "前置条件：盾没放出来"
            if skill_id == "zhaohuankulou":
                owned = [e for e in session.world.each(SummonOwner)
                         if e.get(SummonOwner).owner_id == player.id]
                assert owned, "前置条件：召唤没生效"
            # 放完之后**当刻**按普攻：必须能出刀
            mob = _punch_bag(session, player, skill_id, distance=1)
            session.queue_attack(player, mob)
            session.step(1)
            assert (session.world.get_data(KEY_ATTACK_PACE) or {}).get(player.id) == 2, (
                f"{skill_id} 之后普攻没能立刻出刀")
        finally:
            session.shutdown()


@case
def test_non_attack_skill_is_not_blocked_by_the_window():
    """攻击窗口里的非攻击技能照放：治疗不会因为"刚砍了一刀"而干等。

    判据只看**拒绝码**：窗口挡人时码是 ``cooldown``。治疗被挡的原因只允许
    是它自己的 cd（1.6s = 16 刻，比窗口还长），所以逐刻扫 10 刻：
    该放的时候放下，被挡的时候**一定不是因为攻击窗口**。
    """
    session, player = _setup("taoist", level=7)
    try:
        _learn(player, "zhiyu")
        mob = _punch_bag(session, player, "zhiyu", distance=1)
        session.queue_attack(player, mob)
        session.step(1)                                    # 第 1 刻：普攻占窗口
        pace = session.world.get_data(KEY_ATTACK_PACE) or {}
        assert pace.get(player.id) == 1, f"前置条件：普攻没占窗口：{pace}"

        # 穷举法：窗口内的每一刻都投一个治疗意图，逐刻记录结果。
        # 窗口是第 1..10 刻，第 11 刻起解禁——把"攻击窗口还在不在"与
        # "治疗有没有被挡"两件事拆开看：只要存在一刻是"窗口还在 + 治疗成立"，
        # 就证明攻击窗口管不到治疗（修前它成立只是因为根本没过门，
        # 所以这里同时钉住另一半：窗口内的普攻必须被挡）。
        vitals = player.get(Vitals)
        cast_ticks: list[int] = []
        rejects: list[dict] = []
        session.bus.subscribe(EVT_SKILL_CAST,
                              lambda p: cast_ticks.append(p["tick"])
                              if p.get("caster") == player.id else None)
        session.bus.subscribe(EVT_SKILL_REJECTED,
                              lambda p: rejects.extend(p.get("items") or []))
        for _ in range(10):                                # 第 2..11 刻
            vitals.mp = vitals.max_mp
            vitals.hp = 10
            session.cast_skill(player, "zhiyu", player)
            session.step(1)
        # 治疗自己的 cd 是 16 刻：这一串里应当放出 1 发（第 2 刻），
        # 其余被**它自己的 cd** 挡——绝不是被攻击窗口挡
        assert cast_ticks == [2], f"治疗在攻击窗口里没放出来：{cast_ticks}"
        assert all(item.get("code") == "cooldown" for item in rejects), rejects
        # 窗口解禁后（第 12 刻）再放：仍然成立（说明前一刻被挡是 cd 而不是窗口）
        player.get(Cooldowns).remaining.clear()
        session.cast_skill(player, "zhiyu", player)
        session.step(1)
        assert cast_ticks == [2, 12], f"窗口余韵挡住了治疗：{cast_ticks}"
    finally:
        session.shutdown()


# ── ③ 挤占是双向的：放了技能，普攻就得等 ──────────────

@case
def test_attack_skill_blocks_the_next_melee_swing():
    """放出攻击型技能后立刻按普攻：普攻被窗口挡住（并发 ``cooldown`` 回音）。

    这是"共用一把尺子"最直接的证据——修前普攻照出，两套节奏各算各的。
    """
    session, player = _setup("warrior")
    try:
        _learn(player, "gongsha")
        mob = _punch_bag(session, player, "gongsha")
        session.cast_skill(player, "gongsha", mob)
        session.step(1)                                    # 第 1 刻：技能放出
        assert (session.world.get_data(KEY_ATTACK_PACE) or {}).get(player.id) == 1

        rejects: list[dict] = []
        session.bus.subscribe(EVT_ATTACK_REJECTED,
                              lambda p: rejects.extend(p.get("items") or []))
        session.queue_attack(player, mob)
        session.step(1)                                    # 第 2 刻：窗口内
        assert rejects and rejects[0].get("code") == "cooldown", (
            f"技能放完普攻照样出刀（挤占没生效）：{rejects}")
        assert rejects[0].get("ready_at") == 11, rejects[0]

        # 等到窗口结束（此刻第 2 刻，再走 8 刻到第 10 刻）：第 11 刻普攻略出刀
        for _ in range(8):
            session.step(1)
        assert session.tick == 10, session.tick
        session.queue_attack(player, mob)
        session.step(1)                                    # 第 11 刻
        assert session.tick == 11, session.tick
        assert (session.world.get_data(KEY_ATTACK_PACE) or {}).get(player.id) == 11, (
            "窗口结束后（第 11 刻）普攻没出刀")
    finally:
        session.shutdown()


@case
def test_melee_swing_blocks_the_next_attack_skill():
    """反过来也一样：刚砍过一刀，攻击型技能这一刻放不出来（双向挤占）。"""
    session, player = _setup("warrior")
    try:
        _learn(player, "gongsha")
        mob = _punch_bag(session, player, "gongsha")
        session.queue_attack(player, mob)
        session.step(1)                                    # 第 1 刻：普攻

        rejects: list[dict] = []
        session.bus.subscribe(EVT_SKILL_REJECTED,
                              lambda p: rejects.extend(p.get("items") or []))
        session.cast_skill(player, "gongsha", mob)
        session.step(1)                                    # 第 2 刻：窗口内
        assert rejects and rejects[0].get("code") == "cooldown", (
            f"刚砍过一刀还能立刻放技能（挤占没生效）：{rejects}")
        # 被拒的施放不该扣蓝/进 cd/涨熟练度（门在扣费之前）
        assert player.get(Cooldowns).remaining.get("gongsha", 0.0) == 0.0
        assert player.get(Skills).known["gongsha"] == 1, "被拒的施放涨了熟练度"
    finally:
        session.shutdown()


# ── ④ 窗口记的是"攻击动作" ──────────────────────────

@case
def test_non_attack_cast_does_not_mark_the_window():
    """放魔法盾不写窗口账：那一刻的普攻照出（不是"最近一次施法"的账）。

    坑在实现里很常见——"SkillSystem 结束前统一记一笔 tick"会把盾、治疗、
    隐身一起算进去。账只能记在**攻击动作**上。
    """
    session, player = _setup("mage", level=31)
    try:
        _learn(player, "mofadun")
        session.cast_skill(player, "mofadun", player)
        session.step(1)
        mob = _punch_bag(session, player, "mofadun", distance=1)
        session.queue_attack(player, mob)
        session.step(1)
        assert (session.world.get_data(KEY_ATTACK_PACE) or {}).get(player.id) == 2, (
            "魔法盾被记进了攻击窗口（普攻没能立刻出刀）")
    finally:
        session.shutdown()


@case
def test_window_book_is_still_runtime_only():
    """仍是同一本 ``KEY_ATTACK_PACE``：技能**没有**另开一本账，也不进存档。"""
    from mirsolo.save import schema

    assert KEY_ATTACK_PACE not in schema.WORLD_KEYS
    assert KEY_ATTACK_PACE not in schema.OPTIONAL_WORLD_KEYS
    # 世界数据里不存在"技能侧第二本节奏账"
    session, player = _setup("warrior")
    try:
        _learn(player, "gongsha")
        mob = _punch_bag(session, player, "gongsha")
        session.cast_skill(player, "gongsha", mob)
        session.step(1)
        keys = {k for k in ("skill_pace", "skill_attack_pace", "cast_pace")
                if session.world.get_data(k)}
        assert keys == set(), f"技能侧另开了节奏账：{keys}"
    finally:
        session.shutdown()


# ── ⑤ 施毒 / 远程攻击技能一视同仁 ────────────────────

@case
def test_ranged_and_poison_skills_also_share_the_window():
    """施毒 / 雷电术同样占窗口：它们都是往敌人身上出手的动作。

    施毒的杀伤在后续跳伤上，但它**这一刻**是"朝敌人出手"，与普攻争夺
    同一个窗口；不占的话，道士可以一边每刻贴脸砍、一边白嫖挂毒。
    """
    for class_id, level, skill_id in (("taoist", 14, "shidu"),
                                      ("mage", 17, "leidian")):
        session, player = _setup(class_id, level=level)
        try:
            assert SKILLS[skill_id]["kind"] in ATTACK_KINDS
            _learn(player, skill_id)
            mob = _punch_bag(session, player, skill_id)
            session.cast_skill(player, skill_id, mob)
            session.step(1)
            pace = session.world.get_data(KEY_ATTACK_PACE) or {}
            assert pace.get(player.id) == 1, f"{skill_id} 没占用攻击窗口：{pace}"
        finally:
            session.shutdown()


@case
def test_one_window_is_one_action_within_the_same_tick():
    """同一个 tick 里排了"技能 + 普攻"两个意图：这一格窗口**只被占用一次**。

    ``SkillSystem``（p15）先于 ``AttackSystem``（p30）跑。只在 AttackSystem
    里查窗口是不够的——技能落账发生在它查完之后；不清队列的话，同刻排着的
    普攻会在技能之后**再结算一刀**：一个窗口两个动作。

    ``combat:hit`` 不带"这一刀是技能还是普攻"的标记，所以这里不去数 hit，
    改数更硬的东西：
    ① 玩家每成功施放一次技能，收刀后再投普攻的那一侧必然被窗口挡
       （``cooldown`` 回音），说明技能确实占掉了这一格；
    ② 每 10 刻只有一次"占用"：把"技能施放刻"与"普攻被拒刻"并起来，
       相邻两次间隔都 ≥ 窗口——没有"技能 + 普攻挤在同一刻各算一次"。
    """
    session, player = _setup("warrior")
    try:
        _learn(player, "gongsha")
        mob = _punch_bag(session, player, "gongsha")
        casts: list[int] = []
        rejects: list[dict] = []
        session.bus.subscribe(EVT_SKILL_CAST,
                              lambda p: casts.append(p["tick"])
                              if p.get("caster") == player.id else None)
        session.bus.subscribe(EVT_ATTACK_REJECTED,
                              lambda p: rejects.extend(p.get("items") or []))
        for _ in range(30):
            player.get(Vitals).mp = player.get(Vitals).max_mp
            session.cast_skill(player, "gongsha", mob)
            session.queue_attack(player, mob)
            session.step(1)
        assert len(casts) == 3, f"30 刻应该 3 次技能（10 刻一窗口）：{casts}"
        # ① 每一次施放之后紧跟的普攻都被挡（每一次都被记成 cooldown 回音）
        cool = [r for r in rejects if r.get("code") == "cooldown"]
        ready_ats = {r.get("ready_at") for r in cool}
        assert ready_ats == {c + 10 for c in casts}, (
            f"普攻没有被技能占住的窗口挡住：ready_at {sorted(ready_ats)} vs 施放 {casts}")
        # ② 施放的那一刻，``AttackSystem`` 手里不该还留着这个玩家的普攻意图。
        #    这是"同一刻两个动作"的**直接证据**：修前第 1/11/21 刻
        #    AttackSystem 都会看到一条玩家的意图（技能先结算、普攻紧随其后）。
        #    直接读队列读不到——AttackSystem 跑完就清空了——所以要在它
        #    **入场那一刻**取样。
        seen: dict[int, set[int]] = {}
        attack_system = next(s for s in session.world.systems
                             if type(s).__name__ == "AttackSystem")
        original = type(attack_system).update

        def spy(system, world, dt):
            seen[int(world.get_data("tick", 0))] = {
                i["attacker"].id for i in world.get_data("attack_intents", [])}
            return original(system, world, dt)

        type(attack_system).update = spy
        try:
            for _ in range(30):
                player.get(Vitals).mp = player.get(Vitals).max_mp
                session.cast_skill(player, "gongsha", mob)
                session.queue_attack(player, mob)
                session.step(1)
        finally:
            type(attack_system).update = original
        leaked = [c for c in casts if player.id in seen.get(c, set())]
        assert leaked == [], (
            f"施放的那一刻 AttackSystem 还看到玩家的普攻意图（同一窗口两个动作）：{leaked}")
    finally:
        session.shutdown()


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    return passed, failed


if __name__ == "__main__":
    p, f = run()
    print(f"test_skill_pace: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
