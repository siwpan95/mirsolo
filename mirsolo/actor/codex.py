"""怪物图鉴（P2-S6）—— 击杀记账 + 首次解锁奖励 + 面板数据源。

三条设计取舍，都是为了让"图鉴"这件事**只是记录、不变成第二个成长系统**：

1. **只记击杀，不记"见过"**
   见过没杀过的怪，玩家拿不到任何新信息，列在图鉴里只是一行灰字，
   反而让人以为自己漏了什么。所以"解锁"== "这个 key 存在"，
   不另存一份 ``unlocked`` 集合（两份事实描述同一件事，迟早分叉）。

2. **首次解锁给经验，不给属性点**
   属性点会让玩家的战力脱离职业成长表，升级曲线就对不上了；
   经验是一次性的、可预期的（怪物经验的 ``FIRST_KILL_EXP_PERMILLE``），
   它只让你**早一点**升级，不会让你**多一点**属性。

3. **佣兵/召唤物的击杀也算**
   归属解析已经把击杀记到雇主头上（``SummonOwner``），图鉴走同一条路。
   否则"雇了虎卫去开图，图鉴一格没亮"这种事会让雇兵变成负收益。

图鉴**不**按地图分组：23 种怪分摊在几张图里，按地图分会得到一堆
"这一格只有一种怪"的分组，信息密度还不如一张平表。
"""

from __future__ import annotations

from mirsolo.components import Codex
from mirsolo.data.loader import GameData

#: 首次解锁奖励 = 该怪经验的这个千分比（500‰ = 一半）。
#: 取一半而不是全额：全额等于"第一次打这只怪拿双倍经验"，
#: 会让人倾向于把每种怪都刷一遍再回头练级，节奏被图鉴牵着走。
FIRST_KILL_EXP_PERMILLE = 500

EVT_UNLOCK = "codex:unlock"
"""首次解锁事件。载荷：``{entity, species, name, exp, unlocked, total, tick}``。"""


def record_kill(codex: Codex, species: str) -> bool:
    """记一次击杀，返回**是否为首次**（首次 = 这一格刚解锁）。

    返回值的用途只有"要不要发奖励"——所以这里不碰经验、不发事件，
    纯记账。发奖励是系统的活（它才知道 tick 和总线）。
    """
    first = species not in codex.kills
    codex.kills[species] = int(codex.kills.get(species, 0)) + 1
    return first


def first_kill_exp(data: GameData, species: str) -> int:
    """首次解锁奖励的经验（没这条怪数据就 0，不抛异常）。"""
    spec = data.monsters["monsters"].get(species)
    if spec is None:
        return 0
    return int(spec.get("exp", 0)) * FIRST_KILL_EXP_PERMILLE // 1000


def drop_hint(data: GameData, species: str) -> str:
    """掉落来源的一句话摘要（给面板用）：``金币1-3 · 木剑``。

    只列**物品名**、不列概率：概率是 drops.json 的事，玩家在图鉴里要的是
    "这只怪出什么"，不是"出多少‰"。写概率会让同一行塞进两组数字，读不动。
    """
    table = data.drops["drops"].get(species)
    if not table:
        return "无掉落"
    parts = []
    gold = table.get("gold")
    if gold:
        parts.append(f"金币{gold[0]}-{gold[1]}")
    seen: list[str] = []
    for group in table.get("groups", []):
        for entry in group.get("entries", []):
            item_id = entry.get("item")
            if item_id is None or item_id in seen:
                continue
            seen.append(item_id)
            spec = data.items["items"].get(item_id, {})
            parts.append(str(spec.get("name", item_id)))
    return " · ".join(parts) if parts else "无掉落"


def rows(data: GameData, codex: Codex | None) -> list[dict]:
    """图鉴全表：怪物表里的每一种怪一行（没杀过也是一行，``kills=0``）。

    按怪物表里的**等级**升序、同级按 species 升序：图鉴是"查资料"的地方，
    排序必须稳定且与击杀顺序无关，否则每次打开顺序都不一样就查不到东西。
    """
    kills = codex.kills if codex is not None else {}
    out = []
    for species, spec in data.monsters["monsters"].items():
        out.append({
            "species": species,
            "name": str(spec.get("name", species)),
            "level": int(spec.get("level", 1)),
            "hp": int(spec.get("hp", 0)),
            "exp": int(spec.get("exp", 0)),
            "kills": int(kills.get(species, 0)),
            "unlocked": species in kills,
            "drop": drop_hint(data, species),
        })
    out.sort(key=lambda r: (r["level"], r["species"]))
    return out


def summary(data: GameData, codex: Codex | None) -> dict:
    """``{"total": 怪物种类数, "unlocked": 已解锁数, "pct": 千分比进度}``。

    ``total`` 取自怪物表而不是"杀过的种类"——后者会随存档变化，
    让"图鉴 3/3"这种完成度在一开始就显示成 100%。
    """
    total = len(data.monsters["monsters"])
    unlocked = int(codex.unlocked) if codex is not None else 0
    pct = unlocked * 1000 // total if total else 0
    return {"total": total, "unlocked": unlocked, "pct": pct}
