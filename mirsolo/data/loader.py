"""数据表加载与完整性校验。

数值全部落在 ``data/*.json``（仓库资产，不随包分发），代码只认 ID。
本模块是**唯一**读 JSON 的地方——其他模块拿到的永远是解析后的对象，
不自己去 open 文件，也就不会各自发明一套容错。

校验分两层：

- :meth:`GameData.validate` —— 结构层（表存在、字段齐全、等级曲线连续）；
- :meth:`GameData.cross_check` —— 引用层（掉落里的物品/怪物必须真实存在）。

``scripts/verify.py`` 把后者做成机械门禁，``tests/test_data.py`` 把两层都钉住。
数据是手录的，手录就会漏——所以让机器查，不靠记性。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def data_dir_default() -> Path:
    """默认数据目录：优先环境变量 ``MIRSOLO_DATA``，否则仓库内 ``data/``。

    与 3dworld 的 ``WORLD3D_CONFIGS`` 同一思路：数据是仓库资产，
    非 editable 安装时用环境变量指路，而不是把 7 张表塞进 wheel。
    """
    env = os.environ.get("MIRSOLO_DATA")
    return Path(env) if env else DEFAULT_DATA_DIR

#: 允许的装备槽
SLOTS = ("weapon", "armor", "helmet", "necklace", "bracelet", "ring", "candle")
#: 允许的职业 id
CLASSES = ("warrior", "mage", "taoist")

#: 幸运的合法来源槽位（传奇口径：只有武器与项链带幸运，合计封顶 9）
#: 数据表若给别的槽写 ``luck``，``scripts/verify.py`` 的 ``luck-sources`` 门禁会红。
LUCK_SLOTS = ("weapon", "necklace")

#: 物品大类：装备 / 技能书 / 消耗品 / 矿石（消耗品自 P3-S3、矿石自 P3-S4 起）。
#: 消耗品的回复口径写在各自的 ``consume`` 段里、矿石的产出写在 ``mines.json`` 里，
#: loader 只查键在与值域，不替规则层做任何推断。
ITEM_KINDS = frozenset({"equip", "book", "consumable", "ore", "blessing"})

#: 工具类型全集（P3-S4 挖矿）。工具是"拿在手上才做得了某件事"的装备，
#: 与 ``ring_effect`` 同构：标记写在物品上，代码里不写"如果是鹤嘴锄就……"。
#: 目前只有鹤嘴锄一种，且它占 **weapon 槽**——挖矿时手上必须握着它，
#: 所以挖矿的代价就是"这一刻你的攻击力只有 1-3"，取舍感由此而来。
TOOL_KINDS = frozenset({"pick"})

#: 药水效果全集（数据面事实的唯一定义处，``mirsolo/items/potion.py`` 从这里导入）：
#: hp = 只回血 / mp = 只回蓝 / both = 红蓝都回（太阳水）。
POTION_EFFECTS = frozenset({"hp", "mp", "both"})

#: 特戒效果全集（数据面事实的唯一定义处，``mirsolo/items/rings.py`` 从这里导入）
#: 六枚戒指按**触发时机**分成三类（命中触发 / 挨打触发 / 主动触发），
#: 口径与替代方案写在 ``rings.py`` 的模块 docstring 里。
RING_EFFECTS = frozenset({
    "paralysis",  # 命中后概率定身目标
    "protect",    # 受伤时按比例用 MP 代偿
    "revive",     # 致死那一下拦下来并回血
    "teleport",   # 主动：数据驱动的回城点
    "invis",      # 持续：出手破隐，冷却后重新结
    "flame",      # 命中后追加一次火焰伤害
})

#: 每种效果必须给出的参数键。缺一个就是"表写了一半"——靠
#: ``scripts/verify.py`` 的 ``ring-effects`` 门禁拦住，别指望运行时才发现。
RING_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "paralysis": ("bp", "ticks"),
    "protect": ("permille",),
    "revive": ("charges", "hp_permille"),
    "teleport": ("map", "cooldown_ticks"),
    "invis": ("ticks", "recover_ticks"),
    "flame": ("power_permille",),
}

#: AI 档位（与 ``actor.factory.AI_TIERS`` 同名同义；loader 只认名字，
#: 具体视野/脱战数值由 factory 持有，避免两处都写一份）
AI_TIERS = ("passive", "aggressive", "elite")

#: 攻城目标类型：城门 / 内城门 / 旗台（旗台倒下 = 战役胜利）
SIEGE_OBJECTIVES = frozenset({"gate", "inner_gate", "banner"})

#: 技能 kind 全集（数据面事实的唯一定义处；mirsolo/skills/engine.py 从这里导入）
SKILL_KINDS = frozenset({
    "physical", "physical_aoe", "magic", "magic_aoe", "taoist",
    "heal", "group_heal", "poison", "buff_shield", "buff_invis", "summon",
})


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"数据表缺失: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class GameData:
    """一次性加载全部数据表 + 校验结果。"""

    exp: dict
    classes: dict
    skills: dict
    monsters: dict
    items: dict
    drops: dict
    maps: dict
    sieges: dict = field(default_factory=dict)
    mercenaries: dict = field(default_factory=dict)
    mines: dict = field(default_factory=dict)
    upgrades: dict = field(default_factory=dict)
    data_dir: Path = field(default_factory=data_dir_default)
    errors: list[str] = field(default_factory=list)

    # ── 加载 ────────────────────────────────────────

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "GameData":
        """从目录加载十一张表（缺一张直接抛 FileNotFoundError）。"""
        base = Path(data_dir) if data_dir is not None else data_dir_default()
        game = cls(
            exp=_read_json(base / "exp.json"),
            classes=_read_json(base / "classes.json"),
            skills=_read_json(base / "skills.json"),
            monsters=_read_json(base / "monsters.json"),
            items=_read_json(base / "items.json"),
            drops=_read_json(base / "drops.json"),
            maps=_read_json(base / "maps.json"),
            sieges=_read_json(base / "sieges.json"),
            mercenaries=_read_json(base / "mercenaries.json"),
            mines=_read_json(base / "mines.json"),
            upgrades=_read_json(base / "upgrades.json"),
            data_dir=base,
        )
        game.errors = game.validate() + game.cross_check()
        return game

    # ── 便捷访问 ────────────────────────────────────

    @property
    def exp_levels(self) -> dict[str, int]:
        return self.exp["levels"]

    def exp_to_next(self, level: int) -> int:
        """从 ``level`` 升到 ``level + 1`` 所需经验；表外用最后一档。"""
        levels = self.exp_levels
        raw = levels.get(str(level))
        if raw is not None:
            return int(raw)
        last = max(int(k) for k in levels)
        if level >= last:
            return int(levels[str(last)])
        return int(levels[str(min(max(1, level), last))])

    def max_level(self) -> int:
        return max(int(k) for k in self.exp_levels)

    def monster(self, monster_id: str) -> dict:
        return self.monsters["monsters"][monster_id]

    def item(self, item_id: str) -> dict:
        return self.items["items"][item_id]

    def skill(self, skill_id: str) -> dict:
        return self.skills["skills"][skill_id]

    def siege(self, siege_id: str) -> dict:
        return self.sieges["sieges"][siege_id]

    def merc(self, merc_id: str) -> dict:
        return self.mercenaries["mercenaries"][merc_id]

    def vein(self, vein_id: str) -> dict:
        return self.mines["veins"][vein_id]

    # ── 校验：结构层 ────────────────────────────────

    def validate(self) -> list[str]:
        errs: list[str] = []

        levels = self.exp_levels
        if not levels:
            errs.append("exp.json: levels 为空")
        else:
            keys = sorted(int(k) for k in levels)
            if keys != list(range(1, keys[-1] + 1)):
                errs.append(f"exp.json: 等级曲线不连续，缺 {sorted(set(range(1, keys[-1] + 1)) - set(keys))[:5]}")
            if any(int(v) < 0 for v in levels.values()):
                errs.append("exp.json: 存在负经验值")
            top_key = str(max(keys))
            for key, value in levels.items():
                if int(value) == 0 and key != top_key:
                    errs.append(f"exp.json: 仅封顶级允许 0 经验，{key} 级为 0")

        for cid, spec in self.classes["classes"].items():
            if cid not in CLASSES:
                errs.append(f"classes.json: 未知职业 id {cid!r}")
            for key in ("hp_base", "hp_per_level"):
                if key not in spec:
                    errs.append(f"classes.json[{cid}]: 缺字段 {key}")

        for sid, spec in self.skills["skills"].items():
            for key in ("name", "class", "level_req", "power_permille", "cooldown",
                        "cost_mp", "range", "kind"):
                if key not in spec:
                    errs.append(f"skills.json[{sid}]: 缺字段 {key}")
            if spec.get("class") not in CLASSES:
                errs.append(f"skills.json[{sid}]: 职业 {spec.get('class')!r} 不存在")
            if spec.get("kind") not in SKILL_KINDS:
                errs.append(f"skills.json[{sid}]: 未知 kind {spec.get('kind')!r}")
            if spec.get("kind", "").endswith("_aoe") or spec.get("kind") == "group_heal":
                if "aoe_radius" not in spec:
                    errs.append(f"skills.json[{sid}]: 范围类技能缺 aoe_radius")

        for mid, spec in self.monsters["monsters"].items():
            for key in ("name", "hp", "exp", "dc", "ac", "accuracy", "agility", "ai"):
                if key not in spec:
                    errs.append(f"monsters.json[{mid}]: 缺字段 {key}")
            if len(spec.get("dc", ())) != 2 or len(spec.get("ac", ())) != 2:
                errs.append(f"monsters.json[{mid}]: dc/ac 必须是 (下限, 上限)")

        for iid, spec in self.items["items"].items():
            kind = spec.get("kind", "equip")
            if kind not in ITEM_KINDS:
                errs.append(f"items.json[{iid}]: 未知 kind {kind!r}")
            if kind == "equip" and spec.get("slot") not in SLOTS:
                errs.append(f"items.json[{iid}]: 装备槽位非法 {spec.get('slot')!r}")
            # 特戒：``ring`` 槽只允许六种效果之一，且参数必须齐全。
            # 这条不放宽到"戒指可以不带效果"——一旦允许纯属性戒指，
            # "六戒各有且只有一个效果"就不再是事实，ring-effects 门禁也没法查。
            effect = spec.get("ring_effect")
            if spec.get("slot") == "ring":
                if effect is None:
                    errs.append(f"items.json[{iid}]: 戒指缺 ring_effect")
                elif effect not in RING_EFFECTS:
                    errs.append(f"items.json[{iid}]: 未知 ring_effect {effect!r}")
            elif effect is not None:
                errs.append(f"items.json[{iid}]: ring_effect 只能写在戒指上（当前槽 "
                            f"{spec.get('slot')!r}）")
            if effect is not None and effect in RING_EFFECTS:
                param = spec.get("ring_param")
                if not isinstance(param, dict):
                    errs.append(f"items.json[{iid}]: ring_effect 需要 ring_param 字典")
                else:
                    omitted = [k for k in RING_PARAM_KEYS[effect] if k not in param]
                    if omitted:
                        errs.append(f"items.json[{iid}]: ring_param 缺 {omitted}")
            # 拍卖行（P2-S5）：标了 ``auction`` 就是可寄卖，参数必须齐全。
            # 值域只查"是不是合法万分比/千分比"，而"底价必须高于商店回收价"
            # 这种跨模块的经济约束留给 ``auction-book`` 门禁（那里能读到
            # ``shop.SELL_DIVISOR``，口径只有一份）。
            param = spec.get("auction")
            if param is not None:
                if not isinstance(param, dict):
                    errs.append(f"items.json[{iid}]: auction 必须是字典")
                else:
                    demand = param.get("demand_bp")
                    if not isinstance(demand, int) or not 0 < demand <= 10000:
                        errs.append(
                            f"items.json[{iid}]: auction.demand_bp 必须是 (0, 10000] 的万分比")
                    floor = param.get("floor_permille")
                    if not isinstance(floor, int) or not 0 < floor <= 1000:
                        errs.append(
                            f"items.json[{iid}]: auction.floor_permille 必须是 (0, 1000] 的千分比")
            if kind == "book":
                teaches = spec.get("teaches")
                if teaches not in self.skills["skills"]:
                    errs.append(f"items.json[{iid}]: 技能书 teaches={teaches!r} 不在技能表")
                if spec.get("slot") is not None:
                    errs.append(f"items.json[{iid}]: 技能书不应有槽位 {spec.get('slot')!r}")
            if kind == "consumable":
                consume = spec.get("consume")
                if not isinstance(consume, dict):
                    errs.append(f"items.json[{iid}]: 消耗品缺 consume 字典")
                else:
                    effect_c = consume.get("effect")
                    if effect_c not in POTION_EFFECTS:
                        errs.append(
                            f"items.json[{iid}]: 未知 consume.effect {effect_c!r}")
                    amount_c = consume.get("amount")
                    if not isinstance(amount_c, int) or isinstance(amount_c, bool) \
                            or amount_c <= 0:
                        errs.append(f"items.json[{iid}]: consume.amount 必须为正整数")
                    cd_c = consume.get("cooldown_ticks")
                    if not isinstance(cd_c, int) or isinstance(cd_c, bool) or cd_c <= 0:
                        errs.append(
                            f"items.json[{iid}]: consume.cooldown_ticks 必须为正整数")
                if spec.get("slot") is not None:
                    errs.append(f"items.json[{iid}]: 消耗品不应有槽位 {spec.get('slot')!r}")
            if kind == "ore":
                # 矿石是挖出来的原料：有槽位会被 equip 收进候选里，
                # 能寄卖则会把"底价高于回收价"那条比值拉歪（它是量产物）
                if spec.get("slot") is not None:
                    errs.append(f"items.json[{iid}]: 矿石不应有槽位 {spec.get('slot')!r}")
                if spec.get("auction") is not None:
                    errs.append(f"items.json[{iid}]: 矿石不该能寄卖（只该进商店回收）")
                if int(spec.get("durability", -1)) != 0:
                    errs.append(f"items.json[{iid}]: 矿石没有耐久，durability 应为 0")
                if int(spec.get("weight", 0)) <= 0:
                    errs.append(f"items.json[{iid}]: 矿石必须有重量（否则能无限背）")
            # 纯度（P3-S4）：只能写在矿石上。它是 #25 升级武器的原料口径，
            # 值域 0-100 与"纯度 30/50/70/100 四个档位"是同一件事的两面
            purity = spec.get("purity")
            if purity is not None:
                if kind != "ore":
                    errs.append(f"items.json[{iid}]: purity 只能写在矿石上（当前 {kind}）")
                elif not isinstance(purity, int) or isinstance(purity, bool) \
                        or not 0 < purity <= 100:
                    errs.append(f"items.json[{iid}]: purity 必须是 (0, 100] 的整数")
            # 工具（P3-S4）：标记只能落在武器上——"拿在手上"这个语义
            # 只有 weapon 槽表达得了，挂在戒指上就等于"戴着就能挖"
            tool = spec.get("tool")
            if tool is not None:
                if tool not in TOOL_KINDS:
                    errs.append(f"items.json[{iid}]: 未知 tool {tool!r}")
                elif kind != "equip" or spec.get("slot") != "weapon":
                    errs.append(
                        f"items.json[{iid}]: 工具只能写在武器上（当前 {kind}/"
                        f"{spec.get('slot')!r}）")
            for key in ("name", "weight", "durability", "level_req"):
                if key not in spec:
                    errs.append(f"items.json[{iid}]: 缺字段 {key}")

        # 技能 ↔ 技能书双向必须对得上（单向校验会放过"书指向不存在的技能"）
        for sid, spec in self.skills["skills"].items():
            book_id = spec.get("book")
            if not book_id:
                errs.append(f"skills.json[{sid}]: 缺 book（学习所需技能书）")
                continue
            book = self.items["items"].get(book_id)
            if book is None:
                errs.append(f"skills.json[{sid}]: 技能书 {book_id!r} 不在物品表")
            elif book.get("teaches") != sid:
                errs.append(
                    f"技能书 {book_id!r} 教的是 {book.get('teaches')!r}，"
                    f"但 skills.json[{sid}].book 指向它")

        for mid, table in self.drops["drops"].items():
            if "gold" not in table:
                errs.append(f"drops.json[{mid}]: 缺 gold 区间")
            elif len(table["gold"]) != 2:
                errs.append(f"drops.json[{mid}]: gold 必须是 (下限, 上限)")

        for map_id, spec in self.maps["maps"].items():
            for key in ("name", "kind", "level_range", "spawns"):
                if key not in spec:
                    errs.append(f"maps.json[{map_id}]: 缺字段 {key}")
            if spec.get("kind") not in ("field", "dungeon", "town"):
                errs.append(f"maps.json[{map_id}]: 非法 kind {spec.get('kind')!r}")
            # 传送点必须落在图内——调小地图尺寸后最容易漏改的就是坐标
            width, height = spec.get("size", (0, 0))
            for portal in spec.get("portals", []):
                px, py = portal.get("at", (0, 0))
                if not (0 <= px < width and 0 <= py < height):
                    errs.append(
                        f"maps.json[{map_id}]: 传送点 {portal.get('name')!r} 坐标 "
                        f"({px}, {py}) 越界，地图尺寸 {width}x{height}"
                    )
                if portal.get("to") not in self.maps["maps"]:
                    errs.append(f"maps.json[{map_id}]: 传送目标 {portal.get('to')!r} 不存在")
            for spawn in spec.get("spawns", []):
                if int(spawn.get("count", 0)) <= 0:
                    errs.append(f"maps.json[{map_id}]: 刷新 {spawn.get('monster')!r} 数量非正")
                if int(spawn.get("respawn_seconds", 0)) <= 0:
                    errs.append(f"maps.json[{map_id}]: 刷新 {spawn.get('monster')!r} 间隔非正")
            # 矿点（P3-S4）：坐标在界内 + 不压传送点 + 矿脉存在 + 不重叠。
            # "不压传送点"这条不是洁癖：站在传送点上挖矿，PortalSystem 会在
            # 你刚站稳的那一 tick 把人换走，是个"站得住但留不住"的坑。
            seen_mines: set[tuple[int, int]] = set()
            for mine in spec.get("mines", []):
                mx, my = mine.get("at", (0, 0))
                if not (0 <= mx < width and 0 <= my < height):
                    errs.append(f"maps.json[{map_id}]: 矿点 ({mx}, {my}) 越界，"
                                f"地图尺寸 {width}x{height}")
                if (mx, my) in seen_mines:
                    errs.append(f"maps.json[{map_id}]: 矿点 ({mx}, {my}) 重复")
                seen_mines.add((mx, my))
                if mine.get("vein") not in self.mines.get("veins", {}):
                    errs.append(f"maps.json[{map_id}]: 矿脉 {mine.get('vein')!r} 不存在")
                for portal in spec.get("portals", []):
                    if tuple(portal.get("at", (0, 0))) == (mx, my):
                        errs.append(f"maps.json[{map_id}]: 矿点 ({mx}, {my}) 压在传送点 "
                                    f"{portal.get('name')!r} 上")

        # 攻城战役：结构层（波次/目标/坐标在图内）+ 奖励可解析
        for siege_id, spec in self.sieges.get("sieges", {}).items():
            map_id = spec.get("map")
            map_spec = self.maps["maps"].get(map_id or "")
            if map_spec is None:
                errs.append(f"sieges.json[{siege_id}]: 地图 {map_id!r} 不存在")
                continue
            width, height = map_spec.get("size", (0, 0))

            def _in_bounds(point, label: str) -> None:
                if not point or len(point) != 2:
                    errs.append(f"sieges.json[{siege_id}]: {label} 坐标缺失")
                    return
                if not (0 <= point[0] < width and 0 <= point[1] < height):
                    errs.append(f"sieges.json[{siege_id}]: {label} 坐标 {tuple(point)} "
                                f"越界，地图 {width}x{height}")

            _in_bounds(spec.get("player_spawn"), "player_spawn")
            waves = spec.get("waves") or []
            if not waves:
                errs.append(f"sieges.json[{siege_id}]: 没有任何波次")
            seen_banner = False
            for index, wave in enumerate(waves):
                tag = f"{siege_id}.waves[{index}]"
                if wave.get("objective") not in SIEGE_OBJECTIVES:
                    errs.append(f"sieges.json[{tag}]: 未知目标 {wave.get('objective')!r}")
                if int(wave.get("objective_hp", 0)) <= 0:
                    errs.append(f"sieges.json[{tag}]: objective_hp 必须为正")
                _in_bounds(wave.get("objective_at"), f"{tag}.objective_at")
                for guard in wave.get("guards", []):
                    if guard.get("monster") not in self.monsters["monsters"]:
                        errs.append(f"sieges.json[{tag}]: 守军 {guard.get('monster')!r} 不存在")
                    if int(guard.get("count", 0)) <= 0:
                        errs.append(f"sieges.json[{tag}]: 守军 {guard.get('monster')!r} 数量非正")
                    _in_bounds(guard.get("at"), f"{tag}.{guard.get('monster')}.at")
                reinforce = wave.get("reinforce")
                if reinforce:
                    if reinforce.get("monster") not in self.monsters["monsters"]:
                        errs.append(f"sieges.json[{tag}]: 增援 {reinforce.get('monster')!r} 不存在")
                    if int(reinforce.get("every_ticks", 0)) <= 0:
                        errs.append(f"sieges.json[{tag}]: 增援间隔必须为正")
                    if int(reinforce.get("max", 0)) <= 0:
                        errs.append(f"sieges.json[{tag}]: 增援上限必须为正")
                if wave.get("objective") == "banner":
                    seen_banner = True
            # 没有旗台的战役没有"胜利条件"，玩家会一直打下去
            if not seen_banner:
                errs.append(f"sieges.json[{siege_id}]: 缺少 objective=banner 的终局波次")
            rewards = spec.get("rewards") or {}
            for entry in rewards.get("items", []):
                if entry.get("item") not in self.items["items"]:
                    errs.append(f"sieges.json[{siege_id}]: 奖励物品 {entry.get('item')!r} 不存在")
            if int(spec.get("time_limit_ticks", 0)) <= 0:
                errs.append(f"sieges.json[{siege_id}]: time_limit_ticks 必须为正")

        # 佣兵（P2-S6）：字段与怪物表同构，多出雇佣相关的四个字段
        for merc_id, spec in self.mercenaries.get("mercenaries", {}).items():
            for key in ("name", "level", "hp", "dc", "ac", "mac", "accuracy",
                        "agility", "attack_speed", "ai",
                        "price", "duration_ticks", "level_req", "max"):
                if key not in spec:
                    errs.append(f"mercenaries.json[{merc_id}]: 缺字段 {key}")
            if spec.get("ai") not in AI_TIERS:
                errs.append(f"mercenaries.json[{merc_id}]: 未知 ai {spec.get('ai')!r}")
            for key in ("hp", "price", "duration_ticks", "level_req", "max"):
                if int(spec.get(key, 0)) <= 0:
                    errs.append(f"mercenaries.json[{merc_id}]: {key} 必须为正")
            if len(spec.get("dc", ())) != 2 or len(spec.get("ac", ())) != 2:
                errs.append(f"mercenaries.json[{merc_id}]: dc/ac 必须是 (下限, 上限)")

        # 矿脉（P3-S4）：产出表**一律万分比**（与 drops 的条目权重同尺度）。
        # 两套尺度（概率万分比 / 倍率千分比）在 P2 混过一次、代价是 10 倍误差，
        # 所以这里连名字都带 ``bp`` 后缀，条目权重和必须是 10000。
        for vid, spec in self.mines.get("veins", {}).items():
            for key in ("name", "yield_bp", "capacity", "regrow_ticks", "entries"):
                if key not in spec:
                    errs.append(f"mines.json[{vid}]: 缺字段 {key}")
            yield_bp = spec.get("yield_bp")
            if not isinstance(yield_bp, int) or isinstance(yield_bp, bool) \
                    or not 0 < yield_bp <= 10000:
                errs.append(f"mines.json[{vid}]: yield_bp 必须是 (0, 10000] 的万分比")
            for key in ("capacity", "regrow_ticks"):
                if int(spec.get(key, 0)) <= 0:
                    errs.append(f"mines.json[{vid}]: {key} 必须为正")
            entries = spec.get("entries") or []
            if not entries:
                errs.append(f"mines.json[{vid}]: 没有任何产出条目")
            total = 0
            for entry in entries:
                if entry.get("item") not in self.items["items"]:
                    errs.append(f"mines.json[{vid}]: 矿石 {entry.get('item')!r} 不在物品表")
                bp = entry.get("bp")
                if not isinstance(bp, int) or isinstance(bp, bool) or bp <= 0:
                    errs.append(
                        f"mines.json[{vid}]: 条目 {entry.get('item')!r} 的 bp 必须为正整数")
                total += int(bp or 0)
            if entries and total != 10000:
                errs.append(f"mines.json[{vid}]: 条目权重和为 {total}，应为 10000")

        # 打造（P3-S5）：升级档位表与祝福油参数。
        # 两套尺度在这里继续生效：**成功率是万分比**（``bp``），**金币是千分比**
        # （``gold_permille``）——P2 混过一次、代价是 10 倍误差，所以名字继续带后缀。
        up = self.upgrades.get("upgrade") or {}
        levels = up.get("levels") or []
        max_level = int(up.get("max_level", 0))
        if not levels:
            errs.append("upgrades.json: upgrade.levels 为空")
        if max_level <= 0:
            errs.append("upgrades.json: upgrade.max_level 必须为正")
        purity_bp = up.get("purity_bp") or {}
        for key, bp in purity_bp.items():
            if not str(key).isdigit():
                errs.append(f"upgrades.json: purity_bp 的键 {key!r} 必须是纯度数字")
            if not isinstance(bp, int) or isinstance(bp, bool) or not 0 < bp <= 10000:
                errs.append(f"upgrades.json: purity_bp[{key}] 必须是 (0, 10000] 的万分比")
        for index, row in enumerate(levels):
            for key in ("from", "to", "min_purity", "ore", "gold_permille"):
                if key not in row:
                    errs.append(f"upgrades.json: levels[{index}] 缺字段 {key}")
            if int(row.get("to", 0)) != int(row.get("from", 0)) + 1:
                errs.append(f"upgrades.json: levels[{index}] 必须是逐级 +1")
            if str(row.get("min_purity")) not in purity_bp:
                errs.append(
                    f"upgrades.json: levels[{index}] 的 min_purity "
                    f"{row.get('min_purity')!r} 不在 purity_bp 里")
            for key in ("ore", "gold_permille"):
                if int(row.get(key, 0)) <= 0:
                    errs.append(f"upgrades.json: levels[{index}] 的 {key} 必须为正")
        if levels and int(levels[-1].get("to", 0)) != max_level:
            errs.append("upgrades.json: 最后一档的 to 必须等于 max_level")
        bless = self.upgrades.get("blessing") or {}
        for key in ("base_bp", "step_bp", "min_bp", "max", "min"):
            if key not in bless:
                errs.append(f"upgrades.json: blessing 缺字段 {key}")
        if not 0 < int(bless.get("base_bp", 0)) <= 10000:
            errs.append("upgrades.json: blessing.base_bp 必须是 (0, 10000] 的万分比")
        for key in ("min_bp", "max"):
            if int(bless.get(key, 0)) <= 0:
                errs.append(f"upgrades.json: blessing.{key} 必须为正")
        if int(bless.get("min", 0)) >= 0:
            errs.append("upgrades.json: blessing.min 必须为负（诅咒要有下界）")

        return errs

    # ── 校验：引用层 ────────────────────────────────

    def cross_check(self) -> list[str]:
        errs: list[str] = []
        monsters = set(self.monsters["monsters"])
        items = set(self.items["items"])
        classes = set(self.classes["classes"])

        # 掉落表必须一一对应怪物，且掉落实体真实存在
        for mid in sorted(set(self.drops["drops"]) - monsters):
            errs.append(f"drops.json: 怪物 {mid!r} 不存在于 monsters.json")
        for mid, table in self.drops["drops"].items():
            for group in table.get("groups", []):
                for entry in group.get("entries", []):
                    iid = entry.get("item")
                    if iid not in items:
                        errs.append(f"drops.json[{mid}]: 物品 {iid!r} 不存在于 items.json")
                    if "permille" not in entry:
                        errs.append(f"drops.json[{mid}]: 条目 {iid!r} 缺 permille 权重")

        # 地图刷新表引用的怪物必须存在
        for map_id, spec in self.maps["maps"].items():
            for spawn in spec.get("spawns", []):
                if spawn.get("monster") not in monsters:
                    errs.append(f"maps.json[{map_id}]: 刷新怪物 {spawn.get('monster')!r} 不存在")

        # 技能职业必须已定义；装备职业限制必须合法
        for sid, spec in self.skills["skills"].items():
            if spec.get("class") not in classes:
                errs.append(f"skills.json[{sid}]: 职业 {spec.get('class')!r} 未在 classes.json 定义")
        for iid, spec in self.items["items"].items():
            for cid in spec.get("class", []):
                if cid not in classes:
                    errs.append(f"items.json[{iid}]: 职业限制 {cid!r} 未定义")

        # 掉落/地图/技能等级需求不能超过经验表上限
        top = self.max_level()
        for mid, spec in self.monsters["monsters"].items():
            if spec.get("level", 1) > top:
                errs.append(f"monsters.json[{mid}]: 等级 {spec.get('level')} 超过经验表上限 {top}")

        return errs

    @property
    def ok(self) -> bool:
        return not self.errors
