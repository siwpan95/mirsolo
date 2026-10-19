"""死亡惩罚（P3-S2）—— 让"死一次"真的有代价，同时给一条**花钱减损**的出路。

时序::

    致死那一下 → 发 EVT_DEATH → 玩家进入 :class:`Downed`（HP=0，**还没结算**）
                                   ├─ 30 刻内按 V  → 原地复活（花金币，只掉磨损）
                                   └─ 满 30 刻      → 自动回城（免费，四件惩罚全额）

**为什么惩罚要推迟到玩家做出选择时才结算**：原地付费复活卖的就是"少掉点东西"。
若死亡瞬间先扣光、原地复活再退回来，就得写一套退款逻辑——退多少、掉出去的
物品怎么吐回来、吐回来的还算不算"掉过"，全是说不清的账。推迟结算之后两档各算
各的，**没有退款这条路径**。

**为什么还要留一个自动回城兜底**：headless（``cli/smoke.py``、存档回归）没有
交互，倒地之后必须有确定行为；玩家死了不操作也不会把游戏卡住。

四件惩罚（**全额 = 回城**）

1. **掉经验**：本级升级所需经验 × ``EXP_LOSS_PERMILLE``，且**只掉本级已积累的
   那部分**（``min(损失, prog.exp)``）——绝不会掉回上一级；
2. **掉金币**：持有金币 × ``GOLD_LOSS_PERMILLE``；
3. **全身装备额外磨损**：身上每件 **+``DEATH_WEAR_POINTS`` 点（必然，不是
   概率）**；背包/仓库里的不磨 —— 没穿就不该磨；
4. **随机掉一件背包物品**：``BAG_DROP_BP`` 的概率掉 **1 个**（堆叠只掉 1 个）；
   背包空则不掉。

**原地复活**：花 ``REVIVE_BASE_GOLD + REVIVE_GOLD_PER_LEVEL × 等级`` 金，
惩罚只剩第 3 件（磨损）—— 花钱买的就是"不掉经验金币、不掉东西"。
钱不够就拒绝（提示还差多少），**不做赊账、不做负债**：金币一旦允许为负，
商店回收价与拍卖行底价全都得跟着处理负数，得不偿失。

已知差异（原版对照）：原版 1.76 死亡有几率掉**身上穿着**的装备，红名尤甚。
本作**只掉背包里不穿的东西**——单机丢掉身上辛苦打来的装备惩罚过重，
而且"掉了就读档重打"会养成存档投机，与 P3-S6 的防刷口径直接打架。

口径纪律：**概率用万分比**（``BAG_DROP_BP``，与麻痹/磨损同一把尺子），
**倍率用千分比**（``EXP_LOSS_PERMILLE`` / ``GOLD_LOSS_PERMILLE``）。
两类比率混用在本项目踩过一次 10 倍差，见 docs/数据口径.md。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import (
    Bag,
    DeathLedger,
    Downed,
    Loadout,
    Position,
    Progression,
    Vitals,
    Wallet,
    Wear,
)
from mirsolo.data.loader import GameData
from mirsolo.items import wear
from mirsolo.items.equipment import recompute

#: 倒地到自动回城之间留给玩家按 ``V`` 的窗口（刻）。30 刻 = 3 秒：
#: 够反应，又不至于让人盯着尸体发呆。
DOWN_TICKS = 30

#: 死亡掉经验 = **当前等级升级所需经验** × 该千分比。100 = 10%，
#: 配合只掉"本级已积累"的夹取，实际等于"这一级白打了一成"，绝不掉级。
EXP_LOSS_PERMILLE = 100

#: 死亡掉金币 = 持有金币 × 该千分比。
GOLD_LOSS_PERMILLE = 100

#: 死亡时身上每件装备**必然**额外磨损的点数。刻意不用概率：
#: "死了装备会变差"这条因果必须稳定可感，掷点会把它稀释成玄学。
DEATH_WEAR_POINTS = 1

#: 随机掉一件背包物品的概率（万分比）。2000 = 20%。
BAG_DROP_BP = 2000

#: 原地复活的花费 = ``REVIVE_BASE_GOLD + REVIVE_GOLD_PER_LEVEL × 等级``。
#: 与等级挂钩，是因为高等级一次练级的收益更高，固定价会在后期形同免费。
REVIVE_BASE_GOLD = 500
REVIVE_GOLD_PER_LEVEL = 100


class PenaltyError(ValueError):
    """死亡惩罚/复活被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def home_point(data: GameData, map_id: str) -> tuple[float, float]:
    """回城落点 = 地图中心。

    抽成函数是因为它有两处消费（死亡结算与 ``cli/smoke.py`` 的临时复活），
    各写一份的话，改了地图落点规则就会漏一处。
    """
    spec = data.maps["maps"].get(map_id)
    if spec is None:
        return (0.0, 0.0)
    size = spec.get("size", (0, 0))
    return (float(int(size[0]) // 2), float(int(size[1]) // 2))


def exp_loss(data: GameData, prog: Progression) -> int:
    """这一死要掉多少经验（**尚未**夹取到本级已积累的部分）。

    满级（``exp_to_next == 0``）返回 0：满级角色没有下一级可掉，
    再扣就是在惩罚里塞一个看不见的洞。
    """
    if prog is None:
        return 0
    need = int(data.exp_to_next(int(prog.level)))
    if need <= 0:
        return 0
    return int(need * EXP_LOSS_PERMILLE // 1000)


def capped_exp_loss(data: GameData, prog: Progression) -> int:
    """实际会掉的经验：**不超过本级已积累的部分**，所以永不掉级。"""
    return min(exp_loss(data, prog), max(0, int(prog.exp)))


def gold_loss(wallet: Wallet | None) -> int:
    """这一死要掉多少金币（尚未夹取到持有量）。"""
    if wallet is None:
        return 0
    return int(max(0, int(wallet.gold)) * GOLD_LOSS_PERMILLE // 1000)


def revive_cost(prog: Progression | None) -> int:
    """原地复活的价格：``500 + 100 × 等级``。"""
    level = int(prog.level) if prog is not None else 1
    return int(REVIVE_BASE_GOLD + REVIVE_GOLD_PER_LEVEL * level)


def can_revive_in_place(data: GameData, player: Entity) -> bool:
    """钱够不够原地复活（不够就只能等自动回城）。"""
    wallet = player.get(Wallet)
    if wallet is None:
        return False
    return int(wallet.gold) >= revive_cost(player.get(Progression))


def pick_bag_item(bag: Bag | None, rng) -> str | None:
    """从背包里随机挑一类物品（按 item_id 排序后取下标，顺序必须稳定）。

    只掉**不穿的**——背包本来就不穿，身上的装备一件不动（见模块 docstring）。
    """
    if bag is None or not bag.items:
        return None
    ids = sorted(bag.items)
    return ids[rng.rand_int(0, len(ids) - 1)]


def is_downed(player: Entity) -> bool:
    """玩家是不是正倒在地上等着被复活。"""
    return player is not None and player.has(Downed)


def knock_down(player: Entity, tick: int) -> Downed:
    """进入倒地状态（致死那一下由系统调用）。已经倒着就不重复加。"""
    downed = player.get(Downed)
    if downed is not None:
        return downed
    downed = Downed(at_tick=int(tick), revive_at=int(tick) + DOWN_TICKS)
    player.add(downed)
    return downed


def settle(data: GameData, player: Entity, *, rng, tick: int = 0,
           in_place: bool = False,
           home: tuple[float, float] | None = None) -> dict:
    """结算一次死亡惩罚并复活。**惩罚与复活的唯一入口**。

    ``in_place=False``（回城 / 自动兜底）走四件全额；
    ``in_place=True``（付费原地）只走第 3 件磨损。

    返回 ``{"exp_lost", "gold_lost", "items", "worn", "in_place", "paid", "tick"}``，
    由 :class:`~mirsolo.actor.systems.DeathPenaltySystem` 写进
    ``world.set_data("last_death", ...)`` 供表现层弹提示——规则层自己不存全局状态。
    """
    prog = player.get(Progression)
    wallet = player.get(Wallet)
    if prog is None or wallet is None:
        raise PenaltyError("missing_components", "玩家缺 Progression 或 Wallet")

    result: dict = {
        "exp_lost": 0,
        "gold_lost": 0,
        "items": [],
        "worn": [],
        "in_place": bool(in_place),
        "paid": 0,
        "tick": int(tick),
    }

    if in_place:
        cost = revive_cost(prog)
        if int(wallet.gold) < cost:
            raise PenaltyError("no_gold", f"原地复活要 {cost} 金，还差 {cost - int(wallet.gold)} 金")
        wallet.gold = int(wallet.gold) - cost
        result["paid"] = cost
    else:
        # 1) 经验（夹到本级已积累，绝不掉级）
        loss = capped_exp_loss(data, prog)
        prog.exp = int(prog.exp) - loss
        result["exp_lost"] = loss
        # 2) 金币（夹到持有量，不出现负数）
        gl = min(gold_loss(wallet), int(wallet.gold))
        wallet.gold = int(wallet.gold) - gl
        result["gold_lost"] = gl
        # 4) 随机掉一件背包物品
        bag = player.get(Bag)
        if bag is not None and rng.rand_int(1, 10000) <= BAG_DROP_BP:
            item_id = pick_bag_item(bag, rng)
            if item_id is not None and bag.remove(item_id, 1):
                result["items"].append((item_id, 1))

    # 3) 全身装备额外磨损 —— **两档都要**。这是唯一不可回避的一条：
    #    死了就是死了，装备不会因为你在哪里站起来而少坏一点。
    loadout = player.get(Loadout)
    if loadout is not None:
        book = player.get(Wear)
        for item_id in sorted(set(loadout.slots.values())):
            if wear.add_wear(data, book, item_id, DEATH_WEAR_POINTS):
                result["worn"].append(item_id)
    recompute(player, data)

    ledger = player.get(DeathLedger)
    if ledger is not None:
        ledger.count += 1
        ledger.exp_lost += int(result["exp_lost"])
        ledger.gold_lost += int(result["gold_lost"])
        for item_id, count in result["items"]:
            ledger.items_lost[item_id] = ledger.items_lost.get(item_id, 0) + count

    vitals = player.get(Vitals)
    if vitals is not None:
        vitals.hp = int(vitals.max_hp)
        vitals.mp = int(vitals.max_mp)
    if home is not None and player.has(Position):
        pos = player.get(Position)
        pos.x, pos.y = float(home[0]), float(home[1])

    if player.has(Downed):
        player.remove(Downed)

    return result
