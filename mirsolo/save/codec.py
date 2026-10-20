"""存档编解码 —— 会话 ↔ 纯 JSON 字典。

三层分工（
:mod:`mirsolo.save.schema` 说"存什么"，
本模块说"怎么变"，:mod:`mirsolo.save.store` 说"放哪、怎么安全落地"）。

三条硬纪律，每条都配了回归：

1. **只存真相，派生重算**。``_load_player`` 先按 ``apply_growth`` 重建裸身基准，
   再 ``recompute`` 叠装备加成，最后才把存档里的当前血量**钳进**新上限。
   顺序反过来（先写 hp 再重算）会让上限变化把血顶掉——
   比如"穿着一件 +血装备时存档、读档时那件装备已被磨穿"。
2. **校验和用 FNV-1a，不用 ``hash()``**。Python 的 ``hash()`` 对 ``str``
   带每进程随机盐（``PYTHONHASHSEED``），同一个存档两次运行算出的校验和不同，
   这种"校验和"比没有还糟——它会把好档判成坏档。
3. **坐标用定点整数存**。``float`` 直接 ``json.dumps`` 在
   ``repr`` 四舍五入规则下跨平台一致，但本项目"零浮点不定性"的纪律不允许
   把浮点写进存档，所以坐标按 ``1/1000`` 定点折成整数（瓦片单位下
   0.001 格的精度远超任何玩法需要）。

``SCHEMA_VERSION`` 不符 → :class:`SaveError("bad_version")`；
校验和不符 → :class:`SaveError("bad_checksum")`。**绝不静默吞**：
坏档当成空档读，玩家丢的是一整局，而不是一条报错。
"""

from __future__ import annotations

from typing import Any

from allin_engine import Entity, World

from mirsolo.components import (
    AuctionBook,
    Attributes,
    Bag,
    Blessing,
    Body,
    Codex,
    Cooldowns,
    DeathLedger,
    Downed,
    Identity,
    Invisible,
    Loadout,
    Mining,
    Paralysis,
    Poison,
    Position,
    PotionState,
    Progression,
    RingState,
    Shield,
    Skills,
    Upgrade,
    Vitals,
    Wallet,
    Warehouse,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.determinism import fnv1a64
from mirsolo.items.equipment import recompute
from mirsolo.progression.exp import apply_growth
from mirsolo.save import schema
from mirsolo.skills.engine import reset_attack_pace, reset_cooldowns

#: 坐标定点精度：1/1000 瓦片
POS_SCALE = 1000

#: 会话里"玩家"的固定 eid（见 ``actor.factory.spawn_player``）
PLAYER_EID = "player"


class SaveError(ValueError):
    """存档读写失败。``code`` 是稳定的机器可读原因。

    码表（测试与 UI 都按它分支，**不要**按 message 猜）：

    ``bad_json``       文件不是合法 JSON（截断 / 手改坏 / 编码错）
    ``not_a_save``     是 JSON 但缺 ``kind`` 或 ``kind`` 不对（拿错文件）
    ``bad_version``    ``schema_version`` 与本程序不符
    ``bad_checksum``   内容被改过或写坏了
    ``missing_field``  合法 JSON、版本对、校验和对，但缺关键字段
    ``bad_type``       字段类型不对（例如 items 该是 dict 却是 list）
    ``no_player``      存档里没有玩家实体
    ``unknown_item``   存档引用了数据表里不存在的物品（数据表被改过）
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── 校验和 ──────────────────────────────────────────────

def _canonical(value: Any) -> str:
    """把任意 JSON 值折成**确定性**字符串（键排序、无空白、整数优先）。"""
    if isinstance(value, bool):
        return "b1" if value else "b0"
    if isinstance(value, int):
        return f"i{value}"
    if isinstance(value, float):
        # 存档里不该有裸浮点，但兜一层：折成定点整数，避免 repr 差异
        return f"i{int(round(value * POS_SCALE))}"
    if isinstance(value, str):
        # 长度前缀：不然 ["ab","c"] 与 ["a","bc"] 折出同一个串
        return f"s{len(value)}:{value}"
    if isinstance(value, dict):
        body = ",".join(f"{_canonical(str(k))}={_canonical(v)}"
                        for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
        return f"{{{body}}}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if value is None:
        return "n"
    raise SaveError("bad_type", f"无法编码的类型 {type(value).__name__}")


def checksum(payload: dict) -> str:
    """对 ``payload`` **除 checksum 外**的全部内容算 FNV-1a 64 位摘要（16 位十六进制）。

    刻意不用 ``hash()``：带随机盐，跨进程不一致（见模块 docstring 第 2 条）。
    """
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return f"{fnv1a64(_canonical(body)):016x}"


# ── 定点坐标 ────────────────────────────────────────────

def _pos_out(value: float) -> int:
    return int(round(float(value) * POS_SCALE))


def _pos_in(value: Any) -> float:
    return int(value) / POS_SCALE


# ── 字段编解码（按 schema 的类型码严格来） ──────────────

def _field_out(code: str, value: Any) -> Any:
    if code == "i":
        return int(value)
    if code == "s":
        return str(value)
    if code == "b":
        return bool(value)
    if code == "f":
        return _pos_out(value)
    if code == "r":
        return [int(value[0]), int(value[1])]
    if code in ("m", "sm"):
        return {str(k): (int(v) if code == "m" else str(v))
                for k, v in dict(value or {}).items()}
    if code == "l":
        return list(value or [])
    raise SaveError("bad_type", f"未知类型码 {code!r}")


def _field_in(code: str, value: Any) -> Any:
    if code == "i":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SaveError("bad_type", f"期望整数，得到 {type(value).__name__}")
        return value
    if code == "s":
        if not isinstance(value, str):
            raise SaveError("bad_type", f"期望字符串，得到 {type(value).__name__}")
        return value
    if code == "b":
        if not isinstance(value, bool):
            raise SaveError("bad_type", f"期望布尔，得到 {type(value).__name__}")
        return value
    if code == "f":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SaveError("bad_type", "坐标必须是定点整数")
        return _pos_in(value)
    if code == "r":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise SaveError("bad_type", "区间必须是 [lo, hi]")
        return (int(value[0]), int(value[1]))
    if code == "m":
        if not isinstance(value, dict):
            raise SaveError("bad_type", "期望字典")
        out: dict[str, int] = {}
        for key, item in value.items():
            if isinstance(item, bool) or not isinstance(item, int):
                raise SaveError("bad_type", f"{key!r} 的值不是整数")
            if item != 0:  # 0 条目不落盘：读档后"有 key 但数量为 0"是脏数据
                out[str(key)] = item
        return out
    if code == "sm":
        if not isinstance(value, dict):
            raise SaveError("bad_type", "期望字典")
        return {str(k): str(v) for k, v in value.items()}
    if code == "l":
        if not isinstance(value, list):
            raise SaveError("bad_type", "期望列表")
        return list(value)
    raise SaveError("bad_type", f"未知类型码 {code!r}")


# ── 导出 ────────────────────────────────────────────────

def _dump_component(entity: Entity, data: GameData, name: str) -> dict | None:
    comp = _component(entity, name)
    if comp is None:
        return None
    out: dict[str, Any] = {}
    for field_name, code in schema.type_codes(name):
        if not hasattr(comp, field_name):
            raise SaveError("missing_field", f"{name} 没有字段 {field_name}")
        out[field_name] = _field_out(code, getattr(comp, field_name))
    return out


#: 组件名 → 组件类（只在 codec 内部用；门禁比对时也用同一张表）
_COMPONENT_CLASSES: dict[str, type] = {
    "Identity": Identity,
    "Position": Position,
    "Vitals": Vitals,
    "Progression": Progression,
    "Wallet": Wallet,
    "Bag": Bag,
    "Warehouse": Warehouse,
    "Loadout": Loadout,
    "Wear": Wear,
    "Upgrade": Upgrade,
    "Blessing": Blessing,
    "DeathLedger": DeathLedger,
    "Skills": Skills,
    "Codex": Codex,
    "RingState": RingState,
    "PotionState": PotionState,
    "AuctionBook": AuctionBook,
    # 下面几个不落盘，但读档/清空时要用到
    "Attributes": Attributes,
    "Body": Body,
    "Cooldowns": Cooldowns,
    "Mining": Mining,
    "Shield": Shield,
    "Poison": Poison,
    "Invisible": Invisible,
    "Downed": Downed,
    "Paralysis": Paralysis,
}


def component_class(name: str) -> type | None:
    return _COMPONENT_CLASSES.get(name)


def _component(entity: Entity, name: str) -> Any:
    cls = _COMPONENT_CLASSES.get(name)
    return entity.get(cls) if cls is not None else None


def dump_world(session) -> dict:
    """世界侧的**计时真相**（不含实体）。

    为什么连 ``entity_serial`` 都要存：它只增不减，重开后从 0 起会让新怪
    复用旧 eid，``World.create_entity`` 遇到已存在 eid 会**静默覆盖**。
    为什么存 ``spawn_ticks``：不存的话读档后每个刷点都当"刚看到"，
    一整图怪要等各自 ``respawn_seconds`` 才慢慢回来——那是**给了**玩家
    一个"清场 → 存 → 读"就白刷一图的空子（虽然反而更不方便，但口径不一致
    本身就是缺陷）。
    """
    world: World = session.world
    out: dict[str, Any] = {}
    for key in schema.WORLD_KEYS:
        if key == "mining_veins":
            veins = world.get_data(key) or {}
            out[key] = {str(k): {
                "swings": int(v.get("swings", 0)),
                "total": int(v.get("total", 0)),
                "until": int(v.get("until", 0)),
            } for k, v in dict(veins).items()}
        elif key == "kill_counts":
            out[key] = {str(k): int(v) for k, v in dict(world.get_data(key) or {}).items()}
        elif key == "seed":
            out[key] = int(world.get_data(key, 0))
        elif key == "entity_serial":
            out[key] = int(world.get_data(key, 0))
        else:
            out[key] = str(world.get_data(key) or "")
    out["tick"] = int(world.get_data("tick", 0))
    # 刷怪系统的逐刷点计时（系统内部字段，只能这样拿）
    from mirsolo.actor.systems import RespawnSystem

    spawner = world.get_system(RespawnSystem)
    ticks: dict[str, int] = {}
    if spawner is not None:
        for (map_id, monster_id), at in spawner._last_spawn_tick.items():
            ticks[f"{map_id}|{monster_id}"] = int(at)
    serial = int(getattr(spawner, "_serial", 0)) if spawner is not None else 0
    out[schema.SPAWN_STATE_KEY] = {"last": ticks, "serial": serial}
    out[_SIEGE_KEY] = _dump_siege(world)
    return out


# ── 攻城战役状态（可选键，见 schema.OPTIONAL_WORLD_KEYS）──────
#: 与 ``siege.campaign.KEY_SIEGE`` 同值。这里写字面量而不是 import：
#: ``campaign`` 会 import ``world.travel``，而 ``codec`` 是存档层，
#: 不该为了一个键名把玩法侧的整条依赖链拉进来（导入在函数内做，见下）。
_SIEGE_KEY = schema.OPTIONAL_WORLD_KEYS[0]

#: ``SiegeState`` 的落盘字段 —— **逐个显式列出**，不用 ``dataclass.__dict__``：
#: ``__dict__`` 会把"以后新加的内部字段"悄悄带进存档，改了语义也没人发现。
#: 类型码：``i`` 整数 / ``s`` 字符串 / ``s?`` 字符串或 None。
#: 空行分组：前五项是"这一场战役是什么"，后三项是"打到哪了"。
_SIEGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("siege_id", "s"),
    ("map_id", "s"),
    ("wave", "i"),
    ("started_tick", "i"),
    ("status", "s"),

    ("objective_id", "s?"),   # None = 当前没有目标（终局态）
    ("last_reinforce_tick", "i"),
    ("reinforced", "i"),
)


def _dump_siege(world: World) -> dict | None:
    """把战役状态写成纯 JSON；没有战役就是 ``None``。

    ``objective_id`` 也存：它是**当前那一波**目标的 eid。目标实体本身不落盘
    （与怪物同一取舍），读档时按 ``wave`` 重铺——``spawn_objective`` 的 eid
    只由 ``map`` + ``wave`` 决定，所以重铺出来的 eid 与存档里的是同一个。
    """
    state = world.get_data(_SIEGE_KEY)
    if state is None:
        return None
    out: dict[str, Any] = {}
    for name, code in _SIEGE_FIELDS:
        value = getattr(state, name)
        if code == "s?":
            out[name] = None if value is None else str(value)
        elif code == "i":
            out[name] = int(value)
        else:
            out[name] = str(value)
    return out


def dump_player(session) -> dict:
    """玩家侧的真相（按 schema 的 TRUTH_FIELDS 逐字段写）。"""
    entity = session.world.get_entity(PLAYER_EID)
    if entity is None:
        raise SaveError("no_player", "会话里没有玩家实体")
    comps: dict[str, Any] = {}
    for name in schema.truth_component_names():
        blob = _dump_component(entity, session.data, name)
        if blob is not None:
            comps[name] = blob
    return comps


def dump_session(session, *, label: str = "", saved_at: str = "") -> dict:
    """把整局会话折成一份可写盘的字典（**不含** checksum，由 :func:`seal` 补）。"""
    payload = {
        "kind": "mirsolo-save",
        "schema_version": schema.SCHEMA_VERSION,
        "label": str(label),
        "saved_at": str(saved_at),
        "world": dump_world(session),
        "player": dump_player(session),
    }
    return payload


def seal(payload: dict) -> dict:
    """补上校验和（写盘前的最后一步）。"""
    body = {k: v for k, v in payload.items() if k != "checksum"}
    body["checksum"] = checksum(body)
    return body


def verify(payload: Any) -> dict:
    """校验一份读进来的字典；不合格直接抛 :class:`SaveError`，合格原样返回。

    检查顺序**刻意**是"便宜且能给出最有用提示的在前"：
    JSON 形状 → 版本 → 校验和 → 必填字段。版本不符时算校验和是浪费，
    而校验和要覆盖版本字段本身（否则改版本号能蒙过一堆旧档）。
    """
    if not isinstance(payload, dict):
        raise SaveError("not_a_save", "存档根节点不是对象")
    if payload.get("kind") != "mirsolo-save":
        raise SaveError("not_a_save", f"这个文件不是 mirsolo 存档（kind={payload.get('kind')!r}）")
    version = payload.get("schema_version")
    if not isinstance(version, int) or version != schema.SCHEMA_VERSION:
        raise SaveError("bad_version",
                        f"存档版本 {version!r} 与本程序 {schema.SCHEMA_VERSION} 不符"
                        "（旧档不能读，请开新档）")
    stored = payload.get("checksum")
    if not isinstance(stored, str):
        raise SaveError("bad_checksum", "存档没有校验和")
    actual = checksum(payload)
    if actual != stored:
        raise SaveError("bad_checksum", f"存档校验和不符（文件 {stored} / 实际 {actual}）")
    for key in ("world", "player"):
        if not isinstance(payload.get(key), dict):
            raise SaveError("missing_field", f"存档缺少 {key} 段")
    if not payload["player"]:
        raise SaveError("missing_field", "存档里玩家段是空的")
    return payload


# ── 导入 ────────────────────────────────────────────────

def _set_component(entity: Entity, name: str, blob: dict) -> None:
    """按 schema 字段表把 ``blob`` 写回组件（缺失组件就先补一个再写）。"""
    cls = _COMPONENT_CLASSES[name]
    comp = entity.get(cls)
    if comp is None:
        comp = cls()
        entity.add(comp)
    for field_name, code in schema.type_codes(name):
        if field_name not in blob:
            raise SaveError("missing_field", f"{name}.{field_name} 缺失")
        setattr(comp, field_name, _field_in(code, blob[field_name]))


def _clear_runtime(entity: Entity, world: World) -> None:
    """清空运行时组件：读档 = 重新进图，这一刻的战斗状态已经过去了。

    ``world`` 只为**攻击窗口**那本账（``KEY_ATTACK_PACE``，挂世界数据）：
    它记的是"上一次攻击动作是第几刻"，与技能冷却属于同一件事（出手的余韵），
    进图即清。只清冷却不清窗口的话，读档后第一刻放不出攻击型技能——
    同一个承诺（"进图即清冷却"）被劈成了两半。
    """
    # 技能冷却与换图走同一个函数（skills.engine.reset_cooldowns），
    # 两边各写一份清除逻辑迟早分叉——见 world/spawn.py 顶部。
    reset_cooldowns(entity)
    reset_attack_pace(world, entity)
    for name in ("Shield", "Poison", "Invisible", "Downed", "Paralysis"):
        cls = _COMPONENT_CLASSES.get(name)
        if cls is None:
            continue
        found = entity.get(cls)
        if found is not None:
            entity.remove(cls)
    mining_comp = entity.get(Mining)
    if mining_comp is not None:
        mining_comp.active = False
        mining_comp.vein = -1
        mining_comp.next_tick = 0
        mining_comp.last = ""
    potion = entity.get(PotionState)
    if potion is not None:
        potion.last = ""


def _check_item_refs(data: GameData, comps: dict) -> None:
    """存档里引用的物品必须还在数据表里。

    不检查的话，读档后会得到一个"背包里有一件不存在的东西"的世界：
    ``recompute`` 里 ``spec is None`` 会静默跳过（那是防御、不是许可），
    面板上会显示成一行空名字。数据表被改过就该明确拒读。
    """
    known = data.items["items"]
    seen: set[str] = set()
    for key in ("Bag", "Warehouse"):
        seen.update((comps.get(key) or {}).get("items", {}).keys())
    seen.update((comps.get("Loadout") or {}).get("slots", {}).values())
    seen.update((comps.get("Wear") or {}).get("points", {}).keys())
    seen.update((comps.get("Upgrade") or {}).get("levels", {}).keys())
    seen.update((comps.get("Blessing") or {}).get("levels", {}).keys())
    seen.update((comps.get("DeathLedger") or {}).get("items_lost", {}).keys())
    for listing in (comps.get("AuctionBook") or {}).get("listings", []) or []:
        item_id = listing.get("item") if isinstance(listing, dict) else None
        if item_id:
            seen.add(str(item_id))
    unknown = sorted(item_id for item_id in seen if item_id not in known)
    if unknown:
        raise SaveError("unknown_item", f"存档引用了数据表里没有的物品：{unknown}")


def load_player(session, comps: dict) -> Entity:
    """把玩家段落回会话（**先派生、后真相、最后钳血**）。"""
    entity = session.world.get_entity(PLAYER_EID)
    if entity is None:
        raise SaveError("no_player", "会话里没有玩家实体（请先 populate）")
    if not comps:
        raise SaveError("missing_field", "玩家段为空")
    _check_item_refs(session.data, comps)

    # 1) 先把真相写回去（等级/职业/穿戴/背包/账本…）
    for name in schema.truth_component_names():
        blob = comps.get(name)
        if blob is None:
            if name in ("Identity", "Progression"):
                raise SaveError("missing_field", f"存档缺少 {name} 段")
            continue
        _set_component(entity, name, blob)

    # 2) 再重算派生：先裸身基准（apply_growth 会顺手把血蓝拉满），
    #    再叠装备加成（recompute 会把血蓝钳进上限）
    prog = entity.get(Progression)
    class_spec = session.data.classes["classes"].get(prog.class_id)
    if class_spec is None:
        raise SaveError("unknown_item", f"存档里的职业 {prog.class_id!r} 不在数据表里")
    apply_growth(entity, class_spec, heal_on_level_up=True)
    recompute(entity, session.data)

    # 3) 最后才把存档里的当前血蓝钳进去：上限此刻已经是**正确**的了，
    #    这一步只把"存盘时的血"还回来，不改变上限
    saved = comps.get("Vitals") or {}
    vitals = entity.get(Vitals)
    if vitals is not None:
        vitals.hp = max(0, min(int(saved.get("hp", vitals.max_hp)), vitals.max_hp))
        vitals.mp = max(0, min(int(saved.get("mp", vitals.max_mp)), vitals.max_mp))
        if vitals.hp <= 0:
            # 存档时还活着，读档却 0 血 = 数据不一致；给 1 点，别造出一个"读档即倒地"
            vitals.hp = 1
    _clear_runtime(entity, session.world)
    return entity


def load_world(session, blob: dict) -> None:
    """把世界段落回会话，并清场重铺怪（与 ``travel`` 走同一条铺怪路径）。

    必选键（``schema.WORLD_KEYS``）缺了就报 ``missing_field``；可选键
    （``schema.OPTIONAL_WORLD_KEYS``）缺了取默认值 —— 旧档能读，见 schema 里
    ``SCHEMA_VERSION`` 那段注释。
    """
    world = session.world
    for key in schema.WORLD_KEYS:
        if key not in blob:
            raise SaveError("missing_field", f"世界段缺少 {key}")
        value = blob[key]
        if key == "mining_veins":
            world.set_data(key, {str(k): {
                "swings": int(v.get("swings", 0)),
                "total": int(v.get("total", 0)),
                "until": int(v.get("until", 0)),
            } for k, v in dict(value).items()})
        elif key == "kill_counts":
            world.set_data(key, {str(k): int(v) for k, v in dict(value).items()})
        else:
            world.set_data(key, value)

    world.set_data("tick", int(blob.get("tick", 0)))
    session.map_id = str(blob.get("current_map") or session.map_id)
    world.set_data("current_map", session.map_id)
    session.seed = int(blob.get("seed", session.seed))

    # 清场：怪物/佣兵/召唤物一律移除（它们的状态不落盘）
    for entity in list(world.each_entity()):
        ident = entity.get(Identity)
        if ident is not None and ident.kind == "player":
            continue
        world.remove_entity(entity.id)

    from mirsolo.actor.systems import RespawnSystem
    from mirsolo.world.spawn import spawn_map_monsters

    spawn_map_monsters(world, session.data, session.map_id, session.seed)
    spawn_state = blob.get(schema.SPAWN_STATE_KEY) or {}
    spawner = world.get_system(RespawnSystem)
    if spawner is not None:
        spawner._last_spawn_tick = {}
        for packed, at in dict(spawn_state.get("last") or {}).items():
            map_id, _, monster_id = str(packed).partition("|")
            spawner._last_spawn_tick[(map_id, monster_id)] = int(at)
        spawner._serial = int(spawn_state.get("serial", 0))

    # 战役状态最后落：它要按当前波次重铺目标/守军，必须排在清场与铺怪之后
    _load_siege(session, blob.get(_SIEGE_KEY))


def _load_siege(session, raw) -> None:
    """把战役状态写回世界；``None`` = 此刻没有战役（旧档缺键也走这里）。

    目标与守军实体**不逐只落盘**（与怪物同一取舍），按存档里的 ``wave``
    重铺一波：``spawn_objective`` 的 eid 只由 ``map`` + ``wave`` 决定，
    ``spawn_wave_guards`` 的坐标只由 ``Rng(seed, "siege:<map>:<wave>:<serial>")``
    决定 —— 所以重铺出来的一波与存档里那一波逐只相同，不依赖 int id。
    """
    from mirsolo.siege import campaign

    world = session.world
    if not isinstance(raw, dict):
        # 缺键（旧档）或明确的 null（已结束/没有战役）
        world.set_data(_SIEGE_KEY, None)
        return
    values: dict[str, Any] = {}
    for name, code in _SIEGE_FIELDS:
        if name not in raw:
            raise SaveError("missing_field", f"战役段缺少 {name}")
        value = raw[name]
        if code == "s?":
            values[name] = None if value is None else str(value)
        elif code == "i":
            try:
                values[name] = int(value)
            except (TypeError, ValueError):
                raise SaveError("bad_type", f"战役段 {name} 不是整数") from None
        else:
            values[name] = str(value)
    state = campaign.SiegeState(**values)
    world.set_data(_SIEGE_KEY, state)

    spec = session.data.sieges["sieges"].get(state.siege_id)
    if spec is None or state.finished:
        return  # 数据表里没这场战役 / 已经赢了输了：不留实体，状态照读
    objective = campaign.spawn_objective(world, session.data, spec, state.wave,
                                         eid=state.objective_id)
    campaign.spawn_wave_guards(world, session.data, spec, state.wave,
                               seed=session.seed)
    if state.objective_id is None:
        state.objective_id = objective.eid


def load_session(session, payload: dict) -> dict:
    """校验并整体应用一份存档，返回摘要（给 UI 回音用）。"""
    verify(payload)
    load_world(session, payload["world"])
    load_player(session, payload["player"])
    return summary(payload)


def summary(payload: dict) -> dict:
    """存档摘要（面板/CLI 列表用，不依赖会话）。"""
    player = payload.get("player") or {}
    world = payload.get("world") or {}
    prog = player.get("Progression") or {}
    wallet = player.get("Wallet") or {}
    pos = player.get("Position") or {}
    return {
        "label": payload.get("label", ""),
        "saved_at": payload.get("saved_at", ""),
        "schema_version": payload.get("schema_version"),
        "level": int(prog.get("level", 0)),
        "exp": int(prog.get("exp", 0)),
        "class_id": str(prog.get("class_id", "")),
        "gold": int(wallet.get("gold", 0)),
        "map": str(world.get("current_map", "")),
        "tick": int(world.get("tick", 0)),
        "x": _pos_in(pos.get("x", 0)),
        "y": _pos_in(pos.get("y", 0)),
        "bag_count": sum(int(v) for v in (player.get("Bag") or {}).get("items", {}).values()),
        "deaths": int((player.get("DeathLedger") or {}).get("count", 0)),
    }
