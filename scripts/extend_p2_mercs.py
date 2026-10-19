"""P2-S6 佣兵 —— 新建 ``data/mercenaries.json``（可雇佣的友军）。

原版的佣兵是行会花钱雇来的 NPC 打手，本质是"用金币换一段时间的战斗力"。
单机里这个交换关系完全成立，所以它是六项 P2 内容里**最不需要重造**的一项——
真正要决定的只有两件事：

* **属性不复用怪物表**：雇用兵/虎卫/魔法侍卫在传奇里根本不是怪，
  它们有自己的数值。塞进 ``monsters.json`` 会让"23 种怪"这个事实变脏，
  也会让图鉴把佣兵当成一种可解锁的怪（那是错的）。
  字段与怪物表**同构**（``hp``/``dc``/``ac``/``mac``/``accuracy``/``agility``/
  ``attack_speed``/``ai``），所以造实体时走的是同一条构造路径。
* **有明确的到期时间**：不给"永久随从"。单机里一支不消耗的常驻队伍
  会把所有战斗变成"看着他们打"，而到期制让佣兵回到它本来的定位——
  **一次打宝的筹码**，不是第二个角色。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

MERCENARIES = {
    "gubing": {
        "name": "雇用兵", "level": 20, "hp": 180,
        "dc": [6, 12], "ac": [2, 5], "mac": [0, 2],
        "accuracy": 10, "agility": 8, "attack_speed": 4,
        "ai": "aggressive", "move_speed": 0.9, "size": 1,
        "price": 8000, "duration_ticks": 3000, "level_req": 20, "max": 3,
    },
    "huwei": {
        "name": "虎卫", "level": 35, "hp": 420,
        "dc": [10, 20], "ac": [6, 12], "mac": [2, 5],
        "accuracy": 14, "agility": 10, "attack_speed": 4,
        "ai": "aggressive", "move_speed": 0.9, "size": 1,
        "price": 35000, "duration_ticks": 3000, "level_req": 35, "max": 2,
    },
    "mofashi": {
        "name": "魔法侍卫", "level": 50, "hp": 600,
        "dc": [16, 30], "ac": [8, 14], "mac": [4, 8],
        "accuracy": 18, "agility": 12, "attack_speed": 5,
        "ai": "aggressive", "move_speed": 0.9, "size": 1,
        "price": 150000, "duration_ticks": 3000, "level_req": 50, "max": 1,
    },
}

META = {
    "note": "佣兵：花金币雇一段时间的友军。字段与 monsters.json 同构（造实体走同一条路径）。",
    "duration_ticks": "雇佣时长（10Hz，3000 刻 = 5 分钟）",
    "max": "同一种佣兵同时最多带几名（全局上限由 mercenary.SQUAD_LIMIT 控制）",
}


def main() -> int:
    path = DATA_DIR / "mercenaries.json"
    payload = {"meta": META, "mercenaries": MERCENARIES}
    # 与仓库里其他 data/*.json 一致：CRLF
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    print(f"写入 {path.name}：{len(MERCENARIES)} 种佣兵 "
          f"（{', '.join(MERCENARIES)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
