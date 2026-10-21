"""自动存档与存读接线（P3-S6 P1）—— 四个节点、一条闸门、零副作用。

这一层测的不是"格式对不对"（那是 ``test_save``），而是**什么时候会写盘**：

1. **四个节点各触发一次**：换图 / 升级 / 死亡结算 / 锻造。
2. **同一节点只存一次**：站在一格不动跑 200 帧，自动档不能反复被重写
   （``writes`` 计数才是判据，文件 mtime 在 1 秒内分辨率下看不出来）。
3. **读档绝不引发连锁写盘**：读一份 tick 比自己小的存档后，下一帧
   ``observe`` 必须什么都不做——否则"读档"这件只读操作会把玩家刚读的那格
   自动档当场覆盖掉。这是本模块唯一真正危险的地方。
4. **写盘失败不许把游戏搞崩**：目录只读 / 磁盘满时只记 ``last_error``。
5. **F5/F9 与自动档互不打扰**：手动槽写坏不影响自动槽。

字面量原则：期望的节点名与次数写死（``"map"`` / ``"level"`` / 1 次），
不从实现里读常量。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Progression, Vitals
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth
from mirsolo.save import AUTO_SLOT, AutoSaver, SaveStore
from mirsolo.save.autosave import KEY_LAST_DEATH, KEY_LEVEL_UP, KEY_MAP

CASES = []
DATA = GameData.load()
SEED = 20261019

#: 自动存档只认这四个标签
NODE_LABELS = ("map", "level", "death", "forge")


def case(fn):
    CASES.append(fn)
    return fn


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="mirsolo_auto_")


def _session(map_id: str = "biqi_field"):
    session = build_session(data=DATA, seed=SEED, map_id=map_id)
    player = populate(session, player_class="warrior")["player"]
    session.step(1)  # tick 从 0 抬到 1：0 是"还没开局"，闸门会把它当读档
    return session, player


def _saver(session, directory: str) -> AutoSaver:
    saver = AutoSaver(SaveStore(directory))
    saver.reset(tick=session.tick, map_id=session.map_id, world=session.world)
    return saver


def _level_up(session, player) -> None:
    """造一次升级事件：写 ``last_level_up``（升级的唯一出口是 ``grant_exp``，
    但这里只验"节点识别"，所以直接写世界键——与真实系统写的形状一致）。
    """
    prog = player.get(Progression)
    prog.level += 1
    session.world.set_data(KEY_LEVEL_UP, {
        "entity": player.id, "level": prog.level, "tick": session.tick,
    })


# ── 四个节点 ────────────────────────────────────────────

@case
def test_nothing_is_written_when_nothing_happens():
    """空跑 60 帧：一个自动档都不该出现。"""
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    for _ in range(60):
        session.step(1)
        assert saver.observe(session) == ""
    assert saver.writes == 0, saver.writes
    assert not Path(directory, f"{AUTO_SLOT}.json").exists()


@case
def test_map_change_triggers_exactly_one_autosave():
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    assert saver.observe(session) == "map"
    assert saver.writes == 1, saver.writes
    # 换图之后再跑 30 帧，不该再存
    for _ in range(30):
        session.step(1)
        assert saver.observe(session) == ""
    assert saver.writes == 1, saver.writes


@case
def test_level_up_triggers_exactly_one_autosave():
    session, player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.step(1)
    _level_up(session, player)
    session.step(1)
    assert saver.observe(session) == "level"
    assert saver.writes == 1, saver.writes
    for _ in range(30):  # ``last_level_up`` 会一直挂在那儿，不能每帧都算新节点
        session.step(1)
        assert saver.observe(session) == ""
    assert saver.writes == 1, saver.writes


@case
def test_death_settlement_triggers_exactly_one_autosave():
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.step(1)
    session.world.set_data(KEY_LAST_DEATH, {
        "entity": "player", "tick": session.tick, "exp_lost": 30,
    })
    session.step(1)
    assert saver.observe(session) == "death"
    assert saver.writes == 1, saver.writes


@case
def test_forge_triggers_on_the_monotonic_counter():
    """锻造结果没有世界键，靠表现层累加的计数识别。"""
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.step(1)
    assert saver.observe(session, forge_seq=0) == ""
    session.step(1)
    assert saver.observe(session, forge_seq=1) == "forge"
    assert saver.writes == 1, saver.writes
    session.step(1)
    assert saver.observe(session, forge_seq=1) == ""  # 计数没变就是没锻造
    assert saver.writes == 1, saver.writes


@case
def test_labels_are_from_the_four_known_nodes():
    """自动档的 label 固定写成「自动·<节点名>」——列表里一眼看出为什么存的。"""
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    saver.observe(session)
    payload = SaveStore(directory).read(AUTO_SLOT)
    assert payload["label"] == "自动·map", payload["label"]
    assert payload["label"].split("·")[1] in NODE_LABELS, payload["label"]


@case
def test_autosave_file_lands_in_the_auto_slot_and_passes_its_own_checksum():
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    saver.observe(session)
    path = Path(directory, f"{AUTO_SLOT}.json")
    assert path.is_file()
    payload = SaveStore(directory).read(AUTO_SLOT)  # read 内部走 verify
    assert payload["schema_version"] == 1
    assert payload["world"]["current_map"] == "biqi_mine_1"


# ── 闸门：读档不引发连锁写盘 ────────────────────────────

@case
def test_loading_an_older_save_does_not_trigger_a_write():
    """把 tick 拨回去（= 读了一份旧档）之后，observe 必须一步都不动。

    这是自动存档最危险的地方：读档是"只读"操作，一旦它触发写盘，
    玩家刚读出来的那一格自动档就被覆盖成"刚读进来、还没开打"的状态了。
    """
    session, _player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.step(200)
    assert saver.observe(session) == ""          # 基线跟到 201
    # 模拟读档：tick 倒退 + 闸门抬起（窗口版 ``_load_game`` 就是这么做的）
    session.world.set_data("tick", 50)
    session.world.set_data(KEY_MAP, "biqi_mine_1")   # 顺带造一个"假换图"
    session.map_id = "biqi_mine_1"
    saver.reset(tick=session.tick, map_id=session.map_id, world=session.world)
    for _ in range(30):
        session.step(1)
        assert saver.observe(session) == ""
    assert saver.writes == 0, saver.writes


@case
def test_same_tick_is_never_a_new_node():
    """同一刻里连着发生两件事只存一次盘：tick 必须严格变大。"""
    session, player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    _level_up(session, player)
    # 注意：这里**不推进** tick
    assert saver.observe(session) == ""
    assert saver.writes == 0, saver.writes


@case
def test_reset_clears_a_pending_but_unseen_node():
    """已经发生过、但还没被 observe 看到的节点，reset 之后不再补存。

    ``reset`` 是"我现在站的这一格就是基线"的意思：``_seen[*] = -1`` 是**下限**
    而不是"忘掉一切"——``observe`` 里那条 ``tick > self._last_tick`` 与
    ``_floor`` 一起把它兜住了，所以读到 ``last_level_up.tick == 50`` 时
    「``50 > -1`` 成立、但 ``tick > 基线`` 不成立」→ 不存。
    """
    session, player = _session()
    directory = _tmpdir()
    saver = _saver(session, directory)
    _level_up(session, player)          # 在第 1 刻升了级
    session.step(1)                     # tick → 2
    saver.reset(tick=session.tick, map_id=session.map_id,
                world=session.world)  # 此刻读了一份档
    for _ in range(10):
        session.step(1)
        assert saver.observe(session) == ""
    assert saver.writes == 0, saver.writes


# ── 失败不许崩 ──────────────────────────────────────────

@case
def test_write_failure_is_recorded_not_raised():
    """存档目录是个**文件**（不是目录）时，写盘必然失败——不许抛。"""
    blocked = Path(_tmpdir(), "not_a_dir")
    blocked.write_text("x", encoding="utf-8")
    session, _player = _session()
    saver = AutoSaver(SaveStore(str(blocked)))
    saver.reset(tick=session.tick, map_id=session.map_id, world=session.world)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    assert saver.observe(session) == "map"  # 节点照样识别
    assert saver.writes == 0
    assert saver.last_error, "失败必须留痕"


@case
def test_manual_slot_and_auto_slot_do_not_touch_each_other():
    """F5 存 slot1 与自动存 autosave 是两份文件，互不覆盖。"""
    session, _player = _session()
    directory = _tmpdir()
    store = SaveStore(directory)
    saver = _saver(session, directory)
    store.write(session, "slot1", label="手动")
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    saver.observe(session)
    assert Path(directory, "slot1.json").is_file()
    assert Path(directory, f"{AUTO_SLOT}.json").is_file()
    assert store.read("slot1")["label"] == "手动"
    assert store.read(AUTO_SLOT)["label"].startswith("自动")


@case
def test_autosave_keeps_every_literal_from_the_manual_one():
    """自动档与手动档是同一种格式——自动档也要能直接 ``load`` 进会话。

    这条防的是"自动存档偷偷少存字段"：两条路径都走 ``codec.dump_session``，
    一旦有人给自动档加个精简分支，round-trip 立刻红。
    """
    session, player = _session()
    player.get(Progression).level = 25
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    saver.observe(session)

    other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
    populate(other, player_class="warrior")
    summary = SaveStore(directory).load(other, AUTO_SLOT)
    assert summary["level"] == 25, summary
    assert summary["map"] == "biqi_mine_1", summary


@case
def test_vitals_survive_a_full_autosave_round_trip():
    """血也必须是"存多少还多少"（钳进上限之后）。"""
    session, player = _session()
    player.get(Progression).level = 30
    apply_growth(player, DATA.classes["classes"]["warrior"], heal_on_level_up=True)
    player.get(Vitals).hp = 40
    directory = _tmpdir()
    saver = _saver(session, directory)
    session.map_id = "biqi_mine_1"
    session.world.set_data(KEY_MAP, "biqi_mine_1")
    session.step(1)
    saver.observe(session)

    other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
    other_player = populate(other, player_class="warrior")["player"]
    SaveStore(directory).load(other, AUTO_SLOT)
    assert other_player.get(Vitals).hp == 40, other_player.get(Vitals).hp
    assert other_player.get(Vitals).max_hp == 165, other_player.get(Vitals).max_hp


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
    print(f"test_autosave: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
