"""P2-S4 特戒 —— 给空的 ``ring`` 槽填上六枚戒指，并挂到后期 BOSS 掉落表上。

在此之前 ``SLOTS`` 里一直有 ``ring`` 槽（``panels.SLOT_LABELS`` 也早就显示"戒指"），
但 ``items.json`` 里一件东西都没有——槽是空的，玩家把这个面板区域看懂了也用不上。

单机化取舍（逐枚的记录在 ``docs/数据口径.md``）：

* **一个槽、一枚戒指** —— ``Loadout`` 的 ring 槽没有左右手之分，想换得先摘。
  刻意不开放"双戒指"，否则护身 + 复活叠起来会让"这刀死没死"失去讨论意义。
* **传送戒指的目标是写死的数据** —— 网游里它能指定坐标，那需要一整套 UI 与
  服务端校验；单机取固定回城点（比奇），代价是少一些自由度，收益是能被
  ``ring-effects`` 门禁静态校验（地图存在、冷却为正）。
* **复活戒指的额度按 item_id 记账** —— 本项目没有"物品实例"，两枚同款共用额度。

幂等：戒指按 id 覆盖写回，掉落组按"条目是否已存在"判定，重复跑不会叠加。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

ALL_CLASSES = ["warrior", "mage", "taoist"]

#: 六枚戒指。``ring_param`` 的键必须与 ``loader.RING_PARAM_KEYS`` 一一对应，
#: 少一个键 loader.validate 就会报"表写了一半"。
RINGS = {
    "yinshen_jiezhi": {
        "name": "隐身戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [0, 1], "mac": [0, 1],
        "accuracy": 0, "weight": 1, "durability": 12, "level_req": 38,
        "price": 150000, "tier": "rare", "kind": "equip",
        "ring_effect": "invis",
        "ring_param": {"ticks": 40, "recover_ticks": 30},
    },
    "mabi_jiezhi": {
        "name": "麻痹戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [1, 1], "mac": [1, 1],
        "accuracy": 0, "weight": 2, "durability": 14, "level_req": 40,
        "price": 220000, "tier": "rare", "kind": "equip",
        "ring_effect": "paralysis",
        "ring_param": {"bp": 300, "ticks": 20},
    },
    "chuansong_jiezhi": {
        "name": "传送戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [0, 2], "mac": [0, 2],
        "accuracy": 0, "weight": 2, "durability": 16, "level_req": 42,
        "price": 200000, "tier": "rare", "kind": "equip",
        "ring_effect": "teleport",
        "ring_param": {"map": "biqi_field", "cooldown_ticks": 300},
    },
    "hushen_jiezhi": {
        "name": "护身戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [0, 1], "mac": [1, 1],
        "accuracy": 0, "weight": 2, "durability": 18, "level_req": 44,
        "price": 260000, "tier": "rare", "kind": "equip",
        "ring_effect": "protect",
        "ring_param": {"permille": 1000},
    },
    "huoyan_jiezhi": {
        "name": "火焰戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": [1, 3], "mc": None, "sc": None, "ac": None, "mac": [0, 1],
        "accuracy": 0, "weight": 3, "durability": 20, "level_req": 46,
        "price": 240000, "tier": "rare", "kind": "equip",
        "ring_effect": "flame",
        "ring_param": {"power_permille": 300},
    },
    "fuhuo_jiezhi": {
        "name": "复活戒指", "slot": "ring", "class": list(ALL_CLASSES),
        "atk": None, "mc": None, "sc": None, "ac": [1, 2], "mac": [1, 2],
        "accuracy": 0, "weight": 3, "durability": 22, "level_req": 48,
        "price": 300000, "tier": "rare", "kind": "equip",
        "ring_effect": "revive",
        "ring_param": {"charges": 3, "hp_permille": 500},
    },
}

#: 掉落挂载点：每枚戒指一个来源，全部挂在 BOSS 级怪上（原版也是教主/魔王掉特戒）
#: permille 是"这一组整体是否掉落"的千分比，单个条目用 10000 = 掉就掉它。
DROP_GROUPS = {
    "woma_jiaozhu": {"permille": 300, "entries": [
        {"item": "yinshen_jiezhi", "permille": 10000, "count": [1, 1]}]},
    "zumma_jiaozhu": {"permille": 250, "entries": [
        {"item": "mabi_jiezhi", "permille": 10000, "count": [1, 1]}]},
    "niumo_fashi": {"permille": 250, "entries": [
        {"item": "chuansong_jiezhi", "permille": 10000, "count": [1, 1]}]},
    "chiyue_emo": {"permille": 220, "entries": [
        {"item": "hushen_jiezhi", "permille": 10000, "count": [1, 1]}]},
    "chiyue_mozhu": {"permille": 220, "entries": [
        {"item": "huoyan_jiezhi", "permille": 10000, "count": [1, 1]}]},
    "niumo_wang": {"permille": 200, "entries": [
        {"item": "fuhuo_jiezhi", "permille": 10000, "count": [1, 1]}]},
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
    added, updated = [], []
    for item_id, spec in RINGS.items():
        if item_id in table:
            table[item_id].update(spec)
            updated.append(item_id)
        else:
            table[item_id] = spec
            added.append(item_id)

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
    print(f"新增戒指：{added or '（无）'}")
    print(f"覆盖写回：{updated or '（无）'}")
    print(f"掉落挂载：{dropped or '（已挂过，跳过）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
