"""机械门禁 —— 把"同一事实写在两处必须相等"变成 <1 秒可跑的检查。

用法::

    python scripts\\verify.py            # 8 条门禁，全绿退出码 0
    python scripts\\verify.py --list     # 只列名字与说明
    python scripts\\verify.py --verbose  # 每条的证据
    python scripts\\verify.py --selfcheck  # 给每条注入针对性破坏，证明它拦得住

为什么需要它：本项目的数据表是**手录**的（经验表、装备表、刷新表），
手录就会漏；测试登记表、README 的命令与目录树同样是手写。
这些"两处必须一致"的事，机器查比记性可靠。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_TABLES = ("exp", "classes", "skills", "monsters", "items", "drops", "maps",
               "sieges", "mercenaries")


def gate_data_integrity() -> tuple[bool, str]:
    """九张数据表的引用完整性（掉落/刷新/技能职业/攻城全部可解析）。"""
    from mirsolo.data.loader import GameData

    game = GameData.load()
    if game.errors:
        return False, "; ".join(game.errors[:5])
    return True, (f"{len(game.monsters['monsters'])} 怪物 / {len(game.items['items'])} 物品 / "
                  f"{len(game.skills['skills'])} 技能 / {len(game.maps['maps'])} 地图")


def gate_data_files() -> tuple[bool, str]:
    """data/ 下九张表齐全且是合法 JSON。"""
    missing = [f"{name}.json" for name in DATA_TABLES
               if not (ROOT / "data" / f"{name}.json").exists()]
    if missing:
        return False, f"缺表: {missing}"
    bad = []
    for name in DATA_TABLES:
        try:
            json.loads((ROOT / "data" / f"{name}.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            bad.append(f"{name}.json: {exc}")
    if bad:
        return False, "; ".join(bad)
    return True, f"{len(DATA_TABLES)}/{len(DATA_TABLES)} 张表可解析"


def gate_luck_sources(data=None) -> tuple[bool, str]:
    """幸运（幸运 9）的三条口径：来源槽受限、值域受限、**真的能凑到 9**。

    前两条防的是"数据表写错"（给手镯也写上 luck 就等于悄悄新增了一条幸运来源），
    第三条防的是更阴的那类事故：机制实现了、数据改了，结果玩家怎么搭都到不了 9，
    "幸运 9 刀刀上限"变成只有文档里存在的功能。
    """
    from mirsolo.combat.formulas import LUCK_CAP
    from mirsolo.data.loader import CLASSES, LUCK_SLOTS

    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()

    bad_slot, bad_value = [], []
    for item_id, spec in data.items["items"].items():
        luck = int(spec.get("luck") or 0)
        if not luck:
            continue
        if spec.get("slot") not in LUCK_SLOTS:
            bad_slot.append(f"{item_id}({spec.get('slot')})")
        if not 0 < luck <= LUCK_CAP:
            bad_value.append(f"{item_id}={luck}")

    if bad_slot:
        return False, f"幸运只能写在 {LUCK_SLOTS} 上，以下物品越界: {bad_slot}"
    if bad_value:
        return False, f"幸运值必须落在 (0, {LUCK_CAP}]，以下越界: {bad_value}"

    # 「能凑到 9」要按**职业**分别算：只对战士开放的幸运武器，帮不了法师和道士。
    # 不分开算的话，"道士一辈子到不了幸运 9"这种偏科是查不出来的。
    reach = {}
    for class_id in CLASSES:
        best = {"weapon": 0, "necklace": 0}
        for spec in data.items["items"].values():
            slot = spec.get("slot")
            if slot not in best:
                continue
            allowed = spec.get("class") or []
            if allowed and class_id not in allowed:
                continue
            best[slot] = max(best[slot], int(spec.get("luck") or 0))
        reach[class_id] = best["weapon"] + best["necklace"]

    starving = [f"{cid} 只能凑到 {got}" for cid, got in sorted(reach.items())
                if got < LUCK_CAP]
    if starving:
        return False, f"幸运 {LUCK_CAP} 不可达——{'；'.join(starving)}"
    return True, ("三职业都能凑到幸运 9（武器/项链最高搭配："
                  + "，".join(f"{cid} {reach[cid]}" for cid in CLASSES) + "）")


def gate_ring_effects(data=None) -> tuple[bool, str]:
    """特戒（六枚戒指）：值域合规 + 六效齐备 + **每枚都真能打到**。

    ``loader.validate`` 只查"键在不在"，这里查它查不了的三件事：

    * **值域**——``bp`` 是万分比（``rand_int(1, 10000)``），写成 3000 就是每次都
      麻痹；``permille`` 是千分比（1000 = 100%），写成 10000 会十倍代偿。
      两类比率混用在本项目踩过一次，见 ``docs/数据口径.md``。
    * **六效齐备**——少一种效果就是"文档里写着、游戏里拿不到"，
      和幸运 9 凑不齐是同一类事故。
    * **获取途径**——价格是 15 万起的特戒名义上也在商店货架里，
      但真能拿到只有打 BOSS 这一条路；掉落表漏挂一枚，那枚等于不存在。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.data.loader import RING_EFFECTS, RING_PARAM_KEYS

    def _violations(effect: str, param: dict) -> list[str]:
        """返回该效果参数的不合规说明（空列表 = 合规）。"""
        out = []
        try:
            if effect == "paralysis":
                if not 0 < int(param["bp"]) <= 10000:
                    out.append("bp 必须是万分比 (0, 10000]")
                if int(param["ticks"]) <= 0:
                    out.append("ticks 必须为正")
            elif effect == "protect":
                if not 0 < int(param["permille"]) <= 1000:
                    out.append("permille 必须是千分比 (0, 1000]")
            elif effect == "revive":
                if int(param["charges"]) <= 0:
                    out.append("charges 必须为正")
                if not 0 < int(param["hp_permille"]) <= 1000:
                    out.append("hp_permille 必须是千分比 (0, 1000]")
            elif effect == "teleport":
                if param["map"] not in data.maps["maps"]:
                    out.append(f"目标地图 {param['map']!r} 不存在")
                if int(param["cooldown_ticks"]) <= 0:
                    out.append("cooldown_ticks 必须为正")
            elif effect == "invis":
                if int(param["ticks"]) <= 0:
                    out.append("ticks 必须为正")
                if int(param["recover_ticks"]) < 0:
                    out.append("recover_ticks 不能为负")
            elif effect == "flame":
                if int(param["power_permille"]) <= 0:
                    out.append("power_permille 必须为正")
        except (KeyError, TypeError, ValueError) as exc:
            out.append(f"参数不可解析（{type(exc).__name__}）")
        return out

    implemented: dict[str, list[str]] = {}
    bad: list[str] = []
    for item_id, spec in data.items["items"].items():
        effect = spec.get("ring_effect")
        if effect is None:
            continue
        if effect not in RING_EFFECTS:
            bad.append(f"{item_id}: 未知效果 {effect!r}")
            continue
        implemented.setdefault(effect, []).append(item_id)
        param = spec.get("ring_param") or {}
        omitted = [k for k in RING_PARAM_KEYS[effect] if k not in param]
        if omitted:
            bad.append(f"{item_id}: ring_param 缺 {omitted}")
            continue
        for note in _violations(effect, param):
            bad.append(f"{item_id}({effect}): {note}")
    if bad:
        return False, "; ".join(bad)

    unfinished = sorted(RING_EFFECTS - set(implemented))
    if unfinished:
        return False, f"以下特戒效果没有任何一枚戒指实现: {unfinished}"

    reachable: set[str] = set()
    for table in data.drops["drops"].values():
        for group in table.get("groups", []):
            for entry in group.get("entries", []):
                reachable.add(entry.get("item"))
    rings = {item_id for ids in implemented.values() for item_id in ids}
    stranded = sorted(rings - reachable)
    if stranded:
        return False, f"以下戒指没有任何掉落来源: {stranded}"
    return True, (f"六效齐备（{len(rings)} 枚戒指），参数值域合规，"
                  f"且每枚都有 BOSS 掉落来源")


def gate_auction_book(data=None) -> tuple[bool, str]:
    """拍卖行：装备与技能书都寄卖得掉，且**底价高于商店回收价**。

    ``loader.validate`` 只查"键在不在、值域对不对"，这里查它查不了的两件事：

    * **覆盖**——没标 ``auction`` 的装备在拍卖行里等于不存在，而"为什么这件
      不能寄卖"只有翻 JSON 才看得出来（面板上它显示为灰色"不可寄卖"，
      但没人会去数灰了几行）。
    * **底价 > 回收价**——这是拍卖行**存在的理由**：比卖商店多换点钱。
      数据表一旦写低，玩家永远该直接卖商店，拍卖行就是白做的。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.economy import auction
    from mirsolo.economy.shop import sell_price

    tradeable = [item_id for item_id, spec in data.items["items"].items()
                 if spec.get("kind", "equip") in ("equip", "book")]
    stranded = [i for i in tradeable if auction.auction_param(data, i) is None]
    if stranded:
        return False, f"以下物品不能寄卖: {sorted(stranded)}"
    cheap = []
    for item_id in tradeable:
        floor, cap = auction.price_bounds(data, item_id)
        if floor <= sell_price(data, item_id):
            cheap.append(f"{item_id}(底价 {floor} <= 回收 {sell_price(data, item_id)})")
        if cap <= floor:
            cheap.append(f"{item_id}(上限 {cap} <= 底价 {floor})")
    if cheap:
        return False, "寄卖不比卖商店划算: " + "; ".join(sorted(cheap))
    return True, f"{len(tradeable)} 件可寄卖，底价都高于商店回收价"


def gate_merc_hire(data=None) -> tuple[bool, str]:
    """佣兵：门槛**练得到**、价格随门槛递增、``SQUAD_LIMIT`` 真的在起作用。

    三条都是"改表改出来的事故"：

    * 门槛高于全部地图的等级带上限 → 这条佣兵永远雇不到，等于不存在；
    * 价格与门槛不单调递增 → "贵的更弱"，玩家一眼看出表是乱填的；
    * ``sum(max) <= SQUAD_LIMIT`` → 全局上限永远触发不了，是死代码。
      死掉的约束最危险：它看起来在保护平衡，其实什么都没做。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.actor import mercenary

    table = data.mercenaries.get("mercenaries", {})
    if not table:
        return False, "佣兵表是空的"
    top = max(int(spec["level_range"][1]) for spec in data.maps["maps"].values())

    bad = []
    for merc_id, spec in sorted(table.items()):
        if int(spec["level_req"]) > top:
            bad.append(f"{merc_id} 门槛 {spec['level_req']} 级 > 地图带上限 {top} 级")
        if int(spec["price"]) <= 0 or int(spec["duration_ticks"]) <= 0:
            bad.append(f"{merc_id} 价格/时长必须为正")

    ladder = sorted(table.items(), key=lambda kv: int(kv[1]["level_req"]))
    prices = [int(spec["price"]) for _, spec in ladder]
    if prices != sorted(prices):
        bad.append("价格未随等级门槛递增：" + " < ".join(str(p) for p in prices))

    kind_total = sum(int(spec["max"]) for spec in table.values())
    if kind_total <= mercenary.SQUAD_LIMIT:
        bad.append(f"各 max 之和 {kind_total} <= SQUAD_LIMIT {mercenary.SQUAD_LIMIT}"
                   f"（全局上限永远触发不了）")
    if bad:
        return False, "; ".join(bad)
    return True, (f"{len(table)} 种佣兵，门槛 1-{top} 级内可达，价格递增，"
                  f"全局上限 {mercenary.SQUAD_LIMIT} < 各 max 之和 {kind_total}（确实在起作用）")


def gate_codex_coverage(data=None) -> tuple[bool, str]:
    """图鉴：怪物表每一种都要**查得到出处**（掉落表有条目）。

    图鉴每一行右侧写的是"这只怪出什么"。没有掉落表的怪在图鉴里会显示
    "无掉落"——那等于告诉玩家这只怪白打，而实际上多半只是掉落表漏挂了。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    monsters = data.monsters["monsters"]
    drops = data.drops["drops"]
    if not monsters:
        return False, "怪物表是空的"
    stranded = sorted(species for species in monsters if not drops.get(species))
    if stranded:
        return False, f"以下怪没有掉落表，图鉴会显示「无掉落」: {stranded}"
    return True, f"{len(monsters)} 种怪都有掉落来源，图鉴每一行都查得到出处"


def gate_tests_registered() -> tuple[bool, str]:
    """``tests/run_tests.MODULES`` ↔ ``tests/test_*.py`` 双向一致。"""
    from tests import run_tests

    on_disk = {p.stem for p in (ROOT / "tests").glob("test_*.py")}
    registered = set(run_tests.MODULES)
    missing = sorted(on_disk - registered)
    ghost = sorted(registered - on_disk)
    if missing or ghost:
        return False, f"未登记: {missing} / 不存在: {ghost}"
    return True, f"{len(registered)} 个模块已登记"


def gate_readme_commands() -> tuple[bool, str]:
    """README 里出现的每条 ``python -m <模块>`` 都必须真实可导入。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = set()
    for line in readme.splitlines():
        for token in line.replace("`", " ").split():
            if token.startswith("mirsolo.") or token.startswith("tests."):
                targets.add(token.rstrip(",.;)"))
    bad = []
    for target in sorted(targets):
        if "<" in target or "…" in target:
            continue
        try:
            importlib.import_module(target)
        except Exception as exc:  # noqa: BLE001 - 门禁只关心"能不能导入"
            bad.append(f"{target} ({type(exc).__name__})")
    if bad:
        return False, "; ".join(bad)
    return True, f"{len(targets)} 个引用可导入"


def gate_tree_real() -> tuple[bool, str]:
    """README「项目结构」代码块里列出的路径都必须真实存在。

    目录树是**嵌套**的：``combat/`` 下面的 ``formulas.py`` 实际位于
    ``mirsolo/combat/formulas.py``。这里按缩进维护一个父目录栈，
    把每行还原成完整相对路径再判断——否则每条子项都会被误报成缺失。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    listed: list[str] = []
    in_block = False
    stack: list[tuple[int, str]] = []  # (缩进, 完整路径)

    for line in readme.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            stack.clear()
            continue
        if not in_block or not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "|", "-", ">")):
            continue
        indent = len(line) - len(line.lstrip(" "))
        head = stripped.split()[0]
        is_dir = head.endswith("/")
        name = head.rstrip("/")
        # 只校验看起来像路径的条目（排除 ```bat 代码块里的命令）
        if not (is_dir or name.endswith((".py", ".json", ".toml", ".md"))):
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = stack[-1][1] if stack else ""
        # 目录树的**第一行**通常是项目根目录名（与仓库同名），它不是仓库内的子目录；
        # 后面同名的 `mirsolo/` 才是真正的包目录，不能一并当根。
        if not listed and not prefix and name == ROOT.name:
            full = "."
        else:
            full = f"{prefix}/{name}" if prefix else name
        listed.append(full)
        if is_dir:
            stack.append((indent, "" if full == "." else full))

    missing = [p for p in listed if p != "." and not (ROOT / p).exists()]
    if missing:
        return False, f"目录树里不存在的路径: {missing[:5]}"
    return True, f"{len(listed)} 条路径全部存在"


def gate_package_imports() -> tuple[bool, str]:
    """``mirsolo/`` 下每个模块都能 import（防"改了名没改引用"）。"""
    modules = []
    for path in (ROOT / "mirsolo").rglob("*.py"):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    bad = []
    for name in sorted(modules):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
    if bad:
        return False, "; ".join(bad[:3])
    return True, f"{len(modules)} 个模块可导入"


def gate_map_bands(data=None) -> tuple[bool, str]:
    """地图带必须**等级连续**且**传送图连通**。

    这两件事都是"看表看不出来、进游戏才知道"的问题：
    等级带断一档 → 玩家卡在练级断层；传送不连通 → 后段地图根本走不到。

    ``data`` 可传入已改动的 ``GameData``（``--selfcheck`` 靠这个注入破坏，
    免得为了自检去改磁盘上的真数据）。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    maps = data.maps["maps"]
    # 1) 等级带连续：按起点排序后，后一张的起点不得高于前一张的终点 +1
    bands = sorted(((spec["level_range"][0], spec["level_range"][1], mid)
                    for mid, spec in maps.items()), key=lambda x: (x[0], x[1]))
    gaps = []
    for prev, cur in zip(bands, bands[1:]):
        if cur[0] > prev[1] + 1:
            gaps.append(f"{prev[2]}({prev[0]}-{prev[1]}) → {cur[2]}({cur[0]}-{cur[1]}) 断了")
    if gaps:
        return False, "; ".join(gaps)
    if bands[0][0] > 1:
        return False, f"最低等级带从 {bands[0][0]} 级起，1 级没有地图"
    top = max(b[1] for b in bands)

    # 2) 传送图**有向**连通：从最低等级那张图出发，只沿 portals[].to 走。
    #    不能把"对方的出口"当成本图的入口——那会让"只有出口、没有入口"的
    #    孤岛图也被判成连通（实测注入验证时正是这个漏洞）。
    #
    #    例外：**战役图**（``sieges.json`` 里声明的）不是靠传送点进的，
    #    而是 ``start_siege`` 直接换图。但它们必须给出 ``base_map``——
    #    否则"进得去出不来"就没人管了。例外名单从数据表推导，
    #    不在这里写死地图名（写死等于门禁自己给自己开后门）。
    campaign_maps = {}
    for siege_id, spec in (data.sieges.get("sieges") or {}).items():
        campaign_maps[spec["map"]] = spec.get("base_map")
    for map_id, base in campaign_maps.items():
        if map_id not in maps:
            return False, f"战役 {map_id} 的地图不存在"
        if base is None:
            return False, f"战役图 {map_id} 没有 base_map（进去就出不来）"
        if base not in maps:
            return False, f"战役图 {map_id} 的 base_map {base} 不存在"

    start = bands[0][2]
    seen = {start}
    queue = [start]
    while queue:
        cur = queue.pop()
        for portal in maps[cur].get("portals", []):
            nxt = portal.get("to")
            if nxt in maps and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    unreachable = sorted(set(maps) - seen - set(campaign_maps))
    if unreachable:
        return False, f"按传送方向走不到的图：{unreachable}"
    return True, f"{len(maps)} 张图（含 {len(campaign_maps)} 张战役图），1-{top} 级连续且全连通"


def gate_wear_and_repair(data=None) -> tuple[bool, str]:
    """磨损与修理：``durability`` 真在用、修理比买新的便宜、寿命落在可玩区间。

    三条都是"数值填错却没人发现"的典型：

    * ``durability <= 0`` → 这件装备一穿就废（顺带把修理费算成除零）；
    * **全废修理费 >= 买入价** → "卖掉再买一把"永远更划算，修理按钮是摆设；
    * 期望寿命（磨穿要挨多少次命中）太短 → 打两只怪就得回城修；
      太长 → 一辈子不用管，这个系统等于没做。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.components import Wear
    from mirsolo.items import wear

    items = {iid: spec for iid, spec in data.items["items"].items()
             if spec.get("kind", "equip") == "equip"}
    if not items:
        return False, "装备表是空的"

    bad = []
    for item_id, spec in sorted(items.items()):
        total = int(spec.get("durability", 0) or 0)
        if total <= 0:
            bad.append(f"{item_id} durability 必须为正（现在是 {total}）")
            continue
        wrecked = Wear(points={item_id: total})
        cost = wear.repair_cost(data, wrecked, item_id)
        price = max(1, int(spec.get("price", 1)))
        if cost >= price:
            bad.append(f"{item_id} 修满要 {cost} 金，比买一把新的 {price} 金还贵")
        if cost <= 0:
            bad.append(f"{item_id} 全废了修理费却是 0（修理白嫖）")

    bp = wear.WEAR_BP_PER_HIT
    if not 0 < bp < 10000:
        bad.append(f"WEAR_BP_PER_HIT={bp} 不在万分比区间内")
    else:
        # 期望寿命（命中次数）= durability ÷ (bp / 10000)
        lives = [int(spec.get("durability", 0)) * 10000 // bp for spec in items.values()]
        if min(lives) < 100:
            bad.append(f"最短命的装备只撑 {min(lives)} 次命中（< 100，打两只怪就得修）")
        if max(lives) > 5000:
            bad.append(f"最长命的装备要 {max(lives)} 次命中（> 5000，一辈子不用修）")

    if bad:
        return False, "; ".join(bad)
    shortest = min(int(spec.get("durability", 0)) * 10000 // bp for spec in items.values())
    longest = max(int(spec.get("durability", 0)) * 10000 // bp for spec in items.values())
    return True, (f"{len(items)} 件装备 durability 8-30，修满 < 买入价，"
                  f"期望寿命 {shortest}-{longest} 次命中")



def gate_death_penalty(data=None) -> tuple[bool, str]:
    """死亡惩罚：兜底一定存在、原地复活有代价、惩罚不越过"掉级"红线。

    四件惩罚里最容易悄悄失效的就是"延迟结算"这一条设计：一旦有人图省事
    把扣减挪回致死那一下，原地付费复活就得靠退款实现，而退款逻辑会把
    "掉出去的物品"和"没掉的"混在一起算。因此这里既查数值口径，
    也用真实的 settle 调用验证**两档惩罚确实不同**。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.actor import penalty
    from mirsolo.actor.factory import spawn_player
    from mirsolo.bootstrap import build_session
    from mirsolo.components import Progression, Wallet
    from mirsolo.determinism import Rng

    bad = []

    # 1) 兜底窗口：倒地之后必须有自动回城，否则没有交互的场合会永远躺着
    if penalty.DOWN_TICKS <= 0:
        bad.append(f"DOWN_TICKS={penalty.DOWN_TICKS} 必须为正"
                   f"（没有自动回城兜底，headless 会永远躺地上）")

    # 2) 原地复活必须有代价，且不能越高级越便宜
    if penalty.REVIVE_BASE_GOLD <= 0:
        bad.append(f"REVIVE_BASE_GOLD={penalty.REVIVE_BASE_GOLD} 必须为正"
                   f"（原地复活不能白嫖，否则全额惩罚永远触发不到）")
    if penalty.REVIVE_GOLD_PER_LEVEL < 0:
        bad.append(f"REVIVE_GOLD_PER_LEVEL={penalty.REVIVE_GOLD_PER_LEVEL} 不能为负"
                   f"（等级越高反而越便宜，等于鼓励越级送死）")

    # 3) 倍率必须是千分比，且不足以一次掉光一级（掉级红线）
    for name, value in (("EXP_LOSS_PERMILLE", penalty.EXP_LOSS_PERMILLE),
                        ("GOLD_LOSS_PERMILLE", penalty.GOLD_LOSS_PERMILLE)):
        if not 0 < value <= 500:
            bad.append(f"{name}={value} 不在千分比区间 (0, 500]"
                       f"（死一次最多掉半级，掉到负数/掉光都不行）")

    # 4) 掉物概率必须是万分比
    if not 0 <= penalty.BAG_DROP_BP <= 10000:
        bad.append(f"BAG_DROP_BP={penalty.BAG_DROP_BP} 不在万分比区间 [0, 10000]")

    # 5) 死了必然磨装备：这一件是唯一不可回避的代价
    if penalty.DEATH_WEAR_POINTS < 1:
        bad.append(f"DEATH_WEAR_POINTS={penalty.DEATH_WEAR_POINTS} 至少为 1"
                       f"（死了装备总得坏一点）")

    # 6) 两档必须真的不同：原地不能连经验金币一起掉，
    #    否则"花钱原地复活"就是纯亏，没人会按 R
    session = build_session(data=data, seed=1, map_id="biqi_field")
    try:
        player = spawn_player(session.world, data, class_id="warrior")
        prog = player.get(Progression)
        prog.level = 7
        prog.exp = 600
        player.get(Wallet).gold = 10 ** 6    # 够付 7 级的原地复活费（1200 金）
        home = penalty.home_point(data, "biqi_field")
        back = penalty.settle(data, player, rng=Rng(1, "gate-back"), tick=0,
                              in_place=False, home=home)
        prog.exp = 600
        player.get(Wallet).gold = 10 ** 6
        stay = penalty.settle(data, player, rng=Rng(1, "gate-stay"), tick=1,
                              in_place=True)
    finally:
        session.shutdown()

    if back["exp_lost"] <= 0:
        bad.append("回城那一档没掉经验（全额惩罚形同虚设）")
    if stay["exp_lost"] != 0 or stay["gold_lost"] != 0 or stay["items"]:
        bad.append("原地复活那档连经验/金币/物品一起掉了（花钱买不到减损）")
    if stay["paid"] <= 0:
        bad.append("原地复活没收钱")

    if bad:
        return False, "; ".join(bad)
    return True, (f"倒地 {penalty.DOWN_TICKS} 刻后自动回城；死一次掉经验/金币各 "
                  f"{penalty.EXP_LOSS_PERMILLE}‰，全身磨损 {penalty.DEATH_WEAR_POINTS} 点，"
                  f"{penalty.BAG_DROP_BP / 100:.0f}% 掉一件背包物品；"
                  f"原地复活 {penalty.REVIVE_BASE_GOLD}+"
                  f"{penalty.REVIVE_GOLD_PER_LEVEL}×等级，只掉磨损")

def gate_potion_and_sustain(data=None) -> tuple[bool, str]:
    """药水与续航：九档齐备、冷却真的在拦、每个怪都掉药、自动喝药真会自己喝。

    最容易被悄悄改坏的是**公共冷却**与**掉落覆盖**这两条：

    * 冷却归零 → 背包里有几瓶就能在一个 tick 里灌几瓶，续航直接变成无敌，
      而这条只写在 ``consume.cooldown_ticks`` 里，改表的人未必知道；
    * 某个怪的药水组被删/挪位 → 穷的时候打不动又没有别的补给路，玩家只会
      觉得"这游戏卡住了"。挪位更隐蔽：``roll_drops`` 的流名带组下标，
      把药水组插到前面会把既有装备组的掉落结果**全部洗牌**。

    前几条是数值口径，最后一条（6）是真跑一次手动 + 一次自动，
    证明"喝药"与"自动喝药"这两条路都通着。
    """
    if data is None:
        from mirsolo.data.loader import GameData

        data = GameData.load()
    from mirsolo.actor.factory import spawn_player
    from mirsolo.bootstrap import build_session
    from mirsolo.components import Bag, PotionState, Progression, Vitals
    from mirsolo.items import potion
    from mirsolo.progression.exp import apply_growth

    bad: list[str] = []
    items = {iid: spec for iid, spec in data.items["items"].items()
             if spec.get("kind") == "consumable"}
    if len(items) != 9:
        bad.append(f"药水应该正好 9 档，现在 {len(items)} 件")

    # 1) 三类效果都要有，否则某一职业直接没有续航（法师没蓝药就是根木棍）
    effects = {spec.get("consume", {}).get("effect") for spec in items.values()}
    for need in ("hp", "mp", "both"):
        if need not in effects:
            bad.append(f"缺 {need} 类药水（效果只剩 {sorted(effects)}）")

    # 2) 冷却必须为正：归零就能同 tick 连灌
    for item_id in sorted(items):
        if potion.cooldown_of(data, item_id) <= 0:
            bad.append(f"{item_id} 冷却为 0（可以在一个 tick 里连灌）")

    # 3) 阈值必须是千分比，且不能是 0（0 = 永远不自动喝，等于这功能没做）
    for name, value in (("AUTO_HP_PERMILLE", potion.AUTO_HP_PERMILLE),
                        ("AUTO_MP_PERMILLE", potion.AUTO_MP_PERMILLE)):
        if not 0 < value < 1000:
            bad.append(f"{name}={value} 不在千分比区间 (0, 1000)")
    if potion.BUY_BUNDLE <= 0:
        bad.append(f"BUY_BUNDLE={potion.BUY_BUNDLE} 必须为正")

    # 4) 药水不进拍卖行：量产物挂出去是噪音，还会把"底价高于回收价"拉歪
    auctionable = sorted(i for i, s in items.items() if s.get("auction") is not None)
    if auctionable:
        bad.append(f"药水不该能寄卖：{auctionable}")

    missing: list[str] = []
    misplaced: list[str] = []
    item_table = data.items["items"]

    def kind_of(item_id: str) -> str:
        # 掉落表里出现一个不存在的 id 时返回空串，而不是让门禁崩掉：
        # ``--selfcheck`` 会故意往里面塞 ``no_such_item`` 去验别的门禁，本门禁
        # 只是顺路扫一遍，认不出来就当"不是药水"，由"每个怪都掉药"报出来。
        return item_table.get(item_id, {}).get("kind", "")

    for monster_id, table in sorted(data.drops["drops"].items()):
        groups = table.get("groups", [])
        if not groups:
            missing.append(monster_id)
            continue
        last = groups[-1].get("entries", [])
        if not last or not all(kind_of(e["item"]) == "consumable" for e in last):
            missing.append(monster_id)
        for group in groups[:-1]:
            if any(kind_of(e["item"]) == "consumable"
                   for e in group.get("entries", [])):
                misplaced.append(monster_id)
                break
    if missing:
        bad.append(f"以下怪不掉药水：{sorted(set(missing))}")
    if misplaced:
        bad.append(f"药水组只能追加在末尾（挪位会洗掉既有掉落）：{sorted(set(misplaced))}")

    # 6) 真喝一次（手动）+ 真自己喝一次（自动）
    if not bad:
        session = build_session(data=data, seed=1, map_id="biqi_field")
        try:
            player = spawn_player(session.world, data, class_id="warrior")
            player.get(Progression).level = 30
            apply_growth(player, data.classes["classes"]["warrior"])
            vitals = player.get(Vitals)
            vitals.hp = 40
            player.get(Bag).add("hp_potion_m", 2)
            got = potion.drink(player, data, "hp_potion_m", tick=0)
            if got["hp"] <= 0 or player.get(Bag).items["hp_potion_m"] != 1:
                bad.append("喝药没回血或没扣瓶")
            if not potion.is_cooling(player.get(PotionState), 0):
                bad.append("喝完没进冷却（可以同 tick 连灌）")
            # 自动：不投任何意图，血掉到阈值以下应该自己喝
            vitals.hp = 40
            player.get(PotionState).ready_at = 0
            session.step(1)
            if player.get(Bag).items.get("hp_potion_m", 0) != 0:
                bad.append("血低于 50% 却没有自动喝药（单机没有外挂，这档必须常开）")
        finally:
            session.shutdown()

    if bad:
        return False, "; ".join(bad)
    cools = sorted({potion.cooldown_of(data, i) for i in items})
    return True, (f"{len(items)} 档药水（hp/mp/both 齐备），公共冷却 {cools} 刻；"
                  f"自动阈值 HP<{potion.AUTO_HP_PERMILLE}‰ / MP<{potion.AUTO_MP_PERMILLE}‰；"
                  f"{len(data.drops['drops'])} 个怪都掉药水且药水组一律在末尾；"
                  f"商店一次 {potion.BUY_BUNDLE} 瓶")

GATES = [
    ("data-integrity", "九张数据表的引用完整性（掉落/刷新/技能职业/攻城可解析）", gate_data_integrity),
    ("data-files", "data/ 下九张表齐全且是合法 JSON", gate_data_files),
    ("map-bands", "地图等级带连续且传送图全连通", gate_map_bands),
    ("luck-sources", "幸运只来自武器/项链、值域合规、三职业都凑得到 9", gate_luck_sources),
    ("ring-effects", "六枚特戒六效齐备、参数值域合规、每枚都有掉落来源", gate_ring_effects),
    ("auction-book", "装备/技能书都可寄卖，且底价高于商店回收价", gate_auction_book),
    ("merc-hire", "佣兵门槛练得到、价格递增、全局上限真在起作用", gate_merc_hire),
    ("codex-coverage", "图鉴覆盖的每一种怪都有掉落来源", gate_codex_coverage),
    ("wear-and-repair", "装备耐久在用、修理比买新的便宜、寿命落在可玩区间",
     gate_wear_and_repair),
    ("death-penalty", "死亡惩罚：兜底存在、原地复活有代价、绝不掉级",
     gate_death_penalty),
    ("potion-and-sustain", "药水：九档齐备、公共冷却在拦、每个怪都掉药、自动喝药常开",
     gate_potion_and_sustain),
    ("tests-registered", "run_tests.MODULES ↔ tests/test_*.py 双向一致", gate_tests_registered),
    ("readme-commands", "README 里的 python -m 目标都真实可导入", gate_readme_commands),
    ("readme-tree-real", "README 项目结构里的路径都真实存在", gate_tree_real),
    ("package-imports", "mirsolo/ 下每个模块都能 import", gate_package_imports),
]


def _selfcheck() -> int:
    """给关键门禁注入破坏，断言它们会变红。

    "永远为绿的门禁等于装饰品"——这一步证明门禁是真的在工作，
    而不是恰好没东西可查。
    """
    failures = 0

    # 1) data-integrity：内存里改坏一条掉落引用
    from mirsolo.data.loader import GameData

    game = GameData.load()
    game.drops["drops"]["ji"]["groups"][0]["entries"][0]["item"] = "no_such_item"
    errors = game.cross_check()
    if any("no_such_item" in e for e in errors):
        print("  ok   data-integrity 能拦住死引用")
    else:
        failures += 1
        print("  FAIL data-integrity 未拦住死引用")

    # 2) tests-registered：临时塞一个假模块名
    from tests import run_tests

    original = list(run_tests.MODULES)
    run_tests.MODULES.append("test_this_does_not_exist")
    ok, detail = gate_tests_registered()
    run_tests.MODULES[:] = original
    if not ok:
        print(f"  ok   tests-registered 能拦住未登记模块（{detail}）")
    else:
        failures += 1
        print("  FAIL tests-registered 未拦住不存在的模块")

    # 3) readme-tree-real：临时改 README 里的一个路径
    readme_path = ROOT / "README.md"
    readme_bytes = readme_path.read_bytes()
    # ⚠️ 全程用 bytes 改写：``read_text``/``write_text`` 会在 Windows 上把 LF 翻成
    # CRLF（默认 newline 转换）。本仓库的 README.md 是 LF，而 date 检测到这里
    # 自己也读不出区别——但每次 --selfcheck 都会把整份 README 冲成 CRLF，
    # 于是"换行风格"这件小事会变成下一次折腾时最难解释的那种脏改动。
    text = readme_bytes.decode("utf-8")
    broken = text.replace("    determinism.py", "    does_not_exist.py", 1)
    if broken == text:
        print("  SKIP readme-tree-real 未找到可注入的路径")
    else:
        newline = "\r\n" if b"\r\n" in readme_bytes else "\n"
        readme_path.write_bytes(broken.replace("\n", newline).encode("utf-8"))
        ok, _ = gate_tree_real()
        readme_path.write_bytes(readme_bytes)
        if not ok:
            print("  ok   readme-tree-real 能拦住不存在的路径")
        else:
            failures += 1
            print("  FAIL readme-tree-real 未拦住不存在的路径")

    # 4) map-bands：战役图例外不能变成"后门"——
    #    把 sabak 的 base_map 抹掉，门禁必须红（进去就出不来的战役图不该放行）。
    sieges = game.sieges["sieges"]["sabak"]
    saved_base = sieges.get("base_map")
    sieges["base_map"] = None
    ok, detail = gate_map_bands(game)
    sieges["base_map"] = saved_base
    if not ok and "base_map" in detail:
        print("  ok   map-bands 能拦住没有 base_map 的战役图")
    else:
        failures += 1
        print("  FAIL map-bands 未拦住没有 base_map 的战役图")

    # 5) map-bands 的另一半：真把一张图从传送网里摘掉，必须报"走不到"
    saved_portals = list(game.maps["maps"]["zuma_temple"]["portals"])
    game.maps["maps"]["zuma_temple"]["portals"] = [
        p for p in saved_portals if p.get("to") != "chiyue_valley"]
    ok, detail = gate_map_bands(game)
    game.maps["maps"]["zuma_temple"]["portals"] = saved_portals
    if not ok and "走不到" in detail:
        print("  ok   map-bands 能拦住传送网断开（放行战役图不等于放行孤岛）")
    else:
        failures += 1
        print("  FAIL map-bands 未拦住传送网断开")

    # 6) luck-sources：把幸运写到衣服上 —— 来源槽放开就等于悄悄新增幸运来源
    luck_item = game.items["items"]["buyi"]
    luck_item["luck"] = 3
    ok, detail = gate_luck_sources(game)
    del luck_item["luck"]
    if not ok and "越界" in detail:
        print("  ok   luck-sources 能拦住写在武器/项链以外的幸运")
    else:
        failures += 1
        print("  FAIL luck-sources 未拦住非武器/项链槽上的幸运")

    # 7) luck-sources 的另一半：把项链幸运全清空，必须报"到不了 9"
    stashed = [(spec, spec["luck"]) for spec in game.items["items"].values()
               if spec.get("slot") == "necklace" and spec.get("luck")]
    for spec, _value in stashed:
        spec["luck"] = 0
    ok, detail = gate_luck_sources(game)
    for spec, value in stashed:
        spec["luck"] = value
    if not ok and "不可达" in detail:
        print("  ok   luck-sources 能拦住凑不到幸运 9 的数据（机制不能只活在文档里）")
    else:
        failures += 1
        print("  FAIL luck-sources 未拦住凑不到幸运 9 的数据")

    # 8) ring-effects：把麻痹概率写成 30000（万分比写成百分比口径）
    para = game.items["items"]["mabi_jiezhi"]
    saved_bp = para["ring_param"]["bp"]
    para["ring_param"]["bp"] = 30000
    ok, detail = gate_ring_effects(game)
    para["ring_param"]["bp"] = saved_bp
    if not ok and "万分比" in detail:
        print("  ok   ring-effects 能拦住越界的万分比（两类比率不许混用）")
    else:
        failures += 1
        print("  FAIL ring-effects 未拦住越界的万分比")

    # 9) ring-effects：删掉复活戒指那一项效果，必须报"六效不齐"
    revive = game.items["items"].pop("fuhuo_jiezhi")
    ok, detail = gate_ring_effects(game)
    game.items["items"]["fuhuo_jiezhi"] = revive
    if not ok and "revive" in detail:
        print("  ok   ring-effects 能拦住缺一种特戒效果（不许只活在文档里）")
    else:
        failures += 1
        print("  FAIL ring-effects 未拦住缺失的特戒效果")

    # 10) ring-effects：把复活戒指从掉落表里摘掉，必须报"拿不到"
    stashed_groups = []
    for _mid, table in game.drops["drops"].items():
        kept = [g for g in table.get("groups", [])
                if not any(e.get("item") == "fuhuo_jiezhi" for e in g.get("entries", []))]
        if len(kept) != len(table.get("groups", [])):
            stashed_groups.append((table, list(table["groups"])))
            table["groups"] = kept
    ok, detail = gate_ring_effects(game)
    for table, groups in stashed_groups:
        table["groups"] = groups
    if not ok and "掉落来源" in detail:
        print("  ok   ring-effects 能拦住没有掉落来源的戒指")
    else:
        failures += 1
        print("  FAIL ring-effects 未拦住没有掉落来源的戒指")


    # 11) auction-book：把一件装备的 auction 字段摘掉，必须报"不能寄卖"
    _item = game.items["items"]["mujian"]
    _saved_auction = _item.pop("auction")
    ok, detail = gate_auction_book(game)
    _item["auction"] = _saved_auction
    if not ok and "不能寄卖" in detail:
        print("  ok   auction-book 能拦住漏标 auction 的物品")
    else:
        failures += 1
        print(f"  FAIL auction-book 未拦住漏标 auction 的物品（{detail}）")

    # 12) auction-book 的另一半：把底价压到回收价以下，必须报"不划算"
    _param = game.items["items"]["mujian"]["auction"]
    _saved_floor = _param["floor_permille"]
    _param["floor_permille"] = 1
    ok, detail = gate_auction_book(game)
    _param["floor_permille"] = _saved_floor
    if not ok and "不比卖商店划算" in detail:
        print("  ok   auction-book 能拦住底价不高于回收价（拍卖行不能白做）")
    else:
        failures += 1
        print("  FAIL auction-book 未拦住底价不高于回收价")

    # 13) merc-hire：把魔法侍卫门槛抬到地图带之上，必须报"永远雇不到"
    _merc = game.mercenaries["mercenaries"]["mofashi"]
    _saved_req = _merc["level_req"]
    _merc["level_req"] = 999
    ok, detail = gate_merc_hire(game)
    _merc["level_req"] = _saved_req
    if not ok and "地图带上限" in detail:
        print("  ok   merc-hire 能拦住练不到的等级门槛")
    else:
        failures += 1
        print(f"  FAIL merc-hire 未拦住练不到的等级门槛（{detail}）")

    # 14) merc-hire 的另一半：把所有 max 压到 1，全局上限就成了死代码
    _saved_max = {k: v["max"] for k, v in game.mercenaries["mercenaries"].items()}
    for _spec in game.mercenaries["mercenaries"].values():
        _spec["max"] = 1
    ok, detail = gate_merc_hire(game)
    for _k, _v in _saved_max.items():
        game.mercenaries["mercenaries"][_k]["max"] = _v
    if not ok and "永远触发不了" in detail:
        print("  ok   merc-hire 能拦住够不着的全局上限（死约束）")
    else:
        failures += 1
        print(f"  FAIL merc-hire 未拦住够不着的全局上限（{detail}）")

    # 15) codex-coverage：把鸡从掉落表里摘掉，必须报"图鉴无出处"
    _saved_drop = game.drops["drops"].pop("ji")
    ok, detail = gate_codex_coverage(game)
    game.drops["drops"]["ji"] = _saved_drop
    if not ok and "ji" in detail:
        print("  ok   codex-coverage 能拦住没有掉落来源的怪")
    else:
        failures += 1
        print(f"  FAIL codex-coverage 未拦住没有掉落来源的怪（{detail}）")

    # 16) wear-and-repair：把木剑 durability 写成 0，必须报"必须为正"
    _saved_dur = game.items["items"]["mujian"]["durability"]
    game.items["items"]["mujian"]["durability"] = 0
    ok, detail = gate_wear_and_repair(game)
    game.items["items"]["mujian"]["durability"] = _saved_dur
    if not ok and "必须为正" in detail:
        print("  ok   wear-and-repair 能拦住 durability=0 的装备")
    else:
        failures += 1
        print(f"  FAIL wear-and-repair 未拦住 durability=0（{detail}）")

    # 17) wear-and-repair 的另一半：把修理倍率抬到 150%，修理按钮就成了摆设
    from mirsolo.items import wear as _wear

    _saved_rate = _wear.REPAIR_PERMILLE
    _wear.REPAIR_PERMILLE = 1500
    ok, detail = gate_wear_and_repair(game)
    _wear.REPAIR_PERMILLE = _saved_rate
    if not ok and "买一把新的" in detail:
        print("  ok   wear-and-repair 能拦住修理比买新的还贵（按钮不能是摆设）")
    else:
        failures += 1
        print(f"  FAIL wear-and-repair 未拦住修理贵过买新（{detail}）")

    # 18) death-penalty：把倒地窗口压成 0，等于取消自动回城兜底
    from mirsolo.actor import penalty as _pen

    _saved_down = _pen.DOWN_TICKS
    _pen.DOWN_TICKS = 0
    ok, detail = gate_death_penalty(game)
    _pen.DOWN_TICKS = _saved_down
    if not ok and "必须为正" in detail:
        print("  ok   death-penalty 能拦住没有自动回城兜底（躺地上就没人管）")
    else:
        failures += 1
        print(f"  FAIL death-penalty 未拦住 DOWN_TICKS=0（{detail}）")

    # 19) death-penalty 的另一半：原地复活免费，全额惩罚就永远触发不到
    _saved_base = _pen.REVIVE_BASE_GOLD
    _pen.REVIVE_BASE_GOLD = 0
    _saved_rate = _pen.REVIVE_GOLD_PER_LEVEL
    _pen.REVIVE_GOLD_PER_LEVEL = 0
    ok, detail = gate_death_penalty(game)
    _pen.REVIVE_BASE_GOLD = _saved_base
    _pen.REVIVE_GOLD_PER_LEVEL = _saved_rate
    if not ok and "白嫖" in detail:
        print("  ok   death-penalty 能拦住免费原地复活（四件惩罚形同虚设）")
    else:
        failures += 1
        print(f"  FAIL death-penalty 未拦住免费原地复活（{detail}）")

    # 20) potion-and-sustain：把小瓶的冷却写成 0 —— 背包有几瓶就能同 tick 灌几瓶
    _saved_cd = game.items["items"]["hp_potion_s"]["consume"]["cooldown_ticks"]
    game.items["items"]["hp_potion_s"]["consume"]["cooldown_ticks"] = 0
    ok, detail = gate_potion_and_sustain(game)
    game.items["items"]["hp_potion_s"]["consume"]["cooldown_ticks"] = _saved_cd
    if not ok and "连灌" in detail:
        print("  ok   potion-and-sustain 能拦住冷却为 0 的药水（不许同 tick 连灌）")
    else:
        failures += 1
        print(f"  FAIL potion-and-sustain 未拦住冷却为 0 的药水（{detail}）")

    # 21) potion-and-sustain 的另一半：把鸡的药水组删掉 —— 穷的时候就没补给路了
    _saved_groups = list(game.drops["drops"]["ji"]["groups"])
    game.drops["drops"]["ji"]["groups"] = _saved_groups[:-1]
    ok, detail = gate_potion_and_sustain(game)
    game.drops["drops"]["ji"]["groups"] = _saved_groups
    if not ok and "掉药水" in detail:
        print("  ok   potion-and-sustain 能拦住不掉药水的怪（两条腿不能只剩一条）")
    else:
        failures += 1
        print(f"  FAIL potion-and-sustain 未拦住不掉药水的怪（{detail}）")

    # 22) potion-and-sustain 的第三半：自动喝药阈值归零 = 这个功能等于没做
    from mirsolo.items import potion as _potion

    _saved_hp_gate = _potion.AUTO_HP_PERMILLE
    _potion.AUTO_HP_PERMILLE = 0
    ok, detail = gate_potion_and_sustain(game)
    _potion.AUTO_HP_PERMILLE = _saved_hp_gate
    if not ok and "千分比区间" in detail:
        print("  ok   potion-and-sustain 能拦住阈值为 0（自动喝药不能是死代码）")
    else:
        failures += 1
        print(f"  FAIL potion-and-sustain 未拦住阈值为 0（{detail}）")

    print(f"\nselfcheck: {'PASS' if failures == 0 else f'{failures} 条门禁形同虚设'}")
    return 0 if failures == 0 else 1

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mirsolo-verify", description="机械门禁")
    parser.add_argument("--list", action="store_true", help="只列门禁名与说明")
    parser.add_argument("--verbose", action="store_true", help="打印每条门禁的证据")
    parser.add_argument("--selfcheck", action="store_true", help="注入破坏，证明门禁拦得住")
    args = parser.parse_args(argv)

    if args.list:
        for name, desc, _ in GATES:
            print(f"{name:20s} {desc}")
        return 0

    if args.selfcheck:
        print("=== selfcheck：逐条注入破坏 ===")
        return _selfcheck()

    print("=== mirsolo 机械门禁 ===")
    failed = 0
    for name, desc, fn in GATES:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok:
            print(f"  ok   {name:20s} {detail if args.verbose else desc}")
        else:
            failed += 1
            print(f"  FAIL {name:20s} {detail}")

    print(f"\n{len(GATES) - failed}/{len(GATES)} 条门禁通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
