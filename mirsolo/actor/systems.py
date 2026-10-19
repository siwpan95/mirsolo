"""ECS 系统 —— 把战斗/成长/掉落接到 ``World.update()`` 的固定步长上。

系统清单（按 priority 从小到大执行）：

| priority | 系统 | 职责 |
|---:|---|---|
| 5 | ``RegenSystem`` | 每刻回血回蓝（静止加成，战斗中断） |
| 6 | ``AuctionSystem`` | 拍卖行撮合（每 ``CHECK_TICKS`` 刻结算一轮挂单） |
| 7 | ``MercenarySystem`` | 佣兵到期解散（``Mercenary.until_tick`` 到点即离场） |
| 10 | ``CooldownSystem`` | 每帧递减技能冷却 |
| 11 | ``AiTargetSystem`` | 给主动怪选目标（同阵营跳过、超脱战距离放弃） |
| 12 | ``MovementSystem`` | 玩家八向网格移动（读 move_intent，按刻落格） |
| 13 | ``StatusSystem`` | 持续状态结算（中毒掉血 / 隐身到期） |
| 14 | ``MobChaseSystem`` | 怪物追击（切比雪夫，八向一格） |
| 20 | ``RingEffectSystem`` | 特戒持续效果（隐身戒指结隐；摘下即自然失效） |
| 28 | ``MobAttackSystem`` | 相邻怪按自身攻速排队出刀 |
| 30 | ``AttackSystem`` | 消费攻击意图队列 → 调 ``CombatResolver`` 结算 |
| 40 | ``DeathRewardSystem`` | 结算死亡奖励：经验 + 掉落 + 金币 |
| 41 | ``CodexSystem`` | 图鉴记账 + 首次解锁奖励（订阅死亡事件） |
| 42 | ``DeathPenaltySystem`` | 玩家倒地 → 结算死亡惩罚 → 复活（回城/原地） |
| 90 | ``RespawnSystem`` | 按地图刷新表补刷（P0 用固定 tick 间隔） |

**意图队列**是这一层的核心设计：AI/输入只往 ``world.get_data("attack_intents")``
里追加意图，由 ``AttackSystem`` 统一结算。好处是"谁先动手"不影响结果——
结算顺序由队列顺序 + tick 决定，回放可复算。
"""

from __future__ import annotations

from allin_engine import Entity, System, World

from mirsolo.combat.resolver import EVT_DEATH, CombatContext, CombatResolver
from mirsolo.data.loader import GameData
from mirsolo.components import (
    AiState,
    AuctionBook,
    Attributes,
    Bag,
    Codex,
    Cooldowns,
    DeathLedger,
    Downed,
    Identity,
    Invisible,
    Loadout,
    Mercenary,
    Paralysis,
    Poison,
    Position,
    Progression,
    RingState,
    SummonOwner,
    Vitals,
    Wallet,
    Wear,
)
from mirsolo.actor import codex, mercenary, penalty
from mirsolo.determinism import Rng
from mirsolo.economy import auction
from mirsolo.items import rings
from mirsolo.items import wear
from mirsolo.items.drops import roll_drops
from mirsolo.items.equipment import recompute
from mirsolo.progression.exp import add_exp, apply_growth

KEY_TICK = "tick"
KEY_INTENTS = "attack_intents"
KEY_KILL_COUNTS = "kill_counts"
KEY_REWARDS = "reward_queue"
KEY_MOVE_INTENT = "move_intent"
#: 玩家刚倒地、正等着决定怎么起来（表现层靠它弹"按 R"提示）
KEY_DOWNED = "last_down"
#: 上一次死亡的结算结果（经验/金币/物品/磨损），表现层弹提示用
KEY_LAST_DEATH = "last_death"

#: 佣兵到期离队（字符串与 ``mercenary.EVT_DISMISS`` 同一个，这里导入避免系统与规则层互相 import）
EVT_MERC_DISMISS = mercenary.EVT_DISMISS


def resolve_owner(world: World, killer: Entity) -> Entity:
    """召唤物/佣兵的击杀 → 归属到主人；没有归属或主人已不在场则原样返回。

    抽成一个函数是因为**两个系统都要用**：``DeathRewardSystem`` 发掉落、
    ``CodexSystem`` 记图鉴。各写一份的话，以后加了"第二层归属"（比如佣兵
    带着自己的召唤物）就只有一处会跟着改，另一处静默把奖励算错人。
    """
    ref = killer.get(SummonOwner)
    if ref is None or ref.owner_id is None:
        return killer
    owner = world.get_entity(ref.owner_id)
    return owner if owner is not None else killer


def grant_exp(world: World, data: GameData, entity: Entity, amount: int,
              tick: int | None = None) -> int:
    """加经验的**唯一**入口（升级 → 成长 → 重算装备 → 写升级提示）。

    升级这一步必须连做三件事，漏一件都会出错：

    * ``apply_growth`` 重算覆盖面（不重算就永远停在 1 级属性）；
    * ``recompute`` 重算装备加成（不重算等于升一级把武器丢了）；
    * ``last_level_up`` 写世界状态（表现层靠它弹提示）。

    死亡奖励与图鉴解锁都走这里，所以"哪一路给的经验"不会有两种升级行为。
    返回实际发出的经验（``Progression`` 缺失或数量非正时为 0）。
    """
    prog = entity.get(Progression)
    if prog is None or amount <= 0:
        return 0
    result = add_exp(data, prog, int(amount))
    if result.levels_gained:
        class_spec = data.classes["classes"][prog.class_id]
        apply_growth(entity, class_spec)
        recompute(entity, data)
        world.set_data("last_level_up", {
            "entity": entity.id,
            "level": prog.level,
            "tick": tick,
        })
    return int(amount)


def facing_from_delta(dx: int, dy: int) -> int:
    """八向朝向编号：0=北(-Y)，顺时针每 45° 加 1。

    与传奇客户端一致（北 = 0），因此渲染里"人物面向"可以直接用这个值。
    """
    if dx == 0 and dy == 0:
        return 0
    if dy > 0 and dx == 0:
        return 0
    if dy > 0 and dx > 0:
        return 1
    if dx > 0 and dy == 0:
        return 2
    if dx > 0 and dy < 0:
        return 3
    if dy < 0 and dx == 0:
        return 4
    if dy < 0 and dx < 0:
        return 5
    if dx < 0 and dy == 0:
        return 6
    return 7


class MovementSystem(System):
    """玩家八向网格移动：每 ``step_ticks`` 刻走一格。

    输入不直接改坐标，而是写进 ``world.set_data("move_intent", (dx, dy))``，
    由本系统按刻度统一落格——这样"按住方向键"不会因为帧率不同而走得更快，
    headless 与窗口版也永远一致。
    """

    priority = 12

    def __init__(self, step_ticks: int = 4) -> None:
        self.step_ticks = max(1, step_ticks)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.step_ticks:
            return
        intent = world.get_data(KEY_MOVE_INTENT) or (0, 0)
        dx, dy = int(intent[0]), int(intent[1])
        if dx == 0 and dy == 0:
            return
        player = world.get_entity("player")
        if player is None:
            return
        pos = player.get(Position)
        if pos is None:
            return
        if _is_paralyzed(player, tick):
            return  # 被麻痹：WASD 一样不动（不是"走半格"，那样看不出是麻痹）
        map_id = world.get_data("current_map")
        data = world.get_data("data")
        spec = data.maps["maps"].get(map_id or "", {}) if data is not None else {}
        width, height = spec.get("size", (10**9, 10**9))
        pos.x = max(0, min(width - 1, pos.x + dx))
        pos.y = max(0, min(height - 1, pos.y + dy))
        pos.facing = facing_from_delta(dx, dy)


class MobChaseSystem(System):
    """怪物追击：锁定目标后每 ``step_ticks`` 刻走一格（切比雪夫，八向）。

    与原版一致：怪不会穿墙（P0 地图无墙，P1 接入地牢时这里换成 A*）。
    """

    priority = 14

    def __init__(self, step_ticks: int = 6) -> None:
        self.step_ticks = max(1, step_ticks)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.step_ticks:
            return
        for mob in world.each(AiState, Position):
            state = mob.get(AiState)
            pos = mob.get(Position)
            if _is_paralyzed(mob, tick):
                continue  # 被麻痹的怪钉在原地（但仍能被打、仍会计仇恨）
            goal: Position | None = None
            if state.target_id is not None:
                target = world.get_entity(state.target_id)
                if target is not None:
                    goal = target.get(Position)
                else:
                    state.target_id = None
            # 脱战回巢：没有目标就走回出生点，否则怪会"追一半卡在原地"
            if goal is None:
                hx, hy = state.home
                if int(hx) == int(pos.x) and int(hy) == int(pos.y):
                    continue
                goal = Position(hx, hy)
            dx = int(max(-1, min(1, goal.x - pos.x)))
            dy = int(max(-1, min(1, goal.y - pos.y)))
            if dx == 0 and dy == 0:
                continue
            pos.x += dx
            pos.y += dy
            pos.facing = facing_from_delta(dx, dy)


class MobAttackSystem(System):
    """怪物反击：相邻即按自身攻速排队出刀（与玩家共用 ``AttackSystem`` 结算）。

    为什么怪物也要走意图队列而不是直接调 resolver：
    这样"同一刻玩家和怪同时出手"的先后顺序由队列决定，可回放、可复算。
    """

    priority = 28

    def __init__(self, data) -> None:
        self.data = data

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0:
            return
        intents = list(world.get_data(KEY_INTENTS, []))
        for mob in world.each(AiState, Position, Identity, Vitals):
            ident = mob.get(Identity)
            # 目标（guard）不还手：它没有 AiState，压根进不了这条分支
            if ident.kind not in ("monster", "summon"):
                continue
            state = mob.get(AiState)
            if state.target_id is None:
                continue
            if _is_paralyzed(mob, tick):
                continue  # 定身期间不还手（意图压根不入队，见 AttackSystem 注释）
            target = world.get_entity(state.target_id)
            if target is None:
                continue
            tvitals = target.get(Vitals)
            # 目标已经倒地（玩家等着被复活）就不再排队出刀：否则怪物会围着
            # 尸体一直挥，伤害流水被搅乱，复活之后也可能被残留意图再打一下
            if tvitals is not None and not tvitals.alive:
                state.target_id = None
                continue
            tpos = target.get(Position)
            pos = mob.get(Position)
            if max(abs(tpos.x - pos.x), abs(tpos.y - pos.y)) > 1:
                continue
            spec = self.data.monsters["monsters"].get(ident.species, {})
            interval = max(2, 12 - int(spec.get("attack_speed", 3)))
            if tick - state.last_attack_tick < interval:
                continue
            state.last_attack_tick = tick
            intents.append({"attacker": mob, "defender": target,
                            "kind": "physical", "power_permille": 1000})
        world.set_data(KEY_INTENTS, intents)


class AuctionSystem(System):
    """拍卖行撮合：每 ``auction.CHECK_TICKS`` 刻结算一轮挂单（P2-S5）。

    priority 6 = 回血之后、战斗之前：撮合只动金币与挂单账本，
    不碰任何战斗状态，早一刻晚一刻都不影响本刻伤害。
    放在这批"周期性维护"里，是为了让读代码的人不必去猜它躲在哪。

    ``seed`` 由装配层给，流名 ``auction:{seq}@{round}`` 只含挂单序号与轮次，
    因此**多挂/少挂一张单不会挪动别的单的随机数位置**——
    与特戒、幸运 9 是同一条回放纪律。
    """

    priority = 6

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % auction.CHECK_TICKS:
            return
        data = world.get_data("data")
        if data is None:
            return
        bus = world.get_data("bus")
        for entity in world.each(AuctionBook):
            if not entity.get(AuctionBook).listings:
                continue
            auction.settle(entity, data, seed=self.seed, tick=tick, bus=bus)


class MercenarySystem(System):
    """佣兵到期解散：``Mercenary.until_tick`` 到点就把实体移出场（P2-S6）。

    priority 7 = 战斗（28/30）**之前**：到期的人不该在这一刻还挥一刀。
    放在这一档的"周期性维护"里，与 ``AuctionSystem`` 同批——两者都只按
    ``tick`` 判断、都不碰战斗状态，读代码的人不必去猜它躲在哪。

    为什么到点是**直接移除**而不是"留个尸体"：佣兵不是怪物，没有掉落也没有
    刷新表；留尸体只能靠 ``DeathRewardSystem`` 的 summon 分支清掉——那要等它
    真的被打死才触发，到期解散根本走不到那条路，尸体就会永远留在场上。
    """

    priority = 7

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0:
            return
        bus = world.get_data("bus")
        data = world.get_data("data")
        # 必须先把快照取出来再删：world.each 是**生成器**，直接边遍历边
        # remove_entity 会 RuntimeError（字典在迭代中被改大小）——实测踩到。
        for entity in list(world.each(Mercenary)):
            merc = entity.get(Mercenary)
            if not merc.expired(tick):
                continue
            payload = {
                "entity": entity.id,
                "merc_id": merc.merc_id,
                "name": mercenary.dismiss_name(data, merc.merc_id) if data is not None else merc.merc_id,
                "tick": tick,
            }
            world.remove_entity(entity.id)
            if bus is not None:
                bus.emit(EVT_MERC_DISMISS, payload)


class RegenSystem(System):
    """自然回复：存活时每 ``interval`` 刻回 1 点 HP / MP。

    传奇的回血是按固定间隔跳的（不是每秒百分比），所以这里也用整数刻度，
    不做 ``dt`` 插值——插值会让"同一 tick 的存档"出现小数血，回放不干净。
    """

    priority = 5

    def __init__(self, interval: int = 20) -> None:
        self.interval = max(1, interval)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.interval:
            return
        for entity in world.each(Vitals):
            vitals = entity.get(Vitals)
            if not vitals.alive:
                continue
            if vitals.hp < vitals.max_hp:
                vitals.hp += 1
            if vitals.mp < vitals.max_mp:
                vitals.mp += 1


class CooldownSystem(System):
    """冷却递减（最便宜的系统，先跑）。"""

    priority = 10

    def update(self, world: World, dt: float) -> None:
        for entity in world.each(Cooldowns):
            entity.get(Cooldowns).tick(dt)


class AiTargetSystem(System):
    """极简选敌：锁定视野内最近的**敌对阵营**实体，超过脱战距离回巢。

    priority 必须**小于** ``MobChaseSystem``：先选目标再走位，
    否则追击永远慢一拍（每刻都在追上一刻的目标）。

    三个门控缺一不可：

    * ``vision``（切比雪夫）——**视野外不建立仇恨**。少了它，怪会盯住半张图外的
      玩家，走到脱战边界→丢目标→回巢→重新锁定，来回折返永不归位。
    * ``leash``（欧氏，相对出生点）——超过即放弃目标，由 ``MobChaseSystem`` 送回巢。
    * **隐身**——``Invisible.until_tick`` 未过期者一律不可选为目标。

    索敌按 ``faction`` 判定而非 ``kind``：这样道士的召唤骷髅（faction 0）
    能主动打怪，怪也会反过来打召唤物，不必为随从另写一套 AI。
    """

    priority = 11

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        candidates = [
            e for e in world.each(Identity, Position, Vitals)
            if e.get(Vitals).alive and not _is_invisible(e, tick)
        ]
        for mob in world.each(AiState, Identity, Position):
            state = mob.get(AiState)
            ident = mob.get(Identity)
            pos = mob.get(Position)
            # guard = 攻城目标（城门/旗台）：它不是怪、没有 AiState 的追人逻辑，
            # 但**必须能被玩家选为目标**，否则城门永远砍不动
            if ident.kind not in ("monster", "summon", "guard"):
                continue
            home_x, home_y = state.home
            if (pos.x - home_x) ** 2 + (pos.y - home_y) ** 2 > state.leash ** 2:
                state.target_id = None
                continue
            # passive 怪不主动索敌：只有被打了（resolver 写入 target_id）才还手
            if state.tier == "passive" and state.target_id is None:
                continue
            best: Entity | None = None
            best_dist = float("inf")
            for other in candidates:
                if other is mob or other.get(Identity).faction == ident.faction:
                    continue
                opos = other.get(Position)
                if max(abs(opos.x - pos.x), abs(opos.y - pos.y)) > state.vision:
                    continue  # 视野外不索敌
                dist = (opos.x - pos.x) ** 2 + (opos.y - pos.y) ** 2
                if dist < best_dist:
                    best, best_dist = other, dist
            state.target_id = best.id if best is not None else None
            if best is not None:
                state.aggro = min(1000, state.aggro + 1)


def _is_invisible(entity: Entity, tick: int) -> bool:
    buff = entity.get(Invisible)
    return buff is not None and buff.until_tick > tick


class RingEffectSystem(System):
    """特戒的持续效果 —— 目前只有隐身戒指的"反复结隐"。

    priority 20 = AI（11-14）之后、攻击（30）之前：本 tick 内谁出了手、
    谁被破隐已经记在 :class:`RingState` 里，此刻读到的是完整的。

    为什么写成"每刻刷新"而不是"攻击时顺手结一次"：持续型 buff 必须有
    **单一刷新点**。分散实现的话，摘掉戒指后残留一层隐身只是时间问题；
    写在这里，摘掉戒指的效果就是"下一刻起自然不再续"。

    另一个容易漏的点：这里必须按 ``Loadout`` 遍历而不是按玩家实体，
    否则"给怪也穿上戒指做测试"的路径和真机的路径会长成两个样。
    """

    priority = 20

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        data = world.get_data("data")
        if data is None:
            return
        self.ctx.tick = tick
        for entity in world.each(Loadout):
            ticks = rings.can_cloak(data, entity, tick)
            if ticks is None:
                continue
            vitals = entity.get(Vitals)
            if vitals is not None and not vitals.alive:
                continue
            self.resolver.apply_invisible(entity, ticks=ticks)


def _is_paralyzed(entity: Entity, tick: int) -> bool:
    """定身判定：截止刻之前一律不能动、不能出手。

    与隐身一个写法——集中在这里，四个消费点（移动/追击/排队/结算）都调它，
    不会出现"移动系统知道麻痹了、攻击系统不知道"这种半残实现。
    """
    buff = entity.get(Paralysis)
    return buff is not None and buff.until_tick > tick


class StatusSystem(System):
    """状态跳伤：施毒的每一跳都走 ``CombatResolver``（因此过护盾、能致死）。

    为什么单独一个系统而不是塞进 ``RegenSystem``：
    回血是"每个实体都跑"，跳毒是"只有中毒者跑"，且必须按各自 ``interval``
    错开——混在一起会让"回血 1 点 / 跳毒 3 点"的净变化变得不可预测。
    """

    priority = 13

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0:
            return
        for entity in world.each(Poison, Vitals):
            poison = entity.get(Poison)
            if poison.remaining_ticks <= 0:
                continue
            if tick % max(1, poison.interval):
                continue
            if not entity.get(Vitals).alive:
                poison.remaining_ticks = 0
                continue
            self.ctx.tick = tick
            self.resolver.tick_poison(entity)


class AttackSystem(System):
    """消费攻击意图并结算。

    意图格式::

        {"attacker": <Entity>, "defender": <Entity>, "kind": "physical",
         "power_permille": 1000}
    """

    priority = 30

    def __init__(self, ctx: CombatContext) -> None:
        self.ctx = ctx
        self.resolver = CombatResolver(ctx)

    def update(self, world: World, dt: float) -> None:
        intents: list[dict] = world.get_data(KEY_INTENTS, [])
        if not intents:
            return
        self.ctx.tick = int(world.get_data(KEY_TICK, 0))
        ready: list[dict] = []
        dropped: list[dict] = []
        tick = self.ctx.tick
        for intent in intents:
            attacker = intent["attacker"]
            defender = intent["defender"]
            vitals = defender.get(Vitals)
            atk_vitals = attacker.get(Vitals)
            if vitals is None or atk_vitals is None or not vitals.alive or not atk_vitals.alive:
                continue
            if _is_paralyzed(attacker, tick):
                # 定身期间的出手**作废**而不是排队等着：留着的话解除那一刻会把
                # 攒下的刀一次全打出来，玩家看着像"卡了一下突然爆发"。
                # 注意与上面"死亡目标"的处理不同——那条会保留意图。
                dropped.append(intent)
                continue
            kind = intent.get("kind", "physical")
            power = int(intent.get("power_permille", 1000))
            if kind == "magic":
                self.resolver.magic(attacker, defender, power_permille=power)
            elif kind == "taoist":
                self.resolver.taoist(attacker, defender, power_permille=power)
            else:
                self.resolver.physical(attacker, defender, power_permille=power)
            ready.append(intent)
        # 消费已处理意图（保留未处理的，例如死亡目标）
        handled = {id(i) for i in ready} | {id(i) for i in dropped}
        world.set_data(KEY_INTENTS, [i for i in intents if id(i) not in handled])


class DeathRewardSystem(System):
    """死亡奖励：经验按 ``monsters.json``、掉落按 ``drops.json``、金币入包。

    为什么订阅事件而不是每帧扫血量：死亡是**一次性事件**，
    用 :class:`allin_engine.EventBus` 订阅可以保证"只结算一次"，
    也避免系统之间靠轮询血量互相猜测。
    """

    priority = 40

    def __init__(self, *, data, seed: int) -> None:
        self.data = data
        self.seed = seed
        self._pending: list[dict] = []

    def bind(self, world: World) -> "DeathRewardSystem":
        """挂到世界的总线上（由 bootstrap 调用）。"""
        bus = world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_DEATH, self._on_death)
        self._world = world
        return self

    def _on_death(self, event: dict) -> None:
        self._pending.append(event)

    def update(self, world: World, dt: float) -> None:
        if not self._pending:
            return
        events, self._pending = self._pending, []
        for event in events:
            killer = world.get_entity(event.get("attacker"))
            victim = world.get_entity(event.get("defender"))
            if victim is None:
                continue
            ident = victim.get(Identity)
            species = ident.species if ident is not None else ""
            spec = self.data.monsters["monsters"].get(species, None)

            # 召唤物击杀：奖励记到主人头上（随从没背包没钱包，记它身上等于吞掉落）
            if killer is not None:
                killer = resolve_owner(world, killer)

            # 召唤物尸体：没有怪表条目、也不发奖励，但必须清出场，否则越积越多
            if ident is not None and ident.kind == "summon":
                world.remove_entity(victim.id)
                continue

            # 守军走的是普通 monster（有 monsters.json 条目），所以这条判定不变；
            # 攻城目标（kind="guard"）的奖励由 SiegeSystem 统一发，这里不能重复给。
            if killer is None or ident is None or ident.kind != "monster" or spec is None:
                continue

            # 1) 经验（升级三件套在 grant_exp 里一次做完，别在这里手抄一遍）
            grant_exp(world, self.data, killer, int(spec.get("exp", 0)),
                      event.get("tick"))

            # 2) 掉落（kill_index 从世界状态取，存档后重打不刷）
            counts: dict = world.get_data(KEY_KILL_COUNTS, {})
            kill_index = int(counts.get(species, 0)) + 1
            counts[species] = kill_index
            world.set_data(KEY_KILL_COUNTS, counts)

            table = self.data.drops["drops"].get(species)
            if table:
                drop = roll_drops(table, seed=self.seed, monster_id=species,
                                  kill_index=kill_index)
                wallet = killer.get(Wallet)
                if wallet is not None:
                    wallet.gold += drop.gold
                bag = killer.get(Bag)
                if bag is not None:
                    for item_id, count in drop.items:
                        bag.add(item_id, count)
                        # 掉出来的装备是**全新**的：磨损账按 item_id 记，
                        # 不重置的话"打废一把木剑"之后连掉落的木剑都是废的。
                        wear.reset(killer.get(Wear), item_id)
                world.set_data(KEY_REWARDS, list(world.get_data(KEY_REWARDS, [])) + [{
                    "monster": species,
                    "kill_index": kill_index,
                    "gold": drop.gold,
                    "items": list(drop.items),
                    "tick": event.get("tick"),
                }])

            # 3) 尸体移除（P0 不留尸体）
            world.remove_entity(victim.id)


class CodexSystem(System):
    """图鉴记账：怪物死亡 → 记到归属者名下，首次解锁再发一笔经验（P2-S6）。

    priority 41 = 死亡奖励（40）**之后**：先发该发的掉落，再处理图鉴，
    这样"这一刀的收益"在逻辑上是完整的一段，图鉴只是顺带记一笔。

    ⚠️ 为什么 ``species`` 与归属要在**事件回调里**取好、而不是等 ``update``：
    ``DeathRewardSystem`` 在 priority 40 会把尸体 ``remove_entity``，
    等本系统在 41 跑的时候受害者实体已经没了，``victim_eid`` 也查不到人。
    回调发生在 ``emit`` 的那一刻（``AttackSystem`` 里），尸体还在场。
    """

    priority = 41

    def __init__(self, *, data: GameData) -> None:
        self.data = data
        self._pending: list[dict] = []
        self._world: World | None = None

    def bind(self, world: World) -> "CodexSystem":
        """挂到世界的总线上（由 bootstrap 调用）。"""
        bus = world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_DEATH, self._on_death)
        self._world = world
        return self

    def _on_death(self, event: dict) -> None:
        world = self._world
        if world is None:
            return
        victim = world.get_entity(event.get("defender"))
        if victim is None:
            return
        ident = victim.get(Identity)
        if ident is None or ident.kind != "monster":
            return
        if ident.species not in self.data.monsters["monsters"]:
            return
        killer = world.get_entity(event.get("attacker"))
        if killer is None:
            return
        killer = resolve_owner(world, killer)
        self._pending.append({
            "species": ident.species,
            "owner": killer.id,
            "tick": event.get("tick"),
        })

    def update(self, world: World, dt: float) -> None:
        if not self._pending:
            return
        events, self._pending = self._pending, []
        for event in events:
            owner = world.get_entity(event["owner"])
            if owner is None:
                continue
            book = owner.get(Codex)
            if book is None:
                continue
            species = event["species"]
            if not codex.record_kill(book, species):
                continue
            bonus = codex.first_kill_exp(self.data, species)
            if bonus > 0:
                grant_exp(world, self.data, owner, bonus, event["tick"])
            bus = world.get_data("bus")
            if bus is not None:
                spec = self.data.monsters["monsters"][species]
                bus.emit(codex.EVT_UNLOCK, {
                    "entity": owner.id,
                    "species": species,
                    "name": spec.get("name", species),
                    "exp": bonus,
                    "unlocked": book.unlocked,
                    "total": len(self.data.monsters["monsters"]),
                    "tick": event["tick"],
                })


class RespawnSystem(System):
    """按地图刷新表补刷怪物 —— **每个刷点用自己的 ``respawn_seconds``**。

    之前是全局固定 30 秒一刀切，直接无视了地图表里逐怪给的间隔
    （尸王 180 秒 / 稻草人 20 秒），数据与行为对不上。
    现在按刷点记账：缺的怪要等"上次补刷 + 该刷点间隔"之后才补，
    同一刷点一次最多补满缺口。
    """

    priority = 90

    #: 记账粒度（刻）：每 10 刻（1 秒）检查一次刷点，避免每帧遍历刷新表
    CHECK_INTERVAL = 10

    def __init__(self, *, data, seed: int, tick_dt: float = 0.1) -> None:
        self.data = data
        self.seed = seed
        self.tick_dt = tick_dt
        self._serial = 0
        #: (map_id, monster_id) -> 上次补刷的刻
        self._last_spawn_tick: dict[tuple[str, str], int] = {}

    def _interval_ticks(self, spawn: dict) -> int:
        seconds = float(spawn.get("respawn_seconds", 30))
        return max(1, int(round(seconds / max(1e-6, self.tick_dt))))

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))
        if tick == 0 or tick % self.CHECK_INTERVAL:
            return
        map_id = world.get_data("current_map")
        spec = self.data.maps["maps"].get(map_id or "")
        if spec is None:
            return
        for spawn in spec.get("spawns", []):
            monster_id = spawn["monster"]
            alive = 0
            corpses: list[int] = []
            for entity in list(world.each(Identity, Vitals)):
                ident = entity.get(Identity)
                if ident.kind != "monster" or ident.species != monster_id:
                    continue
                if entity.get(Vitals).alive:
                    alive += 1
                else:
                    corpses.append(entity.id)
            # 清掉没有走过死亡事件的尸体（例如测试里直接置零血），否则永远占着名额
            for entity_id in corpses:
                world.remove_entity(entity_id)

            want = int(spawn.get("count", 0))
            key = (map_id, monster_id)
            last = self._last_spawn_tick.get(key)
            if last is None:
                # 首次看到该刷点：计时从此刻开始（开局铺怪由 populate() 负责，
                # 补刷不应该"一进图就把缺口全填满"）
                self._last_spawn_tick[key] = tick
                continue
            if tick - last < self._interval_ticks(spawn):
                continue  # 还没到该刷点的补刷时间
            missing = max(0, want - alive)
            if missing == 0:
                self._last_spawn_tick[key] = tick  # 满员时也从现在重新计时
                continue
            self._last_spawn_tick[key] = tick
            for _ in range(missing):
                self._serial += 1
                rng = Rng(self.seed, f"spawn:{map_id}:{monster_id}:{self._serial}")
                x = rng.rand_int(0, int(spec["size"][0]) - 1)
                y = rng.rand_int(0, int(spec["size"][1]) - 1)
                from mirsolo.actor.factory import spawn_monster

                spawn_monster(world, self.data, monster_id, x=float(x), y=float(y))


class DeathPenaltySystem(System):
    """死亡惩罚：玩家倒地 → 结算惩罚 → 复活（P3-S2）。

    priority 42 = 图鉴（41）**之后**：死亡奖励（40）与图鉴（41）都只认
    ``kind == "monster"``，玩家死不走那两条路；但把它排在这一串的最后，
    "死一次"这件事的所有副作用在代码里就是连着的一段，不用跳着看。

    为什么也订阅事件而不是每帧扫血量：与 ``DeathRewardSystem`` 同一个理由——
    死亡是**一次性事件**，靠事件保证只结算一次，也避免两个系统各自轮询血量、
    互相猜测对方有没有处理过。

    ⚠️ 惩罚是**延迟结算**的：致死那一下只让人倒地，真正的扣减发生在
    玩家选择复活方式（或等满 ``DOWN_TICKS`` 自动回城）的那一刻。
    理由见 ``mirsolo/actor/penalty.py`` 的模块 docstring。
    """

    priority = 42

    def __init__(self, *, data, seed: int) -> None:
        self.data = data
        self.seed = seed
        self._pending: list[dict] = []

    def bind(self, world: World) -> "DeathPenaltySystem":
        """挂到世界的总线上（由 bootstrap 调用）。"""
        bus = world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_DEATH, self._on_death)
        self._world = world
        return self

    def _on_death(self, event: dict) -> None:
        self._pending.append(event)

    def update(self, world: World, dt: float) -> None:
        tick = int(world.get_data(KEY_TICK, 0))

        # 1) 新死的人 → 倒地（等玩家选怎么起来）
        if self._pending:
            events, self._pending = self._pending, []
            for event in events:
                victim = world.get_entity(event.get("defender"))
                if victim is None:
                    continue
                ident = victim.get(Identity)
                # 只有玩家会倒地：怪物尸体由 DeathRewardSystem 直接清出场
                if ident is None or ident.kind != "player":
                    continue
                if victim.has(Downed):
                    continue
                penalty.knock_down(victim, tick)
                world.set_data(KEY_DOWNED, {
                    "entity": victim.id,
                    "at_tick": tick,
                    "revive_at": tick + penalty.DOWN_TICKS,
                })

        # 2) 倒地满了 DOWN_TICKS 还没选 → 自动回城，走全额惩罚。
        #    这个兜底是必须的：headless 没有交互，倒地后必须有确定行为。
        for entity in list(world.each(Downed)):
            downed = entity.get(Downed)
            if tick < downed.revive_at:
                continue
            map_id = world.get_data("current_map")
            home = penalty.home_point(self.data, map_id)
            rng = Rng(self.seed, f"death:{entity.id}@{tick}")
            got = penalty.settle(self.data, entity, rng=rng, tick=tick,
                                 in_place=False, home=home)
            world.set_data(KEY_LAST_DEATH, {"entity": entity.id, **got})
            # 已经起来了：清掉倒地状态，否则「按 R 复活」的提示会一直挂着
            if (world.get_data(KEY_DOWNED) or {}).get("entity") == entity.id:
                world.set_data(KEY_DOWNED, None)
