"""佣兵（P2-S6）—— 花钱买来的战力必须**买得到、也走得掉**。

钉死的四件事：

1. **钱真的扣、人真的出现**：扣款与造人在同一次调用里完成，
   且造出来的是友军（``kind="summon"`` + ``faction=0``）+ 有归属
   （``SummonOwner``）——没有归属的话，佣兵打死的怪掉落会凭空消失。
2. **等级门槛是真的**：1 级号雇不到 20 级的佣兵。这条是唯一挡住
   "花钱平推全图"的东西，松了整个成长曲线就没意义。
3. **两个上限各自生效**：``max`` 卡"同一种别带一堆"，``SQUAD_LIMIT``
   卡"别带一个班"。只测其中一个，另一个就会在某个数据表改动后悄悄失效。
4. **到点必走**：到期由 ``MercenarySystem`` 解散，不是等它被打死。
   留尸体只能靠死亡清理那条路，而到期根本走不到那里。

字面量原则：期望值写死（8000 / 3000 / 3 / 4），不从数据表推导。
"""

from __future__ import annotations

from mirsolo.actor import mercenary
from mirsolo.actor.systems import KEY_INTENTS, KEY_TICK, MobAttackSystem
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    AiState,
    Identity,
    Mercenary,
    Position,
    Progression,
    SummonOwner,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.presentation.panels import build_mercenary_panel

CASES = []
DATA = GameData.load()
MERCS = DATA.mercenaries["mercenaries"]

# ── 常量口径的字面量（改常量/改表必须同步改这里）────────────────
SQUAD_LIMIT = 4      # 全局随从上限
GUBING_PRICE = 8000  # 雇用兵价格
GUBING_MAX = 3       # 雇用兵同名上限
DURATION_TICKS = 3000  # 雇佣时长（10Hz，300 秒）
GUBING_LEVEL_REQ = 20  # 雇用兵等级门槛


def case(fn):
    CASES.append(fn)
    return fn


def _session():
    # 与 test_auction 同一个坑：不传 ``data=`` 会拿到另一份 GameData
    session = build_session(data=DATA)
    player = populate(session, with_spawns=False)["player"]
    return session, player


def _rich(player, *, level: int = 50, gold: int = 10 ** 6) -> None:
    player.get(Progression).level = level
    player.get(Wallet).gold = gold


def _expect(code: str, fn, *args, **kwargs):
    """断言 ``fn`` 以 ``code`` 拒绝码失败。"""
    try:
        fn(*args, **kwargs)
    except mercenary.MercenaryError as exc:
        assert exc.code == code, f"期望拒绝码 {code}，实际 {exc.code}"
        return
    raise AssertionError(f"应当以 {code} 拒绝，但通过了")


@case
def test_every_merc_is_hireable_at_its_own_level():
    """每种佣兵在自己的等级门槛上都必须雇得到（不存在"表里有、雇不了"）。"""
    for merc_id, spec in MERCS.items():
        session, player = _session()
        try:
            _rich(player, level=int(spec["level_req"]), gold=int(spec["price"]))
            got = mercenary.hire(session.world, DATA, player, merc_id, tick=0)
            assert got["paid"] == int(spec["price"]), (merc_id, got)
            assert got["until_tick"] == int(spec["duration_ticks"]), (merc_id, got)
            assert player.get(Wallet).gold == 0, merc_id
        finally:
            session.shutdown()


@case
def test_hire_costs_gold_and_creates_a_friend():
    session, player = _session()
    try:
        _rich(player)
        got = mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        assert got["paid"] == GUBING_PRICE, got
        assert player.get(Wallet).gold == 10 ** 6 - GUBING_PRICE

        merc = session.world.get_entity(got["eid"])
        assert merc is not None, "雇佣后场上没有这个实体"
        ident = merc.get(Identity)
        assert ident.kind == "summon", ident.kind   # 走现有 summon 判定
        assert ident.faction == 0, ident.faction    # 友军：索敌会自己找怪打
        assert merc.get(SummonOwner).owner_id == player.id, "没有归属，击杀奖励会消失"
        assert merc.get(Mercenary).merc_id == "gubing"
        assert merc.get(Vitals).hp == int(MERCS["gubing"]["hp"])
        # 站在雇主右边一格，不与雇主完全重合（重合时相邻判定与渲染都难解释）
        here = player.get(Position)
        assert merc.get(Position).x == here.x + 1.0, (merc.get(Position), here)
        assert merc.get(Position).y == here.y, (merc.get(Position), here)
    finally:
        session.shutdown()


@case
def test_level_gate_is_enforced_and_costs_nothing():
    session, player = _session()
    try:
        _rich(player, level=GUBING_LEVEL_REQ - 1)
        _expect("level_too_low", mercenary.hire, session.world, DATA, player,
                "gubing", tick=0)
        assert player.get(Wallet).gold == 10 ** 6, "被拒绝却扣了钱"
        assert mercenary.squad(session.world, player.id) == [], "被拒绝却造出了实体"
    finally:
        session.shutdown()


@case
def test_no_gold_is_refused():
    session, player = _session()
    try:
        _rich(player, gold=GUBING_PRICE - 1)
        _expect("no_gold", mercenary.hire, session.world, DATA, player,
                "gubing", tick=0)
    finally:
        session.shutdown()


@case
def test_unknown_merc_is_refused():
    session, player = _session()
    try:
        _rich(player)
        _expect("unknown_merc", mercenary.hire, session.world, DATA, player,
                "no_such_merc", tick=0)
    finally:
        session.shutdown()


@case
def test_kind_limit_is_enforced():
    session, player = _session()
    try:
        _rich(player)
        for _ in range(GUBING_MAX):
            mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        assert mercenary.count_of_kind(session.world, player.id, "gubing") == GUBING_MAX
        _expect("kind_full", mercenary.hire, session.world, DATA, player,
                "gubing", tick=0)
        # 别的上限还没满，所以此刻报的是 kind_full 而不是 squad_full
        mercenary.hire(session.world, DATA, player, "huwei", tick=0)
    finally:
        session.shutdown()


@case
def test_squad_limit_is_enforced():
    """"每一种都没超自己的 max"仍然不能无限带：3 雇用兵 + 1 虎卫 = 4 已满。"""
    session, player = _session()
    try:
        _rich(player)
        for _ in range(GUBING_MAX):
            mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        mercenary.hire(session.world, DATA, player, "huwei", tick=0)
        assert len(mercenary.squad(session.world, player.id)) == SQUAD_LIMIT
        _expect("squad_full", mercenary.hire, session.world, DATA, player,
                "mofashi", tick=0)
    finally:
        session.shutdown()


@case
def test_expiry_dismisses_the_merc():
    """到点由 ``MercenarySystem`` 解散——不是等它被打死才清场。"""
    session, player = _session()
    try:
        _rich(player)
        got = mercenary.hire(session.world, DATA, player, "gubing", tick=session.tick)
        assert got["until_tick"] == DURATION_TICKS, got
        eid = got["eid"]
        assert session.world.get_entity(eid) is not None

        seen: list[dict] = []
        session.bus.subscribe(mercenary.EVT_DISMISS, seen.append)

        # 到期前一刻还在，到点那一格就没了
        session.step(DURATION_TICKS - 1)
        assert session.world.get_entity(eid) is not None, "提前离队了"
        session.step(2)
        assert session.world.get_entity(eid) is None, "到期没离队"
        assert len(seen) == 1 and seen[0]["merc_id"] == "gubing", seen
        assert seen[0]["name"] == "雇用兵", seen[0]
    finally:
        session.shutdown()


@case
def test_mercenaries_do_not_survive_a_map_change():
    """已知取舍：换图清掉除玩家外的所有实体，佣兵不跟随（面板上写明了）。

    这一条是**把现状钉住**而不是宣称它合理：以后要改成"佣兵跟着换图"，
    这个用例会先变红，逼人去改 ``travel`` 而不是偷偷改别的地方。
    """
    from mirsolo.world.travel import travel

    session, player = _session()
    try:
        _rich(player)
        got = mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        assert len(mercenary.squad(session.world, player.id)) == 1
        travel(session, "wugong_cave_1")
        assert len(mercenary.squad(session.world, player.id)) == 0, "佣兵跟着换图了"
        assert session.world.get_entity(got["eid"]) is None
    finally:
        session.shutdown()


@case
def test_panel_shows_the_price_and_the_gate():
    session, player = _session()
    try:
        _rich(player, level=1, gold=0)
        panel = build_mercenary_panel(player=player, data=DATA,
                                      world=session.world, tick=0)
        hires = [r for r in panel.rows if r.action == "hire"]
        assert len(hires) == len(MERCS), len(hires)
        first = hires[0]
        assert first.item == "gubing", first.item
        assert str(GUBING_PRICE) in first.detail, first.detail
        assert first.enabled is False, "1 级 0 金不该能雇"
        assert "需 20 级" in first.detail, first.detail
        # 门槛够了但钱不够 → 报的是钱，不是等级
        player.get(Progression).level = 50
        again = build_mercenary_panel(player=player, data=DATA,
                                      world=session.world, tick=0)
        row = next(r for r in again.rows if r.item == "gubing")
        assert "钱不够" in row.detail, row.detail
    finally:
        session.shutdown()


@case
def test_panel_lists_my_squad_with_the_time_left():
    session, player = _session()
    try:
        _rich(player)
        mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        panel = build_mercenary_panel(player=player, data=DATA,
                                      world=session.world, tick=0)
        crew = [r for r in panel.rows if r.action == "none"]
        assert len(crew) == 1, crew
        assert crew[0].text == "雇用兵", crew[0].text
        assert "剩余 300 秒" in crew[0].detail, crew[0].detail
        # 队伍栏标题带人数与全局上限
        joined = " ".join(s.text for s in panel.sections)
        assert f"1/{SQUAD_LIMIT}" in joined, joined
        # 提示必须写明"不跟着换图"，否则玩家会以为钱被吞了
        assert any("不会跟着换图" in t.text for t in panel.texts)
    finally:
        session.shutdown()


@case
def test_mercenary_attack_pace_comes_from_its_own_table():
    """佣兵攻速必须读 ``mercenaries.json`` 自己的 ``attack_speed``，不是怪表默认 3。

    修前：``MobAttackSystem`` 按 ``Identity.species`` 查 ``monsters.json``，
    佣兵的 species（gubing/huwei/mofashi）不在表里 → 查表落空 → 默认 3 →
    **9 刻一刀**，表里写好的 4/4/5 是死数据。
    修后：gubing/huwei=4 → 8 刻一刀（前 3 刀 ``[1, 9, 17]``）、
    mofashi=5 → 7 刻一刀（``[1, 8, 15]``）。

    三个佣兵**全钉**：表里写了三个攻速值，只测一个会漏掉 mofashi 的 5 档。
    """
    from allin_engine import World

    def swings_for(merc_id: str) -> list[int]:
        world = World()
        merc = world.create_entity("m#1")
        merc.add(Identity(name=merc_id, kind="summon", faction=0, species=merc_id))
        merc.add(Position(x=5.0, y=5.0))
        merc.add(Vitals(hp=10 ** 6, max_hp=10 ** 6, mp=0, max_mp=0))
        ai = AiState(home=(5.0, 5.0))
        merc.add(ai)
        tgt = world.create_entity("t#1")
        tgt.add(Identity(name="沙包", kind="monster", faction=1, species="shiwang"))
        tgt.add(Position(x=6.0, y=5.0))
        tgt.add(Vitals(hp=10 ** 6, max_hp=10 ** 6, mp=0, max_mp=0))
        ai.target_id = tgt.id
        world.set_data(KEY_INTENTS, [])
        sys = MobAttackSystem(DATA)
        swings: list[int] = []
        for tick in range(1, 25):
            world.set_data(KEY_TICK, tick)
            sys.update(world, 0.1)
            intents = world.get_data(KEY_INTENTS) or []
            if any(i.get("attacker") == merc for i in intents):
                swings.append(tick)
            world.set_data(KEY_INTENTS, [])
        return swings

    expect = {"gubing": [1, 9, 17], "huwei": [1, 9, 17], "mofashi": [1, 8, 15]}
    got = {mid: swings_for(mid)[:3] for mid in expect}
    assert got == expect, f"佣兵出刀节奏不对（应当 {expect}）：{got}"


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
    print(f"test_mercenary: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
