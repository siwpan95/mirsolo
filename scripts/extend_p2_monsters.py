"""P2-S2：赤月峡谷 / 牛魔洞穴的怪种、终局装备与掉落。

幂等：已经存在就跳过（重跑不会覆盖手调过的数值）。

数值口径沿现有曲线外推（祖玛卫士 47 → 祖玛教主 50 之后继续往上），
并在 ``docs/数据口径.md`` 记明是**近似模型**而非原版实测值。

用法::

    .venv\\Scripts\\python.exe scripts\\extend_p2_monsters.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

#: 赤月（50-51）与牛魔（51-52）的怪种。
#: 设计意图写在名字旁边——单机没有"抢怪"，所以差异要体现在**打法**上：
#: 魔蛛快而脆（要抢先手）、僵尸厚而慢（要打续航）、恶魔是精英（要留技能）。
MONSTERS = {
    "chiyue_mozhu": {
        "name": "赤月魔蛛", "level": 50, "hp": 760, "exp": 1900,
        "dc": [28, 46], "ac": [11, 19], "mac": [5, 9],
        "accuracy": 23, "agility": 14, "attack_speed": 5,
        "ai": "aggressive", "move_speed": 1.2, "size": 1,
    },
    "xuejiangshi": {
        "name": "血僵尸", "level": 50, "hp": 980, "exp": 2100,
        "dc": [30, 48], "ac": [12, 20], "mac": [4, 8],
        "accuracy": 22, "agility": 9, "attack_speed": 4,
        "ai": "aggressive", "move_speed": 0.9, "size": 1,
    },
    "chiyue_emo": {
        "name": "赤月恶魔", "level": 51, "hp": 1350, "exp": 2600,
        "dc": [33, 52], "ac": [13, 22], "mac": [6, 10],
        "accuracy": 25, "agility": 12, "attack_speed": 5,
        "ai": "elite", "move_speed": 1.0, "size": 1,
    },
    "niumo_zhanshi": {
        "name": "牛魔战士", "level": 51, "hp": 1150, "exp": 2800,
        "dc": [36, 56], "ac": [14, 23], "mac": [5, 9],
        "accuracy": 26, "agility": 11, "attack_speed": 5,
        "ai": "aggressive", "move_speed": 1.0, "size": 1,
    },
    "niumo_fashi": {
        "name": "牛魔法师", "level": 52, "hp": 1020, "exp": 3000,
        "dc": [38, 60], "ac": [12, 20], "mac": [8, 14],
        "accuracy": 27, "agility": 12, "attack_speed": 5,
        "ai": "aggressive", "move_speed": 1.0, "size": 1,
    },
    "niumo_wang": {
        "name": "牛魔王", "level": 52, "hp": 3400, "exp": 5600,
        "dc": [42, 68], "ac": [16, 28], "mac": [8, 15],
        "accuracy": 30, "agility": 12, "attack_speed": 5,
        "ai": "elite", "move_speed": 0.8, "size": 2,
    },
}

#: 终局装备（level_req 48-52）。三职业各一件武器，衣服/头盔全职业通用。
ITEMS = {
    "chiyue_mojian": {
        "name": "赤月魔剑", "slot": "weapon", "class": ["warrior"],
        "atk": [22, 36], "mc": None, "sc": None, "ac": None, "mac": None,
        "accuracy": 0, "weight": 26, "durability": 26, "level_req": 48,
        "price": 26000, "tier": "rare", "kind": "equip",
    },
    "chiyue_mozhang": {
        "name": "赤月魔杖", "slot": "weapon", "class": ["mage"],
        "atk": None, "mc": [12, 20], "sc": None, "ac": None, "mac": None,
        "accuracy": 0, "weight": 12, "durability": 20, "level_req": 48,
        "price": 26000, "tier": "rare", "kind": "equip",
    },
    "chiyue_daojian": {
        "name": "赤月道剑", "slot": "weapon", "class": ["taoist"],
        "atk": None, "mc": None, "sc": [12, 20], "ac": None, "mac": None,
        "accuracy": 0, "weight": 14, "durability": 22, "level_req": 48,
        "price": 26000, "tier": "rare", "kind": "equip",
    },
    "chiyue_zhanjia": {
        "name": "赤月战甲", "slot": "armor", "class": ["warrior", "mage", "taoist"],
        "atk": None, "mc": None, "sc": None, "ac": [9, 13], "mac": [2, 4],
        "accuracy": 0, "weight": 24, "durability": 28, "level_req": 48,
        "price": 30000, "tier": "rare", "kind": "equip",
    },
    "niumo_zhanfu": {
        "name": "牛魔战斧", "slot": "weapon", "class": ["warrior"],
        "atk": [26, 42], "mc": None, "sc": None, "ac": None, "mac": None,
        "accuracy": 0, "weight": 30, "durability": 30, "level_req": 50,
        "price": 42000, "tier": "rare", "kind": "equip",
    },
    "niumo_toukui": {
        "name": "牛魔头盔", "slot": "helmet", "class": ["warrior", "mage", "taoist"],
        "atk": None, "mc": None, "sc": None, "ac": [5, 8], "mac": [2, 4],
        "accuracy": 0, "weight": 12, "durability": 20, "level_req": 50,
        "price": 20000, "tier": "rare", "kind": "equip",
    },
}

#: 掉落：金币区间 + 组权重。稀有装备走得慢（permille 小），技能书兜底。
DROPS = {
    "chiyue_mozhu": {
        "gold": [280, 520],
        "groups": [
            {"permille": 6000, "entries": [
                {"item": "chiyue_mojian", "permille": 4000, "count": [1, 1]},
                {"item": "chiyue_mozhang", "permille": 3000, "count": [1, 1]},
                {"item": "chiyue_daojian", "permille": 3000, "count": [1, 1]},
            ]},
            {"permille": 2500, "entries": [
                {"item": "chiyue_zhanjia", "permille": 10000, "count": [1, 1]},
            ]},
        ],
    },
    "xuejiangshi": {
        "gold": [300, 560],
        "groups": [
            {"permille": 5500, "entries": [
                {"item": "chiyue_mojian", "permille": 3500, "count": [1, 1]},
                {"item": "chiyue_zhanjia", "permille": 3500, "count": [1, 1]},
                {"item": "niumo_toukui", "permille": 3000, "count": [1, 1]},
            ]},
        ],
    },
    "chiyue_emo": {
        "gold": [420, 780],
        "groups": [
            {"permille": 7000, "entries": [
                {"item": "chiyue_mojian", "permille": 3000, "count": [1, 1]},
                {"item": "chiyue_mozhang", "permille": 3000, "count": [1, 1]},
                {"item": "chiyue_daojian", "permille": 2500, "count": [1, 1]},
                {"item": "chiyue_zhanjia", "permille": 1500, "count": [1, 1]},
            ]},
            {"permille": 2000, "entries": [
                {"item": "book_liehuo", "permille": 5000, "count": [1, 1]},
                {"item": "book_bingpaoxiao", "permille": 3000, "count": [1, 1]},
                {"item": "book_zhaohuanshen", "permille": 2000, "count": [1, 1]},
            ]},
        ],
    },
    "niumo_zhanshi": {
        "gold": [480, 860],
        "groups": [
            {"permille": 6500, "entries": [
                {"item": "niumo_zhanfu", "permille": 3500, "count": [1, 1]},
                {"item": "niumo_toukui", "permille": 3500, "count": [1, 1]},
                {"item": "chiyue_zhanjia", "permille": 3000, "count": [1, 1]},
            ]},
        ],
    },
    "niumo_fashi": {
        "gold": [520, 900],
        "groups": [
            {"permille": 6500, "entries": [
                {"item": "niumo_zhanfu", "permille": 3000, "count": [1, 1]},
                {"item": "niumo_toukui", "permille": 3000, "count": [1, 1]},
                {"item": "chiyue_mozhang", "permille": 2500, "count": [1, 1]},
                {"item": "chiyue_daojian", "permille": 1500, "count": [1, 1]},
            ]},
            {"permille": 1800, "entries": [
                {"item": "book_mofadun", "permille": 5000, "count": [1, 1]},
                {"item": "book_huoqiang", "permille": 3000, "count": [1, 1]},
                {"item": "book_jiti_zhiyu", "permille": 2000, "count": [1, 1]},
            ]},
        ],
    },
    "niumo_wang": {
        "gold": [900, 1600],
        "groups": [
            {"permille": 9000, "entries": [
                {"item": "niumo_zhanfu", "permille": 3000, "count": [1, 1]},
                {"item": "chiyue_mojian", "permille": 2000, "count": [1, 1]},
                {"item": "chiyue_mozhang", "permille": 2000, "count": [1, 1]},
                {"item": "chiyue_daojian", "permille": 1500, "count": [1, 1]},
                {"item": "chiyue_zhanjia", "permille": 1000, "count": [1, 1]},
                {"item": "niumo_toukui", "permille": 500, "count": [1, 1]},
            ]},
            {"permille": 3000, "entries": [
                {"item": "book_liehuo", "permille": 3000, "count": [1, 1]},
                {"item": "book_bingpaoxiao", "permille": 2500, "count": [1, 1]},
                {"item": "book_mofadun", "permille": 2500, "count": [1, 1]},
                {"item": "book_zhaohuanshen", "permille": 2000, "count": [1, 1]},
            ]},
        ],
    },
}

#: 刷新：赤月/牛魔换成自家怪（原先是复用祖玛怪，等于"换皮图"）
SPAWN_OVERRIDES = {
    # 赤月恶魔是 elite——精英在地图上只刷 1 只（test_data 钉住），
    # 想多来几只靠"刷新间隔"调，不要靠数量堆。
    "chiyue_valley": [
        {"monster": "chiyue_mozhu", "count": 10, "respawn_seconds": 70},
        {"monster": "xuejiangshi", "count": 8, "respawn_seconds": 80},
        {"monster": "chiyue_emo", "count": 1, "respawn_seconds": 300},
    ],
    "niumo_cave": [
        {"monster": "niumo_zhanshi", "count": 10, "respawn_seconds": 75},
        {"monster": "niumo_fashi", "count": 8, "respawn_seconds": 80},
        {"monster": "niumo_wang", "count": 1, "respawn_seconds": 900},
    ],
}


def _load(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def _save(name: str, payload: dict) -> None:
    path = DATA / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def main() -> int:
    monsters = _load("monsters")
    items = _load("items")
    drops = _load("drops")
    maps = _load("maps")

    added_m = [k for k in MONSTERS if k not in monsters["monsters"]]
    monsters["monsters"].update({k: v for k, v in MONSTERS.items()
                                 if k not in monsters["monsters"]})
    added_i = [k for k in ITEMS if k not in items["items"]]
    items["items"].update({k: v for k, v in ITEMS.items() if k not in items["items"]})
    added_d = [k for k in DROPS if k not in drops["drops"]]
    drops["drops"].update({k: v for k, v in DROPS.items() if k not in drops["drops"]})

    changed_maps = []
    for map_id, spawns in SPAWN_OVERRIDES.items():
        spec = maps["maps"].get(map_id)
        if spec is None:
            print(f"[SKIP] 地图 {map_id} 不存在")
            continue
        if spec.get("spawns") != spawns:
            spec["spawns"] = spawns
            changed_maps.append(map_id)

    for name, payload in (("monsters", monsters), ("items", items),
                          ("drops", drops), ("maps", maps)):
        _save(name, payload)

    print(f"怪 +{len(added_m)}：{added_m or '（已存在，跳过）'}")
    print(f"装备 +{len(added_i)}：{added_i or '（已存在，跳过）'}")
    print(f"掉落 +{len(added_d)}：{len(added_d)} 条")
    print(f"刷新表改写：{changed_maps or '（已是目标配置，跳过）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
