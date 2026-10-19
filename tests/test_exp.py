"""成长：经验曲线查表、连升结转、封顶、属性成长。

其中 ``test_exp_total_matches_table`` 钉的是**表自身的一致性**：
网络上流传的"1-50 总经验 10.8185 亿"与本表逐级相加（10.447969 亿）
不一致——见 ``docs/数据口径.md`` 的差异记录。这里断言的是**本表算出来的值**，
不是网页的宣称值；改了任何一个等级都必须同步改这条。
"""

from __future__ import annotations

from mirsolo.components import Attributes, Body, Progression, Vitals
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import (
    add_exp,
    apply_growth,
    exp_to_next,
    grow_attributes,
    total_exp_span,
)

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


class _FakeEntity:
    """最小实体替身：只实现 apply_growth 需要的 get/add。"""

    def __init__(self, prog, vitals, attr, body):
        self._c = {type(prog): prog, type(vitals): vitals, type(attr): attr, type(body): body}

    def get(self, comp_type):
        return self._c.get(comp_type)


@case
def test_exp_curve_lookup():
    assert exp_to_next(DATA, 1) == 100
    assert exp_to_next(DATA, 9) == 2500
    assert exp_to_next(DATA, 35) == 8_800_000
    assert exp_to_next(DATA, 49) == 200_000_000
    assert exp_to_next(DATA, 50) == 0, "50 级封顶，不应再有升级需求"


@case
def test_exp_total_matches_table():
    """1→50 逐级相加 = 1,044,796,900（非网页宣称的 10.8185 亿，见文档）。"""
    assert total_exp_span(DATA, 50) == 1_044_796_900


@case
def test_exp_curve_is_monotonic_after_10():
    """10 级以后曲线必须单调递增（手录表最容易在这里写错一位数）。"""
    values = [exp_to_next(DATA, lv) for lv in range(10, 50)]
    for prev, nxt in zip(values, values[1:]):
        assert nxt > prev, f"经验曲线在 {prev} -> {nxt} 处不单调"


@case
def test_add_exp_single_level():
    prog = Progression(level=1, exp=0)
    result = add_exp(DATA, prog, 100)
    assert result.levels_gained == 1
    assert prog.level == 2 and prog.exp == 0


@case
def test_add_exp_carries_overflow_across_levels():
    """一次给 1000 经验：100+200+300+400 = 1000 → 恰好升到 5 级、余 0。"""
    prog = Progression(level=1, exp=0)
    result = add_exp(DATA, prog, 1000)
    assert result.levels_gained == 4, result
    assert prog.level == 5 and prog.exp == 0


@case
def test_add_exp_partial_keeps_remainder():
    prog = Progression(level=1, exp=0)
    add_exp(DATA, prog, 130)
    assert prog.level == 2 and prog.exp == 30


@case
def test_add_exp_zero_is_noop():
    prog = Progression(level=3, exp=50)
    result = add_exp(DATA, prog, 0)
    assert prog.level == 3 and prog.exp == 50 and result.levels_gained == 0


@case
def test_cap_level_drops_overflow():
    """封顶后经验不再累积，丢弃量要能被观测到（避免"经验条一直涨"的假象）。"""
    prog = Progression(level=50, exp=0)
    result = add_exp(DATA, prog, 12345)
    assert prog.level == 50 and prog.exp == 0
    assert result.overflow_dropped == 12345


@case
def test_grow_attributes_level_one_is_base():
    warrior = DATA.classes["classes"]["warrior"]
    grown = grow_attributes(warrior, 1)
    assert grown["hp_max"] == 20
    assert grown["dc"] == (1, 2)
    assert grown["ac"] == (0, 2)
    assert grown["accuracy"] == 5 and grown["agility"] == 5
    assert grown["max_weight"] == 30


@case
def test_grow_attributes_level_five():
    """5 级（n=4）：HP 36 / DC 2-4 / AC 0-3 / 准确 7 / 敏捷 5 / 负重 38。

    手算：hp 20+4×4=36；dc_min 1+4//4=2；dc_max 2+4//2=4；
    ac_min 0+4//5=0；ac_max 2+4//3=3；准确 5+4//2=7；敏捷 5+4//5=5。
    """
    warrior = DATA.classes["classes"]["warrior"]
    grown = grow_attributes(warrior, 5)
    assert grown == {
        "hp_max": 36, "mp_max": 0, "dc": (2, 4), "mc": (0, 0), "sc": (0, 0),
        "ac": (0, 3), "mac": (0, 0), "accuracy": 7, "agility": 5, "max_weight": 38,
    }, grown


@case
def test_grow_is_monotonic():
    """等级越高，所有成长维度不得倒退（负数成长是数据表写错的典型症状）。"""
    warrior = DATA.classes["classes"]["warrior"]
    prev = grow_attributes(warrior, 1)
    for level in range(2, 51):
        cur = grow_attributes(warrior, level)
        assert cur["hp_max"] >= prev["hp_max"]
        assert cur["dc"][0] >= prev["dc"][0] and cur["dc"][1] >= prev["dc"][1]
        assert cur["ac"][0] >= prev["ac"][0] and cur["ac"][1] >= prev["ac"][1]
        prev = cur


@case
def test_apply_growth_heals_to_full():
    """升级回满血蓝（传奇手感），且不超上限。"""
    prog = Progression(level=5, exp=0)
    vitals = Vitals(hp=1, max_hp=20, mp=0, max_mp=0)
    attr = Attributes()
    body = Body()
    entity = _FakeEntity(prog, vitals, attr, body)
    apply_growth(entity, DATA.classes["classes"]["warrior"])
    assert vitals.max_hp == 36 and vitals.hp == 36
    assert attr.dc == (2, 4) and attr.ac == (0, 3)
    assert body.max_weight == 38


@case
def test_apply_growth_without_heal_clamps():
    """不回血模式下只做钳制，不能出现 hp > max_hp。"""
    prog = Progression(level=1, exp=0)
    vitals = Vitals(hp=999, max_hp=20, mp=0, max_mp=0)
    entity = _FakeEntity(prog, vitals, Attributes(), Body())
    apply_growth(entity, DATA.classes["classes"]["warrior"], heal_on_level_up=False)
    assert vitals.hp == vitals.max_hp == 20


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
    print(f"test_exp: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
