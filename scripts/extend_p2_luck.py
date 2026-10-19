"""P2-S3 幸运 9 —— 给终局武器/项链写 ``luck``，并把项链挂进掉落表。

为什么是"改数据"而不是"新增一整套装备"：传奇的幸运本来就不来自某件特定装备，
而是**武器被喝祝福油**累积出来的。单机里没有交易与物品实例状态（见
``docs/数据口径.md`` 的"祝福油为什么不入表"），于是把"已经喝满的幸运"
直接写进终局武器的定义里——效果是等价的，且不需要额外的消耗品系统。

幂等：重复跑只会覆盖同一批字段；掉落组按"条目是否已存在"判定，不会重复追加。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

ALL_CLASSES = ["warrior", "mage", "taoist"]

#: 终局武器的"已注幸运"值：7 点，对应原版喝满祝福油的武器
WEAPON_LUCK = {
    "chiyue_mojian": 7,     # 赤月魔剑（战）lv48
    "chiyue_mozhang": 7,    # 赤月魔杖（法）lv48
    "chiyue_daojian": 7,    # 赤月道剑（道）lv48
    "niumo_zhanfu": 7,      # 牛魔战斧（战）lv50
}

#: 项链：此前 items.json 里完全没有这个槽的东西，这里补齐两条幸运项链
NECKLACES = {
    "zhufu_xianglian": {
        "name": "祝福项链", "slot": "necklace", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [0, 1], "mac": [1, 2],
        "accuracy": 0, "weight": 2, "durability": 15, "level_req": 33,
        "price": 26000, "tier": "rare", "kind": "equip", "luck": 1,
    },
    "xingyun_xianglian": {
        "name": "幸运项链", "slot": "necklace", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [1, 2], "mac": [2, 3],
        "accuracy": 0, "weight": 2, "durability": 18, "level_req": 44,
        "price": 88000, "tier": "rare", "kind": "equip", "luck": 2,
    },
}

#: 掉落挂载点（新开一个组，不动既有组——组之间用独立随机流，改不到老掉落）
DROP_GROUPS = {
    "woma_weishi": {"permille": 600, "entries": [
        {"item": "zhufu_xianglian", "permille": 10000, "count": [1, 1]}]},
    "zumma_weishi": {"permille": 400, "entries": [
        {"item": "zhufu_xianglian", "permille": 6000, "count": [1, 1]},
        {"item": "xingyun_xianglian", "permille": 4000, "count": [1, 1]}]},
    "chiyue_emo": {"permille": 500, "entries": [
        {"item": "xingyun_xianglian", "permille": 10000, "count": [1, 1]}]},
    "niumo_wang": {"permille": 900, "entries": [
        {"item": "xingyun_xianglian", "permille": 10000, "count": [1, 1]}]},
}


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _save(name: str, payload: dict) -> None:
    """写回并强制 CRLF —— 仓库里的 data/*.json 一直是 CRLF，保持一致免得整表 diff。"""
    path = DATA_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def main() -> int:
    items = _load("items")
    drops = _load("drops")

    table = items["items"]
    added = []
    for item_id, spec in NECKLACES.items():
        if item_id not in table:
            table[item_id] = spec
            added.append(item_id)

    changed = []
    for item_id, luck in WEAPON_LUCK.items():
        spec = table.get(item_id)
        if spec is None:
            print(f"[SKIP] 武器 {item_id} 不在 items.json")
            continue
        if spec.get("luck") != luck:
            spec["luck"] = luck
            changed.append(item_id)

    dropped = []
    for monster_id, group in DROP_GROUPS.items():
        table_def = drops["drops"].get(monster_id)
        if table_def is None:
            print(f"[SKIP] 掉落表 {monster_id} 不存在")
            continue
        groups = table_def.setdefault("groups", [])
        wanted = {entry["item"] for entry in group["entries"]}
        if any(wanted & {entry.get("item") for entry in existing.get("entries", [])}
               for existing in groups):
            continue  # 已经挂过，幂等跳过
        groups.append(json.loads(json.dumps(group)))  # 深拷贝，防多次写共享同一 dict
        dropped.append(monster_id)

    _save("items", items)
    _save("drops", drops)
    print(f"项链 +{len(added)}：{added or '（已存在，跳过）'}")
    print(f"武器写入 luck：{changed or '（已是目标值，跳过）'}")
    print(f"掉落挂载：{dropped or '（已挂过，跳过）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
