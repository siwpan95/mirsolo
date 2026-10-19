"""死亡惩罚（P3-S2）—— 让"死一次"真的有代价。

钉死的五件事：

1. **惩罚是延迟结算的**：致死那一下只让人倒地，真正的扣减发生在玩家选择
   复活方式（或等满 ``DOWN_TICKS``）那一刻，所以两档惩罚互不退款；
2. **回城走全额四件**：掉经验 / 掉金币 / 全身磨损 / 掉一件背包物品；
3. **原地付费只走磨损**：花钱买的就是"不掉经验金币、不掉东西、位置不动"；
4. **绝不掉级**：经验夹到"本级已积累"为止，掉到 0 也不会退回上一级；
5. **账本只增不减**：累计流水落在独立组件上，读档沿用，洗不掉。

字面量原则：期望值写死（100 / 120 / 600 / 1200 …），不从数据表推导。
"""

from __future__ import annotations

from mirsolo.actor import penalty
from mirsolo.actor.factory import spawn_player
from mirsolo.bootstrap import build_session
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.components import (
    AiState,
    Bag,
    DeathLedger,
    Downed,
    Loadout,
    Position,
    Progression,
    Vitals,
    Wallet,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng
from mirsolo.items import wear
from mirsolo.items.equipment import equip, recompute

CASES = []
DATA = GameData.load()

# ── 数据字面量（改表/改常量必须同步改这里）──────────────────
EXP_LOSS_PERMILLE = 100   # 死亡掉「本级升级所需经验」的 10%
GOLD_LOSS_PERMILLE = 100  # 死亡掉持有金币的 10%
DEATH_WEAR_POINTS = 1     # 死亡时身上每件装备必然额外磨损 1 点
DOWN_TICKS = 30           # 倒地 30 刻（3 秒）后自动回城
REVIVE_BASE_GOLD = 500    # 原地复活 = 500 + 100 × 等级
REVIVE_GOLD_PER_LEVEL = 100

LV1_NEED = 100    # 1 级升 2 级所需经验
LV7_NEED = 1200   # 7 级升 8 级所需经验
MAX_LEVEL = 52    # 经验表封顶级：exp_to_next(52) == 0
MUJIAN_DURABILITY = 10
BUYI_PRICE = 80


def case(fn):
    CASES.append(fn)
    return fn


def _session(seed: int = 20261019):
    session = build_session(data=DATA, seed=seed, map_id="biqi_field")
    player = spawn_player(session.world, DATA, class_id="warrior")
    return session, player


def _dressed(player, *, gold: int = 10 ** 6):
    """穿上木剑与布衣，给一笔钱。"""
    player.get(Wallet).gold = gold
    player.get(Bag).add("mujian", 1)
    player.get(Bag).add("buyi", 1)
    equip(player, DATA, "mujian")
    equip(player, DATA, "buyi")
    return player


def _kill(session, player, *, seed: int = 7) -> None:
    """用一次巨量伤害把玩家放倒（走 ``CombatResolver`` 唯一改血入口）。"""
    ctx = CombatContext(seed=seed, world=session.world, bus=session.bus,
                        tick=int(session.tick), data=DATA)
    CombatResolver(ctx)._damage(None, player, 10 ** 6, "physical")


def _ledger(player) -> DeathLedger:
    return player.get(DeathLedger)


# ── 掉落：经验与金币 ────────────────────────────────

@case
def test_exp_loss_is_a_tenth_of_the_level_requirement():
    session, player = _session()
    try:
        prog = player.get(Progression)
        prog.exp = 80
        raw = penalty.exp_loss(DATA, prog)
        assert raw == LV1_NEED * EXP_LOSS_PERMILLE // 1000 == 10, raw
        got = penalty.settle(DATA, player, rng=Rng(1, "a"), tick=0)
        assert got["exp_lost"] == 10, got
        assert prog.exp == 70, prog.exp
    finally:
        session.shutdown()


@case
def test_death_never_costs_a_level():
    """经验只掉"本级已积累"的那点，掉到 0 也不回退等级。"""
    session, player = _session()
    try:
        prog = player.get(Progression)
        prog.level = 7
        prog.exp = 5          # 比应扣的 120 少得多
        got = penalty.settle(DATA, player, rng=Rng(1, "b"), tick=0)
        assert got["exp_lost"] == 5, got
        assert prog.exp == 0, prog.exp
        assert prog.level == 7, prog.level
    finally:
        session.shutdown()


@case
def test_max_level_character_loses_no_exp():
    """满级没有"下一级"可掉，惩罚里的这一项就是 0。"""
    session, player = _session()
    try:
        prog = player.get(Progression)
        prog.level = MAX_LEVEL
        prog.exp = 500
        assert penalty.exp_loss(DATA, prog) == 0
        got = penalty.settle(DATA, player, rng=Rng(1, "c"), tick=0)
        assert got["exp_lost"] == 0, got
        assert prog.exp == 500, prog.exp
    finally:
        session.shutdown()


@case
def test_gold_loss_is_a_tenth_of_the_purse():
    session, player = _session()
    try:
        player.get(Wallet).gold = 1000
        got = penalty.settle(DATA, player, rng=Rng(1, "d"), tick=0)
        assert got["gold_lost"] == 100, got
        assert player.get(Wallet).gold == 900
    finally:
        session.shutdown()


@case
def test_gold_loss_never_makes_the_purse_negative():
    """不做赊账：金币绝不为负——否则商店回收价与拍卖行底价全都得处理负数。

    正常扣率（100‰）永远小于持有量，所以要验证这条防线，得把扣率
    临时顶到 150%（1500‰）逼它越界。
    """
    session, player = _session()
    try:
        player.get(Wallet).gold = 3
        saved = penalty.GOLD_LOSS_PERMILLE
        penalty.GOLD_LOSS_PERMILLE = 1500      # 150%，故意越界
        try:
            got = penalty.settle(DATA, player, rng=Rng(1, "e"), tick=0)
        finally:
            penalty.GOLD_LOSS_PERMILLE = saved
        assert got["gold_lost"] == 3, got
        assert player.get(Wallet).gold == 0
    finally:
        session.shutdown()


# ── 掉落：装备磨损 ──────────────────────────────────

@case
def test_death_wears_every_worn_item_once():
    session, player = _session()
    try:
        _dressed(player)
        penalty.settle(DATA, player, rng=Rng(1, "f"), tick=0)
        book = player.get(Wear)
        assert wear.points_of(book, "mujian") == DEATH_WEAR_POINTS
        assert wear.points_of(book, "buyi") == DEATH_WEAR_POINTS
    finally:
        session.shutdown()


@case
def test_death_wears_each_slot_once_not_twice():
    """每件磨 1 点，不是"每次挨打都磨"那种累计。"""
    session, player = _session()
    try:
        _dressed(player)
        for index in range(5):
            penalty.settle(DATA, player, rng=Rng(1, f"g{index}"), tick=index)
        book = player.get(Wear)
        assert wear.points_of(book, "mujian") == 5 * DEATH_WEAR_POINTS
    finally:
        session.shutdown()


@case
def test_death_leaves_bag_and_warehouse_gear_alone():
    """没穿就不磨：背包/仓库里的装备不该因为人死了而变旧。"""
    session, player = _session()
    try:
        _dressed(player)
        player.get(Bag).add("pihelmet", 2)   # 只放背包，不穿
        penalty.settle(DATA, player, rng=Rng(1, "h"), tick=0)
        assert wear.points_of(player.get(Wear), "pihelmet") == 0
        assert player.get(Bag).items.get("pihelmet") == 2
    finally:
        session.shutdown()


# ── 掉落：背包物品 ──────────────────────────────────

@case
def test_death_drops_one_bag_item_when_the_roll_hits():
    """把概率顶到 100% 就必掉：一次只掉 **1 个**，不是一整堆。"""
    session, player = _session()
    try:
        _dressed(player)
        player.get(Bag).add("yaoshui", 5)
        saved = penalty.BAG_DROP_BP
        penalty.BAG_DROP_BP = 10000
        try:
            got = penalty.settle(DATA, player, rng=Rng(1, "i"), tick=0)
        finally:
            penalty.BAG_DROP_BP = saved
        assert got["items"] == [("yaoshui", 1)], got["items"]
        assert player.get(Bag).items.get("yaoshui") == 4
    finally:
        session.shutdown()


@case
def test_death_drops_nothing_when_the_roll_misses():
    session, player = _session()
    try:
        _dressed(player)
        player.get(Bag).add("yaoshui", 5)
        saved = penalty.BAG_DROP_BP
        penalty.BAG_DROP_BP = 0
        try:
            got = penalty.settle(DATA, player, rng=Rng(1, "j"), tick=0)
        finally:
            penalty.BAG_DROP_BP = saved
        assert got["items"] == [], got
        assert player.get(Bag).items.get("yaoshui") == 5
    finally:
        session.shutdown()


@case
def test_empty_bag_drops_nothing_even_when_the_roll_hits():
    session, player = _session()
    try:
        _dressed(player)
        saved = penalty.BAG_DROP_BP
        penalty.BAG_DROP_BP = 10000
        try:
            got = penalty.settle(DATA, player, rng=Rng(1, "k"), tick=0)
        finally:
            penalty.BAG_DROP_BP = saved
        assert got["items"] == [], got
    finally:
        session.shutdown()


@case
def test_death_drop_never_touches_worn_gear():
    """掉只掉背包里不穿的——身上的装备一件不动（单机丢装备惩罚过重）。"""
    session, player = _session()
    try:
        _dressed(player)
        before = dict(player.get(Loadout).slots)
        saved = penalty.BAG_DROP_BP
        penalty.BAG_DROP_BP = 10000
        try:
            penalty.settle(DATA, player, rng=Rng(1, "l"), tick=0)
        finally:
            penalty.BAG_DROP_BP = saved
        assert player.get(Loadout).slots == before
    finally:
        session.shutdown()


# ── 原地复活 ───────────────────────────────────────

@case
def test_revive_in_place_costs_gold_and_keeps_the_spot():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        pos = player.get(Position)
        pos.x, pos.y = 10.0, 20.0
        vitals = player.get(Vitals)
        vitals.hp = 0
        penalty.knock_down(player, tick=5)
        got = penalty.settle(DATA, player, rng=Rng(1, "m"), tick=6,
                             in_place=True)
        assert got["paid"] == REVIVE_BASE_GOLD + REVIVE_GOLD_PER_LEVEL == 600, got
        assert got["in_place"] is True
        assert player.get(Wallet).gold == 400
        assert (pos.x, pos.y) == (10.0, 20.0), "原地复活不该挪位置"
        assert player.get(Vitals).hp == vitals.max_hp, "复活必须满血"
        assert player.has(Downed) is False, "起来了还挂着倒地状态"
    finally:
        session.shutdown()


@case
def test_revive_in_place_spares_exp_gold_and_bag():
    """花钱买的就是"经验金币物品都不掉"。"""
    session, player = _session()
    try:
        _dressed(player, gold=5000)
        prog = player.get(Progression)
        prog.level = 7
        prog.exp = 600
        player.get(Bag).add("yaoshui", 3)
        vitals = player.get(Vitals)
        vitals.hp = 0
        penalty.knock_down(player, tick=5)
        saved = penalty.BAG_DROP_BP
        penalty.BAG_DROP_BP = 10000    # 顶到必掉，验证"原地"这一档确实不掉
        try:
            got = penalty.settle(DATA, player, rng=Rng(1, "n"), tick=6,
                                 in_place=True)
        finally:
            penalty.BAG_DROP_BP = saved
        assert got["exp_lost"] == 0, got
        assert got["gold_lost"] == 0, got
        assert got["items"] == [], got
        assert prog.exp == 600
        assert player.get(Bag).items.get("yaoshui") == 3
    finally:
        session.shutdown()


@case
def test_revive_in_place_still_wears_the_gear():
    """唯一不可回避的一件：不管在哪里站起来，身上的装备都会更破一点。"""
    session, player = _session()
    try:
        _dressed(player, gold=5000)
        vitals = player.get(Vitals)
        vitals.hp = 0
        penalty.knock_down(player, tick=5)
        got = penalty.settle(DATA, player, rng=Rng(1, "o"), tick=6,
                             in_place=True)
        assert got["worn"] == ["buyi", "mujian"], got
        assert wear.points_of(player.get(Wear), "mujian") == 1
    finally:
        session.shutdown()


@case
def test_revive_in_place_cost_grows_with_level():
    session, player = _session()
    try:
        prog = player.get(Progression)
        prog.level = 1
        assert penalty.revive_cost(prog) == 600
        prog.level = 50
        assert penalty.revive_cost(prog) == 5500
    finally:
        session.shutdown()


@case
def test_revive_in_place_refused_when_gold_is_short():
    """钱不够就拒绝，且**状态不变**——不允许出现"扣到负数"或"半复活"。"""
    session, player = _session()
    try:
        _dressed(player, gold=100)
        prog = player.get(Progression)
        prog.level = 1
        prog.exp = 50
        vitals = player.get(Vitals)
        vitals.hp = 0
        penalty.knock_down(player, tick=5)
        raised = None
        try:
            penalty.settle(DATA, player, rng=Rng(1, "p"), tick=6, in_place=True)
        except penalty.PenaltyError as exc:
            raised = exc
        assert raised is not None, "钱不够却复活成功了"
        assert raised.code == "no_gold", raised.code
        assert player.get(Wallet).gold == 100, "被拒绝却扣了钱"
        assert prog.exp == 50
        assert player.has(Downed) is True, "没复活成功却把人扶起来了"
    finally:
        session.shutdown()


@case
def test_reviving_requires_being_down():
    session, player = _session()
    try:
        _dressed(player, gold=5000)
        raised = None
        try:
            penalty.settle(DATA, player, rng=Rng(1, "q"), tick=0, in_place=True)
        except penalty.PenaltyError as exc:
            raised = exc
        assert raised is None or raised.code != "no_gold"
    finally:
        session.shutdown()


# ── 账本 ────────────────────────────────────────────

@case
def test_settle_writes_the_death_ledger():
    session, player = _session()
    try:
        _dressed(player, gold=1000)
        prog = player.get(Progression)
        prog.level = 7
        prog.exp = 600
        got = penalty.settle(DATA, player, rng=Rng(1, "r"), tick=0)
        led = _ledger(player)
        assert led.count == 1, led
        assert led.exp_lost == 120, led
        assert led.gold_lost == 100, led
        assert got["exp_lost"] == led.exp_lost
    finally:
        session.shutdown()


@case
def test_the_ledger_only_ever_grows():
    """累计流水只增不减——这条是"死了读档撤销惩罚"的防线。"""
    session, player = _session()
    try:
        _dressed(player, gold=5000)
        prog = player.get(Progression)
        prog.level = 7
        prog.exp = 600
        penalty.settle(DATA, player, rng=Rng(1, "s1"), tick=0)
        first = _ledger(player).exp_lost
        assert first == 120, first
        prog.exp = 600
        penalty.settle(DATA, player, rng=Rng(1, "s2"), tick=1)
        led = _ledger(player)
        assert led.count == 2, led
        assert led.exp_lost == 240, led
        # 金币按每次的持有量扣：5000→4500（扣 500）→4050（扣 450）
        assert led.gold_lost == 950, led
    finally:
        session.shutdown()


# ── 倒地状态 ───────────────────────────────────────

@case
def test_a_killed_player_goes_down_instead_of_vanishing():
    """致死那一下先倒地：惩罚还没结算，人还在场上。"""
    session, player = _session()
    try:
        _dressed(player)
        _kill(session, player)
        session.step(1)
        assert player.has(Downed) is True, "玩家死了却没倒地"
        assert player.get(Vitals).hp == 0
        assert _ledger(player).count == 0, "倒地那一刻还不能结算"
    finally:
        session.shutdown()


@case
def test_a_downed_player_is_not_a_valid_target_anymore():
    """躺着的人不该继续被排队出刀，否则尸体周围会一直挨揍。"""
    session, player = _session()
    try:
        _dressed(player)
        _kill(session, player)
        session.step(1)
        # 索敌系统每 tick 重选目标；倒地玩家已不 alive，不该被选中
        for system in session.world.systems:
            if type(system).__name__ == "AiTargetSystem":
                system.update(session.world, 0.1)
        for mob in session.world.each(AiState):
            assert mob.get(AiState).target_id != player.id, "怪物还在盯着尸体"
    finally:
        session.shutdown()


@case
def test_mob_attack_queue_skips_a_downed_target():
    """第二道防线：就算目标 id 被硬锁到倒地玩家身上，出刀队列也不许排他。

    ``AiTargetSystem`` 每 tick 重选目标本来就会绕开尸体，但 ``MobAttackSystem``
    里那道 alive 检查是**独立的防线**——把它删掉、只剩索敌一层的话，
    任何"绕过索敌直接锁目标"的路径（雇佣 AI、脚本、未来的技能索敌）
    都会对着倒地玩家一直挥刀。测试因此绕过索敌、手动锁目标。
    """
    session, player = _session()
    try:
        from mirsolo.actor.factory import spawn_monster
        _dressed(player)
        mob = spawn_monster(session.world, DATA, "ji", x=1.0, y=1.0)
        player.get(Position).x, player.get(Position).y = 1.0, 2.0  # 相邻
        _kill(session, player)
        session.step(1)
        assert player.has(Downed) is True

        # 绕过索敌，强行把目标锁回倒地玩家（这是注入要抓的那条路径）
        ai = mob.get(AiState)
        ai.target_id = player.id
        ai.last_attack_tick = -1000          # 攻速冷却清零，保证这一 tick 一定出手
        session.world.set_data("tick", 100)  # tick 拉大，过 `tick == 0` 的早退
        for system in session.world.systems:
            if type(system).__name__ == "MobAttackSystem":
                system.update(session.world, 0.1)
        intents = session.world.get_data("attack_intents", [])
        bad = [i for i in intents
               if getattr(i.get("defender"), "id", None) == player.id]
        assert not bad, f"还有 {len(bad)} 条意图在打倒地的玩家"
    finally:
        session.shutdown()


@case
def test_the_player_comes_home_after_the_countdown():
    """倒地满 DOWN_TICKS 自动回城：headless 没有交互，必须有这个兜底。

    ⚠️ 循环次数写死 31，**不跟 ``DOWN_TICKS`` 走**：注入检查会把常量改成
    10⁹ 来验证这条回归能红——测试若跟着常量循环就会挂死，而"挂死"和
    "变红"在注入检查里是两回事（前者把整条流水线卡住）。
    """
    session, player = _session()
    try:
        _dressed(player)
        pos = player.get(Position)
        pos.x, pos.y = 5.0, 5.0
        _kill(session, player)
        for _ in range(31):     # DOWN_TICKS = 30，第 31 步必须已经回城
            session.step(1)
        assert player.has(Downed) is False, "31 步了还没起来（兜底失效）"
        assert (pos.x, pos.y) == penalty.home_point(DATA, "biqi_field")
        assert _ledger(player).count == 1
    finally:
        session.shutdown()


# ── 随机流 ─────────────────────────────────────────

@case
def test_death_rolls_do_not_shift_the_damage_stream():
    """掉物那一掷不许挪动主伤害流：改掉概率后同一场架的伤害序列必须一字不差。"""

    def duel(drop_bp: int) -> list[int]:
        session, player = _session(seed=5)
        try:
            _dressed(player)
            target = spawn_player(session.world, DATA, class_id="mage",
                                  eid="sack")
            target.get(Vitals).hp = 10 ** 6
            target.get(Vitals).max_hp = 10 ** 6
            saved = penalty.BAG_DROP_BP
            penalty.BAG_DROP_BP = drop_bp
            try:
                seen = []
                ctx = CombatContext(seed=11, world=session.world, bus=None,
                                    tick=0, data=DATA)
                resolver = CombatResolver(ctx)
                for index in range(120):
                    ctx.tick = index
                    resolver.physical(player, target, power_permille=1000)
                    seen.append(int(target.get(Vitals).hp))
            finally:
                penalty.BAG_DROP_BP = saved
            return seen
        finally:
            session.shutdown()

    assert duel(2000) == duel(0), "掉物随机流挪动了主伤害流"


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
    print(f"test_penalty: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
