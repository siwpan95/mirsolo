"""拍卖行（P2-S5）—— 每条规则都要能"关掉就变红"。

钉死的四件事：

1. **拍卖行不能是提款机**：要价有上限（``cap`` = 商店买入价 × 1200‰），
   且挂单即离手——同一件装备不允许"穿着身上还在挂"。
2. **要价越高越难成交**：概率是**线性**的（挂底价 = ``demand_bp``，顶到上限 = 0），
   线性才可能被逐点钉住；任何"更自然的曲线"都只能靠肉眼调。
3. **拍卖行必须比卖商店值钱**：底价 > 商店回收价（``SELL_DIVISOR``），
   否则玩家永远该直接卖商店，拍卖行就是白做的。
4. **撮合的随机流独立且不挪位**：挂单/撤单不得改变主战斗流的位置，
   也不得挪动**别的挂单**的随机流（流名只含挂单序号与轮次）。

字面量原则：期望值写死，不从数据推导。
"""

from __future__ import annotations

from contextlib import contextmanager

from mirsolo.bootstrap import build_session, populate
from mirsolo.components import AuctionBook, Bag, Wallet
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng
from mirsolo.economy import auction
from mirsolo.economy.shop import SELL_DIVISOR, buy_price, sell_price
from mirsolo.presentation.panels import build_auction_panel

CASES = []
DATA = GameData.load()
ITEMS = DATA.items["items"]

# ── 常量口径的字面量（改常量必须同步改这里）────────────────────
CAP_PERMILLE = 1200     # 要价上限 = 商店买入价 × 1.2
FEE_PERMILLE = 100      # 成交手续费 10%
CHECK_TICKS = 600       # 撮合间隔
TIER_COUNT = 3          # 速卖 / 常价 / 高价

#: 测通用的一件低级装备（木剑：1 级、价格低，挂单不会撞上负重/容量问题）
SAMPLE = "mujian"


def case(fn):
    CASES.append(fn)
    return fn


@contextmanager
def _demand(item_id: str, bp: int):
    """临时把某件物品的需求度改成 ``bp``（0 = 永不成交，10000 = 必成交）。

    为什么临时改而不是找"刚好会成交的种子"：后者把测试变成概率游戏，
    换个种子就飘。而"需求度设为满分=必成交"是**定义**上的必然，
    断言的是"成交后钱该怎么算"，不是"这次运气好不好"。
    """
    param = ITEMS[item_id]["auction"]
    original = dict(param)
    param["demand_bp"] = bp
    try:
        yield
    finally:
        param.clear()
        param.update(original)


def _session():
    # 必须把模块级的 ``DATA`` 传进去：``build_session()`` 默认自己再 load 一份，
    # 那样 ``_demand`` 改的就是另一份表，测试会假装通过（真实踩过一次）。
    session = build_session(data=DATA)
    populate(session, player_class="warrior")
    return session, session.world.get_entity("player")


def _expect(code: str, fn, *args, **kwargs):
    """断言 ``fn`` 以 ``code`` 拒绝码失败（返回值不是重点）。"""
    try:
        fn(*args, **kwargs)
    except auction.AuctionError as exc:
        assert exc.code == code, f"期望拒绝码 {code}，实际 {exc.code}：{exc}"
        return exc
    raise AssertionError(f"应以 {code} 拒绝，却成功了")


def _floor_and_cap(item_id: str) -> tuple[int, int]:
    bounds = auction.price_bounds(DATA, item_id)
    assert bounds is not None, f"{item_id} 应当可寄卖"
    return bounds


# ── 数据面：可寄卖与价格区间 ──────────────────────────

@case
def test_every_tradeable_item_is_listable():
    for item_id, spec in ITEMS.items():
        if spec.get("kind", "equip") not in ("equip", "book"):
            continue
        assert auction.auction_param(DATA, item_id) is not None, f"{item_id} 不可寄卖"


@case
def test_the_ceiling_is_above_the_floor_and_above_the_shop():
    for item_id, spec in ITEMS.items():
        if spec.get("kind", "equip") not in ("equip", "book"):
            continue
        floor, cap = _floor_and_cap(item_id)
        assert floor < cap, f"{item_id}: 底价 {floor} 不小于上限 {cap}"
        # 拍卖行存在的意义就是比卖商店多换钱：底价必须高于回收价
        assert floor > sell_price(DATA, item_id), (
            f"{item_id}: 底价 {floor} 不高于商店回收 {sell_price(DATA, item_id)}")
        # 上限按常量算，不能是随手写的数
        assert cap == max(floor + 1, buy_price(DATA, item_id) * CAP_PERMILLE // 1000)


@case
def test_shop_recycle_is_still_exactly_one_third():
    """回收价口径没被拍卖行改动（1/3 是既有纪律，别顺手改坏）。"""
    assert SELL_DIVISOR == 3
    for item_id in (SAMPLE, "fuhuo_jiezhi"):
        assert sell_price(DATA, item_id) == buy_price(DATA, item_id) // 3


# ── 概率曲线 ────────────────────────────────────────

@case
def test_chance_is_linear_from_the_floor_up_to_the_ceiling():
    floor, cap = _floor_and_cap(SAMPLE)
    demand = int(ITEMS[SAMPLE]["auction"]["demand_bp"])
    assert auction.chance_bp(DATA, SAMPLE, floor) == demand, "底价应是满概率"
    assert auction.chance_bp(DATA, SAMPLE, cap) == 0, "顶到上限应永不成交"
    # 中点 = 一半概率（线性是刻意的，曲线只能靠肉眼调）
    middle = floor + (cap - floor) // 2
    assert auction.chance_bp(DATA, SAMPLE, middle) == demand * (cap - middle) // (cap - floor)


@case
def test_ask_tiers_are_ordered_and_inside_the_bounds():
    floor, cap = _floor_and_cap(SAMPLE)
    assert len(auction.ASK_TIERS) == TIER_COUNT
    asks = [auction.ask_for_tier(DATA, SAMPLE, i) for i in range(TIER_COUNT)]
    assert asks == sorted(asks), f"档位要价必须递增：{asks}"
    assert all(floor <= ask < cap for ask in asks), f"档位越界：{asks} 不在 [{floor}, {cap})"
    # 最高档不能顶到上限——顶到就恒 0，这一档就成了摆设
    assert auction.chance_bp(DATA, SAMPLE, asks[-1]) > 0


# ── 挂牌 / 撤单 ─────────────────────────────────────

@case
def test_listing_takes_the_item_out_of_the_bag():
    session, player = _session()
    try:
        player.get(Bag).add(SAMPLE, 2)
        floor, _cap = _floor_and_cap(SAMPLE)
        got = auction.list_item(player, DATA, SAMPLE, floor, tick=10)
        assert player.get(Bag).items.get(SAMPLE, 0) == 1, "挂单必须让物品离手"
        assert got["seq"] == 1 and got["ask"] == floor
        book = player.get(AuctionBook)
        assert book is not None and len(book.listings) == 1
        assert book.listings[0]["item"] == SAMPLE
    finally:
        session.shutdown()


@case
def test_listing_is_refused_for_unlistable_or_out_of_range_items():
    session, player = _session()
    try:
        player.get(Bag).add("not_an_item", 1)
        _expect("unknown_item", auction.list_item, player, DATA, "not_an_item", 1)
        floor, cap = _floor_and_cap(SAMPLE)
        _expect("not_enough", auction.list_item, player, DATA, SAMPLE, floor)
        player.get(Bag).add(SAMPLE, 1)
        _expect("bad_price", auction.list_item, player, DATA, SAMPLE, floor - 1)
        _expect("bad_price", auction.list_item, player, DATA, SAMPLE, cap)
    finally:
        session.shutdown()


@case
def test_cancel_returns_the_item_and_unknown_seq_is_refused():
    session, player = _session()
    try:
        player.get(Bag).add(SAMPLE, 1)
        floor, _cap = _floor_and_cap(SAMPLE)
        got = auction.list_item(player, DATA, SAMPLE, floor)
        assert player.get(Bag).items.get(SAMPLE, 0) == 0
        auction.cancel_listing(player, DATA, got["seq"])
        assert player.get(Bag).items.get(SAMPLE, 0) == 1, "撤单应把物品还回来"
        assert player.get(AuctionBook).listings == []
        _expect("no_listing", auction.cancel_listing, player, DATA, got["seq"])
    finally:
        session.shutdown()


@case
def test_cancel_is_refused_when_the_bag_is_full():
    session, player = _session()
    try:
        player.get(Bag).add(SAMPLE, 1)
        floor, _cap = _floor_and_cap(SAMPLE)
        got = auction.list_item(player, DATA, SAMPLE, floor)
        # 把背包塞满（塞一件别的，SAMPLE 已经离手了）
        bag = player.get(Bag)
        filler = 0
        while not bag.full:
            bag.add(f"filler{filler}", 1)
            filler += 1
        _expect("bag_full", auction.cancel_listing, player, DATA, got["seq"])
        assert len(player.get(AuctionBook).listings) == 1, "拒绝撤单后挂单必须还在"
    finally:
        session.shutdown()


# ── 撮合 ────────────────────────────────────────────

@case
def test_a_guaranteed_sale_pays_the_ask_minus_the_fee():
    session, player = _session()
    try:
        player.get(Wallet).gold = 0
        player.get(Bag).add(SAMPLE, 1)
        floor, _cap = _floor_and_cap(SAMPLE)
        with _demand(SAMPLE, 10000):  # 需求度满分 = 挂底价必成交
            auction.list_item(player, DATA, SAMPLE, floor)
            sold = auction.settle(player, DATA, seed=7, tick=CHECK_TICKS)
        assert len(sold) == 1, sold
        assert sold[0]["gross"] == floor
        assert sold[0]["fee"] == floor * FEE_PERMILLE // 1000
        assert player.get(Wallet).gold == floor - floor * FEE_PERMILLE // 1000
        assert player.get(AuctionBook).listings == [], "成交后挂单必须消失"
        assert player.get(Bag).items.get(SAMPLE, 0) == 0, "物品被买走了，不该回背包"
    finally:
        session.shutdown()


@case
def test_zero_demand_never_sells_and_keeps_the_listing():
    session, player = _session()
    try:
        player.get(Wallet).gold = 0
        player.get(Bag).add(SAMPLE, 1)
        floor, _cap = _floor_and_cap(SAMPLE)
        with _demand(SAMPLE, 0):
            auction.list_item(player, DATA, SAMPLE, floor)
            sold = auction.settle(player, DATA, seed=7, tick=CHECK_TICKS)
        assert sold == []
        assert player.get(Wallet).gold == 0
        assert len(player.get(AuctionBook).listings) == 1, "没卖掉的挂单要继续挂着"
    finally:
        session.shutdown()


@case
def test_the_sale_rate_tracks_the_declared_chance():
    """统计口径：挂底价跑满 ``ROUNDS`` 轮，成交率应贴近 ``demand_bp``。"""
    ROUNDS = 400
    TOLERANCE = 1000  # 万分比容差（±10 个百分点）
    session, player = _session()
    try:
        floor, _cap = _floor_and_cap(SAMPLE)
        demand = int(ITEMS[SAMPLE]["auction"]["demand_bp"])
        hits = 0
        for index in range(ROUNDS):
            player.get(Bag).add(SAMPLE, 1)
            auction.list_item(player, DATA, SAMPLE, floor)
            # 每轮换一个 seq（流名含 seq），等价于"不同的挂单各掷一次"
            sold = auction.settle(player, DATA, seed=index, tick=CHECK_TICKS)
            hits += len(sold)
            for listing in list(player.get(AuctionBook).listings):
                auction.cancel_listing(player, DATA, listing["seq"])
        rate = hits * 10000 // ROUNDS
        assert abs(rate - demand) <= TOLERANCE, f"实测成交率 {rate}bp 偏离表值 {demand}bp"
    finally:
        session.shutdown()


@case
def test_one_listing_does_not_shift_another_listings_stream():
    """两张单互不影响：流名只含各自的 seq，多挂一张不该挪动另一张的运气。"""
    session, player = _session()
    try:
        floor, _cap = _floor_and_cap(SAMPLE)
        # A 单独挂
        player.get(Bag).add(SAMPLE, 1)
        solo = auction.list_item(player, DATA, SAMPLE, floor)
        solo_sold = bool(auction.settle(player, DATA, seed=99, tick=CHECK_TICKS))
        auction.cancel_listing(player, DATA, solo["seq"])
        player.get(Wallet).gold = 0

        # A 与 B 一起挂（A 拿到同一个 seq 需要重置账本）
        book = player.get(AuctionBook)
        book.listings.clear()
        book.next_seq = 1
        player.get(Bag).add(SAMPLE, 2)
        first = auction.list_item(player, DATA, SAMPLE, floor)
        auction.list_item(player, DATA, SAMPLE, floor)
        assert first["seq"] == solo["seq"], "两次实验里 A 必须拿到同一个 seq"
        pair = auction.settle(player, DATA, seed=99, tick=CHECK_TICKS)
        pair_sold = any(item["seq"] == solo["seq"] for item in pair)
        assert pair_sold == solo_sold, "另一张挂单改变了 A 的成交结果"
    finally:
        session.shutdown()


@case
def test_two_listings_do_not_share_one_random_stream():
    """两张单不能共用一条随机流——否则它们的成交永远**同步**（同进同退）。

    上一个用例只证明"多挂一张不会挪动 A"，它挡不住另一种坏法：
    流名里漏掉 ``seq``（``auction:{round}``），两张单会取到**同一个**随机数，
    于是要么一起成交、要么一起流拍。50% 概率下 40 个种子里几乎必然出现
    "只成交一张"；一条都没出现，就说明它们在共用流。
    """
    split = False
    for seed in range(1, 41):
        session, player = _session()
        try:
            with _demand(SAMPLE, 5000):
                floor, _cap = _floor_and_cap(SAMPLE)
                player.get(Bag).add(SAMPLE, 2)
                auction.list_item(player, DATA, SAMPLE, floor)
                auction.list_item(player, DATA, SAMPLE, floor)
                sold = auction.settle(player, DATA, seed=seed, tick=CHECK_TICKS)
                if len({item["seq"] for item in sold}) == 1:
                    split = True
                    break
        finally:
            session.shutdown()
    assert split, "40 个种子里两张挂单从未分开成交——它们在共用同一条随机流"


@case
def test_settle_is_deterministic_for_the_same_seed():
    def run_once(seed: int) -> list[int]:
        session, player = _session()
        try:
            floor, _cap = _floor_and_cap(SAMPLE)
            player.get(Bag).add(SAMPLE, 1)
            auction.list_item(player, DATA, SAMPLE, floor)
            sold = auction.settle(player, DATA, seed=seed, tick=CHECK_TICKS)
            return [item["seq"] for item in sold]
        finally:
            session.shutdown()

    assert run_once(2026) == run_once(2026), "同种子必须同结果"


# ── 系统接线 ────────────────────────────────────────

@case
def test_the_system_settles_on_its_own_schedule():
    session, player = _session()
    try:
        player.get(Wallet).gold = 0
        player.get(Bag).add(SAMPLE, 1)
        floor, _cap = _floor_and_cap(SAMPLE)
        net = floor - floor * FEE_PERMILLE // 1000
        with _demand(SAMPLE, 10000):
            auction.list_item(player, DATA, SAMPLE, floor)
            session.step(CHECK_TICKS - 1)          # 还差一刻：不该结算
            assert len(player.get(AuctionBook).listings) == 1, "提前结算了"
            assert player.get(Wallet).gold == 0
            session.step(2)                        # 跨过检查点
            assert player.get(AuctionBook).listings == [], "到点没结算"
            assert player.get(Wallet).gold == net
    finally:
        session.shutdown()


@case
def test_auction_rolls_leave_the_main_stream_untouched():
    """挂单不该挪动主战斗流：裸跑与挂着单跑，同一条战斗流逐位一致。"""
    def sample_stream(ticks: int, with_listing: bool) -> list[int]:
        session, player = _session()
        try:
            floor, _cap = _floor_and_cap(SAMPLE)
            if with_listing:
                player.get(Bag).add(SAMPLE, 1)
                auction.list_item(player, DATA, SAMPLE, floor)
            out = []
            for tick in range(ticks):
                rng = Rng(session.seed, f"physical:probe->{tick}")
                out.append(rng.rand_int(1, 10000))
            return out
        finally:
            session.shutdown()

    plain = sample_stream(300, with_listing=False)
    listed = sample_stream(300, with_listing=True)
    assert plain == listed, "挂单改变了主战斗流的位置"


# ── 面板 ────────────────────────────────────────────

@case
def test_panel_shows_the_ask_and_the_odds():
    session, player = _session()
    try:
        player.get(Bag).add(SAMPLE, 2)
        panel = build_auction_panel(player=player, data=DATA)
        rows = [row for row in panel.rows if row.action == "list"]
        assert len(rows) == 1, [row.text for row in panel.rows]
        ask = auction.ask_for_tier(DATA, SAMPLE, 0)
        chance = auction.chance_bp(DATA, SAMPLE, ask)
        assert str(ask) in rows[0].detail, rows[0].detail
        assert f"{chance // 100}%" in rows[0].detail, rows[0].detail
    finally:
        session.shutdown()


@case
def test_panel_lists_my_listings_and_the_tier_switch():
    session, player = _session()
    try:
        floor, _cap = _floor_and_cap(SAMPLE)
        player.get(Bag).add(SAMPLE, 1)
        auction.list_item(player, DATA, SAMPLE, floor, tick=5)
        panel = build_auction_panel(player=player, data=DATA)
        cancels = [row for row in panel.rows if row.action == "cancel"]
        assert len(cancels) == 1, [row.text for row in panel.rows]
        assert str(floor) in cancels[0].detail

        switch = [row for row in panel.rows if row.action == "cycle_tier"]
        assert len(switch) == 1
        assert auction.ASK_TIERS[0][0] in switch[0].text
        assert auction.cycle_ask_tier(player) == auction.ASK_TIERS[1][0]
        assert player.get(AuctionBook).ask_tier == 1
        # 档位会绕回第一档
        auction.cycle_ask_tier(player)
        auction.cycle_ask_tier(player)
        assert player.get(AuctionBook).ask_tier == 0
    finally:
        session.shutdown()


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    return passed, failed


if __name__ == "__main__":
    p, f = run()
    print(f"test_auction: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
