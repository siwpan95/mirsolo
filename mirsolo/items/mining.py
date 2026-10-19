"""挖矿 —— 矿点 / 鹤嘴锄 / 每锄产出（P3-S4）。

原版传奇的挖矿是"在矿洞里找个墙角，拿鹤嘴锄一下一下敲"。本作把它落成
四个可测的事实：

1. **工具就是武器。** 鹤嘴锄占 ``weapon`` 槽、攻击 1-3，挖矿时手上必须握着它。
   所以"挖矿"的代价不是一条规则，而是装备槽自己的语义：**这一刻你打不动怪**。
   想打架就得换武器，取舍感由此而来——不需要额外写"挖矿时攻击力减半"这种补丁。
2. **矿点固定在图里。** 坐标写在 ``maps.json`` 的 ``mines`` 段（与传送点同级），
   不按种子撒：可预期、可测，截图验收也好钉。
3. **每锄一个独立随机流。** ``mine:{map_id}:{vein_index}@{swing}``——
   与战斗、掉落、磨损的流全部分开，所以"先挖矿还是先打架"不会互相挪动读数。
4. **矿石纯度落在 id 上。** 黑铁矿石按纯度分四档（30/50/70/100），
   沿用 P3-S1「磨损按 ``item_id`` 记账」那条纪律：背包是堆叠的、没有实物实例 id，
   所以纯度只能落在 id 上，而不是给每块矿石发一个实例号。

**每锄必然磨 1 点**（``WEAR_PER_SWING``），不是概率：挖矿是生产成本，
概率磨损会让"锄头还能用多久"变成随机数，玩家没法规划。战斗磨损才是概率的
（那边的 300‰ 是"损耗"，这边的 1 点是"成本"）。
"""

from __future__ import annotations

from mirsolo.components import Bag, Downed, Loadout, Position, Wear
from mirsolo.data.loader import TOOL_KINDS, GameData
from mirsolo.determinism import Rng, splitmix64
from mirsolo.items.wear import add_wear

#: 每多少刻出一锄。10 刻 = 1 秒：与药水冷却同频，
#: "喝一口药、出一锄"这两件事在手感上是同一个节拍。
MINING_TICKS = 10

#: 每锄磨掉鹤嘴锄多少点耐久（必然，不是概率——见模块 docstring）。
WEAR_PER_SWING = 1

#: 能挖矿的工具标记（与 ``loader.TOOL_KINDS`` 同源；
#: 数据面唯一定义处在 ``data/items.json`` 的 ``tool`` 字段上）。
PICK_TOOL = "pick"

#: 挖不动的原因码（表现层据此给提示，不靠猜）。
#: 刻意**没有** "cooling"：十刻一锄是节奏门控，够不够着节奏由
#: ``Mining.next_tick`` 管，表现层根本不需要知道"冷却"这回事。
MINE_BLOCKERS = frozenset({"downed", "no_pick", "no_vein", "depleted"})


def pick_ids(data: GameData) -> tuple[str, ...]:
    """所有能挖矿的工具 id（按 id 排序，保证顺序稳定）。

    刻意**遍历数据**而不是把 ``"hezuichu"`` 写死在代码里：工具是数据面的事实，
    哪天加一把"龙骨鹤嘴锄"，规则层不用改一行。
    """
    return tuple(sorted(iid for iid, spec in data.items["items"].items()
                        if spec.get("tool") in TOOL_KINDS))


def holding_pick(entity, data: GameData) -> bool:
    """手上（``weapon`` 槽）握着鹤嘴锄吗。

    只看 **weapon 槽**——背包里有一把不算。这条就是"挖矿时战力骤降"的
    全部实现：拿锄头的时候，身上那把斩马刀正在包里躺着。
    """
    loadout = entity.get(Loadout)
    if loadout is None:
        return False
    return loadout.slots.get("weapon") in pick_ids(data)


def vein_at(map_spec: dict, x: int, y: int) -> int | None:
    """脚下这格是第几个矿点（不是矿点返回 ``None``）。

    刻意用**精确坐标相等**而不是"附近一格"：矿点与传送点同构，
    精确站立让"我在挖哪个矿脉"没有歧义，也让"矿点不压传送点"那条
    数据校验有意义（压上去的话，人会在站稳的那一 tick 被换走）。
    """
    for index, mine in enumerate(map_spec.get("mines", [])):
        mx, my = mine.get("at", (0, 0))
        if int(x) == int(mx) and int(y) == int(my):
            return index
    return None


def vein_of(map_spec: dict, index: int) -> dict:
    """第 ``index`` 个矿点的定义（``{"at": [x, y], "vein": 矿脉 id}``）。"""
    return map_spec.get("mines", [])[index]


def can_mine(entity, data: GameData, map_spec: dict, x: int, y: int) -> str | None:
    """现在能不能开挖。``None`` = 能，否则是 ``MINE_BLOCKERS`` 里的原因码。

    倒地不挖（与 P3-S2 同源：倒地期间什么都不该能做）；
    没锄头不挖；脚下没矿点不挖。
    """
    if entity.has(Downed):
        return "downed"
    if not holding_pick(entity, data):
        return "no_pick"
    if vein_at(map_spec, x, y) is None:
        return "no_vein"
    return None


def _pick_ore(entries: list[dict], rng: Rng) -> tuple[dict, int]:
    """按 ``bp``（万分比）权重挑一块矿石，连掷出的点一起返回。

    与 ``items/drops.py`` 的 ``_pick_entry`` 同构，只是权重的键名是 ``bp``——
    矿脉表一律万分比（与 drops 的条目权重同尺度），避开 P2 踩过的两套尺度混用。
    权重和为 0 时取第一条，防御坏数据。
    """
    total = sum(int(e.get("bp", 0)) for e in entries)
    point = rng.rand_int(1, max(1, total))
    acc = 0
    for entry in entries:
        acc += int(entry.get("bp", 0))
        if point <= acc:
            return entry, point
    return entries[-1], point


def swing(data: GameData, *, seed: int, map_id: str, vein_index: int,
          swing_index: int, vein_id: str) -> dict:
    """挖一锄。

    先掷"出不出矿"（``yield_bp``），中了再掷"出哪一种"（条目权重）。
    两次掷点共用**同一个**流，所以流名只有一个，回放时可逐锄复算。

    返回 ``{"ore": item_id | None, "roll": 出矿判定, "roll_item": 条目判定}``——
    空锄（``ore`` 为 ``None``）也是正常结果，"十锄出几锄"这个节奏本身就是设计。
    """
    spec = data.vein(vein_id)
    stream = f"mine:{map_id}:{vein_index}@{swing_index}"
    rng = Rng(seed ^ splitmix64(1), stream)
    judged = rng.rand_int(1, 10000)
    if judged > int(spec.get("yield_bp", 0)):
        # 空锄也要把判定点记下来：否则"这一锄为什么没出矿"就无从复算
        return {"ore": None, "roll": judged, "roll_item": 0}
    entries = spec.get("entries") or []
    entry, point = _pick_ore(entries, rng)
    return {"ore": str(entry["item"]), "roll": judged, "roll_item": point}


def is_depleted(state: dict, tick: int) -> bool:
    """这个矿点是不是挖空了（还在恢复倒计时里）。"""
    return tick < int(state.get("until", 0))


def regrow(state: dict, tick: int) -> None:
    """到点恢复（储量清零）。

    **累计锄数不跟着清零**：它给随机流名用。一清零，同一个流名就会被重用，
    于是"矿脉长回来之后的前几锄"会挖出和上一次一模一样的矿石。
    """
    until = int(state.get("until", 0))
    if until and tick >= until:
        state["swings"] = 0
        state["until"] = 0


def mine_one(entity, data: GameData, *, map_spec: dict, map_id: str, seed: int,
             tick: int, state: dict, vein_index: int) -> dict:
    """结算一锄（含磨损 + 产出入包），返回这一锄的账。

    顺序是**先磨损、后产出**：锄头在挥出去的那一刻就掉一点，哪怕这一锄是空的。
    不然"空锄不磨"会让玩家算出"先空挥到运气好再挖"这种空子。
    """
    vein_id = str(vein_of(map_spec, vein_index)["vein"])
    spec = data.vein(vein_id)

    total = int(state.get("total", 0))
    got = swing(data, seed=seed, map_id=map_id, vein_index=vein_index,
                swing_index=total, vein_id=vein_id)
    state["total"] = total + 1
    state["swings"] = int(state.get("swings", 0)) + 1

    # 磨损走 P3-S1 的 add_wear（同一条规则），且**必然 1 点**：
    # 这里不掷点，所以不消耗任何随机流，也就不可能挪动战斗磨损的读数
    loadout = entity.get(Loadout)
    book = entity.get(Wear)
    if loadout is not None and book is not None:
        pick_id = loadout.slots.get("weapon")
        if pick_id:
            add_wear(data, book, pick_id, WEAR_PER_SWING)

    # 挖到了但**放不进背包**也算没挖到（背包按种类计容量，满了就装不进新种类）。
    # 刻意不做"塞不进去就丢在地上"：地上那块没有坐标、没人捡得回来
    stored = ""
    ore = got["ore"]
    if ore is not None:
        bag = entity.get(Bag)
        if bag is not None and bag.add(ore, 1):
            stored = ore

    # 挖到储量上限就枯竭（倒计时记在 until 上，到点由 regrow 恢复）
    if int(state["swings"]) >= int(spec.get("capacity", 1)):
        state["until"] = tick + int(spec.get("regrow_ticks", 0))

    return {"ore": stored, "missed": ore if not stored else None,
            "roll": got["roll"], "roll_item": got["roll_item"],
            "swings": int(state["swings"]), "total": int(state["total"]),
            "until": int(state.get("until", 0)),
            "capacity": int(spec.get("capacity", 1))}


def lightest_ore_weight(data: GameData) -> int:
    """最轻的那种矿石有多重（"还背得动一块吗"的判定基准）。

    用最轻的那档而不是"随便挑一个"：玩家背满之前，最后一块**可能是最轻的**，
    拿最重的当基准会提前把人赶回城。
    """
    weights = [int(spec.get("weight", 1)) for spec in data.items["items"].values()
               if spec.get("kind") == "ore"]
    return min(weights) if weights else 1


def is_ore(data: GameData, item_id: str) -> bool:
    return data.items["items"].get(item_id, {}).get("kind") == "ore"


def ore_ids(data: GameData) -> tuple[str, ...]:
    """全部矿石 id（按 id 排序；黑铁矿石的四个纯度档各算一种）。"""
    return tuple(sorted(iid for iid, spec in data.items["items"].items()
                        if spec.get("kind") == "ore"))


def purity_of(data: GameData, item_id: str) -> int:
    """这块矿石的纯度（普通矿石没有纯度，返回 0）。

    纯度是**数据面**字段（``items.json`` 的 ``purity``），#25 升级武器直接读它。
    """
    return int(data.items["items"].get(item_id, {}).get("purity", 0) or 0)


def carried_ore_weight(entity, data: GameData) -> int:
    """背包里矿石的总重量（背满就得回城——挖矿的节奏靠负重限速）。"""
    bag = entity.get(Bag)
    if bag is None:
        return 0
    total = 0
    for item_id, count in bag.items.items():
        if is_ore(data, item_id):
            total += int(data.items["items"][item_id].get("weight", 0)) * int(count)
    return total


def highest_purity(data: GameData, bag: Bag | None) -> str | None:
    """包里纯度最高的那一块黑铁矿石（没有则返回 ``None``）。

    这是给 #25 武器升级留的接口：纯度按 id 分档，所以"挑最高的"就是
    按 ``purity`` 扫一遍——不需要给每块矿石发实例号。
    """
    if bag is None:
        return None
    best: str | None = None
    best_value = 0
    for item_id, count in bag.items.items():
        if int(count) <= 0 or not is_ore(data, item_id):
            continue
        value = purity_of(data, item_id)
        if value > best_value:
            best, best_value = item_id, value
    return best


def player_xy(entity) -> tuple[int, int] | None:
    """玩家所在的格子坐标。"""
    pos = entity.get(Position)
    if pos is None:
        return None
    return int(pos.x), int(pos.y)
