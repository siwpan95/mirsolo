"""沙巴克攻城战役：波次推进、准入锁定、失败出口、奖励、增援上限、确定性。

这里盯的是「单机化改造最容易翻车」的六件事：

1. **旗台没打下就能砍** —— 玩家会以为自己砍错了东西（准入锁定失效）；
2. **打掉城门不刷下一波** —— 战役卡死，没出口；
3. **玩家死了还在刷增援** —— 回来一看满地残局；
4. **超时不判负** —— 挂机也能"打赢"；
5. **奖励重复发** —— 目标 ``kind="guard"`` 挂了会不会走 ``DeathRewardSystem``；
6. **同种子铺兵不一样** —— 重开一次阵型就变，像随机生成的地图。

数值全部用 ``data/sieges.json`` 里的字面量（外城 1200 / 内城 1800 / 旗台 2600，
``sabak`` 三波守军 7 / 7 / 8），不调生产函数现算——自指式断言抓不住 bug。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    Bag,
    Position,
    Progression,
    SiegeObjective,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.siege.campaign import (
    EVT_SIEGE_LOST,
    EVT_SIEGE_OBJECTIVE_DOWN,
    EVT_SIEGE_WAVE,
    EVT_SIEGE_WON,
    GUARD_PREFIX,
    KEY_SIEGE,
    LOST,
    RUNNING,
    WON,
    SiegeError,
    SiegeState,
    siege_status,
    spawn_objective,
    spawn_wave_guards,
    start_siege,
)

CASES = []
DATA = GameData.load()

#: 三波的字面量：外城 1200 / 内城 1800 / 旗台 2600
WAVE_HP = (1200, 1800, 2600)
#: 三波守军数量字面量
WAVE_GUARDS = (7, 7, 8)


def case(fn):
    CASES.append(fn)
    return fn


def _session(*, player_class: str = "warrior"):
    """空图会话（不铺常规怪），免得野外怪混进攻城点兵。"""
    session = build_session(data=DATA, seed=7, map_id="biqi_field")
    player = populate(session, player_class=player_class, with_spawns=False)["player"]
    return session, player


def _siege(*, player_class: str = "warrior"):
    session, player = _session(player_class=player_class)
    started = start_siege(session, "sabak")
    return session, player, started


def _events(session, name: str) -> list[dict]:
    """取总线上的事件 payload（现订现收，调用之后才发生的才进列表）。"""
    got: list[dict] = []
    session.bus.subscribe(name, got.append)
    return got


def _kill(world, eid: str) -> None:
    """把某个实体血清零（模拟被打倒）。"""
    entity = world.get_entity(eid)
    entity.get(Vitals).hp = 0


def _monsters_alive(world) -> int:
    from mirsolo.components import Identity

    return sum(1 for entity in world.each(Identity, Vitals)
               if entity.get(Identity).kind == "monster" and entity.get(Vitals).alive)


# ── 开局 ────────────────────────────────────────

@case
def test_start_siege_places_player_and_first_wave():
    """开局：玩家落在城外 [8,32]，第一波目标是外城城门，1200 血，7 个守军。"""
    session, player, started = _siege()

    assert session.map_id == "sabak", session.map_id
    assert (player.get(Position).x, player.get(Position).y) == (8.0, 32.0)

    assert started["wave"] == 0
    assert started["guards"] == WAVE_GUARDS[0]
    assert started["objective_hp"] == WAVE_HP[0]

    objective = session.world.get_entity(started["objective"])
    assert objective is not None
    assert objective.get(SiegeObjective).wave == 0
    assert objective.get(SiegeObjective).objective == "gate"
    assert _monsters_alive(session.world) == WAVE_GUARDS[0]
    assert siege_status(session.world)["status"] == RUNNING


@case
def test_siege_objective_is_guard_kind_not_monster():
    """目标必须是 ``kind="guard"``：``kind="monster"`` 会被死亡奖励系统当成怪发经验掉落。"""
    session, _, started = _siege()
    from mirsolo.components import Identity

    ident = session.world.get_entity(started["objective"]).get(Identity)
    assert ident.kind == "guard", ident.kind


@case
def test_start_siege_twice_is_rejected():
    """进行中再开一次必须被拒——否则会铺两套城门叠在一起。"""
    session, _, _ = _siege()
    try:
        start_siege(session, "sabak")
    except SiegeError as exc:
        assert exc.code == "already_running", exc.code
    else:
        raise AssertionError("重复开启没有被拒")


@case
def test_unknown_siege_id_is_rejected():
    session, _ = _session()
    try:
        start_siege(session, "no_such_siege")
    except SiegeError as exc:
        assert exc.code == "unknown_siege", exc.code
    else:
        raise AssertionError("未知战役没有被拒")


# ── 准入锁定 ─────────────────────────────────────

@case
def test_only_current_wave_objective_is_damageable():
    """没打下城门就砍旗台？旗台根本不吃伤害（``invulnerable``）。"""
    session, player, _ = _siege()
    world = session.world
    spec = DATA.sieges["sieges"]["sabak"]

    # 手动把第三波旗台摆进来，模拟"提前跑到旗台"
    banner = spawn_objective(world, DATA, spec, 2, eid="siege_obj:probe")
    session.step(1)

    assert banner.get(SiegeObjective).invulnerable is True

    hp_before = banner.get(Vitals).hp
    session.queue_attack(player, banner)
    session.step(1)
    assert banner.get(Vitals).hp == hp_before, "锁定目标吃到了伤害"


@case
def test_current_objective_is_not_invulnerable():
    session, _, started = _siege()
    session.step(1)
    objective = session.world.get_entity(started["objective"])
    assert objective.get(SiegeObjective).invulnerable is False


@case
def test_wave_advance_unlocks_next_and_relocks_previous():
    """进第二波后：内城门可打，外城城门（已倒）应被锁。"""
    session, _, started = _siege()
    world = session.world
    gate_eid = started["objective"]

    _kill(world, gate_eid)
    session.step(1)

    status = siege_status(world)
    assert status["wave"] == 1, status
    assert status["objective"] == "inner_gate"
    # 外城城门已经倒了，不该还留着"可打"的标记
    old = world.get_entity(gate_eid)
    if old is not None:
        assert old.get(SiegeObjective).invulnerable is True


# ── 推进与胜利 ───────────────────────────────────

@case
def test_three_waves_then_won_with_literal_hp():
    """依次打掉 1200 / 1800 / 2600 三个目标 → ``won``。"""
    session, _, started = _siege()
    world = session.world
    waves = _events(session, EVT_SIEGE_WAVE)
    downs = _events(session, EVT_SIEGE_OBJECTIVE_DOWN)
    won = _events(session, EVT_SIEGE_WON)

    objective_id = started["objective"]
    for index, expect_hp in enumerate(WAVE_HP):
        objective = world.get_entity(objective_id)
        assert objective.get(Vitals).max_hp == expect_hp, (index, objective.get(Vitals).max_hp)
        _kill(world, objective_id)
        session.step(1)
        status = siege_status(world)
        if index < len(WAVE_HP) - 1:
            assert status["wave"] == index + 1, status
            assert status["objective_max"] == WAVE_HP[index + 1], status
            assert _monsters_alive(world) == WAVE_GUARDS[index + 1], _monsters_alive(world)
            objective_id = status["objective_hp"] is not None and world.get_data(KEY_SIEGE).objective_id
        else:
            assert status["status"] == WON, status

    assert len(downs) == 3, downs
    assert [d["objective"] for d in downs] == ["gate", "inner_gate", "banner"]
    assert len(won) == 1, won
    assert len(waves) == 2, waves  # 开局发一次 + 两次推进


@case
def test_won_grants_gold_items_and_exp():
    """奖励：20000 金 + 2 件物品 + 12000 经验。"""
    session, player, started = _siege()
    world = session.world
    gold_before = player.get(Wallet).gold
    bag_before = dict(player.get(Bag).items)

    objective_id = started["objective"]
    for index in range(len(WAVE_HP)):
        _kill(world, objective_id)
        session.step(1)
        state = world.get_data(KEY_SIEGE)
        objective_id = state.objective_id or objective_id

    assert world.get_data(KEY_SIEGE).status == WON
    assert player.get(Wallet).gold == gold_before + 20000
    bag_after = dict(player.get(Bag).items)
    assert bag_after.get("zhongxingkuijia", 0) == bag_before.get("zhongxingkuijia", 0) + 1
    assert bag_after.get("zhanmadao", 0) == bag_before.get("zhanmadao", 0) + 1


@case
def test_won_rewards_exp_through_same_path_as_kills():
    """升级走 ``add_exp`` + ``apply_growth``，与打怪同一条路（等级必须真的涨）。"""
    session, player, started = _siege()
    world = session.world
    prog = player.get(Progression)
    level_before = prog.level

    objective_id = started["objective"]
    for _ in range(len(WAVE_HP)):
        _kill(world, objective_id)
        session.step(1)
        state = world.get_data(KEY_SIEGE)
        objective_id = state.objective_id or objective_id

    assert prog.level > level_before, (level_before, prog.level)


@case
def test_objective_death_does_not_double_reward():
    """目标 ``kind="guard"`` 不走死亡奖励系统：杀目标不该产出击杀计数。"""
    session, _, started = _siege()
    _kill(session.world, started["objective"])
    session.step(1)
    # 只倒了城门，一个守军都没打；击杀流水应当为空
    assert session.kill_counts() == {}, session.kill_counts()


# ── 失败出口 ─────────────────────────────────────

@case
def test_player_death_loses_and_clears_field():
    """玩家倒下 → 判负，且场上守军/目标全部清掉，不留残局。"""
    session, player, _ = _siege()
    world = session.world
    lost = _events(session, EVT_SIEGE_LOST)

    player.get(Vitals).hp = 0
    session.step(1)

    state = world.get_data(KEY_SIEGE)
    assert state.status == LOST, state.status
    assert state.objective_id is None
    assert len(lost) == 1, lost
    assert _monsters_alive(world) == 0, _monsters_alive(world)
    assert not [e for e in world.list_entity_ids() if e.startswith(GUARD_PREFIX)], world.list_entity_ids()
    assert siege_status(world)["finished"] is True


@case
def test_timeout_loses():
    """超过 ``time_limit_ticks=18000`` 判负，不用真跑那么久——把起始刻度往前挪。"""
    session, _, _ = _siege()
    world = session.world
    state = world.get_data(KEY_SIEGE)
    state.started_tick = -18000  # tick 0 时 elapsed 恰为 18000
    session.step(1)
    assert state.status == LOST, state.status


@case
def test_lost_siege_stops_reinforcing():
    """判负后系统停摆：再推多少 tick 都不该冒出新守军。"""
    session, player, _ = _siege()
    world = session.world
    player.get(Vitals).hp = 0
    session.step(1)
    assert world.get_data(KEY_SIEGE).status == LOST
    session.step(200)
    assert _monsters_alive(world) == 0, _monsters_alive(world)


@case
def test_objective_removed_externally_fails_instead_of_hanging():
    """目标实体被清掉（例如误换图）→ 判负，而不是留一个打不动的僵局。"""
    session, _, started = _siege()
    session.world.remove_entity(started["objective"])
    session.step(1)
    assert session.world.get_data(KEY_SIEGE).status == LOST


# ── 增援 ────────────────────────────────────────

@case
def test_reinforce_respects_every_ticks_and_max():
    """外城增援：每 600 tick 投 1 个，最多 4 个（``max``）。"""
    session, _, started = _siege()
    world = session.world
    state = world.get_data(KEY_SIEGE)
    base = _monsters_alive(world)

    # 第一次增援：刚开局 last_reinforce_tick=0，跑到 600 tick 触发
    session.step(600)
    assert _monsters_alive(world) == base + 1, _monsters_alive(world)
    # 未满 600 不应再投
    session.step(599)
    assert _monsters_alive(world) == base + 1, _monsters_alive(world)
    session.step(1)
    assert _monsters_alive(world) == base + 2, _monsters_alive(world)

    # 一直推到 max=4
    session.step(600 * 4)
    assert state.reinforced == 4, state.reinforced
    assert _monsters_alive(world) == base + 4, _monsters_alive(world)

    # 到顶后再推也不新增
    session.step(600 * 3)
    assert _monsters_alive(world) == base + 4, _monsters_alive(world)
    assert state.reinforced == 4, state.reinforced


# ── 确定性 ───────────────────────────────────────

@case
def test_guard_positions_are_deterministic_for_same_seed():
    """同种子 → 同阵型。重开一次怪站的位置必须一模一样。"""
    first = []
    for _ in range(2):
        session, _ = _session()
        start_siege(session, "sabak")
        rows = []
        for eid in sorted(e for e in session.world.list_entity_ids() if e.startswith(GUARD_PREFIX)):
            pos = session.world.get_entity(eid).get(Position)
            rows.append((eid, pos.x, pos.y))
        first.append(rows)
    assert first[0] == first[1], (first[0], first[1])
    assert len(first[0]) == WAVE_GUARDS[0]


@case
def test_different_seed_changes_guard_positions():
    """不同种子 → 阵型必须不同（否则 Rng 流根本没接上坐标）。"""
    layouts = []
    for seed in (7, 8):
        session = build_session(data=DATA, seed=seed, map_id="biqi_field")
        populate(session, with_spawns=False)
        start_siege(session, "sabak")
        layouts.append(tuple(
            (round(session.world.get_entity(e).get(Position).x, 3),
             round(session.world.get_entity(e).get(Position).y, 3))
            for e in sorted(x for x in session.world.list_entity_ids() if x.startswith(GUARD_PREFIX))
        ))
    assert layouts[0] != layouts[1], "两个种子铺出了同一个阵型"


@case
def test_spawn_wave_guards_count_matches_spec():
    """直接调铺兵函数：三波守军数与数据表字面量一致。"""
    spec = DATA.sieges["sieges"]["sabak"]
    for wave_index, expect in enumerate(WAVE_GUARDS):
        session, _ = _session()
        guards = spawn_wave_guards(session.world, DATA, spec, wave_index, seed=7)
        assert len(guards) == expect, (wave_index, len(guards))
        for guard in guards:
            assert guard.get(Vitals).alive


@case
def test_wave_advance_retires_previous_wave_guards_and_objective():
    """进第二波时：第一波的守军与城门必须退场，否则三道门的兵会全堆在旗台下。"""
    session, _, started = _siege()
    world = session.world
    gate_eid = started["objective"]

    _kill(world, gate_eid)
    session.step(1)

    # 旧城门实体已经清掉（不是"留着标 invulnerable"）
    assert not world.has_entity(gate_eid), "旧城门没有退场"
    # 第一波守军也全部清掉，只剩第二波的 7 个
    wave0 = [e for e in world.list_entity_ids() if e.startswith(f"{GUARD_PREFIX}:sabak:0:")]
    assert wave0 == [], wave0
    wave1 = [e for e in world.list_entity_ids() if e.startswith(f"{GUARD_PREFIX}:sabak:1:")]
    assert len(wave1) == WAVE_GUARDS[1], len(wave1)
    assert _monsters_alive(world) == WAVE_GUARDS[1], _monsters_alive(world)


@case
def test_won_clears_every_siege_entity():
    """打赢后场上不该剩任何城门/旗台/守军——否则玩家站在"已攻陷"的城里还能看见旗台。"""
    session, _, started = _siege()
    world = session.world
    objective_id = started["objective"]
    for _ in range(len(WAVE_HP)):
        _kill(world, objective_id)
        session.step(1)
        state = world.get_data(KEY_SIEGE)
        objective_id = state.objective_id or objective_id

    assert world.get_data(KEY_SIEGE).status == WON
    leftovers = [e for e in world.list_entity_ids()
                 if e.startswith((f"{GUARD_PREFIX}:", "siege_obj:"))]
    assert leftovers == [], leftovers
    assert _monsters_alive(world) == 0


# ── 状态快照 ─────────────────────────────────────

@case
def test_siege_status_reports_guards_alive():
    """HUD 靠 ``guards_alive`` 提示"先清杂兵"，必须准。"""
    session, _, started = _siege()
    status = siege_status(session.world)
    assert status["guards_alive"] == WAVE_GUARDS[0], status

    # 打掉一个守军（走正规伤害路径太重，直接清血再等一 tick 让系统判死）
    guard_eid = next(e for e in sorted(session.world.list_entity_ids())
                     if e.startswith(GUARD_PREFIX))
    _kill(session.world, guard_eid)
    session.step(1)
    assert siege_status(session.world)["guards_alive"] == WAVE_GUARDS[0] - 1


@case
def test_siege_status_is_none_without_campaign():
    session, _ = _session()
    assert siege_status(session.world) is None


@case
def test_siege_status_reports_progress_numbers():
    session, _, _ = _siege()
    status = siege_status(session.world)
    assert status["wave"] == 0
    assert status["wave_count"] == 3
    assert status["wave_name"] == "外城"
    assert status["objective_name"] == "外城城门"
    assert status["objective_hp"] == WAVE_HP[0]
    assert status["limit"] == 18000
    assert status["finished"] is False


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
    print(f"test_siege: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
