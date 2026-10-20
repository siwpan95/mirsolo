"""33~41 档补甲的三条**红证**——改前（基线数据）必须红，改后必须绿。

为什么单独开一个文件：**红证不是回归测试**。回归测试盯的是"现在别坏"，
红证盯的是"这条改动真的把原来的洞补上了"——它必须能在**改动前的基线数据**
上跑出来是红的，否则这条用例等于没写（本项目踩过：门禁自己带盲区、
断言写得恒真，改不改都绿）。

怎么复现"改前红"（不碰工作树、不做 git 写操作）::

    mkdir %TEMP%\\ms_base
    git show HEAD:data/items.json    > %TEMP%\\ms_base\\items.json
    git show HEAD:data/monsters.json > %TEMP%\\ms_base\\monsters.json
    git show HEAD:data/classes.json  > %TEMP%\\ms_base\\classes.json
    set MIRSOLO_DATA_DIR=%TEMP%\\ms_base
    E:\\Python314\\python.exe tests\\test_armor_band.py     :: 3 failed

    set MIRSOLO_DATA_DIR=
    E:\\Python314\\python.exe tests\\test_armor_band.py     :: 3 passed

三条红证各自盯的洞：

1. **防具空档**（法/道）：改前 L20/L28/L32 的最优可穿防具 AC 恒为 ``[3,5]``，
   15 级到 48 级之间**一件都没有**；改后 L33 起必须抬起来。
2. **承伤落差**（战士 L33 vs 沃玛战士）：改前怪 ``dc [15,26]``、己方 AC
   ``[12,22]``，改后怪 ``dc [12,20]``、己方 AC ``[13,24]``。期望每次挨打伤害
   必须落到门槛以下。数值全部由 :mod:`mirsolo.combat.formulas` 的真实函数算出，
   这里只写死门槛字面量。
3. **AC 阶梯单调**：三职业在换装档位上，装备 AC 上限必须**严格递增**。
   改前 15→48 是一条平的线（战士恒 10、法道恒 5），这是空档的判据。

口径说明（与 ``tmp_q1/BALANCE_REPORT.md`` §2.4 一致）：阶梯表里的 AC 是
**装备 AC**（armor + helmet 逐槽取最优，下限相加、上限相加——
``mirsolo/items/equipment.py:158`` 的 ``_add_range``），不含职业成长；
第 2 条算真实承伤时才叠加 ``grow_attributes`` 的成长基准。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mirsolo.combat.formulas import resolve_physical  # noqa: E402
from mirsolo.determinism import Rng  # noqa: E402
from mirsolo.progression.exp import grow_attributes  # noqa: E402

#: 数据目录。设了这个环境变量就走它——用来把同一套断言打在**基线数据**上。
DATA_DIR = Path(os.environ.get("MIRSOLO_DATA_DIR") or (ROOT / "data"))

CASES = []

#: 换装观察点（覆盖 15→48 这段原本的空档）
LEVELS = [1, 10, 15, 20, 25, 30, 33, 35, 40, 41, 45, 48, 50]
#: 每个职业真正会换装的档位——序列在这些点上必须**严格递增**
TIER_LEVELS = [15, 33, 35, 40, 41, 48]
CLASSES = ("warrior", "mage", "taoist")

#: 红证 2 的门槛：战士 L33 在沃玛战士面前的**期望每次挨打伤害**上限。
#: 改前实测 4.10，改后实测 1.41（N=20000，种子固定，逐位可复现）。
INCOMING_DAMAGE_CEILING = 1.50
#: 蒙特卡洛样本量（种子固定 → 结果确定，不 flaky）
SAMPLES = 20_000
#: 红证 2 的随机流种子
SEED = 20251020


def case(fn):
    CASES.append(fn)
    return fn


def _table(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _best_slot_ac(items: dict, class_id: str, level: int, slot: str):
    """同 slot 内取 ac 最优（先比上限、再比下限）。"""
    best = None
    for item_id, spec in items.items():
        if spec.get("slot") != slot or not spec.get("ac"):
            continue
        if int(spec.get("level_req", 0)) > level:
            continue
        if class_id not in (spec.get("class") or []):
            continue
        ac = tuple(spec["ac"])
        if best is None or (ac[1], ac[0]) > (best[1][1], best[1][0]):
            best = (item_id, ac)
    return best


def equip_ac(items: dict, class_id: str, level: int) -> tuple[int, int]:
    """**装备 AC**：armor + helmet 逐槽取最优，下限相加、上限相加。"""
    lo = hi = 0
    for slot in ("armor", "helmet"):
        best = _best_slot_ac(items, class_id, level, slot)
        if best:
            lo += best[1][0]
            hi += best[1][1]
    return (lo, hi)


def total_ac(items: dict, classes: dict, class_id: str, level: int) -> tuple[int, int]:
    """**真实承伤口径**：职业成长基准 + 装备 AC。"""
    grown = grow_attributes(classes[class_id], level)["ac"]
    lo, hi = equip_ac(items, class_id, level)
    return (grown[0] + lo, grown[1] + hi)


# ── 红证 1：法/道的防具空档 ────────────────────────────────


@case
def test_mage_taoist_armor_gap_is_filled_at_l33():
    """法师/道士 L20/L28/L32 最优防具 AC 恒定，且 **L33 必须抬起来**。

    改前（基线）：布衣 ac[2,3] + 皮头盔 ac[1,2] 是 1~47 级唯一的选择，
    L20/L28/L32/L33 全是 ``[3,5]``——``L33 > L32`` 恒假，这条必红。
    改后：L33 换上本职业甲（法师长袍 ac[3,6] / 道袍 ac[4,7]），上限抬高。
    """
    items = _table("items")["items"]
    for class_id in ("mage", "taoist"):
        flat = [equip_ac(items, class_id, lv) for lv in (20, 28, 32)]
        assert flat[0] == flat[1] == flat[2], (class_id, flat)
        at_32 = equip_ac(items, class_id, 32)
        at_33 = equip_ac(items, class_id, 33)
        assert at_33[1] > at_32[1], (
            f"{class_id} L33 的装备 AC 上限没抬起来（空档没补）："
            f"L32={list(at_32)} L33={list(at_33)}"
        )


# ── 红证 2：战士 L33 的承伤落差 ────────────────────────────


@case
def test_warrior_l33_incoming_damage_is_below_the_ceiling():
    """战士 L33 站在沃玛战士面前，期望每次挨打伤害必须 <= 门槛。

    前后两端**同时**在变，缺一半这条就红：

    * 怪侧 P1：``woma_zhanshi`` dc ``[15,26]`` → ``[12,20]``
    * 己侧补甲：L33 穿上铁盔甲 ac[5,8]（改前只能穿中型盔甲 ac[4,6]）

    数值走 :func:`mirsolo.combat.formulas.resolve_physical` 真算，
    成长基准走 :func:`mirsolo.progression.exp.grow_attributes`——不在这里
    另写一份公式。改前实测 4.10（红），改后实测 1.41（绿）。
    """
    items = _table("items")["items"]
    classes = _table("classes")["classes"]
    monster = _table("monsters")["monsters"]["woma_zhanshi"]

    grown = grow_attributes(classes["warrior"], 33)
    ac = total_ac(items, classes, "warrior", 33)
    rng = Rng(SEED, "redproof-l33")
    hits = damage = 0
    for _ in range(SAMPLES):
        outcome = resolve_physical(
            dc=tuple(monster["dc"]),
            accuracy=int(monster["accuracy"]),
            ac=ac,
            agility=grown["agility"],
            rng=rng,
        )
        if outcome.hit:
            hits += 1
            damage += outcome.damage
    per_swing = damage / SAMPLES
    assert per_swing <= INCOMING_DAMAGE_CEILING, (
        f"战士 L33 在沃玛战士（dc={monster['dc']}）面前期望每次挨打 "
        f"{per_swing:.4f}，超过门槛 {INCOMING_DAMAGE_CEILING}；"
        f"己方总 AC={list(ac)}（装备 {list(equip_ac(items, 'warrior', 33))}）"
    )


# ── 红证 3：AC 阶梯单调 ────────────────────────────────────


@case
def test_ac_ladder_never_flat_across_the_15_48_gap():
    """三职业的装备 AC 上限序列：全程单调不降，且在换装档位上**严格递增**。

    只断言"单调不降"是**不够**的：改前战士的序列是
    ``3,7,10,10,10,10,10,10,10,10,10,17,21``——它本来就单调不降，
    但 15→48 是一条平的线。所以这里额外要求档位点严格递增，
    ``[15,33,35,40,41,48]`` 改前是 ``[10,10,10,10,10,17]``（红）。
    """
    items = _table("items")["items"]
    for class_id in CLASSES:
        seq = [equip_ac(items, class_id, lv)[1] for lv in LEVELS]
        bad = [(LEVELS[i], seq[i], LEVELS[i + 1], seq[i + 1])
               for i in range(len(seq) - 1) if seq[i] > seq[i + 1]]
        assert not bad, (class_id, "AC 阶梯倒退", bad)

        tiers = [equip_ac(items, class_id, lv)[1] for lv in TIER_LEVELS]
        flat = [(TIER_LEVELS[i], tiers[i], TIER_LEVELS[i + 1], tiers[i + 1])
                for i in range(len(tiers) - 1) if tiers[i] >= tiers[i + 1]]
        assert not flat, (
            f"{class_id} 的 AC 阶梯在这些档位上没有抬升（15→48 空档未补）："
            f"{list(zip(TIER_LEVELS, tiers))}；卡住的点 {flat}"
        )


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {fn.__name__}")
    return passed, failed


if __name__ == "__main__":
    print(f"[test_armor_band] 数据目录 = {DATA_DIR}")
    p, f = run()
    print(f"  -> {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
