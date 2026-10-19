"""战斗公式：命中率钳制、最小伤害、技能倍率、魔御/防御的分野。

原则：**每条断言都能手算复核**。公式改了就改这里的期望值，
不允许"跑通就算过"——那等于没测。
"""

from __future__ import annotations

from mirsolo.combat import formulas
from mirsolo.determinism import Rng

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_hit_chance_equal_stats():
    """准确 == 敏捷 时就是基准 75.00%（万分比 7500）。"""
    assert formulas.hit_chance_bp(5, 5) == 7500
    assert formulas.hit_chance_bp(20, 20) == 7500


@case
def test_hit_chance_per_point_is_three_percent():
    """每差 1 点 ±3.00%（300 个万分点）；用未触钳制的差距验证线性段。"""
    assert formulas.hit_chance_bp(6, 5) == 7800
    assert formulas.hit_chance_bp(5, 6) == 7200
    assert formulas.hit_chance_bp(8, 5) == 7500 + 900   # 8400，仍在钳制区间内
    assert formulas.hit_chance_bp(15, 5) == formulas.HIT_MAX_BP  # 10500 被钳到 9700


@case
def test_hit_chance_is_clamped():
    """上下限必须钳住：再高的准确也不能 100%，再低也不会 0%。"""
    assert formulas.hit_chance_bp(500, 0) == formulas.HIT_MAX_BP == 9700
    assert formulas.hit_chance_bp(0, 500) == formulas.HIT_MIN_BP == 2000


@case
def test_hit_rate_matches_declared_chance():
    """**实测命中率必须等于"75%/84%/66%"这几个字面数字**（尺度一致性回归）。

    只断言"chance 的数值对不对"抓不住尺度错配：曾出现常量是千分制（750=75%）
    而掷点是 ``rand_int(1, 10000)`` 万分制，结果实际命中率只有 7.5%，
    所有公式单测却全绿。

    ⚠️ 期望值必须写成**字面量**，不能用 ``hit_chance_bp()`` 现算——
    那样常量被改脏时两边一起漂移，测试会跟着一起错（自指式断言等于没测）。

    三组覆盖：持平 75.00%、+3 点 84.00%、-3 点 66.00%（每点 3.00%）。
    每个种子一条独立随机流，样本互不相关；容差 ±1.5 个百分点。
    """
    samples = 4000
    for accuracy, agility, expected_bp in ((5, 5, 7500), (8, 5, 8400), (5, 8, 6600)):
        hits = 0
        for seed in range(samples):
            outcome = formulas.resolve_physical(
                dc=(1, 1), accuracy=accuracy, ac=(0, 0), agility=agility,
                rng=Rng(seed, f"rate{seed}:{accuracy}:{agility}"),
            )
            hits += 1 if outcome.hit else 0
        observed_bp = hits * 10000 // samples
        assert abs(observed_bp - expected_bp) <= 150, (
            f"准确{accuracy}/敏捷{agility}: 应有 {expected_bp} BP，"
            f"实测 {observed_bp} BP（样本 {samples}）——掷点与命中率尺度不一致？")


def _first_hit(**kwargs):
    """在多个种子里找第一次命中（命中率上限 97%，必然存在）。

    测试不去改生产代码加"必中开关"——那等于为了测试篡改规则；
    改为在随机序列里取第一个命中样本，顺带验证"命中率不是 100%"这件事。
    """
    misses = 0
    for seed in range(500):
        outcome = formulas.resolve_physical(rng=Rng(seed, f"probe{seed}"), **kwargs)
        if outcome.hit:
            return outcome, misses
        misses += 1
    raise AssertionError("500 个种子无一命中，命中率钳制失效")


@case
def test_physical_min_damage_floor():
    """攻 1-1 打 防 100-100：命中即至少 1 点（原版"最小伤害"）。"""
    outcome, _ = _first_hit(dc=(1, 1), accuracy=10000, ac=(100, 100), agility=0,
                            power_permille=1000)
    assert outcome.damage == formulas.MIN_DAMAGE == 1


@case
def test_physical_damage_subtracts_defense_roll():
    """伤害 == roll(DC) − roll(AC)，且两个 roll 都记在账上。"""
    outcome, misses = _first_hit(dc=(10, 10), accuracy=10000, ac=(3, 3), agility=0)
    assert outcome.rolls["atk"] == 10 and outcome.rolls["def"] == 3
    assert outcome.damage == 7
    assert misses < 500, "样本里一次都没失手，说明命中判定可能被短路了"


@case
def test_physical_power_permille_scales_attack():
    """技能倍率作用在**攻击 roll**上，不是最终伤害（否则防御会被重复抵扣）。"""
    outcome, _ = _first_hit(dc=(10, 10), accuracy=10000, ac=(3, 3), agility=0,
                            power_permille=1800)
    assert outcome.rolls["atk_powered"] == 18
    assert outcome.damage == 15  # 18 - 3


@case
def test_miss_deals_zero():
    """未命中必须 0 伤害，且不能有 atk roll（省随机数，也说明没算错）。"""
    for seed in range(200):
        outcome = formulas.resolve_physical(dc=(10, 20), accuracy=0, ac=(0, 0),
                                            agility=500, rng=Rng(seed, f"miss{seed}"))
        if not outcome.hit:
            assert outcome.damage == 0
            assert "atk" not in outcome.rolls
            return
    raise AssertionError("200 个种子里没有一次 miss——命中钳制失效")


@case
def test_magic_ignores_ac_but_uses_mac():
    """魔法的分野：堆 AC 无效，堆 MAC 才减伤。"""
    rng = Rng(11, "magic_ac")
    vs_ac = formulas.resolve_magic(mc=(20, 20), ac=(999, 999), mac=(0, 0), rng=rng)
    assert vs_ac.damage == 20, "魔法被物理防御 AC 抵扣了，公式串了"

    rng = Rng(11, "magic_mac")
    vs_mac = formulas.resolve_magic(mc=(20, 20), ac=(0, 0), mac=(15, 15), rng=rng)
    assert vs_mac.damage == 5


@case
def test_magic_ignore_mac_flag():
    """无视魔御的技能：MAC 再高也照打。"""
    rng = Rng(3, "pen")
    outcome = formulas.resolve_magic(mc=(9, 9), ac=(0, 0), mac=(99, 99),
                                     rng=rng, ignore_mac=True)
    assert outcome.damage == 9 and outcome.rolls["mac"] == 0


@case
def test_magic_never_misses():
    """魔法必中：1000 次结算里 hit 必须全为 True。"""
    for seed in range(1000):
        outcome = formulas.resolve_magic(mc=(1, 1), ac=(0, 0), mac=(0, 0),
                                         rng=Rng(seed, "nomiss"))
        assert outcome.hit


@case
def test_taoist_uses_sc_and_mac():
    """道术走 SC − MAC，与魔法同构但不同属性。"""
    rng = Rng(5, "tao")
    outcome = formulas.resolve_taoist(sc=(12, 12), mac=(4, 4), rng=rng)
    assert outcome.rolls["sc"] == 12 and outcome.damage == 8


@case
def test_heal_is_never_negative():
    """治疗量下限为 0，且不吃抵扣。"""
    for seed in range(100):
        assert formulas.heal_amount((0, 3), Rng(seed, "heal")) >= 0


@case
def test_power_permille_floor_is_not_zero():
    """倍率 0 时仍应保留最小伤害（防止数据表写 0 倍率造成"打不动"的死锁）。"""
    outcome, _ = _first_hit(dc=(10, 10), accuracy=10000, ac=(0, 0), agility=0,
                            power_permille=0)
    assert outcome.damage == formulas.MIN_DAMAGE


@case
def test_attack_interval_tiers():
    """攻速档位：1 档 1.40s，11 档 0.40s，越界钳制。"""
    assert formulas.attack_interval_seconds(1) == 1.40
    assert formulas.attack_interval_seconds(11) == 0.40
    assert formulas.attack_interval_seconds(0) == 1.40
    assert formulas.attack_interval_seconds(99) == 0.40


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
    print(f"test_formulas: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
