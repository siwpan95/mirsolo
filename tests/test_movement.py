"""移动与怪物 AI：落格节奏、地图钳制、朝向、视野、追击、相邻反击、脱战回巢。

这里测的是**游戏规则本身**（不再只是数学）：

* 按住方向键不会因为帧率不同而走得更快（每 4 刻一格）；
* 怪不会隔空打人（必须相邻）；
* 怪只在**视野内**建立仇恨，追不到就回巢——不能"追一半卡在原地"。

⚠️ 测试坐标一律写成**相对玩家出生点的偏移**，不写绝对值：
   ``populate()`` 把玩家放在地图中心（64×64 → 32,32），
   写死 ``x == 1`` 只会测出"玩家不在原点"这件事，与移动规则无关。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import facing_from_delta
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import AiState, Position, Vitals
from mirsolo.data.loader import GameData

CASES = []
DATA = GameData.load()

# 与 MovementSystem / MobChaseSystem 的默认步长对齐（改了那边这里必须一起改）
PLAYER_STEP_TICKS = 4
MOB_STEP_TICKS = 6

# 地图刷怪是每 300 刻补刷一次；超过这个刻数会有新怪乱入干扰断言
RESPAWN_TICK = 300


def case(fn):
    CASES.append(fn)
    return fn


def _session(with_spawns: bool = False):
    """开一个空会话 + 一个玩家，返回 (session, player, 出生点)。"""
    session = build_session(data=DATA, seed=1)
    player = populate(session, with_spawns=with_spawns)["player"]
    origin = player.get(Position)
    return session, player, (origin.x, origin.y)


def _xy(entity) -> tuple[float, float]:
    pos = entity.get(Position)
    return (pos.x, pos.y)


def _chebyshev(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@case
def test_facing_all_eight_directions():
    """北=0 顺时针：0 北 / 2 东 / 4 南 / 6 西，斜向落奇数位。"""
    assert facing_from_delta(0, 0) == 0
    assert facing_from_delta(0, 1) == 0    # 北
    assert facing_from_delta(1, 1) == 1    # 东北
    assert facing_from_delta(1, 0) == 2    # 东
    assert facing_from_delta(1, -1) == 3   # 东南
    assert facing_from_delta(0, -1) == 4   # 南
    assert facing_from_delta(-1, -1) == 5  # 西南
    assert facing_from_delta(-1, 0) == 6   # 西
    assert facing_from_delta(-1, 1) == 7   # 西北


@case
def test_player_moves_one_tile_per_interval():
    """意图 (1,0)：每 4 刻落一格——6 刻走 1 格，再 2 刻（满 8）走第 2 格。"""
    session, player, (x0, y0) = _session()
    try:
        session.world.set_data("move_intent", (1, 0))
        session.step(PLAYER_STEP_TICKS + 2)
        assert player.get(Position).x - x0 == 1, (
            f"{PLAYER_STEP_TICKS + 2} 刻应走 1 格，实际 {player.get(Position).x - x0}")
        session.step(PLAYER_STEP_TICKS - 2)
        assert player.get(Position).x - x0 == 2, (
            f"{PLAYER_STEP_TICKS * 2} 刻应走 2 格，实际 {player.get(Position).x - x0}")
        assert player.get(Position).y == y0, "意图只有 x 分量，不该横向漂移"
        assert player.get(Position).facing == 2, "向东走朝向应为 2"
    finally:
        session.shutdown()


@case
def test_movement_rate_is_independent_of_tick_dt():
    """同样的 tick 数，dt 大小不影响走了几格——移动只认刻度，不认秒数。

    这是"按住方向键不会因为帧率不同而走得更快"的机制保证：
    ``MovementSystem`` 只看 ``tick % step_ticks``，完全没读 ``dt``。
    """
    slow = build_session(data=DATA, seed=1, tick_dt=0.1)
    fast = build_session(data=DATA, seed=1, tick_dt=1.0 / 60.0)
    try:
        slow_player = populate(slow, with_spawns=False)["player"]
        fast_player = populate(fast, with_spawns=False)["player"]
        sx, fx = slow_player.get(Position).x, fast_player.get(Position).x
        slow.world.set_data("move_intent", (1, 0))
        fast.world.set_data("move_intent", (1, 0))
        slow.step(PLAYER_STEP_TICKS * 3)
        fast.step(PLAYER_STEP_TICKS * 3)
        moved_slow = slow_player.get(Position).x - sx
        moved_fast = fast_player.get(Position).x - fx
        assert moved_slow == moved_fast == 3, (moved_slow, moved_fast)
    finally:
        slow.shutdown()
        fast.shutdown()


@case
def test_no_intent_means_no_movement():
    session, player, origin = _session()
    try:
        session.world.set_data("move_intent", (0, 0))
        session.step(PLAYER_STEP_TICKS * 5)
        assert _xy(player) == origin, f"无输入却动了：{origin} -> {_xy(player)}"
    finally:
        session.shutdown()


@case
def test_movement_clamped_to_map_bounds():
    """顶着地图边缘走不会出界（64×64 → x 最大 63）。"""
    session, player, _origin = _session()
    try:
        width = int(DATA.maps["maps"][session.map_id]["size"][0])
        player.get(Position).x = float(width - 1)
        session.world.set_data("move_intent", (1, 0))
        session.step(PLAYER_STEP_TICKS * 10)
        assert player.get(Position).x == float(width - 1), player.get(Position)
    finally:
        session.shutdown()


@case
def test_mob_chases_player():
    """怪锁定玩家后必须逐步逼近（而不是站在原地发呆）。"""
    session, player, (px, py) = _session()
    try:
        # 8 格对角 ≈ 11.3（在视野 10 内、脱战 12 内）
        mob = spawn_monster(session.world, DATA, "kulou", x=px + 7.0, y=py + 7.0)
        before = _chebyshev(_xy(mob), (px, py))
        session.step(MOB_STEP_TICKS * 20)
        after = _chebyshev(_xy(mob), _xy(player))
        assert after < before, f"没逼近：{before} -> {after}"
    finally:
        session.shutdown()


@case
def test_mob_out_of_vision_never_aggros():
    """视野外的怪不建立仇恨——否则它会被远处玩家牵着满地图跑。"""
    session, player, (px, py) = _session()
    try:
        mob = spawn_monster(session.world, DATA, "kulou", x=px + 15.0, y=py)
        start = _xy(mob)
        session.step(MOB_STEP_TICKS * 20)
        assert _xy(mob) == start, f"视野外的怪动了：{start} -> {_xy(mob)}"
        assert mob.get(AiState).target_id is None
    finally:
        session.shutdown()


@case
def test_mob_returns_home_when_leashed():
    """脱战必须回巢：目标跑出视野后怪要走回出生点，不能停在半路。"""
    session, player, (px, py) = _session()
    try:
        home = (px + 8.0, py)
        mob = spawn_monster(session.world, DATA, "kulou", x=home[0], y=home[1])
        session.step(MOB_STEP_TICKS * 10)  # 先追出去一段
        assert _xy(mob) != home, "前置条件不成立：怪根本没离开巢"

        player.get(Position).x = px - 30.0  # 玩家跑远（超出视野 10）
        session.step(MOB_STEP_TICKS * 20)

        assert _xy(mob) == home, f"没回巢：{_xy(mob)} != {home}"
        assert mob.get(AiState).target_id is None
    finally:
        session.shutdown()


@case
def test_adjacent_mob_attacks_player():
    """相邻的怪按攻速出刀：玩家必须在若干刻内掉血。"""
    session, player, (px, py) = _session()
    try:
        spawn_monster(session.world, DATA, "jiangshi", x=px + 1.0, y=py)  # 出生即相邻
        start_hp = player.get(Vitals).hp
        session.step(100)
        assert player.get(Vitals).hp < start_hp, "相邻僵尸 10 秒内没打掉一滴血"
    finally:
        session.shutdown()


@case
def test_far_mob_cannot_attack():
    """不相邻就打不着——隔着半个地图掉血说明攻击没有距离门控。"""
    session, player, (px, py) = _session()
    try:
        spawn_monster(session.world, DATA, "shiwang", x=px + 30.0, y=py)
        start_hp = player.get(Vitals).hp
        session.step(RESPAWN_TICK - 100)  # 200 刻，且不触发地图补刷
        assert player.get(Vitals).hp == start_hp, "尸王隔着 30 格打到了玩家"
    finally:
        session.shutdown()


@case
def test_chase_then_attack_end_to_end():
    """完整链路：进视野 → 追击 → 相邻 → 出刀 → 掉血。"""
    session, player, (px, py) = _session()
    try:
        spawn_monster(session.world, DATA, "kulou", x=px + 6.0, y=py)
        start_hp = player.get(Vitals).hp
        session.step(MOB_STEP_TICKS * 50)  # 300 刻：够走过来并砍数刀
        assert player.get(Vitals).hp < start_hp, "骷髅 30 秒内没能接近并攻击玩家"
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
    print(f"test_movement: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
