"""P2-S5 拍卖行 —— 给可交易物品挂上拍卖参数（需求度 + 底价档）。

在此之前``商店回收价 = 买入价 // 3``是玩家唯一的出货口，打到的多余装备
基本等于废品。拍卖行给的是第二个出口，但**必须比商店回收值钱、又不能值太多**：

* ``floor_permille``（要价下限 = 商店买入价 × 该千分比）一律 > 333‰
  —— 否则挂拍卖行还不如直接卖商店，拍卖行就白做了；
* ``demand_bp``（挂底价时的成交概率）按物品等级分档：低级货到处都是（400bp），
  终局货抢手（850bp）。这是"为什么这件能卖掉、那件卖不掉"的唯一解释，
  因此必须写在表里，而不是在代码里按价格猜。

幂等：按 id 覆盖写回 ``auction`` 字段，重复跑不会叠加。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

#: 按等级分档的 (等级上限, demand_bp, floor_permille)。
#: 档位表只有三档半，刻意不做逐件微调——逐件微调的结果没人复查得动。
TIERS = (
    (15, 400, 500),
    (30, 550, 550),
    (44, 700, 600),
    (10 ** 9, 850, 650),
)

#: 技能书单独一档：不吃等级分档（1 级的书和 50 级的书都是"教一个技能"），
#: 但需求比同级装备低——书是一次性消耗品，买过就没了。
BOOK_DEMAND_BP = 600
BOOK_FLOOR_PERMILLE = 550


def _params(level_req: int, kind: str) -> dict:
    if kind == "book":
        return {"demand_bp": BOOK_DEMAND_BP, "floor_permille": BOOK_FLOOR_PERMILLE}
    for top, demand, floor in TIERS:
        if level_req <= top:
            return {"demand_bp": demand, "floor_permille": floor}
    raise AssertionError("TIERS 必须以无穷大收尾")


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
    table = items["items"]
    touched = []
    for item_id, spec in sorted(table.items()):
        kind = spec.get("kind", "equip")
        if kind not in ("equip", "book"):
            print(f"[SKIP] {item_id}: kind={kind} 不可交易")
            continue
        spec["auction"] = _params(int(spec.get("level_req", 1)), kind)
        touched.append(item_id)
    _save("items", items)
    print(f"写入拍卖参数：{len(touched)} 件（equip + book 全部可寄卖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
