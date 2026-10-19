"""拍卖行（P2-S5）—— 单机化的**寄卖行** + 确定性撮合。

原版拍卖行是玩家对玩家的 C2C 市场，靠的是"服务器里还有别人"。单机没有别人，
所以"卖给谁"必须重新定义：这里做成**寄卖行**——玩家把东西挂给 NPC 行会，
由系统按固定节奏掷点决定"今天有没有人来买"。

单机化缺了任何一条，拍卖行都会退化成提款机，四条约束一条都不能省：

1. **要价有上限**：``ask`` 被钳在 ``[floor, cap)``，``cap`` = 商店买入价
   × ``CAP_PERMILLE``。没有上限的话挂 100 万金也照成交，金币就不再是稀缺资源。
2. **要价越高越难成交**：成交概率从 ``demand_bp`` 线性衰减到 0。这是拍卖行
   唯一的策略维度（快卖还是多赚），也是它区别于"直接卖商店"的地方。
3. **成交扣手续费**：钱是系统凭空造出来的（AI 买家），必须有回收。
4. **挂单即离手**：物品进托管，不能"挂着还能穿"。只有撤单才回背包。

**为什么不做"AI 买家出价"**：让 NPC 报一个价、玩家决定卖不卖，需要一套还价 UI
和一轮往返交互，成本远高于收益；而"玩家定价 + 概率成交"把决策压成一次点击，
并且**概率与要价的关系是可验证的线性函数**（挂底价必按 ``demand_bp`` 成交，
挂上限永远不成交），单测能直接钉住。

随机流一律带 ``auction:`` 前缀且**位置无关**（流名含挂单序号与第几轮）：
挂单/撤单不得挪动主战斗流的位置，否则同一份存档在挂单前后打出的每一刀都对不上。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import AuctionBook, Bag, Wallet
from mirsolo.data.loader import GameData
from mirsolo.economy.shop import buy_price

#: 要价上限 = 商店买入价 × 该千分比（1200‰ = 1.2 倍）。
#: 拍卖行存在的意义是"比卖商店多换点钱"，所以它必须高于回收价（1/3）
#: 又不能高到让金币失去意义。1.2 倍是刻意留的窄口子。
CAP_PERMILLE = 1200

#: 成交手续费（成交价的千分比）。钱由系统造出，抽一成作回收。
FEE_PERMILLE = 100

#: 撮合间隔（刻）。10Hz 下 600 刻 = 60 秒。
CHECK_TICKS = 600

#: 报价档位：（显示名, 档位千分比）。要价 = floor + (cap - floor) × 档位 // 1000。
#: 最高档刻意取 900‰ 而不是 1000‰：档位顶到上限的话成交概率恒为 0，
#: "高价"这一档就成了摆设（挂上去一辈子卖不掉）。
ASK_TIERS: tuple[tuple[str, int], ...] = (
    ("速卖", 0),
    ("常价", 500),
    ("高价", 900),
)

EVT_SOLD = "auction:sold"
"""成交事件。载荷：``{entity, item, count, ask, fee, net, seq, tick}``。"""


class AuctionError(ValueError):
    """挂牌/撤单被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, item_id: str) -> dict:
    spec = data.items["items"].get(item_id)
    if spec is None:
        raise AuctionError("unknown_item", f"未知物品 {item_id!r}")
    return spec


def auction_param(data: GameData, item_id: str) -> dict | None:
    """物品的拍卖参数；没有 ``auction`` 字段的物品**不能挂**（返回 ``None``）。

    为什么是可选字段而不是"什么都让挂"：需求度与底价是**数值**，
    要么显式写进数据表，要么就得在代码里按等级猜——后者正是本项目
    一直避免的"代码里藏数值"。标了才算可挂，没标就是不可挂，
    ``auction-book`` 门禁再检查标了的那些参数是否合法。
    """
    param = _spec(data, item_id).get("auction")
    return dict(param) if isinstance(param, dict) else None


def price_bounds(data: GameData, item_id: str) -> tuple[int, int] | None:
    """``(floor, cap)``：要价的合法区间（左闭右开）。不可挂则返回 ``None``。

    ``floor`` 由数据表的 ``floor_permille`` 决定，``cap`` 一律按 ``CAP_PERMILLE``
    算——上限是**全局常量**而不是逐物品可调，因为它是经济闸门，
    散到 38 件物品上就没人能一眼看出"全服最高能卖多少"。
    """
    param = auction_param(data, item_id)
    if param is None:
        return None
    base = buy_price(data, item_id)
    floor = max(1, base * max(0, int(param.get("floor_permille", 0))) // 1000)
    cap = max(floor + 1, base * CAP_PERMILLE // 1000)
    return floor, cap


def ask_for_tier(data: GameData, item_id: str, tier: int) -> int | None:
    """第 ``tier`` 档的实际要价（越界则钳到首尾档）。不可挂返回 ``None``。"""
    bounds = price_bounds(data, item_id)
    if bounds is None:
        return None
    floor, cap = bounds
    index = max(0, min(len(ASK_TIERS) - 1, int(tier)))
    permille = ASK_TIERS[index][1]
    return floor + (cap - floor) * permille // 1000


def chance_bp(data: GameData, item_id: str, ask: int) -> int:
    """本次撮合的成交概率（万分比）。

    线性衰减：挂 ``floor`` 价 = 满概率 ``demand_bp``，越往上越难，顶到 ``cap`` 为 0。
    选线性而不是别的曲线，是因为它**可被单测逐点钉住**——
    任何"感觉更自然"的曲线都要靠肉眼调，而调出来的数没人复查。
    """
    param = auction_param(data, item_id)
    if param is None:
        return 0
    bounds = price_bounds(data, item_id)
    if bounds is None:
        return 0
    floor, cap = bounds
    demand = int(param.get("demand_bp", 0))
    if ask <= floor:
        return max(0, demand)
    if ask >= cap:
        return 0
    span = cap - floor
    return max(0, demand * (cap - ask) // span)


def _book(entity: Entity) -> AuctionBook:
    """取（没有就建）挂单账本。所有写 side-effect 的入口都从这里走。"""
    book = entity.get(AuctionBook)
    if book is None:
        book = AuctionBook()
        entity.add(book)
    return book


def list_item(entity: Entity, data: GameData, item_id: str, ask: int,
              count: int = 1, tick: int = 0) -> dict:
    """挂牌：校验 → **物品离手**进托管 → 记一张单。

    ``count`` 默认 1（面板点一次 = 一件，与商店同一条纪律）。
    """
    if count <= 0:
        raise AuctionError("bad_count", "数量必须为正")
    param = auction_param(data, item_id)
    if param is None:
        raise AuctionError("not_listable", f"{_spec(data, item_id).get('name', item_id)} 不能寄卖")
    bounds = price_bounds(data, item_id)
    if bounds is None:
        raise AuctionError("not_listable", "这件东西不能寄卖")
    floor, cap = bounds
    bag = entity.get(Bag)
    if bag is None:
        raise AuctionError("missing_components", "缺少 Bag 组件")
    have = bag.items.get(item_id, 0)
    if have < count:
        raise AuctionError("not_enough", f"背包里只有 {have} 件")
    ask = int(ask)
    if ask < floor or ask >= cap:
        raise AuctionError("bad_price", f"要价必须在 {floor}~{cap - 1} 之间")

    bag.remove(item_id, count)
    book = _book(entity)
    seq = book.next_seq
    book.next_seq = seq + 1
    listing = {"seq": seq, "item": item_id, "count": count,
               "ask": ask, "listed_tick": int(tick)}
    book.listings.append(listing)
    return dict(listing)


def ask_tier(entity: Entity) -> int:
    """当前报价档位索引（钳到合法范围；没有账本就按第 0 档并顺手建一个）。

    窗口层点"寄卖"时用它取档位，不必先去判断 ``AuctionBook`` 存不存在——
    "玩家还没打开过拍卖行"是常态，不是错误。
    """
    book = _book(entity)
    return max(0, min(len(ASK_TIERS) - 1, int(book.ask_tier)))


def cycle_ask_tier(entity: Entity) -> str:
    """切换报价档位，返回新档位的显示名（面板底行与浮字共用一份文案）。"""
    book = _book(entity)
    book.ask_tier = (int(book.ask_tier) + 1) % len(ASK_TIERS)
    return ASK_TIERS[book.ask_tier][0]


def cancel_listing(entity: Entity, data: GameData, seq: int) -> dict:
    """撤单：物品退回背包。背包满则**整笔拒绝**（不部分退回）。

    为什么不是"背包满了就先放着"：那等于凭空多出一个隐形仓库，
    玩家会忘记自己还有东西在外面——看得见的拒绝比看不见的堆积好。
    """
    book = _book(entity)
    for index, listing in enumerate(book.listings):
        if int(listing.get("seq", -1)) != int(seq):
            continue
        bag = entity.get(Bag)
        if bag is None:
            raise AuctionError("missing_components", "缺少 Bag 组件")
        item_id = listing["item"]
        if bag.full and item_id not in bag.items:
            raise AuctionError("bag_full", "背包已满，先腾出一格")
        book.listings.pop(index)
        bag.add(item_id, int(listing.get("count", 1)))
        return {"seq": seq, "item": item_id, "count": int(listing.get("count", 1))}
    raise AuctionError("no_listing", f"没有编号为 {seq} 的挂单")


def settle(entity: Entity, data: GameData, *, seed: int, tick: int,
           bus=None) -> list[dict]:
    """一轮撮合：逐单掷点，成交则入账并销毁托管物品。

    ``seed`` 由调用方（``AuctionSystem``）给，流名
    ``auction:{seq}@{round}`` —— **流名含挂单序号而不含调用顺序**，
    所以多挂/少挂一张单不会挪动别的单的随机数位置。
    """
    from mirsolo.determinism import Rng  # 局部导入：避免规则层与工具层循环依赖

    book = _book(entity)
    if not book.listings:
        return []
    round_index = int(tick) // CHECK_TICKS
    wallet = entity.get(Wallet)
    if wallet is None:
        raise AuctionError("missing_components", "缺少 Wallet 组件")

    sold: list[dict] = []
    survivors: list[dict] = []
    for listing in book.listings:
        seq = int(listing.get("seq", 0))
        item_id = listing["item"]
        ask = int(listing.get("ask", 0))
        rng = Rng(seed, f"auction:{seq}@{round_index}")
        if rng.rand_int(1, 10000) > chance_bp(data, item_id, ask):
            survivors.append(listing)
            continue
        count = int(listing.get("count", 1))
        gross = ask * count
        fee = gross * FEE_PERMILLE // 1000
        net = gross - fee
        wallet.gold += net
        record = {"seq": seq, "item": item_id, "count": count, "ask": ask,
                  "gross": gross, "fee": fee, "net": net, "tick": int(tick)}
        sold.append(record)
        if bus is not None:
            bus.emit(EVT_SOLD, dict(record, entity=entity.id))
    book.listings = survivors
    return sold
