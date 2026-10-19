"""确定性随机流：同参数同结果、与调用顺序无关、纯整数。

这些用例守护的是**回放与防 save-scum 的地基**：只要有一条红了，
存档复现、战斗日志复算、"读档重打同一只怪掉落不变"就都不成立。
"""

from __future__ import annotations

from mirsolo.determinism import MASK64, Rng, fnv1a64, roll, splitmix64

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_splitmix_is_bijective_on_probe_values():
    """splitmix64 对连续输入必须给出互不相同的输出（雪崩的基本要求）。"""
    seen = {splitmix64(i) for i in range(1000)}
    assert len(seen) == 1000, f"输出碰撞：{1000 - len(seen)} 个"
    for value in seen:
        assert 0 <= value <= MASK64


@case
def test_splitmix_matches_reference_vector():
    """固定输入固定输出——跨 Python 版本也不许变（回放依赖这一点）。

    参考值用本实现首次运行算出后钉死；**改算法必须同步改这张表**，
    否则老存档的随机序列会静默错位。
    """
    vectors = [
        (0, 0xE220A8397B1DCDAF),
        (1, 0x910A2DEC89025CC1),
        (42, 0xBDD732262FEB6E95),
        (0xFFFFFFFFFFFFFFFF, 0xE4D971771B652C20),
    ]
    for seed, expected in vectors:
        got = splitmix64(seed)
        assert got == expected, f"splitmix64({seed}) = {got:#018x}，期望 {expected:#018x}"


@case
def test_stream_sequence_is_reproducible():
    left, right = Rng(1234, "combat"), Rng(1234, "combat")
    assert [left.rand_int(1, 1000) for _ in range(50)] == [right.rand_int(1, 1000) for _ in range(50)]


@case
def test_different_stream_names_diverge():
    """不同流名必须给出不同序列（否则"隔流"失效，加一次 roll 会牵动全部）。"""
    a = Rng(1234, "hit:1->2@0").rand_int(1, 10**9)
    b = Rng(1234, "hit:1->3@0").rand_int(1, 10**9)
    assert a != b, "两个不同流名给出同一值，流隔离失效"


@case
def test_roll_is_position_independent():
    """``roll`` 只吃 ``(seed, stream, index)``：乱序复算也得到同一组值。"""
    forward = [roll(7, "drop:ji", i, 1, 100) for i in range(20)]
    scrambled = [roll(7, "drop:ji", i, 1, 100) for i in (13, 0, 19, 5, 2, 17, 8, 1,
                                                        11, 4, 16, 7, 3, 18, 9, 6,
                                                        14, 12, 15, 10)]
    assert sorted(zip(range(20), forward)) == sorted(
        (i, v) for i, v in zip((13, 0, 19, 5, 2, 17, 8, 1, 11, 4, 16, 7, 3, 18, 9, 6,
                                14, 12, 15, 10), scrambled)
    ), "乱序复算与正序结果不一致"


@case
def test_rand_int_stays_in_bounds():
    rng = Rng(99, "bounds")
    for _ in range(500):
        value = rng.rand_int(3, 9)
        assert 3 <= value <= 9, value
    assert rng.rand_int(5, 5) == 5


@case
def test_rand_int_single_value_no_consumption():
    """退化区间直接返回，不消耗随机数（避免"空 roll 改变后续序列"）。"""
    rng = Rng(5, "degenerate")
    before = rng.getstate()
    assert rng.rand_int(7, 7) == 7
    assert rng.getstate() == before


@case
def test_chance_intervals_are_extremes():
    """``chance(0)`` 永不命中，``chance(10000)`` 必定命中。"""
    never = Rng(1, "c0")
    assert not any(never.chance(0) for _ in range(200))
    always = Rng(1, "c10000")
    assert all(always.chance(10000) for _ in range(200))


@case
def test_chance_frequency_is_roughly_right():
    """3000 抽样下 25% 判定的命中率应落在 20%~30%（只防"完全反了"这类错）。"""
    rng = Rng(2026, "freq")
    hits = sum(1 for _ in range(3000) if rng.chance(2500))
    ratio = hits / 3000
    assert 0.20 <= ratio <= 0.30, f"2500/10000 判定实际命中率 {ratio:.3f}"


@case
def test_state_roundtrip():
    rng = Rng(31337, "save")
    for _ in range(10):
        rng.rand_int(1, 100)
    restored = Rng.fromstate(rng.getstate())
    assert ([rng.rand_int(1, 1000) for _ in range(10)]
            == [restored.rand_int(1, 1000) for _ in range(10)])


@case
def test_fnv_is_stable():
    assert fnv1a64("") == 0xCBF29CE484222325
    assert fnv1a64("a") == fnv1a64("a")
    assert fnv1a64("a") != fnv1a64("b")


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
    print(f"test_determinism: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
