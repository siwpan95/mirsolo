"""玩家攻速节流 —— 按住攻击键**不等于**每刻一刀。

修前的事实：``AttackSystem`` 只查"双方活着 + 没被麻痹 + 够得着"，
从来不看"上一次出刀是第几刻"。于是按住空格 = 每个逻辑刻一刀，
设计值（攻速 5 = 1.00 秒 = 10 刻一刀）被放大成 10 倍 DPS，
``classes.json`` 里三职业的 ``attack_speed`` 5/4/5 也完全失效——
战士和法师砍得一样快。

怪物侧本来是对的（``MobAttackSystem``：``interval = max(2, 12 - attack_speed)``
+ ``if tick - state.last_attack_tick < interval: continue``），
缺的只是玩家这一半。所以这次改的是同一条纪律的**另一半**，
不是新发明一套节流。

本模块的用例全部是"修前必红"：

* ① 按住 N 刻 → 出刀数 = N / 间隔，不是 N；
* ② 三职业同一 N → 出刀数按各职业 ``attack_speed`` 分化（5/4/5）；
* ③ 冷却中的意图**丢弃**不排队（与距离门同一条纪律）；
* ④ 冷却中的拒绝必须有回音（``combat:rejected`` / ``code="cooldown"``）。

⚠️ 为什么节流记在**世界数据**里而不是给玩家挂个组件：玩家没有
``AiState``（怪的 ``AiState.last_attack_tick`` 就是这本账），而给它补一个
``AiState`` 会让 ``MobChaseSystem`` 顺手把玩家也挪动起来。
``attack_pace`` 是按实体 id 记的一本运行时账，与 ``mining_veins`` 同一形态，
且不进 ``save.schema.WORLD_KEYS`` —— 读档后"随时能出第一刀"才是对的。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import KEY_ATTACK_PACE, KEY_INTENTS
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat import resolver as resolver_mod
from mirsolo.combat.formulas import (
    LOGIC_HZ,
    attack_interval_seconds,
    attack_interval_ticks,
)
from mirsolo.components import AiState, Position, Progression, Vitals
from mirsolo.data.loader import GameData

CASES = []
DATA = GameData.load()

#: 普攻被拒的回音事件（与距离门共用同一条通道）
EVT_ATTACK_REJECTED = resolver_mod.EVT_ATTACK_REJECTED
MELEE_RANGE = resolver_mod.MELEE_RANGE

#: 按住攻击键的刻数（12 秒）。取 120 是为了让 5 档（10 刻）与 4 档（11 刻）
#: 在整数除上真的错开：N=40 时两者都是 4 刀，看不出职业差异。
HOLD_TICKS = 120

#: 三职业在同一 N 下**应当**出几刀（第一刀立刻出，之后每隔 interval 刻一刀）。
#: 战 5 / 道 5 → 1.00s → 10 刻 → 12 刀；法 4 → 1.10s → 11 刻 → 11 刀。
EXPECTED_SWINGS = {"warrior": 12, "mage": 11, "taoist": 12}


def case(fn):
    CASES.append(fn)
    return fn


def _still(mob) -> None:
    """让怪站着不动：测的是**节奏**，不是"它走过来了"这种额外变量。

    ``vision=0`` 走的是索敌自己的门，不是把 AiState 摘掉——
    摘掉会顺手关掉"挨打还手"，测的就不是同一件事了。
    """
    ai = mob.get(AiState)
    if ai is not None:
        ai.vision = 0
        ai.target_id = None


def _duel(class_id: str, *, seed: int = 7, distance: int = 1):
    """一个打不死的沙包 + 一个打不死的玩家，站在 ``distance`` 格外。

    血量两边都拉到 10^6：本模块只关心"挥了几刀"，谁先死都是噪音。
    """
    session = build_session(data=DATA, seed=seed, map_id="biqi_field")
    player = populate(session, player_class=class_id, with_spawns=False)["player"]
    pv = player.get(Vitals)
    pv.max_hp = pv.hp = 10 ** 6
    mob = spawn_monster(session.world, DATA, "shiwang",
                        x=player.get(Position).x + distance,
                        y=player.get(Position).y)
    _still(mob)
    mv = mob.get(Vitals)
    mv.max_hp = mv.hp = 10 ** 6
    return session, player, mob


def _swings(session, player) -> list[int]:
    """订阅玩家的每一次**真正出刀**，返回出手刻的列表。

    命中与未命中都算出刀（miss 也是挥了一刀、也吃冷却）；
    同一个刻可能因为戒指附加伤害来两条事件，所以最后按刻去重。
    """
    seen: list[int] = []

    def _on(payload: dict) -> None:
        if payload.get("attacker") != player.id:
            return
        seen.append(int(payload.get("tick", 0)))

    session.bus.subscribe("combat:hit", _on)
    session.bus.subscribe("combat:miss", _on)
    return seen


def _hold(session, player, mob, ticks: int) -> list[int]:
    """按住攻击键 ``ticks`` 刻，返回实际出刀的刻（去重、升序）。"""
    seen = _swings(session, player)
    for _ in range(ticks):
        session.queue_attack(player, mob)
        session.step(1)
    return sorted(set(seen))


# ── ① 按住不放 ≠ 每刻一刀 ────────────────────────────

@case
def test_holding_attack_does_not_swing_every_tick():
    """按住 120 刻：战士应当只出 12 刀（10 刻一刀），不是 120 刀。

    修前这一条是 **120**（每刻一刀），DPS 是设计值的 10 倍。
    """
    session, player, mob = _duel("warrior")
    try:
        ticks = _hold(session, player, mob, HOLD_TICKS)
        assert len(ticks) == EXPECTED_SWINGS["warrior"], (
            f"按住 {HOLD_TICKS} 刻出了 {len(ticks)} 刀（应当 {EXPECTED_SWINGS['warrior']} 刀）：{ticks}")
        # 也不许是 0：节流收紧到"永远砍不动"同样是坏的
        assert ticks, "一刀都没出（节流把普攻整个关死了）"
    finally:
        session.shutdown()


@case
def test_first_swing_is_not_delayed():
    """第一刀**立刻**出：没挥过 = 随时能出刀，不是开局先罚站 1 秒。

    这条同时是"改动没把现有单刀用例整片打翻"的保险——
    一次 ``queue_attack`` + 一次 ``step`` 必须照旧掉血。
    """
    session, player, mob = _duel("warrior")
    try:
        seen = _swings(session, player)
        hp0 = mob.get(Vitals).hp
        session.queue_attack(player, mob)
        session.step(1)
        assert sorted(set(seen)) == [1], f"第一刀不在第 1 刻：{sorted(set(seen))}"
        assert mob.get(Vitals).hp < hp0, "第一刀没掉血"
        # 第二刻立刻再按：还在冷却里，不该出刀
        session.queue_attack(player, mob)
        session.step(1)
        assert sorted(set(seen)) == [1], f"冷却期内又出了一刀：{sorted(set(seen))}"
    finally:
        session.shutdown()


# ── ② 三职业手感分化（5/4/5） ────────────────────────

@case
def test_attack_speed_tier_changes_the_pace():
    """同一个 N，三职业出刀数按 ``classes.json`` 的 ``attack_speed`` 分化。

    修前三个数都是 120（谁都不节流），这条是"职业手感"唯一的直接证据，
    所以把三个数字**逐字**钉住，不写"应当不同"这种松断言。
    """
    got = {}
    for class_id in ("warrior", "mage", "taoist"):
        session, player, mob = _duel(class_id)
        try:
            got[class_id] = len(_hold(session, player, mob, HOLD_TICKS))
        finally:
            session.shutdown()

    assert got == EXPECTED_SWINGS, (
        f"三职业出刀数不对（应当 {EXPECTED_SWINGS}）：{got}")
    # 法师（4 档）必须**严格慢于**战/道（5 档）——这条是"档位真的接上了"的判据
    assert got["mage"] < got["warrior"], f"法师没有比战士慢：{got}"
    assert got["mage"] < got["taoist"], f"法师没有比道士慢：{got}"
    # 职业差异不许被"每刻一刀"抹平
    assert max(got.values()) < HOLD_TICKS // 2, f"出刀数仍然接近每刻一刀：{got}"


@case
def test_interval_comes_from_the_declared_10hz():
    """秒 → 刻的换算必须写清 10Hz 依据：1 刻 = 0.1 秒，不许在调用处写裸 0.1。

    钉两端（档位 1 与档位 11）+ 三职业实际用的两档（5 / 4）。
    """
    assert LOGIC_HZ == 10, f"逻辑步频率不是 10Hz：{LOGIC_HZ}"
    assert attack_interval_ticks(1) == 14, attack_interval_ticks(1)    # 1.40s
    assert attack_interval_ticks(5) == 10, attack_interval_ticks(5)    # 1.00s
    assert attack_interval_ticks(11) == 4, attack_interval_ticks(11)   # 0.40s
    # 数据表里的三个值：战 5 / 法 4 / 道 5
    for class_id, expect in (("warrior", 10), ("mage", 11), ("taoist", 10)):
        speed = int(DATA.classes["classes"][class_id]["attack_speed"])
        assert attack_interval_ticks(speed) == expect, (
            f"{class_id} 攻速 {speed} 应当 {expect} 刻一刀")
        # 与秒版同源：刻 = 秒 × 10Hz（四舍五入）
        assert attack_interval_ticks(speed) == round(
            attack_interval_seconds(speed) * LOGIC_HZ)
    # 越界档位夹紧，不产生 0 刻或负数
    assert attack_interval_ticks(0) == attack_interval_ticks(1)
    assert attack_interval_ticks(99) == attack_interval_ticks(11)


# ── ③ 冷却里的意图是丢弃，不是排队 ────────────────────

@case
def test_throttled_intents_are_dropped_not_banked():
    """按住不放时攒下的那些意图**作废**，不许在冷却结束时一起打出来。

    与距离门同一条纪律：按下的那一下不该在若干刻之后自己生效，
    否则解除冷却的一刻会"卡一下突然爆发"。
    """
    session, player, mob = _duel("warrior")
    try:
        seen = _swings(session, player)
        for _ in range(HOLD_TICKS):
            session.queue_attack(player, mob)
            session.step(1)
        ticks = sorted(set(seen))
        assert session.world.get_data(KEY_INTENTS) == [], "被丢弃的意图还留在队列里"
        # 任意相邻两刀的间隔都 ≥ 10 刻：没有"补刀"这种现象
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert gaps and min(gaps) >= 10, f"出现了比间隔更密的两刀：{ticks}"
    finally:
        session.shutdown()


@case
def test_pace_is_remembered_per_entity():
    """攻速账按实体记：换目标、停手再按，都不能把冷却清零。

    "打完 A 立刻打 B 可以再出一刀"是玩家最爱钻的空子，
    这本账必须跟着**人**走，不跟目标走。
    """
    session, player, mob = _duel("warrior")
    try:
        seen = _swings(session, player)
        session.queue_attack(player, mob)
        session.step(1)
        assert sorted(set(seen)) == [1]

        other = spawn_monster(session.world, DATA, "shiwang",
                              x=player.get(Position).x + 1,
                              y=player.get(Position).y + 1)
        _still(other)
        ov = other.get(Vitals)
        ov.max_hp = ov.hp = 10 ** 6
        session.queue_attack(player, other)   # 换个目标立刻再按
        session.step(1)
        assert sorted(set(seen)) == [1], f"换目标就绕过了冷却：{sorted(set(seen))}"
        assert ov.hp == 10 ** 6, "换目标那一刀打中了"

        # 停手到冷却结束（第 11 刻）再按：这时才该出第二刀
        for _ in range(9):
            session.step(1)
        session.queue_attack(player, other)
        session.step(1)
        assert len(sorted(set(seen))) == 2, f"冷却结束后没出第二刀：{sorted(set(seen))}"
    finally:
        session.shutdown()


@case
def test_out_of_range_does_not_eat_a_swing():
    """够不着被拒**不吃冷却**：门在节流之前，没挥刀就不该扣掉这一刀。

    顺序反过来的话，"朝着空气挥一刀 → 冷却 1 秒"会让玩家在追怪时
    平白丢掉攻击机会，而且毫无回音。
    """
    session, player, mob = _duel("warrior", distance=4)
    try:
        seen = _swings(session, player)
        session.queue_attack(player, mob)   # 4 格外：被距离门拦下
        session.step(1)
        assert sorted(set(seen)) == [], "4 格外的一刀打中了"

        # 走到贴身：因为上面那一刀没吃冷却，这里应当**立刻**能出刀
        player.get(Position).x = mob.get(Position).x - 1
        player.get(Position).y = mob.get(Position).y
        session.queue_attack(player, mob)
        session.step(1)
        assert sorted(set(seen)), "够不着那一刀把冷却吃掉了（走近后打不出）"
    finally:
        session.shutdown()


# ── ④ 拒绝必须有回音 ────────────────────────────────

@case
def test_cooldown_rejection_echoes():
    """冷却中丢刀要发 ``combat:rejected`` / ``code="cooldown"``，不许静默。

    按住空格却毫无反应，玩家只会以为键坏了。回音走的是与"够不着"
    **同一条**通道（表现层的 ``REJECT_REASONS`` 里本来就有 ``cooldown``
    这一条），因此不需要第二个事件名。
    """
    session, player, mob = _duel("warrior")
    try:
        got: list[dict] = []
        session.bus.subscribe(EVT_ATTACK_REJECTED,
                              lambda p: got.extend(p.get("items") or []))
        session.queue_attack(player, mob)
        session.step(1)                      # 第 1 刻：出刀
        session.queue_attack(player, mob)
        session.step(1)                      # 第 2 刻：冷却中

        codes = [item.get("code") for item in got]
        assert "cooldown" in codes, f"冷却中的丢刀没有回音：{got}"
        echo = next(item for item in got if item.get("code") == "cooldown")
        assert echo.get("attacker") == player.id, echo
        assert echo.get("tick") == 2, echo
        # 回音要能说出"还得等多久"，否则表现层只能写死一句"冷却中"
        assert echo.get("interval") == 10, echo
        assert echo.get("ready_at") == 11, echo
    finally:
        session.shutdown()


@case
def test_rejection_is_deduplicated_per_tick():
    """同一刻里连按多次只回一句：表现层每刻收到一打相同提示等于噪音。

    （表现层一帧能排好几个意图——按住空格时每帧都排——
    但玩家只需要知道"这一刻在冷却"。)
    """
    session, player, mob = _duel("warrior")
    try:
        got: list[dict] = []
        session.bus.subscribe(EVT_ATTACK_REJECTED,
                              lambda p: got.extend(p.get("items") or []))
        session.queue_attack(player, mob)
        session.step(1)
        got.clear()
        for _ in range(5):                   # 同一刻连排 5 个意图
            session.queue_attack(player, mob)
        session.step(1)
        cool = [item for item in got if item.get("code") == "cooldown"]
        assert len(cool) == 1, f"同一刻回了 {len(cool)} 句冷却提示：{got}"
    finally:
        session.shutdown()


# ── ⑤ 怪物侧不受影响 ────────────────────────────────

@case
def test_monsters_keep_their_own_pace():
    """节流只补**玩家这一半**：怪物仍由 ``MobAttackSystem`` 按自己的攻速排队。

    怪物若再被这层节流一次，就等于" ``monsters.json`` 的攻速"有了第二个
    消费者，两边迟早长成不一样。这条钉住"非玩家 = 不在这一层节流"。
    """
    session, player, mob = _duel("warrior")
    try:
        hits: list[dict] = []
        session.bus.subscribe("combat:hit", hits.append)
        session.step(1)
        # 直接给怪排两次刀（跳过 MobAttackSystem 的排队节奏）：
        # 玩家这一层不该拦它
        session.queue_attack(mob, player)
        session.queue_attack(mob, player)
        session.step(1)
        mine = [h for h in hits if h.get("attacker") == mob.id]
        assert len(mine) >= 1, "怪的刀被玩家的节流误伤了"
        # 这本账里只该有玩家：怪的节奏记在它自己的 AiState 上
        pace = session.world.get_data(KEY_ATTACK_PACE) or {}
        assert mob.id not in pace, f"怪被记进了玩家的攻速账：{pace}"
    finally:
        session.shutdown()


@case
def test_pace_book_is_runtime_only():
    """攻速账是**运行时**账：不进存档（读档后随时能出第一刀才是对的）。

    进了存档会多出两种坏味道：一是旧档缺键要升 ``SCHEMA_VERSION``，
    二是"读档瞬间刚好在冷却里"这种凭手感不可复现的状态被固化下来。
    """
    from mirsolo.save import schema

    assert KEY_ATTACK_PACE not in schema.WORLD_KEYS, (
        f"{KEY_ATTACK_PACE} 进了存档键表（读档会把冷却带回来）")
    assert KEY_ATTACK_PACE not in schema.OPTIONAL_WORLD_KEYS


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
    print(f"test_attack_pace: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
