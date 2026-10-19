"""换图：传送点判定、落点、清场、eid 唯一性。

这里盯的都是**玩家能直接感受到的怪现象**：

- 刚进图就被弹回去（落点压在回程传送点上）
- 怪凭空少了一只（eid 重复导致静默覆盖）
- 换图后旧图的怪跟着跑（清场没做干净）

数值一律用字面量（``biqi_field`` 的出口在 ``[60, 6]``、各图回程口在
``[4, 4]``），不调生产函数现算——自指式断言等于没测。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Identity, Position, Vitals
from mirsolo.data.loader import GameData
from mirsolo.world.travel import (
    KEY_PORTAL_GRACE,
    PORTAL_ARRIVE_OFFSET,
    TravelError,
    entry_point,
    find_portal,
    travel,
    use_portal_if_any,
)

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


def _session(map_id: str = "biqi_field", *, spawns: bool = False):
    session = build_session(data=DATA, seed=7, map_id=map_id)
    player = populate(session, with_spawns=spawns)["player"]
    return session, player


def _xy(player) -> tuple[float, float]:
    pos = player.get(Position)
    return pos.x, pos.y


# ── 传送点查找 ──────────────────────────────────────

@case
def test_find_portal_matches_only_that_tile():
    """必须同格才算——站旁边一格不该被传走。"""
    portal = find_portal(DATA, "biqi_field", 60, 6)
    assert portal is not None and portal["to"] == "biqi_mine_1", portal
    assert find_portal(DATA, "biqi_field", 61, 6) is None
    assert find_portal(DATA, "biqi_field", 60, 7) is None
    assert find_portal(DATA, "biqi_field", 59, 5) is None
    # 浮点坐标按四舍五入落到格子（玩家位置是 float）
    assert find_portal(DATA, "biqi_field", 60.0, 6.0) is not None
    assert find_portal(DATA, "no_such_map", 60, 6) is None


@case
def test_entry_point_is_inside_and_off_the_back_portal():
    """落点要在回程口内侧，且**不能**落在任何传送点上（否则进去就被弹回）。"""
    for map_id in DATA.maps["maps"]:
        if map_id == "biqi_field":
            continue  # 起点图没有"回程口"
        x, y = entry_point(DATA, "wugong_cave_1" if map_id == "wugong_cave_1" else map_id,
                           from_map="biqi_mine_1")
        assert find_portal(DATA, map_id, x, y) is None, (map_id, x, y)
        spec = DATA.maps["maps"][map_id]
        assert 0 <= x < spec["size"][0] and 0 <= y < spec["size"][1], (map_id, x, y)

    # biqi_mine_1 的回程口在 [4,4]、地图中心 32,32 → 往内侧偏 PORTAL_ARRIVE_OFFSET 格
    x, y = entry_point(DATA, "biqi_mine_1", from_map="biqi_field")
    assert (x, y) == (4 + PORTAL_ARRIVE_OFFSET, 4 + PORTAL_ARRIVE_OFFSET), (x, y)
    # 没有匹配入口（例如直接跳图）时退回地图中心，而不是随便落到某格
    assert entry_point(DATA, "biqi_mine_1", from_map=None) == (32, 32)
    assert entry_point(DATA, "biqi_mine_1", from_map="zuma_temple") == (32, 32)


# ── 换图本体 ────────────────────────────────────────

@case
def test_travel_moves_player_and_swaps_monsters():
    session, player = _session("biqi_field", spawns=True)
    try:
        before = {e.eid for e in session.world.each(Identity)}
        old_count = len(before)
        assert old_count > 1

        result = travel(session, "biqi_mine_1")
        assert result["from"] == "biqi_field" and result["to"] == "biqi_mine_1"
        assert session.map_id == "biqi_mine_1"
        # 两个 map 字段必须同步，否则 HUD 显示旧图、补刷系统却按新图跑
        assert session.world.get_data("current_map") == "biqi_mine_1"

        x, y = _xy(player)
        assert (x, y) == result["at"]
        assert find_portal(DATA, "biqi_mine_1", x, y) is None

        after = {e.eid for e in session.world.each(Identity)}
        assert player.eid in after
        assert before & after == {player.eid}, before & after  # 旧图实体全清掉
        # biqi_mine_1 刷新表：骷髅 12 + 僵尸 14 + 尸王 1 = 27 只（手算字面量）
        mobs = [e for e in session.world.each(Identity)
                if e.get(Identity).kind == "monster"]
        assert len(mobs) == 27, len(mobs)
        species = {e.get(Identity).species for e in mobs}
        assert species <= {"kulou", "jiangshi", "shiwang"}, species
    finally:
        session.shutdown()


@case
def test_travel_rejects_unknown_same_and_dead():
    session, player = _session("biqi_field")
    try:
        for target, code in (("atlantis", "unknown_map"), ("biqi_field", "same_map")):
            try:
                travel(session, target)
            except TravelError as exc:
                assert exc.code == code, (target, exc.code)
            else:
                raise AssertionError(f"{target} 应该被拒")

        player.get(Vitals).hp = 0
        try:
            travel(session, "biqi_mine_1")
        except TravelError as exc:
            assert exc.code == "dead", exc.code
        else:
            raise AssertionError("死亡状态应该不能换图")
        assert session.map_id == "biqi_field"  # 被拒时不能半途改状态
    finally:
        session.shutdown()


@case
def test_walking_onto_portal_triggers_travel():
    """端到端：把人放到传送点上，推一刻就该换图（走 PortalSystem，不是窗口）。"""
    session, player = _session("biqi_field", spawns=False)
    try:
        pos = player.get(Position)
        pos.x, pos.y = 60.0, 6.0          # biqi_field 的矿洞入口
        assert find_portal(DATA, "biqi_field", 60, 6)["to"] == "biqi_mine_1"
        session.step(1)
        assert session.map_id == "biqi_mine_1", session.map_id
        x, y = _xy(player)
        assert find_portal(DATA, "biqi_mine_1", x, y) is None
        # 到达后短时间内免疫：同一刻内不会再被弹回去
        assert use_portal_if_any(session) is None
        assert session.map_id == "biqi_mine_1"
    finally:
        session.shutdown()


@case
def test_portal_does_not_fire_while_standing_next_to_it():
    session, player = _session("biqi_field", spawns=False)
    try:
        pos = player.get(Position)
        pos.x, pos.y = 59.0, 6.0
        session.step(3)
        assert session.map_id == "biqi_field"
    finally:
        session.shutdown()


@case
def test_entity_ids_stay_unique_across_round_trips():
    """来回换图 + 清尸体后，eid 不能撞车。

    ``create_entity`` 撞到已存在的 eid 会**静默覆盖**（数量不变、不报错），
    表现成"怪凭空少一只"；用 ``entity_count()+1`` 造 eid 必然踩这个坑。
    """
    session, player = _session("biqi_field", spawns=True)
    try:
        seen: set[str] = set()
        for round_index in range(3):
            travel(session, "biqi_mine_1")
            ids = list(session.world.list_entity_ids())
            assert len(ids) == len(set(ids)), f"第 {round_index} 次进矿洞 eid 重复"
            assert not (set(ids) & seen - {player.eid}), \
                f"第 {round_index} 次进矿洞撞上上一轮的 eid"
            seen |= set(ids)
            travel(session, "biqi_field")
            ids = list(session.world.list_entity_ids())
            assert len(ids) == len(set(ids)), f"第 {round_index} 次回比奇 eid 重复"

        # 清尸体会让 entity_count 倒退——这里把怪打死再清掉，然后造新怪验唯一性
        mobs = [e for e in session.world.each(Identity) if e.get(Identity).kind == "monster"]
        assert mobs
        for mob in mobs[:5]:
            mob.get(Vitals).hp = 0
            session.world.remove_entity(mob.eid)
        survivors = set(session.world.list_entity_ids())
        fresh = spawn_monster(session.world, DATA, "kulou", x=1, y=1)
        assert fresh.eid not in survivors, fresh.eid
        assert session.world.has_entity(fresh.eid)
    finally:
        session.shutdown()


@case
def test_travel_is_reversible_and_deterministic():
    """同一张图来回进出，怪的位置必须一致（随机流名固定为 populate:<map>:...）。"""
    session, player = _session("biqi_field", spawns=True)
    try:
        travel(session, "biqi_mine_1")
        first = sorted((e.get(Identity).species, e.get(Position).x, e.get(Position).y)
                       for e in session.world.each(Identity)
                       if e.get(Identity).kind == "monster")
        travel(session, "biqi_field")
        travel(session, "biqi_mine_1")
        second = sorted((e.get(Identity).species, e.get(Position).x, e.get(Position).y)
                        for e in session.world.each(Identity)
                        if e.get(Identity).kind == "monster")
        assert first == second, "两次进同一张图的怪配置不一致"
    finally:
        session.shutdown()


@case
def test_grace_key_is_written_on_arrival():
    session, player = _session("biqi_field")
    try:
        before = int(session.world.get_data(KEY_PORTAL_GRACE, 0) or 0)
        travel(session, "biqi_mine_1")
        after = int(session.world.get_data(KEY_PORTAL_GRACE, 0) or 0)
        assert after >= before
        assert session.world.get_data(KEY_PORTAL_GRACE) == session.tick + 2, after
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
    print(f"test_travel: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
