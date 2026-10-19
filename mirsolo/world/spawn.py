"""按地图刷新表铺怪 —— 开局与换图共用同一条路径。

抽出来是**为了不让两条路径分叉**：如果 ``populate()`` 用一套刷怪逻辑、
换图用另一套，那么"换图进去的怪"和"开局进去的怪"就会长得不一样，
而这种差异只会在很后面才被发现（"为什么这张图我上次来是 5 只"）。

随机流名固定为 ``populate:<map>:<monster>:<序号>``，所以**同一张图无论
第几次进入，怪的初始配置完全一致**——这是可复现的前提。
"""

from __future__ import annotations

from allin_engine import World

from mirsolo.actor.factory import spawn_monster
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng


def spawn_map_monsters(world: World, data: GameData, map_id: str, seed: int) -> list:
    """按 ``maps.json`` 的 ``spawns`` 铺一遍怪，返回新建实体列表。"""
    spec = data.maps["maps"].get(map_id)
    if spec is None:
        raise KeyError(f"未知地图 {map_id!r}")
    size = spec["size"]
    serial = 0
    spawned = []
    for spawn in spec.get("spawns", []):
        monster_id = spawn["monster"]
        for _ in range(int(spawn.get("count", 0))):
            serial += 1
            rng = Rng(seed, f"populate:{map_id}:{monster_id}:{serial}")
            x = rng.rand_int(0, int(size[0]) - 1)
            y = rng.rand_int(0, int(size[1]) - 1)
            spawned.append(spawn_monster(world, data, monster_id,
                                         x=float(x), y=float(y)))
    return spawned
