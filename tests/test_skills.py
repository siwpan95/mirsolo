"""技能系统：学习门槛、MP/冷却/距离门、伤害与治疗路由、AoE。

原则与公式测试一致：每条断言都能手算复核。
MP/冷却的数字直接来自 ``skills.json``，改表就要改这里的期望值。
"""

from __future__ import annotations

import tempfile

from mirsolo.actor.factory import spawn_monster, spawn_player
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    AiState,
    Bag,
    Cooldowns,
    Identity,
    Invisible,
    Poison,
    Position,
    Progression,
    Shield,
    Skills,
    SummonOwner,
    Vitals,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth
from mirsolo.save import SaveStore
from mirsolo.skills import (
    MAX_PROFICIENCY,
    PLANNED_KINDS,
    SUPPORTED_KINDS,
    SkillError,
    can_cast,
    learn,
)
from mirsolo.world.travel import travel

CASES = []
DATA = GameData.load()
SKILLS = DATA.skills["skills"]

# 与 skills.json 同值的字面量（改表必须同步改）
HUOZHU_MP = SKILLS["huozhu"]["cost_mp"]          # 3
HUOZHU_CD = SKILLS["huozhu"]["cooldown"]          # 1.2
HUOZHU_RANGE = SKILLS["huozhu"]["range"]          # 8
ZHIYU_MP = SKILLS["zhiyu"]["cost_mp"]             # 4


def case(fn):
    CASES.append(fn)
    return fn


def _session():
    """空会话 + 玩家，玩家等级/职业可再调。"""
    session = build_session(data=DATA, seed=1)
    player = populate(session, with_spawns=False)["player"]
    return session, player


def _make_caster(session, class_id: str, level: int):
    """把玩家换成指定职业/等级（apply_growth 是升级的同一入口）。"""
    player = session.world.get_entity("player")
    spec = DATA.classes["classes"][class_id]
    player.get(Skills).known.clear()
    prog = player.get(Progression)
    prog.class_id = class_id
    prog.level = level
    prog.exp = 0
    apply_growth(player, spec)
    return player


def _grant_book(player, skill_id: str, count: int = 1) -> str:
    """把技能书塞进背包（学技能必须先有书）。"""
    book_id = SKILLS[skill_id]["book"]
    player.get(Bag).add(book_id, count)
    return book_id


def _learn_with_book(player, skill_id: str) -> None:
    _grant_book(player, skill_id)
    learn(player, DATA, skill_id)


@case
def test_learn_gates():
    """学习三道门：职业匹配、等级门槛、幂等。"""
    session, player = _session()
    try:
        warrior = _make_caster(session, "warrior", 7)
        _learn_with_book(warrior, "gongsha")           # 7 级战士学攻杀：OK
        assert "gongsha" in warrior.get(Skills).known
        _learn_with_book(warrior, "gongsha")           # 重复学习幂等
        assert warrior.get(Skills).known["gongsha"] == 1

        for sid in ("gongsha", "huozhu", "zhiyu"):
            try:
                learn(warrior, DATA, sid if sid != "gongsha" else "liehuo")
                raise AssertionError(f"{sid} 不该学得会")
            except SkillError as exc:
                assert exc.code in ("level_too_low", "class_mismatch"), exc.code

        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")               # 法师 7 级学火球：OK
        try:
            learn(mage, DATA, "zhiyu")            # 道士技能
            raise AssertionError("法师不该学得会治愈术")
        except SkillError as exc:
            assert exc.code == "class_mismatch"
    finally:
        session.shutdown()


@case
def test_cast_requires_learned():
    """没学过的技能放不出来，也不该掉 MP。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mage.get(Vitals).mp == mp0, "未学习也扣了 MP"
        assert mob.get(Vitals).hp == mob.get(Vitals).max_hp, "未学习却打出了伤害"
    finally:
        session.shutdown()


@case
def test_no_kind_lags_behind_implementation():
    """数据不许跑在实现前面：表里每个 kind 都必须已有行为。

    P1-S1 时表里有 4 个 kind 只落数据（poison/buff_shield/buff_invis/summon），
    由 ``PLANNED_KINDS`` 兜着；P1-S2 把行为补齐后它必须变空。
    这条断言的作用是：**以后往表里加新 kind 而没写行为，测试立刻红**。
    """
    assert PLANNED_KINDS == frozenset(), f"这些 kind 只有数据没有行为：{PLANNED_KINDS}"
    kinds = {spec["kind"] for spec in SKILLS.values()}
    assert kinds <= SUPPORTED_KINDS, kinds - SUPPORTED_KINDS
    # 全部 16 技在满级满蓝下都应为"可施放"（不带目标，只看资源/学习门）
    session, player = _session()
    try:
        for sid, spec in SKILLS.items():
            caster = _make_caster(session, spec["class"], 50)
            _learn_with_book(caster, sid)
            caster.get(Vitals).mp = caster.get(Vitals).max_mp
            ok, code = can_cast(caster, DATA, sid)
            assert ok, (sid, code)
    finally:
        session.shutdown()


@case
def test_mp_deducted_and_damage_dealt():
    """火球术：扣 MP 精确等于 cost，目标掉血走魔法路径（无视 AC、吃 MAC）。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mob_hp0 = mob.get(Vitals).hp
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mage.get(Vitals).mp == mp0 - HUOZHU_MP, (mp0, mage.get(Vitals).mp)
        assert mob.get(Vitals).hp < mob_hp0, "火球没造成伤害"
    finally:
        session.shutdown()


@case
def test_insufficient_mp_blocks_cast():
    """MP 不足：拒绝施放、不掉 MP、不掉血。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        mage.get(Vitals).mp = HUOZHU_MP - 1        # 刚好差 1 点
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        mob_hp0 = mob.get(Vitals).hp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp == mob_hp0
        assert mage.get(Vitals).mp == HUOZHU_MP - 1, "MP 不足却被扣了"
    finally:
        session.shutdown()


@case
def test_cooldown_blocks_and_expires():
    """冷却期内再按无效；冷却走完（CooldownSystem 递减）后可再放。

    注意先后关系：CooldownSystem（p10）每刻先跑，SkillSystem（p15）后跑，
    所以第一发放完后立即读到的剩余值是 ``CD − 1 tick``，不是 CD 本身。
    """
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)                             # 第一发放出
        left = mage.get(Cooldowns).remaining.get("huozhu", 0.0)
        assert 0.0 < left <= HUOZHU_CD, left         # 已进冷却且在递减

        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)                             # 冷却中
        assert mage.get(Vitals).mp == mp0, "冷却中施放不应扣 MP"

        session.step(int(HUOZHU_CD / session.tick_dt) + 2)  # 等冷却走完
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0

        session.cast_skill(mage, "huozhu", mob)     # 冷却结束应能再放
        session.step(2)
        releft = mage.get(Cooldowns).remaining.get("huozhu", 0.0)
        assert releft > 0, "冷却结束后再放没有重新进冷却"
    finally:
        session.shutdown()


@case
def test_range_gate():
    """超程施放必须被拒：不掉 MP、不掉血、不进冷却。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        far = HUOZHU_RANGE + 1
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + far, y=mage.get(Position).y)
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mob.get(Vitals).hp == mob.get(Vitals).max_hp
        assert mage.get(Vitals).mp == mp0
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0, "超程却进了冷却"
    finally:
        session.shutdown()


@case
def test_heal_restores_by_sc_roll():
    """治愈术：恢复量 = roll(道术)，不超上限；MP 精确扣除。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 7)
        _learn_with_book(taoist, "zhiyu")
        vitals = taoist.get(Vitals)
        vitals.hp = 5                               # 先打掉一批血
        mp0 = vitals.mp
        session.cast_skill(taoist, "zhiyu", taoist)  # 自疗
        session.step(2)
        assert vitals.hp > 5, "治愈术没回血"
        assert vitals.hp <= vitals.max_hp
        assert vitals.mp == mp0 - ZHIYU_MP
    finally:
        session.shutdown()


@case
def test_aoe_hits_all_in_radius():
    """半月弯刀：半径内两只怪各结算一次，半径外的没事。"""
    session, player = _session()
    try:
        warrior = _make_caster(session, "warrior", 22)
        _learn_with_book(warrior, "banyue")
        px, py = warrior.get(Position).x, warrior.get(Position).y
        near1 = spawn_monster(session.world, DATA, "ji", x=px + 1, y=py)
        near2 = spawn_monster(session.world, DATA, "ji", x=px + 1, y=py + 1)  # 距目标 1 格
        far = spawn_monster(session.world, DATA, "ji", x=px + 5, y=py)
        session.cast_skill(warrior, "banyue", near1)  # 近战 AoE 以目标为中心 r=1
        session.step(2)
        assert near1.get(Vitals).hp < near1.get(Vitals).max_hp
        assert near2.get(Vitals).hp < near2.get(Vitals).max_hp, "半径内的怪没被打到"
        assert far.get(Vitals).hp == far.get(Vitals).max_hp, "半径外的怪被波及了"
    finally:
        session.shutdown()


@case
def test_magic_aoe_and_group_heal_shapes():
    """冰咆哮打敌方群体、集体治愈回友方群体——两个方向各自只影响本阵营。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 35)
        _learn_with_book(mage, "bingpaoxiao")
        px, py = mage.get(Position).x, mage.get(Position).y
        mobs = [spawn_monster(session.world, DATA, "ji", x=px + dx, y=py)
                for dx in (1, 2)]
        before = [m.get(Vitals).hp for m in mobs]
        session.cast_skill(mage, "bingpaoxiao", mobs[0])
        session.step(2)
        for mob, hp0 in zip(mobs, before):
            assert mob.get(Vitals).hp < hp0, "冰咆哮没覆盖到半径内目标"

        taoist = _make_caster(session, "taoist", 26)
        _learn_with_book(taoist, "jiti_zhiyu")
        taoist.get(Vitals).hp = 3
        session.cast_skill(taoist, "jiti_zhiyu", taoist)
        session.step(2)
        assert taoist.get(Vitals).hp > 3, "集体治愈没生效"
    finally:
        session.shutdown()


@case
def test_book_gate_and_consumption():
    """技能书是真门槛：没书学不会；学会消耗一本；已学再学不吃书。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 35)
        try:
            learn(taoist, DATA, "zhiyu")
            raise AssertionError("没有技能书也学会了")
        except SkillError as exc:
            assert exc.code == "missing_book", exc.code
        assert "zhiyu" not in taoist.get(Skills).known

        book_id = _grant_book(taoist, "zhiyu", 2)
        learn(taoist, DATA, "zhiyu")
        assert taoist.get(Bag).items.get(book_id, 0) == 1, "没消耗技能书"

        learn(taoist, DATA, "zhiyu")  # 幂等：不再吃第二本
        assert taoist.get(Bag).items.get(book_id, 0) == 1, "重复学习又吃了一本"
        assert taoist.get(Skills).known["zhiyu"] == 1
    finally:
        session.shutdown()


@case
def test_proficiency_grows_and_caps():
    """熟练度：每成功施放 +1，封顶 MAX_PROFICIENCY；被拒的施放不涨。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        mob = spawn_monster(session.world, DATA, "shiwang",  # 血厚，打不死
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        seen = [mage.get(Skills).known["huozhu"]]
        for _ in range(MAX_PROFICIENCY + 2):
            mage.get(Vitals).mp = mage.get(Vitals).max_mp
            session.cast_skill(mage, "huozhu", mob)
            session.step(int(SKILLS["huozhu"]["cooldown"] / session.tick_dt) + 2)
            seen.append(mage.get(Skills).known["huozhu"])
        assert seen[1] == 2 and seen[-1] == MAX_PROFICIENCY, seen
        assert seen == sorted(seen), f"熟练度出现回落：{seen}"
    finally:
        session.shutdown()


@case
def test_poison_damage_over_time():
    """施毒：挂上 Poison 状态后按 interval 跳伤，跳完自动结束。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 14)
        _learn_with_book(taoist, "shidu")
        spec = SKILLS["shidu"]
        mob = spawn_monster(session.world, DATA, "shiwang",  # 180 血，毒不死便于观察
                            x=taoist.get(Position).x + 2, y=taoist.get(Position).y)
        hp0 = mob.get(Vitals).hp
        session.cast_skill(taoist, "shidu", mob)
        session.step(2)
        poison = mob.get(Poison)
        assert poison is not None and poison.remaining_ticks == spec["poison_ticks"], poison

        # 跳一次（interval 刻）就掉 spec["poison_damage"] 点
        session.step(int(spec["poison_interval"]))
        assert mob.get(Vitals).hp <= hp0 - spec["poison_damage"], (hp0, mob.get(Vitals).hp)

        # 全部跳完后状态结束，不再掉血
        # ⚠️ 不能用 hp 等值断言：RegenSystem 每 20 刻回 1 点血，
        #    怪的血量不是单调递减。改数"毒伤事件"的条数。
        session.step(int(spec["poison_interval"] * spec["poison_ticks"]))
        assert poison.remaining_ticks <= 0
        poison_hits: list[dict] = []
        session.bus.subscribe("combat:hit", lambda p: poison_hits.append(p)
                              if p.get("kind") == "poison" else None)
        session.step(int(spec["poison_interval"] * 3))
        assert poison_hits == [], f"毒结束后仍在掉血：{poison_hits}"
    finally:
        session.shutdown()


@case
def test_shield_absorbs_before_hp():
    """魔法盾：先扣盾、再扣血。盾还在时血量必须一点不掉。"""
    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 31)
        _learn_with_book(mage, "mofadun")
        session.cast_skill(mage, "mofadun", mage)
        session.step(2)
        shield = mage.get(Shield)
        assert shield is not None and shield.current > 0, shield

        # 放一只相邻怪来砍：伤害应该被盾吃掉，血量不动
        spawn_monster(session.world, DATA, "jiangshi",
                      x=mage.get(Position).x + 1, y=mage.get(Position).y)
        hp0 = mage.get(Vitals).hp
        session.step(40)
        assert mage.get(Shield).current < shield.max, "盾一点没消耗"
        assert mage.get(Vitals).hp == hp0, "盾还在却掉血了"
    finally:
        session.shutdown()


@case
def test_invisibility_hides_and_breaks_on_attack():
    """隐身：怪立刻丢失目标；主动出手即破隐、怪重新锁定。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 21)
        _learn_with_book(taoist, "yinshen")
        mob = spawn_monster(session.world, DATA, "kulou",
                            x=taoist.get(Position).x + 1, y=taoist.get(Position).y)
        session.step(2)
        assert mob.get(AiState).target_id is not None, "前置条件不成立：怪没锁定玩家"

        session.cast_skill(taoist, "yinshen", taoist)
        session.step(3)
        assert taoist.get(Invisible).until_tick > session.tick
        assert mob.get(AiState).target_id is None, "隐身了怪还盯着我"

        # ⚠️ 同样不能用 hp 等值断言（RegenSystem 会回血），改数落在玩家身上的命中事件
        hits: list[dict] = []
        session.bus.subscribe("combat:hit",
                              lambda p: hits.append(p) if p.get("defender") == taoist.id else None)
        session.step(30)
        assert hits == [], f"隐身期间被打：{hits}"

        session.queue_attack(taoist, mob)  # 主动出手 → 破隐
        session.step(3)
        assert taoist.get(Invisible) is None, "出手没有破隐"
        assert mob.get(AiState).target_id is not None, "破隐后怪没有重新锁定"
    finally:
        session.shutdown()


@case
def test_summon_fights_and_rewards_owner():
    """召唤：随从替主人打怪，经验与掉落记到**主人**头上（不是随从）。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 19)
        _learn_with_book(taoist, "zhaohuankulou")
        px, py = taoist.get(Position).x, taoist.get(Position).y
        exp0 = taoist.get(Progression).exp
        spawn_monster(session.world, DATA, "ji", x=px + 2, y=py)

        session.cast_skill(taoist, "zhaohuankulou", taoist)
        session.step(2)
        summons = [e for e in session.world.each(SummonOwner)
                   if e.get(SummonOwner).owner_id == taoist.id]
        assert len(summons) == 1, [e.eid for e in summons]
        assert summons[0].get(Identity).kind == "summon"
        assert summons[0].get(Identity).faction == taoist.get(Identity).faction

        session.step(200)  # 随从走过去把鸡砍了
        assert taoist.get(Progression).exp > exp0, "随从击杀没给主人经验"
        assert any(r["monster"] == "ji" for r in session.rewards()), session.rewards()
    finally:
        session.shutdown()


@case
def test_summon_respects_max_count():
    """召唤上限：再召唤不会越堆越多（旧的那只被替换）。"""
    session, player = _session()
    try:
        taoist = _make_caster(session, "taoist", 35)
        _learn_with_book(taoist, "zhaohuankulou")
        for _ in range(2):
            taoist.get(Vitals).mp = taoist.get(Vitals).max_mp
            session.cast_skill(taoist, "zhaohuankulou", taoist)
            session.step(int(SKILLS["zhaohuankulou"]["cooldown"] / session.tick_dt) + 2)
        summons = [e for e in session.world.each(SummonOwner)
                   if e.get(SummonOwner).owner_id == taoist.id]
        assert len(summons) == SKILLS["zhaohuankulou"]["summon_max"], len(summons)
    finally:
        session.shutdown()


@case
def test_entering_a_map_clears_skill_cooldown():
    """进图即清冷却：换图后**第 1 刻**就能再放，读档同理。

    冷却记的是"上一次出手的余韵"，而换图 / 读档都等于重新进图——进图之前
    攒下的那截冷却不该跟着人走（不清的话，1.2 秒的火球冷却会跟着人跨图，
    进新图还要干等 12 刻）。

    两条路必须走**同一个函数**：本项目在铺怪上已经因为两条路径各写一份而
    分叉过（见 ``world/spawn.py`` 顶部），所以这里除了验行为，还验两个模块
    拿到的是同一个 ``reset_cooldowns`` 对象——防止哪天某一侧自己 inline 一份。
    """
    from importlib import import_module

    # 用 import_module 取模块本体：``mirsolo.world`` 包把 ``travel`` 这个名字
    # 重新导出成了函数，``from mirsolo.world import travel`` 拿到的是那个函数
    travel_mod = import_module("mirsolo.world.travel")
    codec_mod = import_module("mirsolo.save.codec")
    engine_mod = import_module("mirsolo.skills.engine")

    # 只验"模块里有同名函数"是不够的：真分叉的人会留着那行 import，然后在
    # 调用点自己 clear 一份（试过，那样下面这条是绿的）。所以两条都验：
    # ① 拿到的是技能域那个函数对象；② 源码里的清除确实是调用它，而不是就地 clear。
    import inspect

    shared = getattr(travel_mod, "reset_cooldowns", None)
    assert shared is not None, "换图路径没有接 reset_cooldowns"
    assert shared is engine_mod.reset_cooldowns, "换图的 reset_cooldowns 不是技能域那个"
    for mod in (travel_mod, codec_mod):
        src = inspect.getsource(mod)
        assert getattr(mod, "reset_cooldowns", None) is shared, \
            f"{mod.__name__} 没有共用技能域的 reset_cooldowns"
        assert "reset_cooldowns(" in src, f"{mod.__name__} 没调用 reset_cooldowns"
        assert "remaining.clear()" not in src, \
            f"{mod.__name__} 自己就地 clear 了冷却账（两条路径分叉）"

    session, player = _session()
    try:
        mage = _make_caster(session, "mage", 7)
        _learn_with_book(mage, "huozhu")
        mob = spawn_monster(session.world, DATA, "ji",
                            x=mage.get(Position).x + 2, y=mage.get(Position).y)
        session.cast_skill(mage, "huozhu", mob)
        session.step(2)
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) > 0.0, "第一发没进冷却"

        # ① 换图：下一刻就该放得出去，而不是等冷却走完
        travel(session, "biqi_mine_1")
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0, "换图没清冷却"
        assert can_cast(mage, DATA, "huozhu") == (True, "ok"), can_cast(mage, DATA, "huozhu")
        px, py = mage.get(Position).x, mage.get(Position).y
        mob2 = spawn_monster(session.world, DATA, "ji", x=px + 2, y=py)
        mp0 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob2)
        session.step(1)
        assert mage.get(Vitals).mp < mp0, "换图后第 1 刻没能施放"
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) > 0.0, "施放了却没进冷却"

        # ② 读档：存档里根本没有冷却字段（Cooldowns 是运行时组件），但真实读档
        #    是往**正在跑的会话**上套（presentation/app.py: saves.load(self.session)），
        #    此刻玩家身上还挂着冷却，必须由 _clear_runtime 清掉。
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) > 0.0, "读档前不在冷却中"
        with tempfile.TemporaryDirectory() as tmp:
            SaveStore(tmp).write(session, "slot1")
            SaveStore(tmp).load(session, "slot1")
        assert mage.get(Cooldowns).remaining.get("huozhu", 0.0) == 0.0, "读档没清冷却"
        assert can_cast(mage, DATA, "huozhu") == (True, "ok"), can_cast(mage, DATA, "huozhu")
        px, py = mage.get(Position).x, mage.get(Position).y
        mob3 = spawn_monster(session.world, DATA, "ji", x=px + 2, y=py)
        mp1 = mage.get(Vitals).mp
        session.cast_skill(mage, "huozhu", mob3)
        session.step(1)
        assert mage.get(Vitals).mp < mp1, "读档后第 1 刻没能施放"
    finally:
        session.shutdown()


@case
def test_skill_kind_partition_is_pinned():
    """kind 分区钉死：数据表新增 kind 时，本测试会红——必须同时决定实现批次。"""
    kinds = {spec["kind"] for spec in SKILLS.values()}
    assert kinds <= (SUPPORTED_KINDS | PLANNED_KINDS), kinds - (SUPPORTED_KINDS | PLANNED_KINDS)
    assert SUPPORTED_KINDS & PLANNED_KINDS == frozenset()
    # 本批必须已覆盖三职业
    for class_id, expected in (("warrior", 4), ("mage", 5), ("taoist", 7)):
        owned = [s for s in SKILLS.values() if s["class"] == class_id]
        assert len(owned) == expected, (class_id, len(owned))
    assert len(SKILLS) >= 12


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
    print(f"test_skills: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
