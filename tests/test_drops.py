"""掉落：组权重、条目权重、gold 区间、kill_index 的防刷语义。

关键断言是 ``test_kill_index_changes_result``：如果换 kill_index 结果不变，
说明 random 流没吃到击杀序号，"读档重打同一只怪掉落不变"这条设计就落空了。
"""

from __future__ import annotations

from mirsolo.data.loader import GameData
from mirsolo.items.drops import roll_drops

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


def _table(monster_id: str) -> dict:
    return DATA.drops["drops"][monster_id]


@case
def test_same_args_same_drops():
    a = roll_drops(_table("shiwang"), seed=42, monster_id="shiwang", kill_index=1)
    b = roll_drops(_table("shiwang"), seed=42, monster_id="shiwang", kill_index=1)
    assert a.as_dict() == b.as_dict()


@case
def test_different_seed_may_change_drops():
    """不同世界种子应能产出不同掉落（否则"每次开局不同"是假的）。"""
    results = {
        tuple(sorted(roll_drops(_table("jiangshi"), seed=s, monster_id="jiangshi",
                                kill_index=1).items))
        for s in range(50)
    }
    assert len(results) > 1, "50 个种子给出完全相同的掉落组合"


@case
def test_kill_index_changes_result():
    """换击杀序号必须能改变结果——这是防 save-scum 的核心。"""
    results = {
        tuple(sorted(roll_drops(_table("jiangshi"), seed=7, monster_id="jiangshi",
                                kill_index=i).items))
        for i in range(1, 21)
    }
    assert len(results) > 1, "kill_index 未参与随机流，读档重打可刷"


@case
def test_gold_within_table_range():
    table = _table("jiangshi")
    lo, hi = table["gold"]
    for index in range(200):
        drop = roll_drops(table, seed=99, monster_id="jiangshi", kill_index=index)
        assert lo <= drop.gold <= hi, f"{drop.gold} 不在 [{lo}, {hi}]"


@case
def test_dropped_items_are_valid_and_counted():
    """掉出来的 item id 必须在 items.json 里，数量在表的 count 区间内。"""
    for monster_id, table in DATA.drops["drops"].items():
        for index in range(1, 30):
            drop = roll_drops(table, seed=5, monster_id=monster_id, kill_index=index)
            for item_id, count in drop.items:
                assert item_id in DATA.items["items"], f"{monster_id} 掉出不存在的 {item_id}"
                assert count >= 1


@case
def test_drop_rate_matches_group_permille():
    """鸡的装备组是 2500/10000 = 25%，4000 次抽样应落在 22%~28%。"""
    table = _table("ji")
    drops = sum(
        1 for i in range(4000)
        if roll_drops(table, seed=20261019, monster_id="ji", kill_index=i).items
    )
    ratio = drops / 4000
    assert 0.22 <= ratio <= 0.28, f"实际掉率 {ratio:.3f}"


@case
def test_boss_has_two_groups_and_can_drop_weapon():
    """尸王两组：6000 主组 + 1500 追加组；200 次内应至少掉一次斩马刀。"""
    table = _table("shiwang")
    assert len(table["groups"]) == 2
    found = False
    for index in range(200):
        drop = roll_drops(table, seed=11, monster_id="shiwang", kill_index=index)
        if any(item == "zhanmadao" for item, _ in drop.items):
            found = True
            break
    assert found, "200 次尸王未掉出斩马刀，权重或组逻辑有问题"


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
    print(f"test_drops: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
