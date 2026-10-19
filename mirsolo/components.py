"""ECS 组件定义 —— mirsolo 的全部实体数据面。

组件一律用 ``@dataclass`` 继承 ``allin_engine.Component``（引擎推荐形态，
见 ``allin_engine/core/ecs.py`` 模块 docstring）。组件只存数据、不含逻辑；
行为全部落在 ``mirsolo/actor/systems.py`` 与 ``mirsolo/combat/`` 的纯函数里。

属性命名对齐传奇 1.76 的六维口径：
``dc`` 攻击 / ``mc`` 魔法 / ``sc`` 道术 / ``ac`` 防御 / ``mac`` 魔御，
每项都是 ``(下限, 上限)``。这是原版"攻击 0-30"这类写法的直接映射，
也是伤害公式里 roll 区间的来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from allin_engine import Component


@dataclass
class Identity(Component):
    """身份：显示名 + 实体种类 + 阵营 + 数据表 id。

    kind:
        ``player`` / ``monster`` / ``npc`` / ``summon``
    faction:
        ``0`` = 玩家方，``1`` = 怪物方（单机里阵营只用于选敌与仇恨）
    species:
        ``monsters.json`` 里的 id（如 ``"jiangshi"``）。**必须与 eid 分开**：
        ``World.entities`` 以 eid 为键，同种怪若共用 eid 会互相覆盖；
        掉落/经验/刷新表则一律按 species 查表。
    """

    name: str = ""
    kind: str = "monster"
    faction: int = 1
    species: str = ""


@dataclass
class Position(Component):
    """世界坐标（瓦片单位）。P0 用整数网格寻路 + 连续坐标表现。"""

    x: float = 0.0
    y: float = 0.0
    facing: int = 0  # 0..7，对应传奇八向


@dataclass
class Vitals(Component):
    """生命 / 魔法。``max_*`` 由成长系统在升级时重算。"""

    hp: int = 30
    max_hp: int = 30
    mp: int = 0
    max_mp: int = 0

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def heal(self, amount: int) -> int:
        """回血，返回实际恢复量（不超上限）。"""
        if amount <= 0:
            return 0
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp - before

    def take(self, amount: int) -> int:
        """扣血（不为负），返回实际扣除量。"""
        if amount <= 0:
            return 0
        before = self.hp
        self.hp = max(0, self.hp - amount)
        return before - self.hp


@dataclass
class Attributes(Component):
    """战斗属性六维 + 命中/敏捷/幸运，全部整数。

    区间一律 ``(下限, 上限)``；单值属性（准确/敏捷/幸运）用同值元组或裸 int 均可
    —— 这里统一用裸 int，避免读的时候再解包。
    """

    dc: tuple[int, int] = (1, 2)   # 攻击力
    mc: tuple[int, int] = (0, 0)   # 魔法力
    sc: tuple[int, int] = (0, 0)   # 道术
    ac: tuple[int, int] = (0, 0)   # 防御
    mac: tuple[int, int] = (0, 0)  # 魔御
    accuracy: int = 5              # 准确
    agility: int = 5               # 敏捷
    #: 幸运：来自**武器 + 项链**两个槽，由 ``equipment.recompute`` 求和并钳到
    #: ``formulas.LUCK_CAP``（9）。9 点 = 刀刀打上限。
    #: ⚠️ 这个值**不是**一级加成属性，它只在结算时收窄 roll 区间——
    #: 想改实际口径请去 ``formulas.lucky_range``，别在这里加开关。
    luck: int = 0


@dataclass
class Progression(Component):
    """等级与经验：``exp`` 是**本级已积累**经验，升级时扣除本级阈值。"""

    level: int = 1
    exp: int = 0
    class_id: str = "warrior"


@dataclass
class Body(Component):
    """体型/负重：影响可穿装备（重量）与拾取容量。"""

    weight: int = 0
    max_weight: int = 30


@dataclass
class Wallet(Component):
    """金币。传奇只有一种货币，单机也不引入第二货币。"""

    gold: int = 0


@dataclass
class Warehouse(Component):
    """仓库：``item_id -> 数量``。与背包分开计容量，存取都受各自上限约束。"""

    capacity: int = 80
    items: dict[str, int] = field(default_factory=dict)

    @property
    def used(self) -> int:
        return len(self.items)

    @property
    def full(self) -> bool:
        return self.used >= self.capacity


@dataclass
class Loadout(Component):
    """穿戴槽：``slot -> item_id``，未穿戴的槽不在字典里。"""

    slots: dict[str, str] = field(default_factory=dict)


@dataclass
class Wear(Component):
    """装备磨损账本：``item_id -> 已磨损点数``（P3-S1）。

    ⚠️ 为什么不挂在 :class:`Loadout` 上：和 ``RingState`` 同一个理由——
    ``Loadout.slots`` 是"当前穿着"的真相，每次穿脱都会被 ``recompute``
    全量重算；而磨损是**跨次数的累计账本**，跟着重算就等于每穿脱一次免费修一次。

    为什么按 ``item_id`` 而不是按"第几件实物"记：背包是堆叠的
    （``item_id -> 数量``），没有实物实例 id。按 id 记的语义是"这一类装备里
    最破的那件"，于是"打废了换一把同 id 的"洗不掉磨损——只有**花钱**
    （修理 / 卖掉再买一件新的）才洗得掉，正好堵住免费满耐久的空子。
    """

    points: dict[str, int] = field(default_factory=dict)


@dataclass
class Bag(Component):
    """背包：``item_id -> 数量`` 的有序字典（P0 不做堆叠上限）。"""

    capacity: int = 48
    items: dict[str, int] = field(default_factory=dict)

    @property
    def used(self) -> int:
        return len(self.items)

    @property
    def full(self) -> bool:
        return self.used >= self.capacity

    def add(self, item_id: str, count: int = 1) -> int:
        """放入物品，返回实际放入数量（背包满则 0 或部分）。"""
        if count <= 0:
            return 0
        if self.full and item_id not in self.items:
            return 0
        self.items[item_id] = self.items.get(item_id, 0) + count
        return count

    def remove(self, item_id: str, count: int = 1) -> int:
        """取出物品，返回实际取出数量。"""
        have = self.items.get(item_id, 0)
        take = min(have, max(0, count))
        if take == 0:
            return 0
        left = have - take
        if left:
            self.items[item_id] = left
        else:
            self.items.pop(item_id, None)
        return take


@dataclass
class Skills(Component):
    """已学技能：``skill_id -> 熟练度等级``（1 起）。"""

    known: dict[str, int] = field(default_factory=dict)


@dataclass
class Cooldowns(Component):
    """技能冷却剩余秒数：``skill_id -> 剩余``。"""

    remaining: dict[str, float] = field(default_factory=dict)

    def ready(self, skill_id: str) -> bool:
        return self.remaining.get(skill_id, 0.0) <= 0.0

    def tick(self, dt: float) -> None:
        """每帧递减（不删除 0 值，避免字典抖动影响确定性遍历顺序）。"""
        for key in list(self.remaining):
            left = self.remaining[key] - dt
            self.remaining[key] = left if left > 0.0 else 0.0


@dataclass
class SummonOwner(Component):
    """召唤物归属：击杀奖励（经验/掉落）记到主人头上，而不是随从身上。

    没有它，道士的骷髅打死怪会"掉落被吞掉"——随从没有背包和钱包，
    奖励结算到它身上等于凭空消失。
    """

    owner_id: int | None = None


@dataclass
class Shield(Component):
    """魔法盾：吸收伤害的护盾池（先扣盾、再扣血）。

    只在 ``CombatResolver`` 里被消耗——与"唯一改血处"同源，
    避免某个系统绕过结算直接改血。
    """

    current: int = 0
    max: int = 0

    def absorb(self, amount: int) -> tuple[int, int]:
        """吸收 ``amount``，返回 ``(盾吃掉的量, 漏到血上的量)``。"""
        if amount <= 0 or self.current <= 0:
            return 0, amount
        eaten = min(self.current, amount)
        self.current -= eaten
        return eaten, amount - eaten

    def refresh(self, amount: int) -> None:
        """重新结盾（取较大值，连续施法不叠加上限）。"""
        self.max = max(self.max, int(amount))
        self.current = max(self.current, int(amount))


@dataclass
class Poison(Component):
    """施毒：持续伤害状态。同一实体只保留最强/最新的那份毒。"""

    remaining_ticks: int = 0
    damage_per_tick: int = 1
    interval: int = 10  # 每 10 刻（1 秒）跳一次
    source: int | None = None  # 施毒者实体 id（给死亡奖励归因）


@dataclass
class Invisible(Component):
    """隐身：怪物在 ``until_tick`` 之前看不到该实体；主动出手即破。"""

    until_tick: int = -1


@dataclass
class Paralysis(Component):
    """麻痹：``until_tick`` 之前**不能移动、不能出手**（玩家与怪同一套）。

    为什么是独立组件而不是某个 ``is_stunned`` 标记：定身要同时掐掉移动与出手
    两个入口（``MovementSystem`` / ``MobChaseSystem`` / ``MobAttackSystem`` /
    ``AttackSystem``），散在各处的 ``if not paralyzed`` 迟早漏一个——
    而漏一个就等于"麻痹了却还在还手"，比没有麻痹更费解。
    """

    until_tick: int = -1
    source: int | None = None  # 施术者实体 id（给以后的仇恨/归属归因留位置）

    def active(self, tick: int) -> bool:
        return self.until_tick > tick

    def remaining(self, tick: int) -> int:
        return max(0, self.until_tick - tick)


@dataclass
class RingState(Component):
    """特戒的运行时记账（与装备槽解耦）。

    为什么不直接挂在 ``Loadout`` 上：``recompute`` 每次穿/脱都会把属性
    **全量重算**一遍（这是对的，见 equipment 模块 docstring），而"复活戒指还剩
    几次"属于跨次数的账本——跟着 recompute 重算就等于每次换装备白送一次复活。

    ``charges`` 按 ``item_id`` 记账而不是按"物品实例"：本项目的背包是
    ``item_id -> 数量``，没有实例 id，所以"同款两枚戒指"共用额度。
    这是单机化的已知取舍（记在 docs/数据口径.md）。
    """

    charges: dict[str, int] = field(default_factory=dict)
    cloak_at: int = -1               # 出手破隐后允许重新结隐的最早刻
    last_travel_tick: int = -10**9   # 传送戒指上次发动的刻（算冷却用）


@dataclass
class AuctionBook(Component):
    """拍卖行的挂单账本（P2-S5）。

    为什么挂单要单独一处、而不是"背包里标个记号"：**挂单即离手**是拍卖行的
    第四条约束（见 ``economy/auction.py``）——物品托管期间不能再穿、不能再卖，
    否则同一件装备会有两个去处。放在背包里标记，迟早有人写出
    "穿着身上还在挂单"的状态。

    ``next_seq`` 只增不减：它是撮合随机流名的一部分（``auction:{seq}@{round}``），
    一旦因为撤单而回退，两张不同的单会共用同一条随机流。
    """

    listings: list[dict] = field(default_factory=list)
    next_seq: int = 1
    #: 面板上的报价档位（速卖/常价/高价）。存组件而不是窗口状态：
    #: 换面板、关窗口都不该把玩家选好的档位冲掉。
    ask_tier: int = 0


@dataclass
class Codex(Component):
    """怪物图鉴（P2-S6）：``species -> 累计击杀数``。

    "解锁"就是"这个 key 存在"——不另存一份 ``unlocked`` 集合。
    两份事实描述同一件事，迟早会分叉（击杀了却没解锁 / 解锁了却显示 0 杀）。

    图鉴只记**击杀**，不记"见过"：见过没杀过的怪玩家拿不到任何信息，
    列在图鉴里只是一行灰字，反而让人以为自己漏了什么。
    """

    kills: dict[str, int] = field(default_factory=dict)

    @property
    def unlocked(self) -> int:
        return len(self.kills)


@dataclass
class Mercenary(Component):
    """佣兵标记（P2-S6）：雇佣来源 + 到期刻。

    佣兵实体本身复用怪物的全套组件（``Attributes``/``AiState``/``Vitals``），
    只是 ``Identity.kind == "summon"`` 且 ``faction == 0``——
    这样索敌、追击、出手、死亡清理全部走现有路径，不必另写一套 AI。
    本组件只带"这是谁雇的、雇到什么时候"这两个**非战斗**事实。
    """

    merc_id: str = ""
    until_tick: int = -1

    def expired(self, tick: int) -> bool:
        return self.until_tick <= tick


@dataclass
class SiegeObjective(Component):
    """攻城目标（城门 / 内城门 / 旗台）。

    刻意**不带 ``AiState``**：目标是被打的死物，不会追人、不会选敌。
    系统各自按 ``Identity.kind`` 过滤，所以这里只需要"我是哪一波的目标"。

    ``wave`` 是波次序号：非当前波次的目标不可打（``SiegeSystem`` 把它设成
    ``invulnerable``），这样玩家没法跳过外城直接砍旗台。
    """

    siege_id: str = ""
    wave: int = 0
    objective: str = "gate"  # gate / inner_gate / banner
    hp: int = 1
    max_hp: int = 1
    invulnerable: bool = False

    @property
    def destroyed(self) -> bool:
        return self.hp <= 0


@dataclass
class AiState(Component):
    """怪物 AI 的极小状态：目标实体 id + 仇恨值 + 上次攻击刻。

    两个半径必须分开，否则会出现"追-丢-回-追"的死循环：

    ``vision``
        索敌视野（切比雪夫格数）。玩家在视野外时**不建立仇恨**——
        原版怪不会隔着半张地图盯上你。
    ``leash``
        脱战距离（与出生点的直线距离）。超过即放弃目标并回巢。
    ``tier``
        AI 档位：``passive`` 不主动索敌（挨打才还手）/ ``aggressive`` 常规 /
        ``elite`` 教主（视野与脱战更大，靠数据表 ``ai`` 字段指定）。
    """

    target_id: int | None = None
    aggro: int = 0
    last_attack_tick: int = -10**9
    home: tuple[float, float] = (0.0, 0.0)
    leash: float = 12.0  # 脱离距离（超过则回巢）
    vision: float = 10.0  # 索敌视野（切比雪夫格数）
    tier: str = "aggressive"  # passive / aggressive / elite（来自 monsters.json 的 ai 字段）
