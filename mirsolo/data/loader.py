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
    data_dir: Path = field(default_factory=data_dir_default)
    errors: list[str] = field(default_factory=list)

    # ── 加载 ────────────────────────────────────────

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "GameData":
        """从目录加载七张表（缺一张直接抛 FileNotFoundError）。"""
        base = Path(data_dir) if data_dir is not None else data_dir_default()
        game = cls(
            exp=_read_json(base / "exp.json"),
            classes=_read_json(base / "classes.json"),
            skills=_read_json(base / "skills.json"),
            monsters=_read_json(base / "monsters.json"),
            items=_read_json(base / "items.json"),
            drops=_read_json(base / "drops.json"),
            maps=_read_json(base / "maps.json"),
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
            for key in ("name", "class", "level_req", "power_permille", "cooldown"):
                if key not in spec:
                    errs.append(f"skills.json[{sid}]: 缺字段 {key}")
            if spec.get("class") not in CLASSES:
                errs.append(f"skills.json[{sid}]: 职业 {spec.get('class')!r} 不存在")

        for mid, spec in self.monsters["monsters"].items():
            for key in ("name", "hp", "exp", "dc", "ac", "accuracy", "agility", "ai"):
                if key not in spec:
                    errs.append(f"monsters.json[{mid}]: 缺字段 {key}")
            if len(spec.get("dc", ())) != 2 or len(spec.get("ac", ())) != 2:
                errs.append(f"monsters.json[{mid}]: dc/ac 必须是 (下限, 上限)")

        for iid, spec in self.items["items"].items():
            if spec.get("slot") not in SLOTS:
                errs.append(f"items.json[{iid}]: 非法槽位 {spec.get('slot')!r}")
            for key in ("name", "weight", "durability", "level_req"):
                if key not in spec:
                    errs.append(f"items.json[{iid}]: 缺字段 {key}")

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
