"""场景构建 —— 把 ECS 世界翻译成"要画什么"，零 GL、零 pyglet。

这一层只产出**数据**（实例列表 + 瓦片配色），因此可以在没有显卡、
没有窗口的环境里单测：颜色是否确定、实例数是否与实体数一致、
排序是否稳定，全都能断言。

配色策略：**零外部素材**，颜色由 ``(地图 id, 格子坐标)`` / ``物种 id``
确定性哈希得到——同一张图每次进都是同一个色，但不同格子有细微差异，
避免纯色地板看起来像塑料。
"""

from __future__ import annotations

from dataclasses import dataclass

from mirsolo.components import Identity, Position, Vitals

RGBA = tuple[float, float, float, float]


def _hash01(seed: int, a: int, b: int) -> float:
    """把 (seed, a, b) 折成 [0,1) 的确定性小数（纯整数混合，无全局状态）。"""
    h = (seed * 0x9E3779B1) & 0xFFFFFFFF
    h ^= (a * 0x85EBCA77) & 0xFFFFFFFF
    h ^= (b * 0xC2B2AE3D) & 0xFFFFFFFF
    h = (h ^ (h >> 15)) * 0x2545F491 & 0xFFFFFFFF
    h ^= h >> 13
    return (h & 0xFFFFFF) / float(0x1000000)


def _mix(rgb: tuple[float, float, float], amount: float) -> tuple[float, float, float]:
    """整体提亮/压暗（amount > 0 提亮，< 0 压暗）。"""
    if amount >= 0:
        return tuple(min(1.0, c + (1.0 - c) * amount) for c in rgb)  # type: ignore[return-value]
    return tuple(max(0.0, c * (1.0 + amount)) for c in rgb)  # type: ignore[return-value]


#: 地图基调色（草地/洞穴/城镇），按 maps.json 的 kind 取
MAP_PALETTE = {
    "field": (0.24, 0.38, 0.20),
    "dungeon": (0.22, 0.20, 0.24),
    "town": (0.32, 0.28, 0.24),
}

#: 显式配色（便于辨识），未列出的物种走哈希调色
SPECIES_COLOR = {
    "ji": (0.92, 0.90, 0.70),        # 鸡：米白
    "lu": (0.72, 0.55, 0.32),        # 鹿：棕
    "daocaoren": (0.85, 0.72, 0.35),  # 稻草人：枯黄
    "kulou": (0.85, 0.85, 0.88),     # 骷髅：骨白
    "jiangshi": (0.42, 0.50, 0.40),  # 僵尸：腐绿
    "shiwang": (0.62, 0.16, 0.18),   # 尸王：暗红
}

PLAYER_COLOR = (0.28, 0.52, 0.95, 1.0)


@dataclass(frozen=True)
class Instance:
    """一个待绘制方块：位置 + 尺寸 + 颜色。

    ``z`` 是**底面高度**（脚下），渲染时按 ``sz`` 向上长，
    这样"站在地上"不需要额外算偏移。
    """

    x: float
    y: float
    z: float
    sx: float
    sy: float
    sz: float
    color: RGBA
    kind: str
    eid: str = ""

    @property
    def sort_key(self) -> tuple[float, float, str]:
        """绘制顺序：远的先画（x+y 小），同深度按 eid 稳定排序。"""
        return (self.x + self.y, self.z, self.eid)


def tile_color(map_id: str, kind: str, tx: int, ty: int) -> tuple[float, float, float]:
    """瓦片底色：基调 + 逐格哈希微扰（确定性）。"""
    base = MAP_PALETTE.get(kind, (0.30, 0.30, 0.30))
    seed = sum(map(ord, map_id)) & 0xFFFFFFFF
    noise = _hash01(seed, tx, ty)
    shade = (noise - 0.5) * 0.12  # ±6% 明暗
    return _mix(base, shade)


def species_color(species: str, level: int = 1) -> RGBA:
    """怪物颜色：白名单优先，其余按物种哈希取色并按等级提亮。"""
    if species in SPECIES_COLOR:
        rgb = SPECIES_COLOR[species]
    else:
        seed = sum(map(ord, species)) & 0xFFFFFFFF
        rgb = (0.35 + 0.5 * _hash01(seed, 1, 0),
               0.35 + 0.5 * _hash01(seed, 2, 0),
               0.35 + 0.5 * _hash01(seed, 3, 0))
    boost = min(0.25, max(0, level - 1) * 0.01)
    return (*_mix(rgb, boost), 1.0)


def build_instances(world, data, *, player_eid: str = "player") -> list[Instance]:
    """从世界快照出待绘制实例（玩家 + 存活的怪 + 尸体不画）。

    尺寸约定：人形 0.62×0.62×1.5；怪物按 ``monsters.json`` 的 ``size``
    缩放（size 1 → 0.6 见方，size 2 → 1.0 见方，高度略高于人）。
    """
    instances: list[Instance] = []
    for entity in world.each(Identity, Position, Vitals):
        ident = entity.get(Identity)
        pos = entity.get(Position)
        vitals = entity.get(Vitals)
        if not vitals.alive:
            continue
        if ident.kind == "player":
            instances.append(Instance(
                x=pos.x, y=pos.y, z=0.0,
                sx=0.62, sy=0.62, sz=1.45,
                color=PLAYER_COLOR, kind="player",
                eid=entity.eid or player_eid,
            ))
            continue
        if ident.kind != "monster":
            continue
        spec = data.monsters["monsters"].get(ident.species, {})
        size = float(spec.get("size", 1))
        footprint = 0.55 * size
        height = 1.1 * size
        instances.append(Instance(
            x=pos.x, y=pos.y, z=0.0,
            sx=footprint, sy=footprint, sz=height,
            color=species_color(ident.species, int(spec.get("level", 1))),
            kind="monster",
            eid=entity.eid or "",
        ))
    instances.sort(key=lambda i: i.sort_key)
    return instances


def map_floor_bounds(map_spec: dict) -> tuple[float, float]:
    """地面矩形尺寸（宽, 高），供地面着色器铺满。"""
    width, height = map_spec.get("size", (0, 0))
    return float(width), float(height)
