"""存档数据面 —— **唯一**一张"什么进档、什么不进档"的登记表。

整个 P3-S6 的地基不是读写代码，而是这张表。它把 ``mirsolo/components.py``
里的每一个组件显式归到四类中的一类：

``TRUTH``（真相）
    落盘。丢失即不可逆的东西：等级经验、背包仓库、金币、已学技能、
    图鉴击杀数、特戒剩余次数、拍卖挂单、以及三本**跨次数账本**
    （磨损 ``Wear`` / 升级 ``Upgrade`` / 祝福 ``Blessing`` / 死亡 ``DeathLedger``）。

``DERIVED``（派生）
    不落盘，读档后用 ``apply_growth`` + ``recompute`` 重算：六维属性、
    幸运合计、``max_hp`` / ``max_mp``、负重。存它等于把"裸身基准 + 装备加成"
    这份公式的结果再抄一份——两条事实描述同一件事，迟早分叉。

``RUNTIME``（运行时）
    不落盘，读档后清空：战斗中的护盾 / 中毒 / 隐身 / 麻痹 / 倒地、
    技能冷却（浮点秒）、挖矿进行时状态、佣兵与召唤物实体。
    它们描述的是"这一刻正在发生什么"，而读档 = 重新进图，此刻已经过去了。

为什么非要显式登记到"组件"粒度、而不是写个黑名单一刀切：**新增组件必须
被分类**。门禁 ``save-and-load`` 会遍历 ``components.py`` 里所有的
``Component`` 子类，任何一个没出现在这四张表里就直接红——于是"加了个新组件
忘了存"这件事在提交前就被拦住，而不是等到玩家关掉游戏才发现等级还在钱没了。

类型码（``codec`` 按它做严格 round-trip，存进去什么类型、出来还得是什么类型）：

* ``"i"`` int
* ``"s"`` str
* ``"b"`` bool
* ``"f"`` float（**只有坐标用**，存成定点字符串见 codec）
* ``"r"`` range —— ``(下限, 上限)``，落盘成 ``[lo, hi]``
* ``"m"`` map —— ``str -> int``
* ``"sm"`` str-map —— ``str -> str``（穿戴槽）
* ``"l"`` list —— 任意嵌套（拍卖挂单）
"""

from __future__ import annotations

#: 存档格式版本。**任何**影响读档结果的结构改动都必须 +1（不是"改了就 +1"，
#: 而是"旧档读不出来了才 +1"——纯加可选字段不该逼玩家重开档）
#:
#: P0-F3 加 ``siege_state`` 时按这条判据核过：它进的是
#: :data:`OPTIONAL_WORLD_KEYS`，缺键等价于"此刻没有攻城"，旧档照读，
#: 所以版本**保持在 1**。
SCHEMA_VERSION = 1

#: 存档文件后缀
SAVE_SUFFIX = ".json"

#: 真相组件：整体落盘（字段在 :data:`TRUTH_FIELDS` 里逐一列出）
TRUTH_COMPONENTS: tuple[str, ...] = (
    "Identity",
    "Position",
    "Progression",
    "Wallet",
    "Bag",
    "Warehouse",
    "Loadout",
    "Wear",
    "Upgrade",
    "Blessing",
    "DeathLedger",
    "Skills",
    "Codex",
    "RingState",
    "AuctionBook",
)

#: 派生组件：不落盘，读档后重算
DERIVED_COMPONENTS: tuple[str, ...] = (
    "Attributes",
    "Body",
)

#: 运行时组件：不落盘，读档后清空（有默认值就写回默认值）
RUNTIME_COMPONENTS: tuple[str, ...] = (
    "Cooldowns",
    "SummonOwner",
    "Shield",
    "Poison",
    "Invisible",
    "Downed",
    "Mining",
    "Paralysis",
    "Mercenary",
    "AiState",
    "SiegeObjective",
)

#: 部分落盘组件：只存其中几个字段，其余派生/运行时
PARTIAL_COMPONENTS: tuple[str, ...] = (
    "Vitals",      # 只存 hp / mp；max_* 由 apply_growth 重算
    "PotionState",  # 只存 ready_at；last 是表现层，读档清空
)

#: 每个组件落盘的字段与类型码（``(字段名, 类型码)`` 序列，顺序即写入顺序）
TRUTH_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "Identity": (
        ("name", "s"),
        ("kind", "s"),
        ("faction", "i"),
        ("species", "s"),
    ),
    "Position": (
        ("x", "f"),
        ("y", "f"),
        ("facing", "i"),
    ),
    "Vitals": (
        ("hp", "i"),
        ("mp", "i"),
    ),
    "Progression": (
        ("level", "i"),
        ("exp", "i"),
        ("class_id", "s"),
    ),
    "Wallet": (
        ("gold", "i"),
    ),
    "Bag": (
        ("capacity", "i"),
        ("items", "m"),
    ),
    "Warehouse": (
        ("capacity", "i"),
        ("items", "m"),
    ),
    "Loadout": (
        ("slots", "sm"),
    ),
    "Wear": (
        ("points", "m"),
    ),
    "Upgrade": (
        ("levels", "m"),
        ("attempts", "m"),
    ),
    "Blessing": (
        ("levels", "m"),
        ("attempts", "m"),
    ),
    "DeathLedger": (
        ("count", "i"),
        ("exp_lost", "i"),
        ("gold_lost", "i"),
        ("items_lost", "m"),
    ),
    "Skills": (
        ("known", "m"),
    ),
    "Codex": (
        ("kills", "m"),
    ),
    "RingState": (
        ("charges", "m"),
        ("cloak_at", "i"),
        ("last_travel_tick", "i"),
    ),
    "PotionState": (
        ("ready_at", "i"),
    ),
    "AuctionBook": (
        ("listings", "l"),
        ("next_seq", "i"),
        ("ask_tier", "i"),
    ),
}

#: 世界侧要落盘的数据键（其余一律重生）。
#: 缺任何一个 = 旧档读不出来 → 只能跟着 ``SCHEMA_VERSION`` 一起升。
WORLD_KEYS: tuple[str, ...] = (
    "seed",
    "current_map",
    "kill_counts",
    "entity_serial",
    "mining_veins",
)

#: 可选的世界数据键：**旧档里没有**，缺键取"等于没有这件事"的默认值，不报错。
#: 有了这一条，给世界段加"新出现的状态"就不必逼玩家重开档 ——
#: 缺 ``siege_state`` 的旧档读进来就是"此刻没有攻城"，这正是 ``SCHEMA_VERSION``
#: 保持在 1 的理由（见文件头那条注释的判据：旧档读得出来就不 +1）。
OPTIONAL_WORLD_KEYS: tuple[str, ...] = (
    "siege_state",
)

#: 刷怪系统的运行时状态键（系统内部字段，经 ``snapshot`` 导出）
SPAWN_STATE_KEY = "spawn_ticks"


def type_codes(component_name: str) -> tuple[tuple[str, str], ...]:
    """该组件落盘字段的 ``(字段名, 类型码)`` 序列；不落盘的组件返回空元组。"""
    return TRUTH_FIELDS.get(component_name, ())


def classified() -> tuple[str, ...]:
    """全部**已归类**的组件名（给门禁做差集用）。"""
    return (TRUTH_COMPONENTS + DERIVED_COMPONENTS
            + RUNTIME_COMPONENTS + PARTIAL_COMPONENTS)


def truth_component_names() -> tuple[str, ...]:
    """真正会写进存档文件的组件名（含部分落盘的）。"""
    return TRUTH_COMPONENTS + PARTIAL_COMPONENTS
