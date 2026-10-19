"""一次性数据扩展脚本：怪物 / 掉落 / 地图（P1 剩余批次）。

可重复执行（幂等：按 id 覆盖，不重复追加）。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def save(name: str, obj: dict) -> None:
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def mob(name, level, hp, exp, dc, ac, mac, acc, agi, spd, ai="aggressive", move=1.0, size=1):
    return {"name": name, "level": level, "hp": hp, "exp": exp, "dc": dc, "ac": ac,
            "mac": mac, "accuracy": acc, "agility": agi, "attack_speed": spd,
            "ai": ai, "move_speed": move, "size": size}


NEW_MONSTERS = {
    "wugong": mob("蜈蚣", 22, 130, 210, [7, 13], [2, 6], [0, 2], 12, 6, 4, move=1.2),
    "qianchong": mob("钳虫", 24, 160, 240, [8, 15], [3, 7], [0, 2], 12, 5, 4),
    "bande": mob("半兽人", 28, 210, 330, [10, 18], [4, 8], [1, 3], 14, 6, 4),
    "banzhanshi": mob("半兽战士", 31, 280, 430, [12, 21], [5, 9], [1, 3], 15, 7, 4),
    "woma_zhanshi": mob("沃玛战士", 35, 340, 620, [15, 26], [6, 11], [2, 4], 17, 8, 4),
    "woma_weishi": mob("沃玛卫士", 38, 460, 820, [18, 30], [7, 13], [2, 5], 18, 9, 4),
    "woma_jiaozhu": mob("沃玛教主", 40, 1600, 2600, [26, 44], [9, 17], [4, 8], 22, 10, 5,
                        ai="elite", move=0.8, size=2),
    "heiming_zhanshi": mob("暗黑战士", 42, 520, 980, [22, 36], [8, 14], [3, 6], 20, 9, 4),
    "emo_gongjianshou": mob("恶魔弓箭手", 45, 560, 1120, [24, 40], [8, 15], [3, 7], 24, 12, 4),
    "zumma_weishi": mob("祖玛卫士", 47, 680, 1500, [26, 44], [10, 18], [4, 8], 22, 10, 5),
    "zumma_jiaozhu": mob("祖玛教主", 50, 2400, 4200, [34, 58], [13, 24], [6, 12], 26, 11, 5,
                         ai="elite", move=0.8, size=2),
}


def drop_table(gold, gear_permille, gear, book_permille=None, book=None):
    groups = [{"permille": gear_permille,
               "entries": [{"item": iid, "permille": 10000, "count": [1, 1]} for iid in gear]}]
    if book:
        groups.append({"permille": book_permille,
                       "entries": [{"item": book, "permille": 10000, "count": [1, 1]}]})
    return {"gold": gold, "groups": groups}


NEW_DROPS = {
    "wugong": drop_table([8, 20], 2200, ["buyi", "pihelmet"]),
    "qianchong": drop_table([10, 24], 2400, ["buyi", "pihelmet"], 300, "book_gongsha"),
    "bande": drop_table([15, 35], 2600, ["wumujian", "qingtongkuijia"], 300, "book_cisha"),
    "banzhanshi": drop_table([20, 45], 2600, ["wumujian", "qingtongkuijia"], 400, "book_huozhu"),
    "woma_zhanshi": drop_table([30, 70], 2800, ["zhanmadao", "zhongxingkuijia"], 400, "book_leidian"),
    "woma_weishi": drop_table([40, 90], 2800, ["zhanmadao", "zhongxingkuijia"], 500, "book_zhiyu"),
    "woma_jiaozhu": drop_table([300, 600], 6000,
                               ["zhanmadao", "zhongxingkuijia", "qingtongtoukui"],
                               1200, "book_liehuo"),
    "heiming_zhanshi": drop_table([50, 110], 3000,
                                  ["zhanmadao", "zhongxingkuijia"], 500, "book_linghunhuofu"),
    "emo_gongjianshou": drop_table([60, 130], 3000,
                                   ["zhanmadao", "zhongxingkuijia"], 500, "book_banyue"),
    "zumma_weishi": drop_table([70, 150], 3200,
                               ["zhanmadao", "zhongxingkuijia"], 600, "book_huoqiang"),
    "zumma_jiaozhu": drop_table([500, 900], 7000,
                                ["zhanmadao", "zhongxingkuijia", "qingtongtoukui"],
                                1500, "book_bingpaoxiao"),
}


def map_spec(name, kind, lo, hi, spawns, portals, safe=False):
    return {"name": name, "kind": kind, "level_range": [lo, hi], "size": [64, 64],
            "seed_role": kind, "safe_zone": safe, "spawns": spawns, "portals": portals}


NEW_MAPS = {
    "wugong_cave_1": map_spec(
        "蜈蚣洞一层", "dungeon", 20, 26,
        [{"monster": "wugong", "count": 14, "respawn_seconds": 40},
         {"monster": "qianchong", "count": 10, "respawn_seconds": 45}],
        [{"to": "biqi_mine_1", "at": [4, 4], "name": "出口"},
         {"to": "orc_tomb_1", "at": [60, 60], "name": "古墓入口"}]),
    "orc_tomb_1": map_spec(
        "半兽人古墓", "dungeon", 26, 33,
        [{"monster": "bande", "count": 12, "respawn_seconds": 50},
         {"monster": "banzhanshi", "count": 8, "respawn_seconds": 60},
         {"monster": "shiwang", "count": 1, "respawn_seconds": 240}],
        [{"to": "wugong_cave_1", "at": [4, 4], "name": "出口"},
         {"to": "woma_temple", "at": [60, 60], "name": "寺庙入口"}]),
    "woma_temple": map_spec(
        "沃玛寺庙", "dungeon", 33, 40,
        [{"monster": "woma_zhanshi", "count": 12, "respawn_seconds": 60},
         {"monster": "woma_weishi", "count": 8, "respawn_seconds": 75},
         {"monster": "woma_jiaozhu", "count": 1, "respawn_seconds": 600}],
        [{"to": "orc_tomb_1", "at": [4, 4], "name": "出口"},
         {"to": "stone_tomb", "at": [60, 60], "name": "石墓入口"}]),
    "stone_tomb": map_spec(
        "石墓阵", "dungeon", 40, 44,
        [{"monster": "heiming_zhanshi", "count": 12, "respawn_seconds": 65},
         {"monster": "woma_weishi", "count": 6, "respawn_seconds": 80}],
        [{"to": "woma_temple", "at": [4, 4], "name": "出口"},
         {"to": "fengmo_valley", "at": [60, 60], "name": "封魔谷"}]),
    "fengmo_valley": map_spec(
        "封魔谷", "field", 44, 47,
        [{"monster": "emo_gongjianshou", "count": 14, "respawn_seconds": 70},
         {"monster": "heiming_zhanshi", "count": 8, "respawn_seconds": 70}],
        [{"to": "stone_tomb", "at": [4, 4], "name": "出口"},
         {"to": "zuma_temple", "at": [60, 60], "name": "祖玛入口"}]),
    "zuma_temple": map_spec(
        "祖玛寺庙", "dungeon", 47, 50,
        [{"monster": "zumma_weishi", "count": 12, "respawn_seconds": 80},
         {"monster": "zumma_jiaozhu", "count": 1, "respawn_seconds": 900}],
        [{"to": "fengmo_valley", "at": [4, 4], "name": "出口"}]),
}


def main() -> None:
    mon = load("monsters.json")
    mon["monsters"].update(NEW_MONSTERS)
    mon["meta"]["note"] = (
        "P1 怪表：比奇带 6 种 + 蜈蚣洞/半兽古墓/沃玛/石墓/封魔/祖玛 11 种。"
        "早期 6 种经验对齐公开口径（鸡 10 / 鹿 20 / 僵尸 150 / 尸王 300），后期按等级带近似；"
        "dc/ac 均为 (下限, 上限)。ai: passive（被打才还手）/ aggressive / elite（教主，视野与脱战更大）。"
    )
    save("monsters.json", mon)

    drops = load("drops.json")
    drops["drops"].update(NEW_DROPS)
    drops["meta"]["note"] = (
        "每组独立 roll 组权重（万分比，非互斥）；组内按 permille 挑一条；gold 单独 roll 区间。"
        "P1 起怪也掉技能书（低概率），与原版「书靠打或买」一致。"
    )
    save("drops.json", drops)

    maps = load("maps.json")
    maps["maps"].update(NEW_MAPS)
    portals = maps["maps"]["biqi_mine_1"].setdefault("portals", [])
    if not any(p.get("to") == "wugong_cave_1" for p in portals):
        portals.append({"to": "wugong_cave_1", "at": [60, 60], "name": "蜈蚣洞"})
    maps["meta"]["note"] = (
        "P1 八张图等级带连续：比奇省外围 1-10 → 矿洞 11-20 → 蜈蚣洞 20-26 → 半兽古墓 26-33 → "
        "沃玛寺庙 33-40 → 石墓阵 40-44 → 封魔谷 44-47 → 祖玛寺庙 47-50。"
        "respawn_seconds 是该刷点自己的补刷间隔（教主 10-15 分钟）。"
    )
    save("maps.json", maps)

    print(f"monsters={len(mon['monsters'])} drops={len(drops['drops'])} maps={len(maps['maps'])}")


if __name__ == "__main__":
    main()
