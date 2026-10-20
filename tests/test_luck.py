"""幸运 9（P2-S3）—— 三条必须钉死的保证。

1. **端点精确**：``luck=0`` 与"根本没传 lucky 参数"逐位一致（不许偷偷改基线），
   ``luck=LUCK_CAP`` 时每一刀都落在攻击上限（这是"刀刀上限"的全部含义）。
2. **不污染随机流**：换幸运值不改变**每次结算消耗的随机数个数**。
   这条看着吹毛求疵，实际很硬：本项目的确定性是靠"流名 + 消耗个数"对齐的，
   一旦随装备变化，换一把刀回放就错位了。由 ``test_stream_tail_*`` 钉住。
3. **来源受控**：幸运只来自武器与项链（``loader.LUCK_SLOTS``），
   且数据表里**真的存在一组能凑到 9 的搭配**——否则机制形同虚设。

字面量原则与其他测试一致：期望值写死，不自指调用生产函数。
"""

from __future__ import annotations

from mirsolo.actor.factory import spawn_monster
from mirsolo.bootstrap import build_session, populate
from mirsolo.combat import formulas
from mirsolo.combat.formulas import CURSE_CAP, LUCK_CAP
from mirsolo.combat.resolver import CombatContext, CombatResolver
from mirsolo.components import Attributes, Bag, Progression
from mirsolo.data.loader import LUCK_SLOTS, GameData
from mirsolo.determinism import Rng
from mirsolo.items.equipment import equip
from mirsolo.presentation.panels import build_character_panel
from mirsolo.progression.exp import apply_growth

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

# 数据表口径的**字面量**（改表必须同步改这里，否则就是悄悄换了规则）
CHIYUE_WEAPON_LUCK = 7      # 赤月魔剑/魔杖/道剑
NIUMO_AXE_LUCK = 7          # 牛魔战斧
ZHUFU_NECKLACE_LUCK = 1     # 祝福项链
XINGYUN_NECKLACE_LUCK = 2   # 幸运项链


def case(fn):
    CASES.append(fn)
    return fn


# ── 1. 几何：幸运收窄区间 ──────────────────────────────────

@case
def test_lucky_range_endpoints_are_exact():
    """两个端点必须精确落在传奇口径上（期望值是手算的字面量）。"""
    assert formulas.lucky_range((22, 36), 0) == (22, 36)   # 0 → 原样
    assert formulas.lucky_range((22, 36), LUCK_CAP) == (36, 36)  # 9 → 单点上限
    # 中间点：spread 10，(9-5)=4 → 20 - 10*4//9 = 16
    assert formulas.lucky_range((10, 20), 5) == (16, 20)
    assert formulas.lucky_range((10, 20)) == (10, 20)      # 默认参数等于 0


@case
def test_lucky_range_swaps_inverted_range():
    """区间倒挂时先交换（数据表写反了也不能崩，更不能给负的展开）。"""
    assert formulas.lucky_range((36, 22), 9) == (36, 36)
    assert formulas.lucky_range((36, 22), 0) == (22, 36)


@case
def test_clamp_luck_bounds():
    assert formulas.clamp_luck(0) == 0
    assert formulas.clamp_luck(LUCK_CAP) == LUCK_CAP
    assert formulas.clamp_luck(99) == LUCK_CAP
    # P3-S5 起**下界放开**：负值就是诅咒，不再被归零。
    # 归零等于诅咒没有代价（普通武器自带 luck 0，钳完还是 0），
    # 祝福油"失败降 1"就只剩"白扔一瓶油"这一种损失。
    assert formulas.clamp_luck(-5) == -5
    assert formulas.clamp_luck(-CURSE_CAP) == -CURSE_CAP
    assert formulas.clamp_luck(-99) == -CURSE_CAP


@case
def test_curse_narrows_from_the_other_end():
    """诅咒是幸运的镜像：幸运抬下限，诅咒压上限（期望值为手算字面量）。"""
    # spread 14：(22,36) 在 luck=-3 时 → 22 + 14*(9-3)//9 = 22+9 = 31
    assert formulas.lucky_range((22, 36), -3) == (22, 31)
    # luck=-7 → 22 + 14*2//9 = 22+3 = 25
    assert formulas.lucky_range((22, 36), -CURSE_CAP) == (22, 25)
    assert formulas.lucky_range((22, 36), -1) == (22, 34)
    # 0 仍然是原样（诅咒与幸运共用的这个端点不许动）
    assert formulas.lucky_range((22, 36), 0) == (22, 36)
    # 镜像性：+9 塌到上限，-7 虽然没有塌到单点，但一定比原区间窄
    assert formulas.lucky_range((22, 36), LUCK_CAP) == (36, 36)
    assert formulas.lucky_range((22, 36), -CURSE_CAP)[1] < 36


# ── 2. 基线不能被偷偷改动 ──────────────────────────────────

@case
def test_zero_luck_is_identical_to_omitting_it():
    """``luck=0`` 必须和完全不传幸运逐位一致——幸运不许改变裸状态。"""
    for seed in range(120):
        kwargs = dict(dc=(10, 30), accuracy=60, ac=(2, 6), agility=3)
        plain = formulas.resolve_physical(rng=Rng(seed, "base"), **kwargs)
        zero = formulas.resolve_physical(rng=Rng(seed, "base"), luck=0, **kwargs)
        assert plain.as_dict() == zero.as_dict(), (
            f"seed={seed}: luck=0 的结果和不传 lucky 不一样，裸基线被改了")


@case
def test_stream_tail_is_independent_of_luck():
    """**幸运值不许改变消耗的随机数个数**（除非区间真的塌成了单点）。

    做法：找一个会命中的种子，分别在 luck=0/1/5 下结算，然后各自再从同一条流里
    抽一个数；只要三次的这个"尾数"相同，说明结算吃掉的随机数个数与幸运无关。
    """
    kwargs = dict(dc=(10, 30), ac=(2, 6), agility=3, accuracy=60)
    seed = None
    for candidate in range(400):
        probe = formulas.resolve_physical(rng=Rng(candidate, "flow"), **kwargs)
        if probe.hit:
            seed = candidate
            break
    assert seed is not None, "400 个种子里一次都没命中，命中判定可能被短路"

    tails = []
    for luck in (0, 1, 5):
        rng = Rng(seed, "flow")
        outcome = formulas.resolve_physical(rng=rng, luck=luck, **kwargs)
        assert outcome.hit, f"luck={luck} 时同一颗种子不再命中"
        tails.append(rng.rand_int(1, 1_000_000))
    assert len(set(tails)) == 1, (
        f"不同幸运值消耗了不同数量的随机数：{tails}（回放会因为换装备而错位）")


@case
def test_collapsed_range_shares_the_no_draw_fastpath():
    """幸运 9 会把区间收成单点，于是这一刀**不抽随机数**——与天生就是 ``(x, x)``
    的区间走同一条快路径（``Rng.rand_int`` 在 ``lo == hi`` 时直接返回，不取 u64）。

    这条不是洁癖而是对齐：幸运满了以后"10-30 打到 30"必须等价于"这把武器的攻击
    天生没有浮动"。若两者消耗不同，同一帧的防御 roll 就会错开一个位置。
    """
    kwargs = dict(ac=(2, 6), agility=3, accuracy=60)
    seed = None
    for candidate in range(400):
        if formulas.resolve_physical(dc=(30, 30), rng=Rng(candidate, "same"),
                                     **kwargs).hit:
            seed = candidate
            break
    assert seed is not None, "400 个种子里一次都没命中"

    flat = Rng(seed, "same")
    flat_outcome = formulas.resolve_physical(dc=(30, 30), rng=flat, **kwargs)
    lucky = Rng(seed, "same")
    lucky_outcome = formulas.resolve_physical(dc=(10, 30), rng=lucky,
                                              luck=LUCK_CAP, **kwargs)
    assert flat_outcome.hit and lucky_outcome.hit
    assert flat_outcome.rolls["atk"] == lucky_outcome.rolls["atk"] == 30
    assert flat.rand_int(1, 1_000_000) == lucky.rand_int(1, 1_000_000), (
        "幸运收成单点后的随机数消耗，与天生就是单点的区间不一致")


# ── 3. 端点 9：刀刀上限 ────────────────────────────────────

@case
def test_luck_nine_every_hit_is_the_cap():
    """幸运 9 的全部含义：每一刀落在攻击上限 36（`dc=(22, 36)` 的字面量）。"""
    hits = 0
    for seed in range(1500):
        outcome = formulas.resolve_physical(
            dc=(22, 36), accuracy=10_000, ac=(0, 0), agility=0,
            rng=Rng(seed, f"cap{seed}"), luck=LUCK_CAP)
        if not outcome.hit:
            continue
        hits += 1
        assert outcome.rolls["atk"] == 36, (
            f"seed={seed}: 幸运 9 仍打出了 {outcome.rolls['atk']}，不是上限 36")
    assert hits > 1200, f"1500 次只命中 {hits} 次，样本太薄，这条断言失去意义"


@case
def test_luck_monotonically_pushes_damage_to_the_cap():
    """幸运越高平均伤害越接近上限；0 时≈区间中点，9 时就是上限本身。"""
    def mean_at(luck: int) -> int:
        """只统计**命中**的刀——命中率钳在 97%，把 miss 的 0 算进来只会稀释信号。"""
        total = hits = 0
        samples = 2400
        for seed in range(samples):
            outcome = formulas.resolve_physical(
                dc=(10, 30), accuracy=10_000, ac=(0, 0), agility=0,
                rng=Rng(seed, f"mean{seed}"), luck=luck)
            if not outcome.hit:
                continue
            total += outcome.damage
            hits += 1
        return total // hits

    low, mid, cap = mean_at(0), mean_at(5), mean_at(LUCK_CAP)
    assert abs(low - 20) <= 1, f"幸运 0 的平均伤害应≈区间中点 20，实测 {low}"
    assert low < mid < cap, f"平均伤害未随幸运单调上升：{low} / {mid} / {cap}"
    assert cap == 30, f"幸运 9 的平均伤害应等于上限 30，实测 {cap}"


@case
def test_magic_and_taoist_are_narrowed_too():
    """法/道同样吃幸运（单机化取舍：否则他们的幸运装毫无意义）。"""
    for seed in range(600):
        magic = formulas.resolve_magic(mc=(4, 20), ac=(0, 0), mac=(0, 0),
                                       rng=Rng(seed, f"mg{seed}"), luck=LUCK_CAP)
        tao = formulas.resolve_taoist(sc=(3, 18), mac=(0, 0),
                                      rng=Rng(seed, f"ta{seed}"), luck=LUCK_CAP)
        assert magic.rolls["mc"] == 20, f"幸运 9 的魔法没落在上限 20：{magic.rolls}"
        assert tao.rolls["sc"] == 18, f"幸运 9 的道术没落在上限 18：{tao.rolls}"


@case
def test_heal_is_not_lucky():
    """治疗/护盾不吃幸运：它们没有"上下限抽取"，套上去会变成每次刷满。"""
    highest = 0
    total = 0
    samples = 400
    for seed in range(samples):
        amount = formulas.heal_amount((0, 10), Rng(seed, "heal"))
        highest = max(highest, amount)
        total += amount
    assert highest <= 10
    assert highest != 10 or total // samples < 9, (
        "治疗看起来被幸运收窄到了上限（每次都是 10？）")


# ── 4. 记账：结算要能自证清白 ──────────────────────────────

@case
def test_luck_is_accounted_in_rolls():
    """吃幸运时账上要有 ``luck`` 与被抬起来的下限；不吃时不写多余字段。"""
    rng = Rng(7, "rolls")
    engaged = formulas.resolve_physical(dc=(10, 30), accuracy=10_000, ac=(0, 0),
                                        agility=0, rng=rng, luck=5)
    assert engaged.rolls["luck"] == 5
    # dc=(10,30)：30 − (30−10)×(9−5)//9 = 30 − 8 = 22
    assert engaged.rolls["atk_floor"] == 22
    plain = formulas.resolve_physical(dc=(10, 30), accuracy=10_000, ac=(0, 0),
                                      agility=0, rng=Rng(7, "rolls"), luck=0)
    assert plain.rolls["luck"] == 0
    assert "atk_floor" not in plain.rolls, "没吃幸运就不该写 atk_floor"


@case
def test_magic_and_taoist_record_their_floor():
    magic = formulas.resolve_magic(mc=(4, 20), ac=(0, 0), mac=(0, 0),
                                   rng=Rng(3, "mf"), luck=LUCK_CAP)
    tao = formulas.resolve_taoist(sc=(3, 18), mac=(0, 0),
                                  rng=Rng(3, "tf"), luck=LUCK_CAP)
    assert magic.rolls["mc_floor"] == 20
    assert tao.rolls["sc_floor"] == 18


# ── 5. 数据：来源受控 + 真的能凑到 9 ───────────────────────

@case
def test_luck_only_lives_on_weapon_and_necklace():
    """幸运只能写在武器与项链上（别的槽写了就是数据表错误，不是"潜能新槽位"）。"""
    offenders = [
        item_id for item_id, spec in ITEMS.items()
        if int(spec.get("luck") or 0) and spec.get("slot") not in LUCK_SLOTS
    ]
    assert not offenders, f"这些物品在非武器/项链槽上写了幸运：{offenders}"
    for item_id, spec in ITEMS.items():
        luck = int(spec.get("luck") or 0)
        if luck:
            assert 0 < luck <= LUCK_CAP, f"{item_id}: 幸运 {luck} 超出 [1, {LUCK_CAP}]"


@case
def test_luck_nine_is_reachable_with_real_gear():
    """必须真的存在一组装备能凑到 9——否则"幸运 9"只是文档里的口号。"""
    assert ITEMS["chiyue_mojian"]["luck"] == CHIYUE_WEAPON_LUCK
    assert ITEMS["niumo_zhanfu"]["luck"] == NIUMO_AXE_LUCK
    assert ITEMS["zhufu_xianglian"]["luck"] == ZHUFU_NECKLACE_LUCK
    assert ITEMS["xingyun_xianglian"]["luck"] == XINGYUN_NECKLACE_LUCK
    assert CHIYUE_WEAPON_LUCK + XINGYUN_NECKLACE_LUCK == LUCK_CAP == 9


# ── 6. 接线：装备 → 属性 → 结算（端到端） ──────────────────

def _session():
    return build_session(data=DATA, seed=5)


def _armed_player(level: int = 50, *, weapon: str | None = "chiyue_mojian",
                  necklace: str | None = "xingyun_xianglian"):
    """造一个指定等级、指定武器/项链的战士（走 grow_attributes + equip 真路径）。"""
    session = _session()
    player = populate(session, with_spawns=False)["player"]
    spec = DATA.classes["classes"]["warrior"]
    prog = player.get(Progression)
    prog.level = level
    prog.exp = 0
    apply_growth(player, spec)
    bag = player.get(Bag)
    for item_id in (weapon, necklace):
        if item_id:
            bag.add(item_id, 1)
            equip(player, DATA, item_id)
    return session, player


@case
def test_equipping_lucky_gear_sets_and_clamps_luck():
    """穿/脱都要反映到 ``Attributes.luck``：这条赋值是可能漏接线的第一处。"""
    _, player = _armed_player(weapon="chiyue_mojian", necklace="xingyun_xianglian")
    assert player.get(Attributes).luck == LUCK_CAP

    _, upper = _armed_player(weapon="chiyue_mojian", necklace="zhufu_xianglian")
    assert upper.get(Attributes).luck == CHIYUE_WEAPON_LUCK + ZHUFU_NECKLACE_LUCK == 8

    _, naked = _armed_player(weapon=None, necklace=None)
    assert naked.get(Attributes).luck == 0, "裸身不该有幸运"


@case
def test_resolver_feeds_luck_into_all_three_channels():
    """物理/魔法/道术三条通路都要把 ``Attributes.luck`` 递进去。

    这是最容易只改一半的地方：公式改了、resolver 忘了传，
    公式单测全绿而游戏里毫无变化。所以这条**必须**走 resolver，不能只测公式。
    """
    session, player = _armed_player()
    target = spawn_monster(session.world, DATA, "kulou", eid="luck_dummy")
    target.get(Attributes).ac = (0, 0)
    target.get(Attributes).mac = (0, 0)
    target.get(Attributes).agility = 0

    attr = player.get(Attributes)
    cap = attr.dc[1]                      # 穿着装备后的攻击上限（自己去看，不推导）
    attr.mc, attr.sc = (4, 20), (3, 18)   # 让魔法/道术通路也有真实的上下限可收窄

    found = 0
    for tick in range(400):
        ctx = CombatContext(seed=session.seed, world=session.world,
                            bus=session.bus, tick=tick)
        resolver = CombatResolver(ctx)
        physical = resolver.physical(player, target)
        if physical.hit:
            # 战士穿的是赤月魔剑 + 幸运 9 → 每刀必须落在攻击上限
            assert physical.rolls["atk"] == cap, (
                f"tick={tick}: resolver 没把幸运 9 传下去，"
                f"atk={physical.rolls['atk']}，上限应为 {cap}")
            found += 1
            if found >= 20:
                break
    assert found >= 20, f"400 tick 里只命中 {found} 次，命中钳制可能失效"

    ctx = CombatContext(seed=session.seed, world=session.world, bus=session.bus,
                        tick=9001)
    resolver = CombatResolver(ctx)
    magic = resolver.magic(player, target)
    taoist = resolver.taoist(player, target)
    assert magic.rolls["mc"] == 20 and magic.rolls["mc_floor"] == 20, (
        f"魔法通路没吃到幸运：{magic.rolls}")
    assert taoist.rolls["sc"] == 18 and taoist.rolls["sc_floor"] == 18, (
        f"道术通路没吃到幸运：{taoist.rolls}")


@case
def test_character_panel_reports_luck_and_the_cap():
    """看得见才算数：角色面板必须写出当前幸运，并在 9 点标注"刀刀上限"。"""
    _, player = _armed_player()
    panel = build_character_panel(player=player, data=DATA)
    joined = " ".join(text.text for text in panel.texts)
    assert f"幸运 {LUCK_CAP}" in joined, f"面板没写幸运：{joined!r}"
    assert "刀刀上限" in joined, f"幸运已满却没有标注：{joined!r}"

    _, partial = _armed_player(weapon="zhanmadao", necklace="zhufu_xianglian")
    plain = " ".join(t.text for t in build_character_panel(player=partial,
                                                           data=DATA).texts)
    assert "幸运 1" in plain
    assert "刀刀上限" not in plain, "幸运没到 9 却标了刀刀上限"


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
    print(f"test_luck: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
