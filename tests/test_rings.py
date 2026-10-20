"""特戒六枚（P2-S4）—— 每条规则都要能"关掉就变红"。

钉死的四件事：

1. **概率用万分比、倍率用千分比**：``bp`` 是 0..10000（``rand_int(1, 10000)``），
   ``permille`` 是 1000 = 100%。两类比率混用在本项目踩过一次，测试用字面量对齐表的读数。
2. **戒指的随机流必须独立**：戴上/摘下戒指不改变主伤害流的消耗位置——
   否则"换一枚戒指，同一局的回放就错位"（与幸运 9 同一条纪律）。
3. **效果必须落在 ECS 行为上**，不只是数据里写着：麻痹要真的让被打者停手、
   定身期间攒的刀不许在解除瞬间一起打出来、摘下隐身戒指后 buff 要自然过期。
4. **看得见**：角色面板要写出戒指名、效果、剩余次数与麻痹倒计时。

字面量原则与其他测试一致：期望值写死，数量断言尽量不从数据推导。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat.resolver import (
    EVT_DEATH,
    EVT_HIT,
    MELEE_RANGE,
    CombatContext,
    CombatResolver,
)
from mirsolo.components import (
    Attributes,
    Bag,
    Invisible,
    Loadout,
    Paralysis,
    Position,
    Progression,
    Shield,
    Vitals,
)
from mirsolo.data.loader import SLOTS, GameData
from mirsolo.items.equipment import equip, unequip
from mirsolo.items.rings import (
    EVT_REVIVE,
    RingError,
    charges_left,
    flame_damage,
    split_protect,
    try_travel,
)
from mirsolo.presentation.panels import build_character_panel
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

# ── 数据表口径的**字面量**（改表必须同步改这里）──────────────────
MABI_BP = 300                 # 麻痹戒指：万分比
MABI_TICKS = 20               # 麻痹持续刻数
INVIS_TICKS = 40              # 隐身持续时间
INVIS_RECOVER = 30            # 出手破隐后的重结冷却
REVIVE_CHARGES = 3            # 复活戒指总次数
REVIVE_HP_PERMILLE = 500      # 复活血量（千分比）
PROTECT_PERMILLE = 1000       # 护身全额代偿
FLAME_PERMILLE = 300          # 火焰附加倍率
TELEPORT_HOME = "biqi_field"  # 传送戒指回城点
TELEPORT_COOLDOWN = 300       # 传送冷却刻数

#: 六枚戒指 → 效果（"六效齐备"的字面量断言）
SIX_RINGS = {
    "mabi_jiezhi": "paralysis",
    "hushen_jiezhi": "protect",
    "fuhuo_jiezhi": "revive",
    "chuansong_jiezhi": "teleport",
    "yinshen_jiezhi": "invis",
    "huoyan_jiezhi": "flame",
}


def case(fn):
    CASES.append(fn)
    return fn


def _level_up(player, level: int = 50, class_id: str = "warrior") -> None:
    prog = player.get(Progression)
    prog.level = level
    prog.exp = 0
    apply_growth(player, DATA.classes["classes"][class_id])


def _armed(ring: str | None = None, *, level: int = 50, seed: int = 5,
           class_id: str = "warrior"):
    """一个 Lv50 的测试角色，可选戴一枚戒指（走 ``equip`` 真路径）。"""
    session = build_session(data=DATA, seed=seed)
    player = populate(session, with_spawns=False)["player"]
    _level_up(player, level, class_id)
    if ring is not None:
        player.get(Bag).add(ring, 1)
        equip(player, DATA, ring)
    return session, player


def _dummy(session, *, eid: str = "ring_dummy", hp: int = 10**6,
           dc: tuple[int, int] = (0, 0), ac: tuple[int, int] = (0, 0),
           agility: int = 0, accuracy: int = 5000):
    """一块不会死的沙包/木桩：把 hp 拉高、把进攻/防御拉到便于断言的位置。"""
    mob = spawn_monster(session.world, DATA, "kulou", eid=eid)
    attr = mob.get(Attributes)
    attr.dc, attr.ac = tuple(dc), tuple(ac)
    attr.agility, attr.accuracy = agility, accuracy
    vitals = mob.get(Vitals)
    vitals.hp = vitals.max_hp = hp
    return mob


def _step_into_reach(player, target):
    """站到**够得着**的格子再出手（普攻击程 = 切比雪夫 ``MELEE_RANGE`` 格）。

    木桩默认摆在 (0,0) 而角色站在地图中心，加了普攻距离门之后隔着半张图的刀
    一滴血都不结算。"走过去"这一步放在这里，用例就不必各写一份坐标算术。
    """
    pos, tpos = player.get(Position), target.get(Position)
    pos.x, pos.y = tpos.x + MELEE_RANGE, tpos.y


def _expect(code: str, fn, *args, **kwargs):
    """断言被拒，且拒绝码正好是 ``code``。"""
    try:
        fn(*args, **kwargs)
    except RingError as exc:
        assert exc.code == code, f"期望拒绝码 {code}，实际 {exc.code}"
        return exc
    raise AssertionError(f"本应被拒（{code}），却成功了")


# ── 1. 数据面：六效齐备、一个 slot ──────────────────────────

@case
def test_six_ring_effects_are_all_implemented():
    """六枚戒指各占一种效果——少一种就等于"文档里有、游戏里拿不到"。"""
    for item_id, effect in SIX_RINGS.items():
        spec = ITEMS.get(item_id)
        assert spec is not None, f"缺少戒指 {item_id}"
        assert spec["slot"] == "ring", f"{item_id} 不在 ring 槽"
        assert spec["ring_effect"] == effect, f"{item_id} 效果变了：{spec['ring_effect']}"
        for cid in ("warrior", "mage", "taoist"):
            assert cid in spec["class"], f"{item_id} 对 {cid} 不可用"


@case
def test_ring_is_a_single_slot():
    """``SLOTS`` 里只有一个 ring：本项目不做"左右手各一枚"，
    两枚叠加（护身 + 复活）会让"这刀到底死没死"失去讨论意义。
    """
    assert SLOTS.count("ring") == 1, SLOTS
    session, player = _armed("fuhuo_jiezhi")
    player.get(Bag).add("mabi_jiezhi", 1)
    equip(player, DATA, "mabi_jiezhi")
    assert player.get(Loadout).slots["ring"] == "mabi_jiezhi"
    assert "fuhuo_jiezhi" in player.get(Bag).items, "换下来的戒指应回背包"


@case
def test_ring_effects_only_live_on_rings():
    """非戒指装备不许带 ``ring_effect``——否则会凭空多出第二条生效路径。"""
    for item_id, spec in ITEMS.items():
        if spec.get("slot") == "ring":
            continue
        assert "ring_effect" not in spec, f"{item_id} 偷偷带了 ring_effect"


# ── 2. 麻痹 ────────────────────────────────────────────────

@case
def test_paralysis_fires_and_freezes_for_the_declared_ticks():
    """把 bp 强行拉到万分之万来验证"每刀必麻"，再核验 until_tick 的字面量。"""
    data = GameData.load()
    data.items["items"]["mabi_jiezhi"]["ring_param"]["bp"] = 10000
    session = build_session(data=data, seed=5)
    player = populate(session, with_spawns=False)["player"]
    _level_up(player, 50)
    player.get(Bag).add("mabi_jiezhi", 1)
    equip(player, data, "mabi_jiezhi")
    target = _dummy(session, eid="para_target")
    target.get(Attributes).agility = 0

    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=0)
    resolver = CombatResolver(ctx)
    fired = 0
    for tick in range(120):
        ctx.tick = tick
        outcome = resolver.physical(player, target)
        if not outcome.hit:
            continue
        buff = target.get(Paralysis)
        assert buff is not None, f"tick={tick}: 万分之万却没有麻痹"
        assert buff.until_tick == tick + MABI_TICKS, buff.until_tick
        assert buff.source == player.id
        fired += 1
    assert fired >= 100, f"120 刻只触发 {fired} 次，命中钳制可能挡掉了太多刀"


@case
def test_paralysis_rate_tracks_the_table_value():
    """真实的 300/10000 必须落在统计带里——防止把万分比当百分比写。"""
    session, player = _armed("mabi_jiezhi")
    target = _dummy(session, eid="rate_target")
    target.get(Attributes).agility = 0
    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=0)
    resolver = CombatResolver(ctx)
    hits = fired = 0
    for tick in range(2000):
        target.remove(Paralysis)   # 每次重计，避免叠看见连环触发
        ctx.tick = tick
        outcome = resolver.physical(player, target)
        if not outcome.hit:
            continue
        hits += 1
        if target.get(Paralysis) is not None:
            fired += 1
    rate = fired / max(1, hits)
    table_rate = MABI_BP / 10000
    assert abs(rate - table_rate) < 0.02, (
        f"实际触发率 {rate:.4f} 与表值 {table_rate} 差太多（当成百分比写了？）")


@case
def test_paralysis_stops_movement_and_strikes_but_never_bursts():
    """被麻痹的人不动、也不出手；攒下的刀不许在解除瞬间一次性打出来。"""
    session, player = _armed()
    target = _dummy(session, eid="freeze_target")
    # 把劣势项归零：目标不还手，玩家也不会被打死，测的就是"能不能出手"这一件事
    target.get(Attributes).dc = [0, 0]
    player.add(Paralysis(until_tick=10**6, source=None))
    _step_into_reach(player, target)

    pos = player.get(Position)
    start_x, start_y = pos.x, pos.y
    session.world.set_data("move_intent", (1, 0))
    session.step(20)
    assert (pos.x, pos.y) == (start_x, start_y), "被麻痹却还能走动"

    hp_before = target.get(Vitals).hp
    session.queue_attack(player, target)
    session.step(2)
    assert target.get(Vitals).hp == hp_before, "被麻痹却还能出手"
    assert session.world.get_data("attack_intents") == [], (
        "定身期间的意图必须作废，不能攒着等解除")

    player.get(Paralysis).until_tick = 0  # 解除
    session.queue_attack(player, target)
    session.step(2)
    assert target.get(Vitals).hp < hp_before, "解除后应该打得动了"


@case
def test_paralysis_monsters_cannot_chase_or_retaliate():
    """覆盖面也要到怪身上：定身期间的怪既追不上人，也排不了攻击的队。"""
    session, player = _armed()
    target = _dummy(session, eid="frozen_mob")
    target.get(Position).x = player.get(Position).x + 2
    target.get(Position).y = player.get(Position).y
    target.add(Paralysis(until_tick=10**6, source=player.id))

    hp_before = player.get(Vitals).hp
    frozen_at = target.get(Position).x
    session.step(40)
    assert target.get(Position).x == frozen_at, "被麻痹的怪还在追"
    assert player.get(Vitals).hp == hp_before, "被麻痹的怪还在还手"


# ── 3. 护身 ────────────────────────────────────────────────

@case
def test_protect_splits_damage_by_the_declared_permille():
    """1000‰ = 全额代偿；MP 不够时把顶不动的那部分照掉血（不是全有全无）。"""
    session, player = _armed("hushen_jiezhi")
    vitals = player.get(Vitals)
    vitals.max_hp, vitals.hp = 200, 200
    vitals.max_mp, vitals.mp = 50, 50

    assert split_protect(DATA, player, 30) == (30, 0), "MP 充足应全额代偿"
    assert split_protect(DATA, player, 80) == (50, 30), "MP 不够时顶不动的照掉血"
    assert split_protect(DATA, player, 0) == (0, 0), "零伤害不该有副作用"


@case
def test_protect_ring_keeps_the_hp_bar_still():
    """端到端：戴着护身挨刀，血条一动不动，扣的是蓝。"""
    session, player = _armed("hushen_jiezhi")
    vitals = player.get(Vitals)
    vitals.max_hp, vitals.hp = 200, 200
    vitals.max_mp, vitals.mp = 500, 500      # MP 必须管够：这条测的是"全额代偿"
    attacker = _dummy(session, eid="beater", dc=(100, 100), agility=0)

    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=3)
    outcome = CombatResolver(ctx).physical(attacker, player)
    assert outcome.hit and outcome.damage > 0
    assert vitals.hp == 200, f"护身戒指没顶住：hp={vitals.hp}"
    assert vitals.mp == 500 - outcome.damage, "掉的应该是 MP，且等于伤害量"

    vitals.mp = 0  # 没蓝了就该正常掉血
    hp_before = vitals.hp
    CombatResolver(ctx).physical(attacker, player)
    assert vitals.hp < hp_before, "没 MP 时护身必须失效，否则等于无限蓝盾"


@case
def test_shield_is_spent_before_the_ring():
    """顺序：魔法盾 → 护身戒指。反过来的话两层吸收会叠成一个翻倍的血池。"""
    session, player = _armed("hushen_jiezhi")
    vitals = player.get(Vitals)
    vitals.max_hp, vitals.hp = 200, 200
    vitals.max_mp, vitals.mp = 500, 500
    player.add(Shield(current=5, max=5))
    attacker = _dummy(session, eid="shielder", dc=(100, 100), agility=0)

    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=3)
    outcome = CombatResolver(ctx).physical(attacker, player)
    dealt = outcome.damage
    assert dealt > 5, f"这一刀伤害 {dealt} 太小，测不出前后顺序"
    shield = player.get(Shield)
    assert shield.current == 0, "盾应该先被吃完"
    assert vitals.mp == 500 - (dealt - 5), "剩下的才轮到戒指用 MP 顶"
    assert vitals.hp == 200, "盾 + 戒指都没顶住？"


# ── 4. 复活 ────────────────────────────────────────────────

@case
def test_revive_swallows_the_lethal_blow_without_a_death_event():
    """关键不是"血回来了"，而是**压根没死过**：EVT_DEATH 一次都不许发出。"""
    session, player = _armed("fuhuo_jiezhi")
    deaths: list[dict] = []
    revives: list[dict] = []
    session.bus.subscribe(EVT_DEATH, deaths.append)
    session.bus.subscribe(EVT_REVIVE, revives.append)

    vitals = player.get(Vitals)
    vitals.max_hp = 200
    killer = _dummy(session, eid="executioner", dc=(500, 500), agility=0)
    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=9)

    for used in range(REVIVE_CHARGES):
        vitals.hp = 200
        CombatResolver(ctx).physical(killer, player)
        assert deaths == [], f"第 {used + 1} 次复活血回来了，但死亡事件还是发出去了"
        assert len(revives) == used + 1, f"第 {used + 1} 次没发出复活事件"
        assert vitals.hp == 200 * REVIVE_HP_PERMILLE // 1000 == 100, vitals.hp
        assert charges_left(DATA, player) == REVIVE_CHARGES - used - 1

    # 额度耗尽：这次必须真的死
    vitals.hp = 200
    CombatResolver(ctx).physical(killer, player)
    assert deaths and deaths[0]["defender"] == player.id, "额度用尽后没有死亡事件"
    assert vitals.hp == 0


@case
def test_revive_covers_the_exact_lethal_boundary():
    """**边界**：伤害正好等于剩余血量也算致死——写成 ``amount > hp`` 就会漏救。

    这条线上失败很隐蔽：血量亏一点点时不救，看上去只是"随机地偶尔没救回来"。
    """
    session, player = _armed("fuhuo_jiezhi")
    deaths: list[dict] = []
    session.bus.subscribe(EVT_DEATH, deaths.append)
    vitals = player.get(Vitals)
    vitals.max_hp = 5000
    killer = _dummy(session, eid="boundary", dc=(200, 200), agility=0)
    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=9)

    vitals.hp = 5000
    outcome = CombatResolver(ctx).physical(killer, player)
    dealt = outcome.damage
    assert 0 < dealt < 5000, f"沙包的伤害 {dealt} 不适合做边界测试"
    assert charges_left(DATA, player) == REVIVE_CHARGES, "这一刀不该触发复活"

    vitals.hp = dealt  # 压到分界线上（同一 tick 同一条流，伤害还是这个数）
    CombatResolver(ctx).physical(killer, player)
    assert deaths == [], f"边界上没救回来，死了：hp={vitals.hp}"
    assert charges_left(DATA, player) == REVIVE_CHARGES - 1
    assert vitals.hp == 5000 * REVIVE_HP_PERMILLE // 1000, vitals.hp


@case
def test_revive_does_not_apply_without_the_ring():
    """同一刀、同一个 tick，摘掉戒指就得死——证明救回来的是戒指，不是 hp 算错。"""
    session, player = _armed()
    vitals = player.get(Vitals)
    vitals.max_hp, vitals.hp = 200, 200
    killer = _dummy(session, eid="noring_killer", dc=(500, 500), agility=0)
    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus, tick=9)
    CombatResolver(ctx).physical(killer, player)
    assert vitals.hp == 0, "没戴复活戒指却没死"
    assert charges_left(DATA, player) is None


# ── 5. 传送 ────────────────────────────────────────────────

@case
def test_teleport_goes_home_and_then_cools_down():
    session, player = _armed("chuansong_jiezhi")
    session.map_id = "chiyue_valley"
    session.world.set_data("current_map", "chiyue_valley")

    result = try_travel(DATA, session, player, tick=1000)
    assert result["to"] == TELEPORT_HOME, result
    _expect("same_map", try_travel, DATA, session, player, tick=1200)

    # 冷却要单独测：先回到别的图，否则会先被 "same_map" 拦下
    session.map_id = "chiyue_valley"
    session.world.set_data("current_map", "chiyue_valley")
    _expect("cooling", try_travel, DATA, session, player, tick=1000 + TELEPORT_COOLDOWN - 1)
    result = try_travel(DATA, session, player, tick=1000 + TELEPORT_COOLDOWN)
    assert result["to"] == TELEPORT_HOME, "冷却结束后应当能再传一次"


@case
def test_teleport_refuses_by_reason():
    """每个拒绝情形一个稳定 code——窗口层靠它翻成人话，写错会造成误导提示。"""
    session, player = _armed()
    _expect("no_ring", try_travel, DATA, session, player, tick=1)

    session, wrong = _armed("fuhuo_jiezhi")
    session.map_id = "chiyue_valley"
    session.world.set_data("current_map", "chiyue_valley")
    _expect("wrong_ring", try_travel, DATA, session, wrong, tick=1)

    session, dead = _armed("chuansong_jiezhi")
    session.map_id = "chiyue_valley"
    session.world.set_data("current_map", "chiyue_valley")
    dead.get(Vitals).hp = 0
    _expect("dead", try_travel, DATA, session, dead, tick=1)

    broken = GameData.load()
    broken.items["items"]["chuansong_jiezhi"]["ring_param"]["map"] = "atlantis"
    session, lost = _armed("chuansong_jiezhi")
    session.map_id = "chiyue_valley"
    session.world.set_data("current_map", "chiyue_valley")
    _expect("unknown_map", try_travel, broken, session, lost, tick=1)


# ── 6. 隐身 ────────────────────────────────────────────────

@case
def test_invis_ring_cloaks_breaks_on_strike_and_recloaks_later():
    session, player = _armed("yinshen_jiezhi")
    target = _dummy(session, eid="invis_target")
    target.get(Attributes).dc = [0, 0]

    session.step(1)
    buff = player.get(Invisible)
    assert buff is not None and buff.until_tick == 1 + INVIS_TICKS, buff
    _step_into_reach(player, target)

    session.queue_attack(player, target)
    session.step(1)                       # 这一刻：先结隐 → 再出手 → 出手即破隐
    buff = player.get(Invisible)
    assert buff is None or buff.until_tick <= 2, f"出手没有破隐：{buff}"

    session.step(INVIS_RECOVER - 2)       # 重结冷却窗口内不许偷着再隐
    buff = player.get(Invisible)
    assert buff is None or buff.until_tick <= 2, f"破隐后立刻又结上了：{buff}"

    session.step(2)                       # 越过 recover_ticks
    assert player.get(Invisible).until_tick > session.tick, "冷却结束后没有重新结隐"


@case
def test_invis_expires_naturally_once_the_ring_comes_off():
    """持续型 buff 的死线测试：摘下戒指后不许还有（被续过的）隐身。"""
    session, player = _armed("yinshen_jiezhi")
    session.step(1)
    unequip(player, DATA, "ring")
    player.get(Invisible).until_tick = 0
    session.step(5)
    assert player.get(Invisible).until_tick == 0, "戒指摘了还在持续结隐"


# ── 7. 火焰 ────────────────────────────────────────────────

@case
def test_flame_ignores_the_target_armour():
    """火焰伤害不看 ac/mac：换一身重甲，追加的那一下必须一个数都不差。"""
    soft_session, soft = _armed("huoyan_jiezhi")
    hard_session, hard = _armed("huoyan_jiezhi")
    soft_target = _dummy(soft_session, eid="flame_soft", ac=(0, 0), agility=0)
    hard_target = _dummy(hard_session, eid="flame_hard", ac=(60, 60), agility=0)

    hits1, hits2 = [], []
    soft_session.bus.subscribe(EVT_HIT, hits1.append)
    hard_session.bus.subscribe(EVT_HIT, hits2.append)
    ctx1 = CombatContext(seed=5, world=soft_session.world, bus=soft_session.bus, tick=0)
    ctx2 = CombatContext(seed=5, world=hard_session.world, bus=hard_session.bus, tick=0)
    for tick in range(60):
        ctx1.tick = ctx2.tick = tick
        CombatResolver(ctx1).physical(soft, soft_target)
        CombatResolver(ctx2).physical(hard, hard_target)

    flame1 = [(h["tick"], h["damage"]) for h in hits1 if h["kind"] == "flame"]
    flame2 = [(h["tick"], h["damage"]) for h in hits2 if h["kind"] == "flame"]
    main1 = [(h["tick"], h["damage"]) for h in hits1 if h["kind"] == "physical"]
    main2 = [(h["tick"], h["damage"]) for h in hits2 if h["kind"] == "physical"]
    assert flame1 and len(flame1) == len(flame2), (len(flame1), len(flame2))
    assert flame1 == flame2, "火焰伤害被目标防御影响了（元素伤害应无视防御）"
    assert all(dmg > 0 for _t, dmg in flame1), "火焰出现了 0 伤害的无效追加"
    assert main1 != main2, "对照组失效：主伤害通路没吃防御，这条测试没意义"


@case
def test_flame_only_fires_for_its_owner():
    """没戴戒指就没有追加伤害——确认伤害差不是别的副作用造成的。"""
    session, bare = _armed()
    target = _dummy(session, eid="flameless", agility=0)
    hits: list[dict] = []
    session.bus.subscribe(EVT_HIT, hits.append)
    ctx = CombatContext(seed=5, world=session.world, bus=session.bus, tick=0)
    for tick in range(60):
        ctx.tick = tick
        CombatResolver(ctx).physical(bare, target)
    assert not [h for h in hits if h["kind"] == "flame"], "没戴火焰戒指却有火焰伤害"
    assert flame_damage(DATA, bare, None) == 0


# ── 8. 随机流隔离 ──────────────────────────────────────────

@case
def test_ring_rolls_leave_the_main_stream_untouched():
    """换戒指不能挪动主伤害流：同种子同 tick，命中与伤害必须逐位一致。

    这条看着吹毛求疵，实际是回放的分水岭——一旦戒指参与主流掷点，
    "中途捡到一枚戒指"之后的所有战斗都不可复算了。
    """
    session, ringed = _armed("fuhuo_jiezhi")
    session2, bare = _armed()
    a = _dummy(session, eid="stream_a", agility=0)
    b = _dummy(session2, eid="stream_b", agility=0)
    ctx1 = CombatContext(seed=5, world=session.world, bus=session.bus, tick=0)
    ctx2 = CombatContext(seed=5, world=session2.world, bus=session2.bus, tick=0)
    for tick in range(300):
        ctx1.tick = ctx2.tick = tick
        left = CombatResolver(ctx1).physical(ringed, a)
        right = CombatResolver(ctx2).physical(bare, b)
        assert left.as_dict() == right.as_dict(), f"tick={tick}: 戴戒指改变了主伤害流"


# ── 9. 看得见：面板 ────────────────────────────────────────

@case
def test_character_panel_shows_ring_effect_and_charges():
    _, player = _armed("fuhuo_jiezhi")
    joined = " ".join(t.text for t in build_character_panel(player=player,
                                                            data=DATA).texts)
    assert "戒指 复活戒指（复活）" in joined, joined
    assert f"剩余 {REVIVE_CHARGES} 次" in joined, joined

    _, plain = _armed()
    bare = " ".join(t.text for t in build_character_panel(player=plain,
                                                          data=DATA).texts)
    assert "戒指" not in bare, "没戴戒指却显示了戒指行"


@case
def test_character_panel_shows_the_paralysis_countdown():
    _, player = _armed()
    player.add(Paralysis(until_tick=50))
    texts = build_character_panel(player=player, data=DATA, tick=20).texts
    joined = " ".join(t.text for t in texts)
    assert "被麻痹：还剩 30 刻" in joined, joined

    expired = " ".join(t.text for t in build_character_panel(
        player=player, data=DATA, tick=99).texts)
    assert "被麻痹" not in expired, "麻痹过期了还在显示"


@case
def test_equipment_rows_label_the_ring_effect():
    """戒指的属性加成都很低，只看数值玩家会以为这是件垃圾——效果名必须上屏。"""
    _, player = _armed("mabi_jiezhi")
    rows = build_character_panel(player=player, data=DATA).rows
    ring_row = next(r for r in rows if r.action == "unequip" and r.item == "ring")
    assert "特戒·麻痹" in ring_row.detail, ring_row.detail
    assert "麻痹戒指" in ring_row.text, ring_row.text


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
    print(f"test_rings: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
