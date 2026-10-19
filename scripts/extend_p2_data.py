"""P2-S1 数据扩展 —— 沙巴克城地图 + 赤月/牛魔等级带（幂等）。

为什么要一个脚本而不是手改 JSON：这几张表是**手录**的，手改容易漏字段
（``safe_zone`` / ``seed_role`` 这类不影响运行但影响行为的开关）。
脚本可重复执行，第二次跑是空操作，不会把已有值覆盖掉。

同时把地图带从 47-50 延到 47-52：原版赤月峡谷/牛魔洞穴在祖玛之后，
沙巴克攻城是 52 级的终局玩法——等级带必须连续，否则
``gate_map-bands`` 立刻报"练级断层"。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAPS = ROOT / "data" / "maps.json"

#: 攻城图本身的等级带与尺寸（与 sieges.json 的守卫坐标必须放得下）
SIEGE_MAP = {
    "name": "沙巴克城",
    "kind": "town",
    "level_range": [50, 52],
    "size": [64, 64],
    "seed_role": "siege",
    "safe_zone": False,
    # 城里不刷常规怪：守军全部由战役系统按波次投放，补刷会和波次打架
    "spawns": [],
    "portals": [
        {"to": "zuma_temple", "at": [4, 4], "name": "撤军"},
    ],
}

#: 新增/调整的等级带（含把祖玛上沿打开）
BANDS = {
    "biqi_field": [1, 10],
    "biqi_mine_1": [11, 20],
    "wugong_cave_1": [20, 26],
    "orc_tomb_1": [26, 33],
    "woma_temple": [33, 40],
    "stone_tomb": [40, 44],
    "fengmo_valley": [44, 47],
    "zuma_temple": [47, 50],
    "chiyue_valley": [50, 51],
    "niumo_cave": [51, 52],
}

#: 赤月峡谷：祖玛之后的小台阶，怪用现有表里最高两档（不改怪表数值，先跑通形态）
CHIYUE = {
    "name": "赤月峡谷",
    "kind": "dungeon",
    "level_range": [50, 51],
    "size": [64, 64],
    "seed_role": "dungeon",
    "safe_zone": False,
    "spawns": [
        {"monster": "zumma_weishi", "count": 10, "respawn_seconds": 80},
        {"monster": "emo_gongjianshou", "count": 6, "respawn_seconds": 70},
    ],
    "portals": [
        {"to": "zuma_temple", "at": [4, 4], "name": "出口"},
        {"to": "niumo_cave", "at": [60, 60], "name": "牛魔洞穴"},
    ],
}

#: 牛魔洞穴：沙巴克前最后一站
NIUMO = {
    "name": "牛魔洞穴",
    "kind": "dungeon",
    "level_range": [51, 52],
    "size": [64, 64],
    "seed_role": "dungeon",
    "safe_zone": False,
    "spawns": [
        {"monster": "zumma_weishi", "count": 8, "respawn_seconds": 80},
        {"monster": "heiming_zhanshi", "count": 8, "respawn_seconds": 70},
        {"monster": "zumma_jiaozhu", "count": 1, "respawn_seconds": 900},
    ],
    "portals": [
        {"to": "chiyue_valley", "at": [4, 4], "name": "出口"},
        {"to": "sabak", "at": [60, 60], "name": "沙巴克城门"},
    ],
}


def main() -> int:
    data = json.loads(MAPS.read_text(encoding="utf-8"))
    maps = data["maps"]
    changed = []

    # 1) 新图（已存在则原样保留——脚本不覆盖手工调过的坐标）
    for map_id, spec in (("chiyue_valley", CHIYUE), ("niumo_cave", NIUMO),
                         ("sabak", SIEGE_MAP)):
        if map_id not in maps:
            maps[map_id] = spec
            changed.append(f"+{map_id}")

    # 2) 等级带：只改 exp 表范围内的既有关键图（新图已在上面写好）
    for map_id, band in BANDS.items():
        spec = maps.get(map_id)
        if spec is None:
            continue
        if spec.get("level_range") != band:
            spec["level_range"] = band
            changed.append(f"~{map_id} {band}")

    data["meta"]["note"] = (
        "P2 地图带：比奇省外围 1-10 → 矿洞 11-20 → 蜈蚣洞 20-26 → 半兽古墓 26-33 "
        "→ 沃玛寺庙 33-40 → 石墓阵 40-44 → 封魔谷 44-47 → 祖玛寺庙 47-50 "
        "→ 赤月峡谷 50-51 → 牛魔洞穴 51-52；沙巴克城是攻城战役图（守军由 sieges.json 投放，常规刷新为空）。"
        "respawn_seconds 是该刷点自己的补刷间隔（教主 10-15 分钟）。"
    )

    MAPS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print("maps.json 变更:", ", ".join(changed) if changed else "无（已是最新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
