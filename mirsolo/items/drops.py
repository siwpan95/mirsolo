"""掉落系统 —— 组权重 + 条目权重 + 位置无关确定性。

原版掉落表是"某某怪爆某某，爆率若干"，实现上是**分组**：
先 roll 组是否掉落，再 roll 组内具体条目。本模块照此建模：

1. 每组用 ``permille``（万分比）独立判定是否掉落 —— 组与组之间不互斥，
   所以一只 BOSS 可以同时掉装备和技能书；
2. 组内条目按 ``permille`` 权重归一 roll；
3. 金币单独 roll ``gold`` 区间。

**防 save-scum**：结果由 ``(world_seed, monster_id, kill_index)`` 决定，
而 ``kill_index`` 来自世界状态（第几次击杀这只怪）并存档。读档重打同一只怪
不会改变掉落——这不是防作弊，是让"爆装"这件事在单机里仍然值得期待。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mirsolo.determinism import Rng, splitmix64


@dataclass
class DropResult:
    """一次击杀的掉落产物。"""

    gold: int = 0
    items: list[tuple[str, int]] = field(default_factory=list)
    rolls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"gold": self.gold, "items": list(self.items), "rolls": dict(self.rolls)}


def _pick_entry(entries: list[dict], rng: Rng) -> dict:
    """按 permille 权重在组内挑一条（权重和为 0 时取第一条，防御坏数据）。"""
    total = sum(int(e.get("permille", 0)) for e in entries)
    if total <= 0:
        return entries[0]
    point = rng.rand_int(1, total)
    acc = 0
    for entry in entries:
        acc += int(entry.get("permille", 0))
        if point <= acc:
            return entry
    return entries[-1]


def roll_drops(
    drop_table: dict,
    *,
    seed: int,
    monster_id: str,
    kill_index: int,
) -> DropResult:
    """结算一只怪的掉落。

    参数：
        drop_table: ``drops.json`` 里该怪的表（``{"gold": [...], "groups": [...]}``）
        seed: 世界种子
        monster_id: 怪物 id（进流名，不同怪互不干扰）
        kill_index: 该怪的第几次击杀（**必须来自世界状态**，见模块 docstring）
    """
    stream = f"drop:{monster_id}:{kill_index}"
    result = DropResult()

    gold_lo, gold_hi = drop_table.get("gold", (0, 0))
    gold_rng = Rng(seed ^ splitmix64(1), stream + ":gold")
    result.gold = gold_rng.rand_int(int(gold_lo), int(gold_hi))

    for index, group in enumerate(drop_table.get("groups", [])):
        group_rng = Rng(seed ^ splitmix64(index + 2), stream + f":group{index}")
        chance = int(group.get("permille", 0))
        judged = group_rng.rand_int(1, 10000)
        result.rolls[f"group{index}"] = judged
        if judged > chance:
            continue
        entries = group.get("entries", [])
        if not entries:
            continue
        entry = _pick_entry(entries, group_rng)
        count_lo, count_hi = entry.get("count", (1, 1))
        count = group_rng.rand_int(int(count_lo), int(count_hi))
        item_id = str(entry["item"])
        result.items.append((item_id, count))
        result.rolls[f"group{index}_item"] = entries.index(entry)

    return result
