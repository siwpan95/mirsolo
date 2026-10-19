"""端到端：ECS 装配 → 攻击 → 死亡 → 经验/掉落 → 刷怪。

这一层测的不是"某个函数对不对"，而是**装配是否真的跑起来**：
系统优先级、事件总线订阅、世界数据键、实体增删，任何一环接错都会在这里红。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.actor.systems import KEY_KILL_COUNTS, KEY_REWARDS
from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Attributes, Bag, Identity, Position, Progression, Vitals, Wallet
from mirsolo.data.loader import GameData

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_session_has_core_services():
    session = build_session(data=DATA, seed=1, map_id="biqi_field")
    try:
        assert session.world is not None and session.bus is not None
        # 用**名字集合**而不是数量断言：少装一个系统时能直接看出少的是哪个
        expected = {
            "TickSystem", "RegenSystem", "PotionSystem", "CooldownSystem",
            "MovementSystem",
            "SkillSystem", "StatusSystem", "MobChaseSystem", "AiTargetSystem",
            "RingEffectSystem", "MobAttackSystem", "AttackSystem",
            "AuctionSystem", "MercenarySystem",
            "DeathRewardSystem", "CodexSystem", "DeathPenaltySystem",
            "RespawnSystem", "SiegeSystem", "PortalSystem",
        }
        names = {type(s).__name__ for s in session.world.systems}
        assert names == expected, f"系统装配与预期不符：多 {names - expected} 少 {expected - names}"
        # 系统按 priority 排过序后执行：TickSystem 必须最先
        ordered = sorted(session.world.systems, key=lambda s: s.priority)
        assert type(ordered[0]).__name__ == "TickSystem"
        # 关键先后关系：选敌 → 追击 → 出刀 → 结算，顺序错一环就整条链路慢一拍
        order = [type(s).__name__ for s in ordered]
        assert order.index("AiTargetSystem") < order.index("MobChaseSystem") < order.index("MobAttackSystem")
        assert order.index("MobAttackSystem") < order.index("AttackSystem")
        assert order.index("AttackSystem") < order.index("DeathRewardSystem")
        # 图鉴在死亡奖励之后（先发掉落，再顺带记一笔图鉴）
        assert order.index("DeathRewardSystem") < order.index("CodexSystem")
        # 死亡惩罚排在整串死亡副作用的最末：奖励 → 图鉴 → 惩罚/复活
        assert order.index("CodexSystem") < order.index("DeathPenaltySystem")
        # 特戒结隐要在出刀之前读到"本 tick 有没有出手"，又必须在 AI 走位之后
        # 才算得准——夹在两者之间，挪到任一侧都会在重载测试里变红
        assert order.index("AiTargetSystem") < order.index("RingEffectSystem")
        # 拍卖行撮合在战斗之前跑完（只动金币与挂单，不碰战斗状态）
        assert order.index("RegenSystem") < order.index("AuctionSystem")
        assert order.index("AuctionSystem") < order.index("AttackSystem")
        # 佣兵到期解散必须在出刀之前（到期的人不该在这一 tick 还挥一刀）
        assert order.index("AuctionSystem") < order.index("MercenarySystem")
        # 喝药在战斗（28/30）之前：药效必须赶在怪物这一 tick 出刀前进账
        assert order.index("MercenarySystem") < order.index("PotionSystem")
        assert order.index("PotionSystem") < order.index("MobAttackSystem")
        assert order.index("MercenarySystem") < order.index("AttackSystem")
        assert order.index("RingEffectSystem") < order.index("AttackSystem")
        # 战役推进在伤害结算之后（否则读到"打之前"的血量），补刷之前
        assert order.index("DeathRewardSystem") < order.index("SiegeSystem")
        assert order.index("SiegeSystem") < order.index("RespawnSystem")
        # 换图必须最后：先让这一 tick 的移动结算完，再判断脚下是不是传送点
        assert order[-1] == "PortalSystem", order[-1]
    finally:
        session.shutdown()


@case
def test_populate_creates_player_and_monsters():
    session = build_session(data=DATA, seed=1)
    try:
        spawned = populate(session)
        player = spawned["player"]
        assert player.get(Identity).kind == "player"
        assert player.get(Progression).level == 1
        # 比奇省外围刷新表：16 + 10 + 8 = 34 只
        assert len(spawned["monsters"]) == 34, len(spawned["monsters"])
    finally:
        session.shutdown()


@case
def test_kill_grants_exp_and_drops():
    """一刀秒鸡：死亡 → 经验 +10 → 奖励流水 → 尸体移除 → 击杀计数 +1。"""
    session = build_session(data=DATA, seed=20261019)
    try:
        player = populate(session, with_spawns=False)["player"]
        player.add(Attributes(dc=(100, 100), accuracy=10000, agility=0))
        mob = spawn_monster(session.world, DATA, "ji", x=0, y=0)

        for _ in range(30):
            session.queue_attack(player, mob)
            session.step(1)
            if not mob.get(Vitals).alive:
                break

        assert not mob.get(Vitals).alive, "30 刻内未能击杀 1 级鸡"
        assert session.world.get_entity(mob.id) is None, "尸体未移除"
        assert player.get(Progression).exp >= 10, player.get(Progression)
        assert session.kill_counts().get("ji") == 1
        assert len(session.rewards()) == 1
        reward = session.rewards()[0]
        assert reward["monster"] == "ji" and reward["kill_index"] == 1
    finally:
        session.shutdown()


@case
def test_damage_reduces_hp_and_can_kill_player():
    """反向也要通：怪物打玩家要能掉血（不是只有玩家能造成伤害）。"""
    session = build_session(data=DATA, seed=5)
    try:
        player = populate(session, with_spawns=False)["player"]
        mob = spawn_monster(session.world, DATA, "shiwang", x=0, y=0)
        start_hp = player.get(Vitals).hp
        for _ in range(40):
            session.queue_attack(mob, player)
            session.step(1)
            if player.get(Vitals).hp < start_hp:
                break
        assert player.get(Vitals).hp < start_hp, "玩家 40 刻内未被打掉血"
    finally:
        session.shutdown()


@case
def test_same_seed_same_world_history():
    """同种子两次推演：奖励流水必须逐条相同（世界级确定性）。

    注意：升级会调用 ``apply_growth`` 把属性重算成"裸身基准值"，
    因此这里注入的高攻只在前几级有效——本用例只断言**两次推演一致**，
    不断言击杀数量（数量取决于升级时机，那是另一条曲线的事）。
    """
    def run_once() -> list[dict]:
        session = build_session(data=DATA, seed=777)
        try:
            player = populate(session, with_spawns=False)["player"]
            player.add(Attributes(dc=(50, 50), accuracy=10000, agility=0))
            for index in range(20):
                mob = spawn_monster(session.world, DATA, "daocaoren", x=float(index), y=0.0)
                for _ in range(20):
                    session.queue_attack(player, mob)
                    session.step(1)
                    if not mob.get(Vitals).alive:
                        break
            return session.rewards()
        finally:
            session.shutdown()

    first, second = run_once(), run_once()
    assert len(first) == len(second) >= 5, (len(first), len(second))
    assert first == second, "同种子两次推演的奖励流水不一致"


def _alive_monsters(session) -> int:
    """存活怪物数（召唤物不算：它 kind=summon，不占刷新名额）。"""
    return sum(1 for e in session.world.each(Identity, Vitals)
               if e.get(Identity).kind == "monster" and e.get(Vitals).alive)


@case
def test_respawn_refills_after_interval():
    """补刷间隔**按刷点自己的 respawn_seconds**（不是全局 30 秒一刀切）。

    比奇省外围：稻草人 20s / 鸡 25s / 鹿 30s → 200/250/300 刻。
    首个检查刻（10）起算：稻草人 210、鸡 260、鹿 310 才补。
    之前是固定 300 刻全体补刷，直接无视了表里的间隔。
    """
    session = build_session(data=DATA, seed=3)
    try:
        populate(session)
        for mob in list(session.world.each(Vitals)):
            mob.get(Vitals).hp = 0
        session.step(200)   # 还没到任何刷点
        assert _alive_monsters(session) == 0, "补刷来得太早"
        session.step(20)    # 220 ≥ 210：稻草人 8 只
        assert _alive_monsters(session) == 8
        session.step(50)    # 270 ≥ 260：鸡 16 只
        assert _alive_monsters(session) == 24
        session.step(50)    # 320 ≥ 310：鹿 10 只
        assert _alive_monsters(session) == 34
    finally:
        session.shutdown()


@case
def test_boss_respawn_is_much_slower():
    """沃玛教主 600 秒才补刷——教主不能像小怪一样秒回。"""
    session = build_session(data=DATA, seed=3, map_id="woma_temple")
    try:
        populate(session)
        bosses = [e for e in session.world.each(Identity, Vitals)
                  if e.get(Identity).species == "woma_jiaozhu"]
        assert len(bosses) == 1, len(bosses)
        bosses[0].get(Vitals).hp = 0
        session.step(1200)  # 120 秒：小怪会补，教主不会
        bosses = [e for e in session.world.each(Identity, Vitals)
                  if e.get(Identity).species == "woma_jiaozhu"]
        assert bosses == [], "教主 120 秒就复活了，间隔没生效"
    finally:
        session.shutdown()


@case
def test_regen_heals_over_time():
    """自然回血：每 20 刻 +1，100 刻内应恢复 5 点。"""
    session = build_session(data=DATA, seed=3)
    try:
        player = populate(session, with_spawns=False)["player"]
        vitals = player.get(Vitals)
        vitals.hp = 5
        session.step(100)
        assert vitals.hp == 10, vitals.hp
    finally:
        session.shutdown()


@case
def test_kill_count_is_per_species_not_per_entity():
    """击杀序号按 species 累计：两只同种怪分别是 1 和 2（防刷的地基）。"""
    session = build_session(data=DATA, seed=9)
    try:
        player = populate(session, with_spawns=False)["player"]
        player.add(Attributes(dc=(999, 999), accuracy=10000, agility=0))
        for x in (0.0, 1.0):
            mob = spawn_monster(session.world, DATA, "ji", x=x, y=0.0)
            for _ in range(20):
                session.queue_attack(player, mob)
                session.step(1)
                if not mob.get(Vitals).alive:
                    break
        counts = session.kill_counts()
        assert counts.get("ji") == 2, counts
        assert [r["kill_index"] for r in session.rewards()] == [1, 2]
    finally:
        session.shutdown()


@case
def test_rewards_drain_clears_queue():
    session = build_session(data=DATA, seed=9)
    try:
        player = populate(session, with_spawns=False)["player"]
        player.add(Attributes(dc=(999, 999), accuracy=10000, agility=0))
        mob = spawn_monster(session.world, DATA, "ji", x=0.0, y=0.0)
        for _ in range(20):
            session.queue_attack(player, mob)
            session.step(1)
            if not mob.get(Vitals).alive:
                break
        assert session.drain_rewards()
        assert session.rewards() == []
    finally:
        session.shutdown()


@case
def test_player_gold_and_bag_accumulate():
    session = build_session(data=DATA, seed=11)
    try:
        player = populate(session, with_spawns=False)["player"]
        player.add(Attributes(dc=(999, 999), accuracy=10000, agility=0))
        for index in range(30):
            mob = spawn_monster(session.world, DATA, "daocaoren", x=float(index), y=0.0)
            for _ in range(20):
                session.queue_attack(player, mob)
                session.step(1)
                if not mob.get(Vitals).alive:
                    break
        assert player.get(Wallet).gold > 0
        assert player.get(Bag).used > 0, "30 只稻草人一件都没掉，权重可能配错"
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
    print(f"test_bootstrap: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
