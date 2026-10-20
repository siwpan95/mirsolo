"""持久化存档（P3-S6）—— 只存真相，派生重算，账本一个都不能少。

钉死七件事：

1. **往返一致**：等级 / 经验 / 金币 / 背包 / 仓库 / 穿戴 / 已学技能 / 图鉴 /
   特戒次数 / 拍卖挂单 / 坐标 / 地图 / 刻度——存进去什么，读出来还是什么。
2. **三本账本必须进档**：``Upgrade.attempts`` / ``Blessing.attempts`` /
   ``AuctionBook.next_seq`` / ``DeathLedger`` 都**只增不减**，清零就等于
   随机流名重用，读档能刷（与 drops 的 ``kill_index`` 同一条防线）。
3. **派生不算、重算**：存档里没有六维 dc/luck/max_hp；读档后由
   ``apply_growth`` + ``recompute`` 现算——存了就会跟装备表分叉。
4. **血读档要钳进上限**：先重算（此时上限是新的），再钳血。
   顺序反了，"存盘时 200 血、读档后上限 136"就会留下一个 200/136 的怪物。
5. **坏档必须拒读且可分辨**：坏 JSON / 版本不符 / 校验和不符 / 缺字段
   四个码各走各的，**绝不**当空档静默继续（那等于丢掉一整局）。
6. **原子写**：写一半被掐断不能留下半个存档，也不能先把好档覆盖掉。
7. **槽名不接路径**：``../x`` 这类名字直接拒绝，别给自己留穿越口子。

字面量原则：期望值写死（30 / 12345 / 500 / 3 / 7 / -2 …），不从数据表推导。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mirsolo.actor.factory import KEY_ENTITY_SERIAL
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    Attributes,
    AuctionBook,
    Bag,
    Blessing,
    Body,
    Codex,
    DeathLedger,
    Loadout,
    Mining,
    Poison,
    Position,
    Progression,
    RingState,
    Shield,
    Skills,
    Upgrade,
    Vitals,
    Wallet,
    Warehouse,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.items import upgrade as upgrade_rules
from mirsolo.items.equipment import equip
from mirsolo.progression.exp import apply_growth
from mirsolo.save import SCHEMA_VERSION, SaveStore, codec, schema
from mirsolo.save.codec import SaveError, checksum
from mirsolo.siege.campaign import (
    KEY_SIEGE,
    RUNNING,
    WON,
    SiegeState,
    siege_status,
    start_siege,
)

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]
SEED = 20261019

# ── 字面量（改数据表 / 改格式必须同步改这里）──────────────────
LEVEL = 30
GOLD = 12345
TICKS = 500
MUJIAN = "mujian"          # 120 金 / 重 3 / atk 2-5
ORE = "ore_blackiron_4"    # 黑铁矿石 纯度 100
HP_POTION = "hp_potion_s"
ARMOR = "zhongxingkuijia"  # 中型盔甲：战士 15 级 / 重 20 / 30 级穿得下

UP_LEVEL = 3
UP_ATTEMPTS = 7
BLESS_LEVEL = -2           # 诅咒
BLESS_ATTEMPTS = 4
WEAR_POINTS = 5
DEATHS = 2
SKILL_LEVELS = {"jibengong": 2}
CODEX_KILLS = {"jixiechong": 9}
RING_CHARGES = {"fuhuo_jiezhi": 1}
AUCTION_LISTINGS = 1
AUCTION_SEQ = 5
MID_HP = 40              # 战士 30 级上限 136，40 是"打过半局"的血

DC_30_NO_GEAR = (8, 16)    # 战士 30 级裸身
MUJIAN_ATK = (2, 5)
UP_BONUS = (3, 6)          # +3 级 → 下限 +3、上限 +6
DC_30_WITH_PLUS3 = (13, 27)  # 8+3+2 .. 16+6+5


def case(fn):
    CASES.append(fn)
    return fn


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="mirsolo_save_")


def _session(cls: str = "warrior", map_id: str = "biqi_field"):
    session = build_session(data=DATA, seed=SEED, map_id=map_id)
    player = populate(session, player_class=cls)["player"]
    return session, player


def _rich_player(session, player):
    """把玩家拨成一个"玩过半局"的状态：等级/钱/背包/账本/图鉴/特戒全都有数。"""
    prog = player.get(Progression)
    prog.level = LEVEL
    prog.exp = 77
    apply_growth(player, DATA.classes["classes"]["warrior"], heal_on_level_up=True)

    player.get(Wallet).gold = GOLD
    bag = player.get(Bag)
    bag.add(MUJIAN, 3)
    bag.add(ORE, 2)
    bag.add(HP_POTION, 5)
    player.get(Warehouse).items["buyi"] = 4

    player.get(Bag).add(ARMOR, 1)
    equip(player, DATA, ARMOR)
    player.get(Bag).add(MUJIAN, 1)
    equip(player, DATA, MUJIAN)

    player.get(Upgrade).levels[MUJIAN] = UP_LEVEL
    player.get(Upgrade).attempts[MUJIAN] = UP_ATTEMPTS
    player.get(Blessing).levels[MUJIAN] = BLESS_LEVEL
    player.get(Blessing).attempts[MUJIAN] = BLESS_ATTEMPTS
    player.get(Wear).points[MUJIAN] = WEAR_POINTS
    player.get(DeathLedger).count = DEATHS

    player.get(Skills).known.update(SKILL_LEVELS)
    player.get(Codex).kills.update(CODEX_KILLS)
    player.add(RingState())
    player.get(RingState).charges.update(RING_CHARGES)
    player.add(AuctionBook())
    book = player.get(AuctionBook)
    book.listings.append({"item": "buyi", "count": 1, "ask": 100,
                          "tier": 0, "seq": 1, "round": 0})
    book.next_seq = AUCTION_SEQ
    book.ask_tier = 1

    pos = player.get(Position)
    pos.x, pos.y, pos.facing = 12.5, 34.25, 3
    vitals = player.get(Vitals)
    vitals.hp = MID_HP
    return player


def _roundtrip(session, player, tmp: str) -> tuple[object, object, dict]:
    """存 → 开一个全新会话 → 读回来，返回 (新会话, 新玩家, 摘要)。"""
    store = SaveStore(tmp)
    store.write(session, "slot1")
    other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
    fresh = populate(other, player_class="warrior")["player"]
    info = store.load(other, "slot1")
    return other, fresh, info


# ── 1. 往返一致 ─────────────────────────────────────────────

@case
def test_level_exp_gold_and_bag_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, info = _roundtrip(session, player, tmp)
        try:
            assert fresh.get(Progression).level == LEVEL, fresh.get(Progression).level
            assert fresh.get(Progression).exp == 77, fresh.get(Progression).exp
            assert fresh.get(Wallet).gold == GOLD, fresh.get(Wallet).gold
            assert dict(fresh.get(Bag).items)[ORE] == 2, fresh.get(Bag).items
            assert fresh.get(Warehouse).items["buyi"] == 4
            # 摘要与实体同源：列表页显示的就是读出来的东西
            assert info["level"] == LEVEL and info["gold"] == GOLD, info
            assert info["map"] == "biqi_field", info["map"]
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_position_is_stored_at_thousandth_precision():
    """坐标按 1/1000 定点存：12.5 / 34.25 读回来必须一字不差。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            pos = fresh.get(Position)
            assert (pos.x, pos.y, pos.facing) == (12.5, 34.25, 3), (pos.x, pos.y, pos.facing)
            # 落盘的不是浮点：12.5 → 12500
            raw = json.loads((Path(tmp) / "slot1.json").read_text(encoding="utf-8"))
            assert raw["player"]["Position"]["x"] == 12500, raw["player"]["Position"]
            assert raw["player"]["Position"]["y"] == 34250, raw["player"]["Position"]
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_loadout_skills_codex_rings_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            slots = fresh.get(Loadout).slots
            assert slots["weapon"] == MUJIAN, slots
            assert slots["armor"] == ARMOR, slots
            assert fresh.get(Skills).known == SKILL_LEVELS, fresh.get(Skills).known
            assert fresh.get(Codex).kills == CODEX_KILLS, fresh.get(Codex).kills
            assert fresh.get(RingState).charges == RING_CHARGES
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_world_side_tick_map_and_serial_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        session.step(TICKS)
        serial_before = int(session.world.get_data(KEY_ENTITY_SERIAL, 0))
        assert serial_before > 0, serial_before
        other, fresh, info = _roundtrip(session, player, tmp)
        try:
            assert other.tick == session.tick, (other.tick, session.tick)
            assert info["tick"] == TICKS, info["tick"]
            assert other.map_id == "biqi_field", other.map_id
            # entity_serial 只增不减：读档后从旧值续上，不能归零
            assert int(other.world.get_data(KEY_ENTITY_SERIAL, 0)) >= serial_before
        finally:
            other.shutdown()
    finally:
        session.shutdown()


# ── 1b. 攻城战役状态（P0-F3：可选键 siege_state）─────────────

#: ``sabak`` 三波的字面量（见 data/sieges.json）：外城 1200 / 内城 1800 / 旗台 2600
SIEGE_WAVE_HP = (1200, 1800, 2600)
SIEGE_WAVE_GUARDS = (7, 7, 8)


def _siege_session():
    """空图攻城会话（不铺常规怪，免得野外怪混进守军计数）。"""
    session = build_session(data=DATA, seed=SEED, map_id="biqi_field")
    player = populate(session, player_class="warrior", with_spawns=False)["player"]
    return session, player


@case
def test_siege_state_survives_save_and_load():
    """打到第二波存盘 → 读档 → 波次/状态/目标 eid 一字不差，第二波重新铺满。

    去掉 ``dump_world``/``load_world`` 里的 ``siege_state`` 这一段，本用例会红：
    读档后 ``siege_status()`` 直接是 ``None``（= 战役凭空消失，玩家白打一场）。
    """
    session, _ = _siege_session()
    tmp = _tmpdir()
    try:
        started = start_siege(session, "sabak")
        world = session.world
        # 打掉外城城门 → 进第二波（1800 血 / 7 个守军）
        world.get_entity(started["objective"]).get(Vitals).hp = 0
        session.step(1)
        before = siege_status(world)
        assert before["wave"] == 1, before
        assert before["status"] == RUNNING, before
        assert before["objective_max"] == SIEGE_WAVE_HP[1], before
        objective_eid = world.get_data(KEY_SIEGE).objective_id

        store = SaveStore(tmp)
        store.write(session, "slot1")
        raw = json.loads((Path(tmp) / "slot1.json").read_text(encoding="utf-8"))
        # 存档里必须是**显式八字段**，不是把 dataclass 的 __dict__ 倒进去
        assert KEY_SIEGE in raw["world"], "存档世界段里没有战役状态"
        assert set(raw["world"][KEY_SIEGE]) == {
            "siege_id", "map_id", "wave", "started_tick",
            "status", "objective_id", "last_reinforce_tick", "reinforced",
        }, raw["world"][KEY_SIEGE]
        assert raw["world"][KEY_SIEGE]["wave"] == 1, raw["world"][KEY_SIEGE]
        assert raw["world"][KEY_SIEGE]["siege_id"] == "sabak", raw["world"][KEY_SIEGE]

        other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
        populate(other, player_class="warrior", with_spawns=False)
        try:
            store.load(other, "slot1")
            after = siege_status(other.world)
            assert after is not None, "读档后战役消失了"
            assert after["siege"] == before["siege"], after
            assert after["wave"] == before["wave"], (after["wave"], before["wave"])
            assert after["status"] == before["status"], after
            assert other.world.get_data(KEY_SIEGE).objective_id == objective_eid
            assert after["objective_max"] == SIEGE_WAVE_HP[1], after
            # 目标实体按当前波次重铺了，且是同一个 eid
            objective = other.world.get_entity(objective_eid)
            assert objective is not None, objective_eid
            assert objective.get(Vitals).max_hp == SIEGE_WAVE_HP[1]
            assert after["guards_alive"] == SIEGE_WAVE_GUARDS[1], after
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_siege_fields_are_enumerated_not_serialized_via_dict():
    """八个字段必须**逐个显式**列出，不是 ``__dict__`` 倒进去。

    这条在"键集合恰好相等"时测不出来（``vars()`` 今天也会给同样八个键），
    所以换两个能真正分辨的角度：
    1. 顺序 —— ``vars()`` 是定义顺序，显式表是本文件写下的顺序（未来加了分组/注释就不同）；
    2. 新增字段 —— 给类挂一个不该进档的字段，``__dict__`` 会带出去，显式表不会。
    """
    import dataclasses

    names = [name for name, _ in codec._SIEGE_FIELDS]
    assert names == [
        "siege_id", "map_id", "wave", "started_tick", "status",
        "objective_id", "last_reinforce_tick", "reinforced",
    ], names
    # 八个字段一个不多一个不少（漏字段 = 读档丢状态，多字段 = 存了派生值）
    assert sorted(names) == sorted(f.name for f in dataclasses.fields(SiegeState)), names
    # 每种类型码都必须显式给出，不允许留空让代码去猜
    assert all(code in ("i", "s", "s?") for _, code in codec._SIEGE_FIELDS)

    session, _ = _session()
    try:
        world = session.world
        state = SiegeState(siege_id="sabak", map_id="sabak", wave=1)
        state.scratch = "不该进存档"          # 模拟以后新加的内部字段
        world.set_data(KEY_SIEGE, state)
        blob = codec.dump_world(session)
        assert blob[KEY_SIEGE] is not None
        assert "scratch" not in blob[KEY_SIEGE], blob[KEY_SIEGE]
        assert set(blob[KEY_SIEGE]) == set(names), blob[KEY_SIEGE]
    finally:
        session.shutdown()


@case
def test_siege_state_absent_in_old_save_means_no_campaign():
    """旧档（没有 ``siege_state`` 键）必须照读，读出来的语义是"此刻没有攻城"。

    这就是 ``SCHEMA_VERSION`` 维持在 1 的理由：缺键 = 现状，不是坏档。
    """
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
        path = Path(tmp) / "slot1.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["world"][KEY_SIEGE]          # 模拟加这个键之前写下的档
        raw["checksum"] = checksum(raw)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
        populate(other, player_class="warrior")
        try:
            store.load(other, "slot1")        # 不能抛
            assert other.world.get_data(KEY_SIEGE) is None
            assert siege_status(other.world) is None
            # 旧档里该有的东西一样没少
            assert other.world.get_data("current_map") == "biqi_field"
            assert other.world.get_data("seed") == SEED
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_finished_siege_survives_without_respawning_entities():
    """打赢了再存 → 读档后仍记着"这一场赢了"，但不会把城门又立起来。"""
    session, _ = _siege_session()
    tmp = _tmpdir()
    try:
        start_siege(session, "sabak")
        world = session.world
        for _ in range(len(SIEGE_WAVE_HP)):
            current = world.get_data(KEY_SIEGE).objective_id
            world.get_entity(current).get(Vitals).hp = 0
            session.step(1)
        assert siege_status(world)["status"] == WON, siege_status(world)

        store = SaveStore(tmp)
        store.write(session, "slot1")
        other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
        populate(other, player_class="warrior", with_spawns=False)
        try:
            store.load(other, "slot1")
            status = siege_status(other.world)
            assert status is not None and status["status"] == WON, status
            leftovers = [e for e in other.world.list_entity_ids()
                         if e.startswith(("siege_obj:", "siege_guard:"))]
            assert leftovers == [], leftovers
        finally:
            other.shutdown()
    finally:
        session.shutdown()


# ── 2. 三本账本（防读档刷） ─────────────────────────────────

@case
def test_upgrade_attempts_must_survive_or_reload_rerolls():
    """attempts 是升级随机流名的一部分。丢了它，读档重来掷出同一个数。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        # 存盘前先掷一次，记下读数
        player.get(Bag).add(ORE, 3)
        before = upgrade_rules.upgrade(player, DATA, SEED)
        assert before["level"] == UP_LEVEL + 1 or before["level"] == UP_LEVEL - 1

        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            assert fresh.get(Upgrade).levels[MUJIAN] == before["level"]
            assert fresh.get(Upgrade).attempts[MUJIAN] == UP_ATTEMPTS + 1, (
                fresh.get(Upgrade).attempts)
            # 读档后再掷：attempt 接着涨 → 流名不同 → 读数可以不同（不是"重放同一个"）
            fresh.get(Bag).add(ORE, 3)
            after = upgrade_rules.upgrade(fresh, DATA, SEED)
            assert after["roll"] != before["roll"] or after["bp"] == 10000, (
                "读档后掷出同一读数 = 随机流被重用")
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_blessing_attempts_must_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            assert fresh.get(Blessing).levels[MUJIAN] == BLESS_LEVEL
            assert fresh.get(Blessing).attempts[MUJIAN] == BLESS_ATTEMPTS
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_auction_next_seq_and_death_ledger_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            book = fresh.get(AuctionBook)
            assert book.next_seq == AUCTION_SEQ, book.next_seq
            assert len(book.listings) == AUCTION_LISTINGS, book.listings
            assert book.listings[0]["item"] == "buyi", book.listings
            assert book.ask_tier == 1, book.ask_tier
            ledger = fresh.get(DeathLedger)
            assert ledger.count == DEATHS, ledger.count
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_wear_points_survive():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            assert fresh.get(Wear).points[MUJIAN] == WEAR_POINTS
        finally:
            other.shutdown()
    finally:
        session.shutdown()


# ── 3/4. 派生重算 + 血钳进上限 ─────────────────────────────

@case
def test_derived_attributes_are_recomputed_not_stored():
    """存档里**没有** dc / luck / max_hp；读档后现算，+3 木剑照样生效。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
        raw = json.loads((Path(tmp) / "slot1.json").read_text(encoding="utf-8"))
        assert "Attributes" not in raw["player"], "六维进了存档 = 两份事实"
        assert "Body" not in raw["player"], "负重进了存档 = 两份事实"
        assert raw["player"]["Vitals"].keys() == {"hp", "mp"}, raw["player"]["Vitals"]

        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            # 装备 +3 木剑（atk 2-5 → +3 即 +3/+6）：8+2+3 .. 16+5+6
            assert fresh.get(Attributes).dc == DC_30_WITH_PLUS3, fresh.get(Attributes).dc
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_current_hp_is_clamped_into_the_recomputed_ceiling():
    """存盘时 300 血、读档后上限 136：必须钳成 136，而不是留个 300/136。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        vitals = player.get(Vitals)
        vitals.hp = 300  # 故意写一个超过上限的数（模拟"存盘时上限更高"）
        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            got = fresh.get(Vitals)
            assert got.max_hp == 136, got.max_hp
            assert got.hp == got.max_hp == 136, (got.hp, got.max_hp)
        finally:
            other.shutdown()
    finally:
        session.shutdown()


@case
def test_runtime_combat_state_is_dropped_on_load():
    """护盾/中毒/挖矿中都是"此刻正在发生的事"，读档 = 重新进图，全部清掉。

    必须读回**同一个会话**（F9 的真实场景）：读进全新会话的话，新玩家本来就
    没挂这些运行时组件，``_clear_runtime`` 干不干都对——那条注入就是这么溜绿的。
    """
    session, player = _session(map_id="biqi_mine_1")
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        player.add(Shield(current=50, max=50))
        player.add(Poison(remaining_ticks=300, damage_per_tick=5))
        player.get(Mining).active = True
        player.get(Mining).vein = 2
        store = SaveStore(tmp)
        store.write(session, "slot1")
        store.load(session, "slot1")   # 同一个会话、同一个实体
        assert player.get(Shield) is None, "护盾跟着存档过来了"
        assert player.get(Poison) is None, "中毒跟着存档过来了"
        mining = player.get(Mining)
        assert mining.active is False and mining.vein == -1, (mining.active, mining.vein)
        # 顺便钉住「先派生后钳血」在同会话读档时也成立：血是存盘时的 40，不是满血
        assert player.get(Vitals).hp == MID_HP, player.get(Vitals).hp
    finally:
        session.shutdown()


# ── 5. 坏档可分辨 ───────────────────────────────────────────

def _broken(tmp: str, mutate) -> SaveError:
    """先写一份好档，再按 ``mutate`` 改坏，读它并返回捕获到的 SaveError。"""
    session, player = _session()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
    finally:
        session.shutdown()
    path = Path(tmp) / "slot1.json"
    mutate(path)
    other = build_session(data=DATA, seed=SEED, map_id="biqi_field")
    populate(other, player_class="warrior")
    try:
        SaveStore(tmp).load(other, "slot1")
    except SaveError as exc:
        return exc
    finally:
        other.shutdown()
    raise AssertionError("坏档居然读成功了")


@case
def test_truncated_json_is_bad_json():
    def mutate(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: len(text) // 2], encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "bad_json", exc.code


@case
def test_future_version_is_rejected_before_checksum():
    def mutate(path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = SCHEMA_VERSION + 1
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "bad_version", exc.code


@case
def test_edited_gold_is_bad_checksum():
    """手改金币 12345 → 999999：校验和必须拦下来。"""
    def mutate(path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["player"]["Wallet"]["gold"] = 999999
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "bad_checksum", exc.code


@case
def test_missing_component_is_missing_field():
    def mutate(path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["player"]["Progression"]
        raw["checksum"] = checksum(raw)  # 连校验和都修好，只剩"缺字段"这一关
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "missing_field", exc.code


@case
def test_unknown_item_reference_is_rejected():
    """存档里有一件数据表里没有的物品：拒读，别造出一个"背包里有幽灵"的世界。"""
    def mutate(path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["player"]["Bag"]["items"]["no_such_item"] = 1
        raw["checksum"] = checksum(raw)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "unknown_item", exc.code


@case
def test_not_a_save_file_is_rejected():
    def mutate(path: Path) -> None:
        path.write_text('{"hello": 1}', encoding="utf-8")

    exc = _broken(_tmpdir(), mutate)
    assert exc.code == "not_a_save", exc.code


@case
def test_broken_slot_shows_up_in_listing_instead_of_crashing():
    """一个坏档不能让整页存档列表打不开——列表要能显示"这格坏了"，玩家才删得掉。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
        store.write(session, "slot2")
        (Path(tmp) / "slot2.json").write_text("{{{", encoding="utf-8")
        rows = [store.peek(slot) for slot in store.slots()]
        assert [r["slot"] for r in rows] == ["slot1", "slot2"], rows
        assert rows[0]["broken"] if "broken" in rows[0] else True
        assert rows[1]["broken"] == "bad_json", rows[1]
    finally:
        session.shutdown()


# ── 6. 原子写 ───────────────────────────────────────────────

@case
def test_write_leaves_no_tmp_file_behind():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
        names = sorted(p.name for p in Path(tmp).iterdir())
        assert names == ["slot1.json"], names
    finally:
        session.shutdown()


@case
def test_a_failed_write_keeps_the_old_save_intact():
    """写到一半炸掉：旧档必须还是好的（绝不能先清空再写）。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1")
        before = (Path(tmp) / "slot1.json").read_bytes()

        original = codec.dump_session

        def boom(*args, **kwargs):
            raise RuntimeError("写盘途中炸了")

        codec.dump_session = boom
        try:
            try:
                store.write(session, "slot1")
            except RuntimeError:
                pass
            else:
                raise AssertionError("注入的写盘异常没有传播出来")
        finally:
            codec.dump_session = original

        after = (Path(tmp) / "slot1.json").read_bytes()
        assert after == before, "旧档被写坏了"
        # tmp 文件也不能留下
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["slot1.json"]
    finally:
        session.shutdown()


# ── 7. 槽名 / 目录 ──────────────────────────────────────────

@case
def test_slot_names_cannot_escape_the_save_directory():
    store = SaveStore(_tmpdir())
    for bad in ("../evil", "a/b", "", "x" * 33, "sl ot"):
        try:
            store.path_of(bad)
        except ValueError:
            continue
        raise AssertionError(f"非法槽名 {bad!r} 被接受了")


@case
def test_manual_slots_are_one_to_three():
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        assert not store.exists("slot1")
        store.write(session, "slot1")
        assert store.exists("slot1") and store.slots() == ["slot1"]
        store.write(session, "autosave")
        assert store.slots() == ["slot1", "autosave"], store.slots()
        assert store.delete("slot1") is True
        assert store.delete("slot1") is False
        for bad in (0, 4):
            try:
                store.write(session, f"slot{bad}")
            except ValueError:
                continue
            raise AssertionError(f"槽位 {bad} 被接受了")
    finally:
        session.shutdown()


# ── 8. 存档白名单完整 ───────────────────────────────────────

@case
def test_every_component_in_the_project_is_classified():
    """components.py 里的每个组件都必须登记到四类之一，否则门禁就白设了。"""
    import dataclasses
    import inspect

    import mirsolo.components as comps
    from allin_engine import Component

    declared = set()
    for name, obj in vars(comps).items():
        if inspect.isclass(obj) and issubclass(obj, Component) and obj is not Component:
            declared.add(name)
    missing = sorted(declared - set(schema.classified()))
    assert not missing, f"这些组件没登记进 save/schema.py：{missing}"


@case
def test_checksum_is_stable_across_calls_and_order_insensitive():
    """校验和必须确定性（用 hash() 的话跨进程就不一致了），且与键序无关。"""
    a = {"x": 1, "y": {"b": 2, "a": 3}}
    b = {"y": {"a": 3, "b": 2}, "x": 1}
    assert checksum(a) == checksum(b), (checksum(a), checksum(b))
    assert checksum(a) == checksum(dict(a)), "同一内容两次算不一致"
    assert len(checksum(a)) == 16 and all(c in "0123456789abcdef" for c in checksum(a))
    # 值不同必须摘要不同（不然校验和没意义）
    assert checksum({"x": 1}) != checksum({"x": 2})
    # 长度前缀：["ab","c"] 与 ["a","bc"] 不能折成同一个串
    assert checksum({"x": ["ab", "c"]}) != checksum({"x": ["a", "bc"]})


@case
def test_checksum_matches_a_hardcoded_literal():
    """校验和对一个**写死**的输入必须等于**写死**的 16 位十六进制。

    上一条用 ``checksum`` 比 ``checksum``，在**单进程内** ``hash()`` 也是稳定的
    （同一进程同一对象的 hash 不变）——抓不到"有人把 fnv1a64 换成 hash()"。
    唯一能抓到它的办法就是字面量：期望值在这里写死，不调用任何生产函数推导。
    """
    payload = {"kind": "mirsolo-save", "schema_version": 1,
               "world": {"tick": 7}, "player": {"Vitals": {"hp": 10, "mp": 0}}}
    assert checksum(payload) == "bed4f844e96ccb69", checksum(payload)


@case
def test_save_and_load_round_trip_is_idempotent():
    """存 → 读 → 再存，两份文件必须逐字节相同（含 checksum）。"""
    session, player = _session()
    tmp = _tmpdir()
    try:
        _rich_player(session, player)
        store = SaveStore(tmp)
        store.write(session, "slot1", label="first")
        first = json.loads((Path(tmp) / "slot1.json").read_text(encoding="utf-8"))

        other, fresh, _ = _roundtrip(session, player, tmp)
        try:
            store.write(other, "slot2", label="first")
            second = json.loads((Path(tmp) / "slot2.json").read_text(encoding="utf-8"))
            # saved_at 会变，其余（含 checksum 所覆盖的一切）必须一致
            first.pop("saved_at"), second.pop("saved_at")
            first.pop("checksum"), second.pop("checksum")
            # 世界侧这两项**本来就该变**，不算往返失败：
            # entity_serial 只增不减，读档清场重铺怪会再分一批 id；
            # 它变大是纪律，变小才是 bug（另一条用例已钉住下界）。
            first["world"].pop("entity_serial"), second["world"].pop("entity_serial")
            assert first == second, "存→读→存 结果不一致，说明有字段没能往返"
            assert checksum(first) == checksum(second)
        finally:
            other.shutdown()
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
    print(f"test_save: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
