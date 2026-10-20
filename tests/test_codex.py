"""怪物图鉴（P2-S6）—— 记的是**击杀**，奖励只给**第一次**。

钉死的四件事：

1. **解锁 == 这个 key 存在**：不另存一份 ``unlocked`` 集合。两份事实描述同一件
   事，迟早会分叉（杀了却没解锁 / 解锁了却显示 0 杀）。
2. **首次才有奖励**：第二次杀同一只怪不该再给经验，否则图鉴变成经验副本。
3. **佣兵/召唤物的击杀记到雇主头上**：归属解析已经在 ``DeathRewardSystem``
   里做过一次，图鉴走同一条路（``resolve_owner``）。否则雇兵开图 = 负收益。
4. **全表可见**：23 种怪每种都有一行（没杀过也是一行）。图鉴是查资料的地方，
   把没打过的藏起来就等于没有图鉴。

字面量原则：期望值写死（23 种怪、鸡 10 经验、奖励 500‰ = 5 点），不从表推导。
"""

from __future__ import annotations

from mirsolo.actor import codex
from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat.resolver import MELEE_RANGE, chebyshev
from mirsolo.components import (
    Attributes,
    Codex,
    Identity,
    Position,
    Progression,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.presentation.panels import build_codex_panel

CASES = []
DATA = GameData.load()
MONSTERS = DATA.monsters["monsters"]

# ── 字面量 ──────────────────────────────────────────
MONSTER_COUNT = 23          # 怪物表种类数
JI_EXP = 10                 # 鸡的经验
FIRST_KILL_PERMILLE = 500   # 首杀奖励 = 经验 × 500‰
JI_BONUS = 5                # 10 × 500 // 1000


def case(fn):
    CASES.append(fn)
    return fn


def _session(*, with_spawns: bool = False):
    session = build_session(data=DATA, seed=20261019)
    player = populate(session, with_spawns=with_spawns)["player"]
    player.add(Attributes(dc=(100, 100), accuracy=10000, agility=0))
    return session, player


def _kill(session, attacker, mob, *, limit: int = 40) -> None:
    """打直到死（``limit`` 是保险丝，别写成死循环）。

    出手前先把人挪到**够得着**的格子：普攻有距离门（切比雪夫 1 格），怪摆在
    (0,0) 而人站在地图中心的话，刀全落空。"走到旁边"这一步放在这里，
    四个用例就不必各写一份坐标算术。
    """
    apos = attacker.get(Position)
    mpos = mob.get(Position)
    if apos is not None and mpos is not None and chebyshev(apos, mpos) > MELEE_RANGE:
        apos.x, apos.y = mpos.x + MELEE_RANGE, mpos.y
    for _ in range(limit):
        session.queue_attack(attacker, mob)
        session.step(1)
        if not mob.get(Vitals).alive:
            return
    raise AssertionError(f"{limit} 刻内没打死 {mob.get(Identity).species}")


@case
def test_record_kill_is_pure_bookkeeping():
    """``record_kill`` 只记账、不碰经验——返回"是不是第一次"就够了。"""
    book = Codex()
    assert codex.record_kill(book, "ji") is True
    assert codex.record_kill(book, "ji") is False
    assert book.kills == {"ji": 2}, book.kills
    assert book.unlocked == 1, book.unlocked
    # 没有 Progression/world 也照样能记账：这就是"纯记账"的意思
    assert codex.first_kill_exp(DATA, "ji") == JI_BONUS


@case
def test_first_kill_unlocks_and_pays_the_bonus():
    session, player = _session()
    try:
        seen: list[dict] = []
        session.bus.subscribe(codex.EVT_UNLOCK, seen.append)
        mob = spawn_monster(session.world, DATA, "ji", x=0, y=0)
        _kill(session, player, mob)

        book = player.get(Codex)
        assert book is not None, "玩家没有图鉴组件"
        assert book.kills.get("ji") == 1, book.kills
        assert len(seen) == 1, seen
        assert seen[0]["species"] == "ji", seen[0]
        assert seen[0]["name"] == "鸡", seen[0]
        assert seen[0]["exp"] == JI_BONUS, seen[0]
        assert seen[0]["total"] == MONSTER_COUNT, seen[0]
        assert seen[0]["unlocked"] == 1, seen[0]
        # 基础经验 + 首杀奖励都进了同一条曲线（走 grant_exp 同一入口）
        assert player.get(Progression).exp >= JI_EXP + JI_BONUS or player.get(Progression).level > 1
    finally:
        session.shutdown()


@case
def test_second_kill_adds_no_bonus():
    session, player = _session()
    try:
        seen: list[dict] = []
        session.bus.subscribe(codex.EVT_UNLOCK, seen.append)
        for _ in range(2):
            mob = spawn_monster(session.world, DATA, "ji", x=0, y=0)
            _kill(session, player, mob)
        assert player.get(Codex).kills["ji"] == 2, player.get(Codex).kills
        assert len(seen) == 1, f"第二次击杀不该再解锁：{seen}"
    finally:
        session.shutdown()


@case
def test_mercenary_kills_count_for_the_owner():
    """佣兵打死的怪要记在雇主图鉴上——否则雇兵开图等于白打。"""
    from mirsolo.actor import mercenary

    session, player = _session()
    try:
        player.get(Progression).level = 50
        player.get(Wallet).gold = 10 ** 6
        got = mercenary.hire(session.world, DATA, player, "gubing", tick=0)
        merc = session.world.get_entity(got["eid"])
        assert merc is not None
        # 佣兵属性只有 6-12 攻，鸡 5 血足够一刀；但命中不够会拖，给它补准头
        merc.add(Attributes(dc=(50, 50), accuracy=10000, agility=0))
        mob = spawn_monster(session.world, DATA, "ji", x=0, y=0)
        _kill(session, merc, mob, limit=200)

        assert player.get(Codex).kills.get("ji") == 1, player.get(Codex).kills
    finally:
        session.shutdown()


@case
def test_every_monster_has_a_row_and_the_order_is_stable():
    rows = codex.rows(DATA, Codex())
    assert len(rows) == MONSTER_COUNT, len(rows)
    keys = [(r["level"], r["species"]) for r in rows]
    assert keys == sorted(keys), "图鉴必须按 (等级, id) 升序，否则每次打开顺序都变"
    assert all(r["kills"] == 0 and not r["unlocked"] for r in rows)
    ji = next(r for r in rows if r["species"] == "ji")
    assert ji["name"] == "鸡" and ji["exp"] == JI_EXP and ji["hp"] == 5


@case
def test_drop_hint_names_the_items_not_the_odds():
    hint = codex.drop_hint(DATA, "ji")
    assert "木剑" in hint, hint          # 物品名要出现（玩家查的是"出什么"）
    assert "金币" in hint, hint
    assert "‰" not in hint, f"图鉴不该塞概率：{hint}"
    assert codex.drop_hint(DATA, "no_such_monster") == "无掉落"


@case
def test_summary_uses_the_table_not_the_kills():
    """分母是怪物表的种类数，不是"杀过的种类"——后者一开始就是 100%。"""
    book = Codex()
    assert codex.summary(DATA, book) == {"total": MONSTER_COUNT, "unlocked": 0, "pct": 0}
    codex.record_kill(book, "ji")
    stat = codex.summary(DATA, book)
    assert stat["unlocked"] == 1 and stat["total"] == MONSTER_COUNT, stat
    assert stat["pct"] == 1000 // MONSTER_COUNT, stat


@case
def test_panel_lists_every_monster_and_marks_the_locked_ones():
    session, player = _session()
    try:
        panel = build_codex_panel(player=player, data=DATA, world=session.world, tick=0)
        # 一屏放不下 23 种：剩下的**靠滚动**，不是丢掉（面板不截断是硬要求）
        species = [r.item for r in panel.rows if r.detail]
        assert 0 < len(species) < MONSTER_COUNT, len(species)
        assert panel.scroll_max == MONSTER_COUNT - len(species), panel.scroll_max
        assert species[0] == "ji", species[0]
        assert "未解锁" in " ".join(r.detail for r in panel.rows)
        joined = " ".join(t.text for t in panel.texts)
        assert f"0/{MONSTER_COUNT}" in joined, joined
        assert "首次击杀额外" in joined, joined
        # 右栏说的是掉落来源，不是第二份怪物列表
        assert "掉落来源" in " ".join(s.text for s in panel.sections)
    finally:
        session.shutdown()


@case
def test_panel_reflects_a_kill():
    session, player = _session()
    try:
        mob = spawn_monster(session.world, DATA, "ji", x=0, y=0)
        _kill(session, player, mob)
        panel = build_codex_panel(player=player, data=DATA, world=session.world, tick=1)
        ji = next(r for r in panel.rows if r.item == "ji" and r.detail)
        assert "击杀 1" in ji.detail, ji.detail
        assert "未解锁" not in ji.detail, ji.detail
        assert "1/23" in " ".join(t.text for t in panel.texts)
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
    print(f"test_codex: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
