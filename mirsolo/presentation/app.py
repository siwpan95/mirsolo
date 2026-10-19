"""窗口与主循环 —— 等距斜俯视可玩版本。

职责边界：

- **不写游戏规则**：移动/攻击/技能都通过 ``world.set_data`` 的意图键交给 ECS
  系统，换图交给 ``PortalSystem``；面板上的买卖穿脱只是把"动作 + 物品 id"
  转手给 ``mirsolo.items.equipment`` / ``mirsolo.economy.shop``。与 headless
  冒烟走的是**同一条结算路径**（这点很关键——否则"看得见的版本"
  与"测过的版本"会悄悄分叉）；
- **只管表现**：相机跟随、HUD、面板绘制、配色、输入采集。

定点步长：窗口循环用 ``GameEngine.advance(real_dt)`` 把真实时间喂给
``FixedTimestepLoop``，逻辑仍按 ``session.tick_dt``（0.1s）推进，
所以掉帧只会少画几帧，不会让战斗结算变快变慢。
"""

from __future__ import annotations

import math
import time

import pyglet
from pyglet import shapes
from pyglet.text import Label

from mirsolo.bootstrap import GameSession, build_session, populate
from mirsolo.determinism import Rng
from mirsolo.combat.resolver import EVT_DEATH
from mirsolo.components import (
    Bag,
    Codex,
    Cooldowns,
    Downed,
    Identity,
    Loadout,
    Mining,
    Position,
    Progression,
    Shield,
    Skills,
    Vitals,
    Wallet,
    Wear,
)
from mirsolo.actor import codex, mercenary, penalty
from mirsolo.actor.systems import (
    KEY_LAST_ORE,
    KEY_MINING_VEINS,
    KEY_LAST_POTION,
    KEY_MINE_TOGGLE,
    KEY_POTION_INTENT,
)
from mirsolo.economy import auction
from mirsolo.economy.shop import (
    TradeError,
    buy,
    deposit,
    repair_all,
    resupply,
    sell,
    sell_ores,
    withdraw,
)
from mirsolo.items import mining, potion, wear
from mirsolo.items.equipment import EquipError, equip, recompute, unequip
from mirsolo.items.rings import RingError, try_travel
from mirsolo.presentation.hud import (
    MAX_SKILL_SLOTS,
    SIEGE_BANNER_HEIGHT,
    HudLayout,
    build_hud,
)
from mirsolo.presentation.iso_camera import IsoCamera
from mirsolo.presentation.panels import Panel, Row, build_panel
from mirsolo.presentation.renderer import GLRenderer
from mirsolo.presentation.scene import MAP_PALETTE, build_instances
from mirsolo.progression.exp import apply_growth
from mirsolo.skills import EVT_SKILL_CAST, SELF_KINDS, SkillError, learn
from mirsolo.siege.campaign import (
    EVT_SIEGE_LOST,
    EVT_SIEGE_WON,
    siege_status,
    start_siege,
)
from mirsolo.world.travel import EVT_MAP_CHANGED

FONT = "Microsoft YaHei"
MELEE_RANGE = 1
TOAST_SECONDS = 1.2

#: 技能栏键位（与 HUD 的 1..5 一一对应；小键盘也收，避免笔记本外接键盘按不出来）
SKILL_KEYS = (
    pyglet.window.key._1, pyglet.window.key._2, pyglet.window.key._3,
    pyglet.window.key._4, pyglet.window.key._5,
    pyglet.window.key.NUM_1, pyglet.window.key.NUM_2, pyglet.window.key.NUM_3,
    pyglet.window.key.NUM_4, pyglet.window.key.NUM_5,
)

#: 特戒指动键（传送戒指）。与技能键一样按一次发动一次，长按不连发
RING_KEY = pyglet.window.key.T

#: 原地复活键。刻意不用 V——V 已经是仓库面板的开关；
#: 同一个键在站立/倒地两种状态下含义不同，迟早按错。
REVIVE_KEY = pyglet.window.key.R

#: 喝药键：Q = 红药（回血），E = 蓝药（回蓝）。挑 WASD 左手边这两个，
#: 是因为血见底的那一刻手就在那儿，不用挪开再找。
POTION_KEYS = {
    pyglet.window.key.Q: "hp",
    pyglet.window.key.E: "mp",
}

# 挖矿键：F。按一次开工、再按一次收工；
# 走开也算收工（系统侧自己停），所以不存在“人在别处、矿在这边”的脏状态。
MINE_KEY = pyglet.window.key.F

#: 面板开关键 → 面板名。放在这里而不是散在 if 里：加面板只改这张表
PANEL_KEYS = {
    pyglet.window.key.C: "character",
    pyglet.window.key.M: "mercenary",
    pyglet.window.key.J: "codex",
    pyglet.window.key.N: "auction",
    pyglet.window.key.B: "shop",
    pyglet.window.key.V: "warehouse",
}

#: 挖矿拒绝码 → 玩家能看懂的话（码本身给测试断言用，不上屏）
MINE_ERRORS = {
    "no_vein": "脚下没有矿点",
    "no_pick": "手上没握鹤嘴锄",
    "downed": "倒地挖不了矿",
    "depleted": "这个矿点挖空了",
    "overweight": "背不动了，回城卸货",
    "bag_full": "背包装不下新种矿石",
}

#: 特戒拒绝码 → 玩家能看懂的话（码本身给测试断言用，不上屏）
RING_ERRORS = {
    "no_ring": "没戴戒指",
    "wrong_ring": "这枚戒指不是传送",
    "unknown_map": "目标地图不存在",
    "same_map": "已经在这张图了",
    "cooling": "冷却中",
    "dead": "死亡状态不能传送",
}

#: 复活的拒绝码 → 玩家能看懂的话。``no_gold`` 不在表里——它的文案含
#: 动态数字（还差多少金），直接把异常消息上屏更准
REVIVE_ERRORS = {
    "not_downed": "还没倒下，不用复活",
    "missing_components": "角色数据不完整，无法复活",
}

#: 拒绝码 → 玩家能看懂的话。码本身是给测试断言用的，不能直接上屏
EQUIP_ERRORS = {
    "unknown_item": "未知物品",
    "not_equipment": "这不是装备",
    "class_mismatch": "职业不符",
    "level_too_low": "等级不够",
    "not_in_bag": "背包里没有",
    "overweight": "负重不足",
    "bag_full": "背包已满",
    "slot_empty": "该槽没有装备",
    "missing_components": "角色数据异常",
}
TRADE_ERRORS = {
    "unknown_item": "未知物品",
    "no_gold": "金币不足",
    "bag_full": "背包已满",
    "overweight": "负重不足",
    "not_enough": "数量不足",
    "stash_full": "仓库已满",
    "bad_count": "数量不对",
    "nothing_to_repair": "这件还是好的",
    "nothing_to_sell": "没有可卖的东西",
    "missing_components": "角色数据异常",
}


AUCTION_ERRORS = {
    "unknown_item": "未知物品",
    "not_listable": "这个不能寄卖",
    "bad_price": "要价超出允许范围",
    "no_listing": "没有这张挂单",
    "not_enough": "数量不足",
    "bag_full": "背包已满",
    "bad_count": "数量不对",
    "missing_components": "角色数据异常",
}


#: 佣兵拒绝码 → 人话。码本身给测试断言用，不上屏
MERC_ERRORS = {
    "unknown_merc": "没有这种佣兵",
    "level_too_low": "等级不够",
    "no_gold": "金币不够",
    "squad_full": "随从已满",
    "kind_full": "这种已带满",
    "missing_components": "角色数据异常",
}


class MirsoloWindow(pyglet.window.Window):
    """主窗口：把会话、相机、渲染器、HUD 串起来。"""

    def __init__(self, *, session: GameSession, width: int = 1280, height: int = 720,
                 caption: str = "mirsolo — 热血传奇单机复刻（致敬）"):
        super().__init__(width=width, height=height, caption=caption, resizable=True,
                         vsync=False)
        self.session = session
        self.camera = IsoCamera(width=width, height=height, view_height=13.0,
                                follow_lerp=1.0)
        self.renderer = GLRenderer()
        self.pressed: set[int] = set()
        self._hud_shapes: list[shapes.Shape] = []
        self._hud_labels: list[Label] = []
        self._last_real_dt = 1.0 / 60.0
        self._toast = ""
        self._toast_until = 0.0
        self._toast_big = False
        self.panel: str | None = None
        self.panel_scroll = 0
        self._panel: Panel | None = None

        bus = session.world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_SKILL_CAST, self._on_skill_cast)
            bus.subscribe("skill:rejected", self._on_skill_rejected)
            bus.subscribe(EVT_MAP_CHANGED, self._on_map_changed)
            bus.subscribe(EVT_SIEGE_WON, self._on_siege_won)
            bus.subscribe(EVT_SIEGE_LOST, self._on_siege_lost)
            bus.subscribe(mercenary.EVT_DISMISS, self._on_merc_dismiss)
            bus.subscribe(codex.EVT_UNLOCK, self._on_codex_unlock)

        player = session.world.get_entity("player")
        if player is not None:
            pos = player.get(Position)
            self.camera.center = (pos.x, pos.y)

    # ── 反馈浮字 ────────────────────────────────────

    def _say(self, text: str) -> None:
        """一行浮字提示（1.2 秒后自动消失）。"""
        self._toast = text
        self._toast_until = time.time() + TOAST_SECONDS
        self._toast_big = False

    def _on_map_changed(self, payload: dict) -> None:
        maps = self.session.data.maps["maps"]
        name = maps.get(payload.get("to"), {}).get("name", payload.get("to"))
        self._say(f"进入 {name}")
        self.panel_scroll = 0
        player = self.session.world.get_entity("player")
        if player is not None:  # 传送是瞬移，相机不要插值过去（否则会飞越大半张图）
            pos = player.get(Position)
            self.camera.center = (pos.x, pos.y)

    # ── 攻城战役反馈 ────────────────────────────────

    def _on_siege_won(self, payload: dict) -> None:
        rewards = self.session.data.sieges["sieges"].get(payload.get("siege"), {})
        title = (rewards.get("rewards") or {}).get("title")
        gold = (rewards.get("rewards") or {}).get("gold", 0)
        word = f"攻陷 {payload.get('name', '沙巴克')}"
        self._on_notice(f"{word}！金币 +{gold}" + (f"　获得称号「{title}」" if title else ""))

    def _on_siege_lost(self, payload: dict) -> None:
        self._on_notice(f"{payload.get('name', '攻城')} 失败——守军重新布防")

    def _on_merc_dismiss(self, payload: dict) -> None:
        """佣兵到期离队。必须**说一句**，否则玩家只会看到一个人凭空消失。"""
        self._say(f"{payload.get('name', '佣兵')} 到期离队")

    def _on_codex_unlock(self, payload: dict) -> None:
        """图鉴首次解锁。带进度与奖励经验，让人知道这一刀额外赚了什么。"""
        name = payload.get("name", payload.get("species", "?"))
        exp = int(payload.get("exp", 0))
        total = int(payload.get("total", 0))
        got = int(payload.get("unlocked", 0))
        self._say(f"图鉴解锁 {name} {got}/{total}　经验 +{exp}")

    def _on_notice(self, text: str) -> None:
        """重要提示（胜负这类）：比普通浮字停得久一些，免得一眨眼就没了。"""
        self._toast = text
        self._toast_until = time.time() + TOAST_SECONDS * 3
        self._toast_big = True

    # ── 施放反馈（浮字） ────────────────────────────

    def _on_skill_cast(self, payload: dict) -> None:
        spec = self.session.data.skills["skills"].get(payload.get("skill"), {})
        targets = payload.get("targets") or [{}]
        first = targets[0]
        detail = ""
        if "damage" in first:
            detail = f"伤害 {first['damage']}"
        elif "heal" in first:
            detail = f"回复 {first['heal']}"
        elif "shield" in first:
            detail = f"护盾 {first['shield']}"
        elif "damage_per_tick" in first:
            detail = f"中毒 {first['damage_per_tick']}/跳"
        elif "summon" in first:
            detail = "召唤成功"
        elif "until_tick" in first:
            detail = "隐身中"
        self._say(f"{spec.get('name', payload.get('skill'))} {detail}".strip())

    def _on_skill_rejected(self, payload: dict) -> None:
        items = payload.get("items") or []
        if not items:
            return
        reasons = {
            "no_mp": "魔法不足",
            "cooldown": "冷却中",
            "not_learned": "尚未学会",
            "out_of_range": "距离太远",
            "bad_target": "目标无效",
            "no_targets": "范围内没有目标",
        }
        first = items[0]
        name = self.session.data.skills["skills"].get(
            first.get("skill"), {}).get("name", first.get("skill"))
        self._say(f"{name}：{reasons.get(first.get('code'), first.get('code'))}")

    # ── 事件 ────────────────────────────────────────

    def on_resize(self, width: int, height: int) -> None:
        super().on_resize(width, height)
        self.camera.width = width
        self.camera.height = height

    def on_key_press(self, symbol: int, modifiers: int) -> None:
        key = pyglet.window.key
        self.pressed.add(symbol)
        if symbol == key.ESCAPE:
            # 面板开着时 ESC 只关面板——直接退游戏会让人白白丢掉一次操作
            if self.panel is not None:
                self._close_panel()
                return
            self.close()
            return
        if symbol in PANEL_KEYS:
            self._toggle_panel(PANEL_KEYS[symbol])
            return
        if symbol == RING_KEY:
            self._use_ring()
            return
        if symbol == REVIVE_KEY:
            self._revive_in_place()
            return
        if symbol in POTION_KEYS:
            self._drink(POTION_KEYS[symbol])
            return
        if symbol == MINE_KEY:
            self._toggle_mining()
            return
        # 技能栏 1..5（数字键与数字小键盘都收）：按一次放一次，
        # 长按不连发——连发交给冷却门控反而会让节奏变乱。
        if symbol in SKILL_KEYS:
            self._cast_slot(SKILL_KEYS.index(symbol) % MAX_SKILL_SLOTS)

    def on_key_release(self, symbol: int, modifiers: int) -> None:
        self.pressed.discard(symbol)

    def on_mouse_scroll(self, x: int, y: int, scroll_x: float, scroll_y: float) -> None:
        if self.panel is not None:
            limit = self._panel.scroll_max if self._panel is not None else 0
            step = 3 if scroll_y > 0 else -3
            self.panel_scroll = max(0, min(limit, self.panel_scroll - step))
            return
        self.camera.zoom(0.9 if scroll_y > 0 else 1.1)

    def on_mouse_press(self, x: int, y: int, button: int, modifiers: int) -> None:
        if self.panel is None or self._panel is None:
            return
        if button != pyglet.window.mouse.LEFT:
            return
        row = self._panel.row_at(x, y)
        if row is not None:
            self._do_row(row)

    # ── 面板 ────────────────────────────────────────

    def _toggle_panel(self, key: str) -> None:
        if self.panel == key:
            self._close_panel()
            return
        self.panel = key
        self.panel_scroll = 0

    def _close_panel(self) -> None:
        self.panel = None
        self.panel_scroll = 0
        self._panel = None

    def _do_row(self, row: Row) -> None:
        """把面板行转成一次规则层调用。

        真正的判定全在 ``equipment`` / ``shop`` 里：这里只负责"翻译拒绝码"
        和"给一句人话"。**绝不在窗口里重算价格或判断等级**——两套判定
        一定会分叉，而分叉出来的那个 bug 只有玩家能碰到。
        """
        player = self.session.world.get_entity("player")
        if player is None:
            return
        data = self.session.data
        if not row.enabled:
            self._say(f"{row.text}：{row.detail or '不能操作'}")
            return
        try:
            if row.action == "equip":
                result = equip(player, data, row.item)
                replaced = result.get("replaced")
                if replaced:
                    old = data.items["items"].get(replaced, {}).get("name", replaced)
                    self._say(f"装备 {row.text}（换下 {old}）")
                else:
                    self._say(f"装备 {row.text}")
            elif row.action == "unequip":
                unequip(player, data, row.item)
                self._say(f"脱下 {row.text.split('：')[-1]}")
            elif row.action == "buy":
                # 药水按捆买，捆数与面板价签同一个出处（potion.BUY_BUNDLE）；
                # 不照着买就会出现“价签写 7000、实际扣 700”的错账。
                kind = data.items["items"].get(row.item, {}).get("kind")
                count = potion.BUY_BUNDLE if kind == "consumable" else 1
                got = buy(player, data, row.item, count)
                self._say(f"买入 {row.text} −{got['paid']} 金")
            elif row.action == "sell":
                got = sell(player, data, row.item)
                self._say(f"卖出 {row.text} ＋{got['earned']} 金")
            elif row.action == "deposit":
                deposit(player, data, row.item)
                self._say(f"存入 {row.text}")
            elif row.action == "withdraw":
                withdraw(player, data, row.item)
                self._say(f"取出 {row.text}")
            elif row.action == "list":
                ask = auction.ask_for_tier(data, row.item, auction.ask_tier(player))
                got = auction.list_item(player, data, row.item, ask,
                                       tick=self.session.tick)
                self._say(f"寄卖 {row.text}　要价 {got['ask']} 金")
            elif row.action == "cancel":
                got = auction.cancel_listing(player, data, int(row.item))
                self._say(f"撤单 {got['item']} ×{got['count']}")
            elif row.action == "repair_all":
                got = repair_all(player, data)
                self._say(f"修理 {len(got['items'])} 件 −{got['paid']} 金")
            elif row.action == "sell_ores":
                got = sell_ores(player, data)
                self._say(f"卖掉 {len(got['items'])} 种矿石 ＋{got['earned']} 金")
            elif row.action == "cycle_tier":
                self._say(f"报价档：{auction.cycle_ask_tier(player)}")
            elif row.action == "hire":
                got = mercenary.hire(self.session.world, data, player, row.item,
                                     tick=self.session.tick)
                self._say(f"雇佣 {got['name']} −{got['paid']} 金")
            else:  # "none"：不可操作的条目（技能书等），点了给句解释
                self._say(f"{row.text}：这个不能直接使用")
        except EquipError as exc:
            self._say(f"{row.text}：{EQUIP_ERRORS.get(exc.code, exc.code)}")
        except TradeError as exc:
            self._say(f"{row.text}：{TRADE_ERRORS.get(exc.code, exc.code)}")
        except auction.AuctionError as exc:
            self._say(f"{row.text}：{AUCTION_ERRORS.get(exc.code, exc.code)}")
        except mercenary.MercenaryError as exc:
            self._say(f"{row.text}：{MERC_ERRORS.get(exc.code, exc.code)}")

# ── 特戒（T 键） ────────────────────────────────

    def _use_ring(self) -> None:
        """传送戒指：规则全在 ``items.rings.try_travel``，这里只翻译拒绝码。

        与买卖穿脱同一条纪律：**窗口层不做 judging**——它连"目标图是哪张"
        都是从数据表读来的，换成任何别的实现都不会分叉。
        """
        player = self.session.world.get_entity("player")
        if player is None:
            return
        try:
            result = try_travel(self.session.data, self.session, player,
                                tick=self.session.tick)
        except RingError as exc:
            self._say(f"特戒：{RING_ERRORS.get(exc.code, exc.code)}")
            return
        self._on_map_changed(result)

    def _revive_in_place(self) -> None:
        """原地复活（``R`` 键）：花金币、位置不动，免掉经验/金币/掉物三件惩罚。

        规则全在 ``actor.penalty``，这里只翻译拒绝码——与买卖穿脱、特戒同一条
        纪律：窗口层不做 judging。

        金币不够时**不做赊账**：金币一旦允许为负，商店回收价与拍卖行底价全都得
        跟着处理负数，代价远大于收益。玩家等几秒自动回城即可。
        """
        player = self.session.world.get_entity("player")
        if player is None:
            return
        data = self.session.data
        try:
            tick = int(self.session.tick)
            rng = Rng(self.session.seed, f"death:{player.id}@{tick}")
            got = penalty.settle(data, player, rng=rng, tick=tick,
                                 in_place=True)
        except penalty.PenaltyError as exc:
            if exc.code == "no_gold":
                self._say(str(exc))
            else:
                self._say(f"复活：{REVIVE_ERRORS.get(exc.code, exc.code)}")
            return
        self._say(f"原地复活 -{got['paid']} 金（经验与背包都没掉）")

    def _drink(self, want: str) -> None:
        """喝药：只往世界投一个意图，真喝由 ``PotionSystem`` 在下一个 tick 做。

        为什么不在窗口里直接调 ``potion.drink``：公共冷却的账写在
        ``PotionState.ready_at`` 上、由系统维护，窗口层绕过去就等于开了
        第二个入账口，表现层与 headless 会走出两套喝药节奏。与移动/攻击
        同一条纪律——表现层只投意图。
        """

        player = self.session.world.get_entity("player")
        if player is None or player.has(Downed):
            return  # 倒地期间连喝药都不行，否则死亡惩罚就只剩掉东西了
        self.session.world.set_data(KEY_POTION_INTENT, want)

    def _report_potion(self) -> None:
        """把上一次**手动**喝药的结果翻译成一句话。

        自动喝药不写这个键（见 ``KEY_LAST_POTION`` 的说明）：它每 tick 都可能
        发生，刷成提示就是噪音，血条动了本身就是反馈。
        """

        got = self.session.world.get_data(KEY_LAST_POTION)
        if not got:
            return
        self.session.world.set_data(KEY_LAST_POTION, None)
        if not got.get("ok"):
            self._say(str(got.get("message", "喝不了")))
            return
        name = self.session.data.items["items"].get(
            got["item"], {}).get("name", got["item"])
        parts = []
        if got.get("hp"):
            parts.append(f"HP +{got['hp']}")
        if got.get("mp"):
            parts.append(f"MP +{got['mp']}")
        joined = "　".join(parts)
        self._say(f"喝下 {name}　{joined}　剩 {got['left']} 瓶")

    def _toggle_mining(self) -> None:
        """按 F：开工 / 收工。

        只投意图，不直接改状态——与移动/攻击同一条纪律，
        窗口层绕过去就会和 headless 走出两套节奏。
        """

        player = self.session.world.get_entity("player")
        if player is None:
            return
        self.session.world.set_data(KEY_MINE_TOGGLE, True)

    def _report_ore(self) -> None:
        """把上一锄的结果翻成一句话（**空锄不回音**）。

        空锄不回音与自动喝药同理：十刻一锄，每锄都弹一行的话屏幕就没法看了。
        挖到了才弹；挖不动的原因也弹——只有“该去别处”的时候才需要告诉玩家。
        """

        got = self.session.world.get_data(KEY_LAST_ORE)
        if not got:
            return
        self.session.world.set_data(KEY_LAST_ORE, None)
        if not got.get("ok"):
            self._say(MINE_ERRORS.get(got.get("code", ""), got.get("code", "挖不动")))
            return
        name = self.session.data.items["items"].get(
            got["ore"], {}).get("name", got["ore"])
        self._say(f"挖到 {name}")

    def _mining_caption(self) -> str:
        """HUD 那一行挖矿状态。空串 = 不占这一行。

        带上“第几锄/储量”是因为挖矿的节奏本身就是信息——玩家得先知道
        矿快挖空了，才会去换下一个点，而不是挖到弹“挖空了”才反应过来。
        """

        player = self.session.world.get_entity("player")
        if player is None:
            return ""
        state = player.get(Mining)
        if state is None or not state.active:
            return ""
        map_spec = self.session.data.maps["maps"][self.session.map_id]
        index = int(state.vein)
        mines = map_spec.get("mines", [])
        if index < 0 or index >= len(mines):
            return ""
        spec = self.session.data.vein(str(mines[index]["vein"]))
        ledger = (self.session.world.get_data(KEY_MINING_VEINS) or {}).get(
            str(index)) or {}
        return f"挖矿中　{spec.get('name', '')}　{int(ledger.get('swings', 0))}/{int(spec.get('capacity', 0))} 锄"

    # ── 输入 → 意图 ─────────────────────────────────


    def _collect_intent(self) -> tuple[int, int]:
        key = pyglet.window.key
        dx = dy = 0
        if key.A in self.pressed or key.LEFT in self.pressed:
            dx -= 1
        if key.D in self.pressed or key.RIGHT in self.pressed:
            dx += 1
        if key.W in self.pressed or key.UP in self.pressed:
            dy += 1
        if key.S in self.pressed or key.DOWN in self.pressed:
            dy -= 1
        return dx, dy

    def _target_in_range(self, reach: int = MELEE_RANGE):
        """最近的敌对怪（切比雪夫距离 ≤ ``reach``）；没有就返回 None。"""
        player = self.session.world.get_entity("player")
        if player is None:
            return None
        faction = player.get(Identity).faction
        ppos = player.get(Position)
        best, best_dist = None, 10**9
        for mob in self.session.world.each(Identity, Position, Vitals):
            ident = mob.get(Identity)
            if ident.faction == faction or not mob.get(Vitals).alive:
                continue
            mpos = mob.get(Position)
            dist = int(max(abs(mpos.x - ppos.x), abs(mpos.y - ppos.y)))
            if dist < best_dist:
                best, best_dist = mob, dist
        return best if best is not None and best_dist <= reach else None

    # ── 技能 ────────────────────────────────────────

    def _skill_order(self) -> list[str]:
        """技能栏顺序：按学习等级升序（同级按 id），取前 5。

        这是键位顺序的**唯一定义处**——HUD 的 ``known_skills`` 用的就是
        本方法算好的有序字典，键位与格子不可能错位。
        """
        player = self.session.world.get_entity("player")
        skills = player.get(Skills) if player is not None else None
        if skills is None:
            return []
        table = self.session.data.skills["skills"]
        ordered = sorted(skills.known,
                         key=lambda sid: (int(table[sid]["level_req"]), sid))
        return ordered[:MAX_SKILL_SLOTS]

    def _cast_slot(self, index: int) -> None:
        order = self._skill_order()
        if index >= len(order):
            return
        skill_id = order[index]
        spec = self.session.data.skills["skills"][skill_id]
        player = self.session.world.get_entity("player")
        if player is None or player.has(Downed):
            return  # 倒地期间放不了技能
        # 自身类（护盾/隐身/召唤）不需要目标；治疗类单机先自疗；攻击类找最近敌人
        kind = spec["kind"]
        if kind in SELF_KINDS:
            target = None
        elif kind in ("heal", "group_heal"):
            target = player
        else:
            target = self._target_in_range(int(spec.get("range", MELEE_RANGE)))
            if target is None:
                self._toast = f"{spec['name']}：范围内没有目标"
                self._toast_until = time.time() + 1.2
                return
        self.session.cast_skill(player, skill_id, target)

    # ── 每帧 ────────────────────────────────────────

    def tick(self, real_dt: float) -> None:
        """推进逻辑 + 采集输入（由 :meth:`run` 或 pyglet 时钟调用）。"""
        self._last_real_dt = min(0.25, max(0.0, real_dt))
        player = self.session.world.get_entity("player")
        # 倒地期间一律不收操作：躺着还能走、还能砍的话，
        # 整条死亡惩罚就退化成血少了一点而已
        stalled = player is not None and player.has(Downed)
        self.session.world.set_data(
            "move_intent", (0, 0) if stalled else self._collect_intent())
        if pyglet.window.key.SPACE in self.pressed and not stalled:
            target = self._target_in_range()
            if target is not None:
                self.session.queue_attack(self.session.world.get_entity("player"), target)
        self.session.engine.advance(self._last_real_dt)
        self._report_potion()
        self._report_ore()

        if player is not None:
            pos = player.get(Position)
            self.camera.follow(pos.x, pos.y + 1.0)
            map_spec = self.session.data.maps["maps"][self.session.map_id]
            self.camera.clamp_to_map(*map_spec["size"])

    def on_draw(self) -> None:
        self.switch_to()
        self.renderer.begin_frame()
        map_spec = self.session.data.maps["maps"][self.session.map_id]
        self.renderer.draw_floor(self.camera, map_spec, self.session.map_id,
                                 MAP_PALETTE.get(map_spec["kind"], (0.3, 0.3, 0.3)))
        self.renderer.draw_instances(self.camera,
                                     build_instances(self.session.world, self.session.data,
                                map_id=self.session.map_id))
        self.renderer.end_3d()
        self._draw_hud()
        self._draw_panel()
        self._flush()

    # ── HUD ────────────────────────────────────────

    def _draw_hud(self) -> None:
        for shape in self._hud_shapes:
            shape.delete()
        for label in self._hud_labels:
            label.delete()
        self._hud_shapes.clear()
        self._hud_labels.clear()

        player = self.session.world.get_entity("player")
        if player is None:
            return
        map_spec = self.session.data.maps["maps"][self.session.map_id]
        skills = player.get(Skills)
        cooldowns = player.get(Cooldowns)
        shield = player.get(Shield)
        # 键位顺序唯一定义在 _skill_order()，这里直接复用，键位与格子不可能错位
        known = {sid: skills.known[sid] for sid in self._skill_order()}
        layout: HudLayout = build_hud(
            vitals=player.get(Vitals),
            progression=player.get(Progression),
            wallet=player.get(Wallet),
            bag=player.get(Bag),
            data=self.session.data,
            kills=sum(self.session.kill_counts().values()),
            map_name=map_spec["name"],
            width=self.width,
            height=self.height,
            known_skills=known,
            skill_names={sid: s["name"] for sid, s in
                         self.session.data.skills["skills"].items()},
            cooldowns=dict(cooldowns.remaining) if cooldowns is not None else None,
            shield=(shield.current, shield.max) if shield is not None else (0, 0),
            siege=siege_status(self.session.world),
            mining=self._mining_caption(),
        )

        for bar in (layout.hp, layout.mp, layout.exp):
            frame = bar.frame
            self._hud_shapes.append(shapes.Rectangle(
                frame.x, frame.y, frame.w, frame.h,
                color=(20, 22, 26, 220)))
            if not bar.empty:
                self._hud_shapes.append(shapes.Rectangle(
                    bar.fill.x, bar.fill.y, bar.fill.w, bar.fill.h,
                    color=tuple(int(c * 255) for c in bar.color)))
            self._hud_labels.append(Label(
                bar.caption, x=frame.x + 4, y=frame.y + 3, font_size=10,
                font_name=FONT, color=(235, 235, 235, 255)))

        for text in layout.texts:
            # TextAnchor.anchor_x 留空 = 自动（HUD 右上角统计靠右）
            anchor = text.anchor_x or ("right" if text.x > self.width * 0.6 else "left")
            self._hud_labels.append(Label(
                text.text, x=text.x, y=text.y, font_size=text.size, font_name=FONT,
                anchor_x=anchor, color=tuple(int(c * 255) for c in text.color)))

        # 药水计数：画在 HUD 布局之外。``build_hud`` 的行位已经被
        # test_presentation 钉住（右上统计是五行），加一行就会红；
        # 而这一行纯粹是“还剩几瓶”的读数，不参与任何布局断言。
        bag = player.get(Bag)
        if bag is not None:
            red = blue = 0
            for item_id, count in bag.items.items():
                hp_gain, mp_gain = potion.gains(self.session.data, item_id)
                if hp_gain:
                    red += int(count)
                if mp_gain:
                    blue += int(count)
            self._hud_labels.append(Label(
                f"Q 红药 ×{red}　E 蓝药 ×{blue}",
                x=self.width - 12, y=self.height - 12 - 68, font_size=12,
                font_name=FONT, anchor_x="right", color=(214, 224, 236, 255)))

        # 技能栏：底槽 + 就绪高亮 + 键位/技能名/熟练度
        for slot in layout.skills:
            rect = slot.rect
            self._hud_shapes.append(shapes.Rectangle(
                rect.x, rect.y, rect.w, rect.h,
                color=(30, 34, 40, 210) if slot.ready else (48, 30, 30, 210)))
            self._hud_shapes.append(shapes.Rectangle(
                rect.x, rect.y, 16, rect.h,
                color=(70, 86, 60, 230) if slot.ready else (90, 60, 60, 230)))
            self._hud_labels.append(Label(
                slot.key, x=rect.x + 8, y=rect.y + 6, font_size=11, font_name=FONT,
                anchor_x="center", color=(240, 240, 240, 255)))
            self._hud_labels.append(Label(
                f"{slot.name} Lv{slot.proficiency}", x=rect.x + 20, y=rect.y + 7,
                font_size=10, font_name=FONT,
                color=(225, 230, 238, 255) if slot.ready else (150, 140, 140, 255)))

        # 倒地横幅：不画出来玩家只会以为血空了，不知道自己在等什么、能按什么。
        # 剩余时间从 revive_at 倒着数（向上取整成秒），玩家读的是秒不是刻。
        if player.has(Downed):
            downed = player.get(Downed)
            tick = int(self.session.tick)
            left = max(0, int(downed.revive_at) - tick)
            cost = penalty.revive_cost(player.get(Progression))
            banner_w, banner_h = 420.0, 48.0
            bx = (self.width - banner_w) / 2
            by = self.height * 0.62
            self._hud_shapes.append(shapes.Rectangle(
                bx, by, banner_w, banner_h, color=(30, 12, 12, 235)))
            self._hud_shapes.append(shapes.Box(
                bx, by, banner_w, banner_h, thickness=2.0,
                color=(190, 70, 60, 255)))
            self._hud_labels.append(Label(
                "你倒下了", x=self.width / 2, y=by + banner_h - 22,
                font_size=15, font_name=FONT, anchor_x="center",
                color=(255, 210, 200, 255)))
            secs = (left + 9) // 10
            self._hud_labels.append(Label(
                f"按 R 原地复活（{cost} 金）  ·  {secs} 秒后自动回城",
                x=self.width / 2, y=by + 9, font_size=11, font_name=FONT,
                anchor_x="center", color=(235, 235, 235, 255)))

        # 攻城状态条：只有战役进行中才有（打完了立刻消失，不留空框）
        banner = layout.siege
        if banner is not None:
            frame = banner.frame
            self._hud_shapes.append(shapes.Rectangle(
                frame.x, frame.y, frame.w, frame.h, color=(14, 16, 20, 232)))
            self._hud_shapes.append(shapes.Box(
                frame.x, frame.y, frame.w, frame.h, thickness=2.0,
                color=(122, 100, 58, 255) if not banner.urgent else (190, 80, 60, 255)))
            # 目标耐久：底槽 + 填充
            bar = banner.objective
            self._hud_shapes.append(shapes.Rectangle(
                bar.frame.x, bar.frame.y, bar.frame.w, bar.frame.h,
                color=(20, 22, 26, 220)))
            if not bar.empty:
                self._hud_shapes.append(shapes.Rectangle(
                    bar.fill.x, bar.fill.y, bar.fill.w, bar.fill.h,
                    color=tuple(int(c * 255) for c in bar.color)))
            # 两行文本：第二行（波次/守军/时钟）紧贴耐久条下方，标题行在最下。
            # 行距 16px 是实测不叠字的最小值——改成更小就会叠（截图验收发现过）。
            row_y = frame.y + SIEGE_BANNER_HEIGHT - 32
            title_y = frame.y + SIEGE_BANNER_HEIGHT - 16
            self._hud_labels.append(Label(
                f"{banner.title}　{bar.caption}", x=frame.x + 10,
                y=title_y,
                font_size=11, font_name=FONT, color=(238, 230, 210, 255)))
            self._hud_labels.append(Label(
                banner.wave_text, x=frame.x + 10, y=row_y,
                font_size=11, font_name=FONT, color=(200, 206, 216, 255)))
            self._hud_labels.append(Label(
                banner.guards_text, x=frame.x + frame.w / 2, y=row_y,
                font_size=11, font_name=FONT, anchor_x="center",
                color=(200, 206, 216, 255)))
            self._hud_labels.append(Label(
                banner.clock_text, x=frame.right - 10, y=row_y,
                font_size=11, font_name=FONT, anchor_x="right",
                color=(255, 110, 90, 255) if banner.urgent else (200, 206, 216, 255)))

        self._hud_labels.append(Label(
            "WASD/方向键移动　空格攻击　1-5 技能　F 挖矿　C 角色　M 佣兵　J 图鉴　N 拍卖行　B 商店　V 仓库　滚轮缩放　ESC 退出",
            x=self.width / 2, y=8, font_size=11, font_name=FONT,
            anchor_x="center", color=(170, 176, 186, 255)))

        # 施放/拒绝反馈（1.2 秒后自动消失；胜负这类重要提示停 3 倍时长且字号更大）
        if self._toast and time.time() < self._toast_until:
            big = self._toast_big
            self._hud_labels.append(Label(
                self._toast, x=self.width / 2, y=self.height - 64,
                font_size=19 if big else 15, font_name=FONT, anchor_x="center",
                color=(255, 240, 170, 255) if big else (255, 228, 150, 255)))

    # ── 面板绘制 ────────────────────────────────────

    def _draw_panel(self) -> None:
        """把当前面板画在 HUD 之上。

        布局由 :mod:`mirsolo.presentation.panels` 算好（纯几何、可单测），
        这里只按矩形与文本锚点落地。``self._panel`` 留着给鼠标命中测试用，
        所以**每帧重建一次**：数据变了面板立刻跟着变（买完钱就少一行变灰）。
        """
        if self.panel is None:
            return
        player = self.session.world.get_entity("player")
        if player is None:
            return
        panel = build_panel(self.panel, player=player, data=self.session.data,
                            width=self.width, height=self.height,
                            scroll=self.panel_scroll, tick=self.session.tick,
                            world=self.session.world)
        self._panel = panel
        self.panel_scroll = min(self.panel_scroll, panel.scroll_max)

        rect = panel.rect
        self._hud_shapes.append(shapes.Rectangle(
            rect.x, rect.y, rect.w, rect.h, color=(16, 18, 22, 244)))
        self._hud_shapes.append(shapes.Box(
            rect.x, rect.y, rect.w, rect.h, thickness=2.0, color=(122, 100, 58, 255)))
        self._hud_shapes.append(shapes.Rectangle(
            rect.x, rect.top - 3, rect.w, 3, color=(122, 100, 58, 255)))
        self._hud_labels.append(Label(
            panel.title, x=rect.x + 10, y=rect.top - 18, font_size=14, font_name=FONT,
            color=(248, 226, 158, 255)))
        self._hud_labels.append(Label(
            f"第 {panel.scroll + 1} 屏" if panel.scroll_max else "",
            x=rect.x + rect.w - 10, y=rect.top - 17, font_size=10, font_name=FONT,
            anchor_x="right", color=(150, 150, 156, 255)))

        for section in panel.sections:
            self._hud_labels.append(Label(
                section.text, x=section.x, y=section.y, font_size=section.size,
                font_name=FONT, color=tuple(int(c * 255) for c in section.color)))

        for index, row in enumerate(panel.rows):
            if index % 2 == 0:  # 斑马纹：行多了不容易串行
                self._hud_shapes.append(shapes.Rectangle(
                    row.rect.x, row.rect.y, row.rect.w, row.rect.h,
                    color=(28, 31, 37, 220)))
            if row.enabled:
                text_color = (232, 234, 238, 255)
            else:
                text_color = (128, 128, 134, 255)
            self._hud_labels.append(Label(
                row.text, x=row.rect.x + 6, y=row.rect.y + 5, font_size=11,
                font_name=FONT, color=text_color))
            if row.detail:
                self._hud_labels.append(Label(
                    row.detail, x=row.rect.right - 6, y=row.rect.y + 5, font_size=10,
                    font_name=FONT, anchor_x="right",
                    color=(160, 176, 150, 255) if row.enabled else (110, 110, 116, 255)))

        for text in panel.texts:
            anchor = text.anchor_x or "left"
            self._hud_labels.append(Label(
                text.text, x=text.x, y=text.y, font_size=text.size, font_name=FONT,
                anchor_x=anchor, color=tuple(int(c * 255) for c in text.color)))

        # 面板与 HUD 共用同一份 shape/label 列表，统一由 _flush 画：
        # pyglet 不会自动画 shape/label，必须显式 draw（首帧截图验证抓到过）
    def _flush(self) -> None:
        for shape in self._hud_shapes:
            shape.draw()
        for label in self._hud_labels:
            label.draw()

    # ── 运行 ────────────────────────────────────────

    def run(self, shot_path: str | None = None, max_frames: int | None = None) -> None:
        """正常开窗运行；给 ``shot_path`` 时渲染一帧存图后退出（CI/验收用）。"""
        if shot_path:
            self.switch_to()
            self.dispatch_events()
            self.tick(1.0 / 60.0)
            self.on_draw()
            pyglet.image.get_buffer_manager().get_color_buffer().save(shot_path)
            self.close()
            return

        frames = 0

        def _step(dt: float) -> None:
            nonlocal frames
            self.tick(dt)
            frames += 1
            if max_frames is not None and frames >= max_frames:
                pyglet.app.exit()

        pyglet.clock.schedule_interval(_step, 1.0 / 60.0)
        pyglet.app.run()


def best_gear(data, class_id: str, level: int) -> list[str]:
    """挑出该职业在该等级能穿的**最好的一套**（每槽一件）。

    评分 = 攻上限 + 防上限（只用于选品，不参与任何战斗计算）。
    刻意不给 mage/taoist 造装备数据之外的捷径：拿不到就是拿不到，
    这样 ``--gear`` 验收出来的属性才和真玩到那个等级一致。
    """
    best: dict[str, tuple[int, str]] = {}
    for item_id, spec in data.items["items"].items():
        if spec.get("kind", "equip") != "equip":
            continue
        allowed = spec.get("class") or []
        if allowed and class_id not in allowed:
            continue
        if int(spec.get("level_req", 1)) > level:
            continue
        slot = spec["slot"]
        score = (sum(spec.get("atk") or (0, 0)) + sum(spec.get("ac") or (0, 0))
                 + sum(spec.get("mc") or (0, 0)) + sum(spec.get("sc") or (0, 0)))
        if slot not in best or score > best[slot][0]:
            best[slot] = (score, item_id)
    return [item_id for _, item_id in sorted(best.values(), key=lambda pair: pair[1])]


def data_items(data) -> list[str]:
    """可寄卖物品 id（验收用：给拍卖行/商店面板的背包栏塞几行真实数据）。"""
    return [item_id for item_id, spec in sorted(data.items["items"].items())
            if spec.get("kind", "equip") in ("equip", "book")]


def prepare_character(session: GameSession, *, class_id: str, level: int = 1,
                      learn_all: bool = False, gear: bool = False,
                      gold: int = 0, mercs: tuple[str, ...] = (),
                      codex_kills: int = 0, bag: bool = False,
                      wear_left: int | None = None,
                      dead: bool = False, hurt: bool = False,
                      potions: bool = False, mine: bool = False,
                      ore: bool = False) -> None:
    """验收辅助：提等级 / 发金币 / 发一套装备 / 雇佣兵 / 记图鉴。

    刻意**复用游戏内路径**（``apply_growth`` + ``learn`` + ``equip`` + ``hire`` +
    真发技能书），而不是直接往 ``Skills.known`` / ``Loadout.slots`` 里塞——
    否则验收版本与真实成长会悄悄分叉。

    两个例外都只碰"显示用"的账本，且写在明处：

    * ``gold``——拍卖行/佣兵的价格是数据表的数，验收时给一笔钱只是为了
      让"买得起"的那几行显现出来，不给钱截图里全是灰行，看不出版式；
    * ``codex_kills``——只往 ``Codex.kills`` 里记账、**不发首杀奖励**，
      纯粹为了让图鉴截图里同时出现"已解锁"与"未解锁"两种行；
    * ``bag``——往背包塞几件可寄卖的装备（**不穿**）：``gear`` 发的装备
      当场穿掉了，拍卖行/商店面板的截图里背包是空的，看不出版式；
    * ``wear_left``——把身上每件装备磨到只剩该千分比（0 = 全磨穿）：
      磨损是打出来的，验收只能倒着造，但走的是 ``wear.add_wear`` +
      ``recompute`` 同一条规则，不是直接改面板文字；
    * ``potions``——走商店 ``resupply`` **真买一批**（钱与负重两道门都在，
      所以最后补到几瓶是算出来的，不是写死的）；金币不够会打一行提示并跳过，
      配合 ``--gold`` 使用；
    * ``hurt``——把血/蓝打到自动阈值以下（HP 400‰ / MP 200‰），开窗第一刻
      就能看到自动喝药启动。刻意**不碰** ``PotionState``：喝不喝由系统自己决定。
    * ``mine``——把人送到本图第一个矿点上、手握鹤嘴锄、并按下 F。
      锄头走 ``equip`` 真路径（负重那道门还在，背包那把不算），
      id 从数据面扫出来（加新工具不用改这里）；这张图没矿点就打一行提示。
    * ``ore``——每种矿石塞一块进背包（验收专用，**不校验负重**，
      否则截图前还得先算一遍这身装备还剩多少余量）。
    """
    player = session.world.get_entity("player")
    if player is None:
        return
    prog = player.get(Progression)
    if level > prog.level:
        prog.level = min(50, level)
        prog.exp = 0
        apply_growth(player, session.data.classes["classes"][class_id])
    if gold:
        player.get(Wallet).gold = int(gold)
    if gear:
        for item_id in best_gear(session.data, class_id, prog.level):
            player.get(Bag).add(item_id, 1)
            try:
                equip(player, session.data, item_id)
            except EquipError:
                continue  # 负重不够就跳过，别让验收流程崩掉
    if bag:
        for item_id in sorted(data_items(session.data))[:4]:
            player.get(Bag).add(item_id, 1)
    if wear_left is not None:
        book = player.get(Wear)
        loadout = player.get(Loadout)
        for item_id in sorted(set((loadout.slots if loadout else {}).values())):
            total = wear.durability_of(session.data, item_id)
            left = max(0, min(total, total * int(wear_left) // 1000))
            wear.add_wear(session.data, book, item_id, total - left)
        recompute(player, session.data)
    if potions:
        try:
            got = resupply(player, session.data)
            print(f"[验收] 补给 {got['items']} 花 {got['paid']} 金，余 {got['gold']}")
        except TradeError as exc:
            print(f"[验收] 补给没成（{exc.code}）：{exc} —— 加 --gold N 再试")
    if hurt:
        vitals = player.get(Vitals)
        if vitals is not None:
            vitals.hp = max(1, int(vitals.max_hp) * 400 // 1000)
            if int(vitals.max_mp) > 0:
                vitals.mp = int(vitals.max_mp) * 200 // 1000
    if mine:
        picks = mining.pick_ids(session.data)
        if not picks:
            print("[验收] 数据面没有能挖矿的工具")
        else:
            player.get(Bag).add(picks[0], 1)
            try:
                equip(player, session.data, picks[0])
            except EquipError as exc:
                print(f"[验收] 拿不动鹤嘴锄（{exc.code}）：{exc} —— 先加 --level N")
        points = session.data.maps["maps"][session.map_id].get("mines", [])
        if points:
            pos = player.get(Position)
            pos.x, pos.y = float(points[0]["at"][0]), float(points[0]["at"][1])
            session.world.set_data(KEY_MINE_TOGGLE, True)
        else:
            print(f"[验收] {session.map_id} 没有矿点（有矿点的是 biqi_mine_1 / wugong_cave_1 / stone_tomb）")
    if ore:
        for item_id in mining.ore_ids(session.data):
            player.get(Bag).add(item_id, 1)
    if dead:
        # 验收开关：只负责致死那一下；倒地 → 结算 → 复活全在
        # ``DeathPenaltySystem`` + ``actor.penalty`` 里，避免两套实现分叉
        vitals = player.get(Vitals)
        if vitals is not None:
            vitals.hp = 0
            bus = session.world.get_data("bus")
            if bus is not None:
                bus.emit(EVT_DEATH, {
                    "attacker": None, "defender": player.id,
                    "killer_eid": None, "victim_eid": player.eid,
                    "tick": int(session.tick),
                })
    for merc_id in mercs:
        try:
            mercenary.hire(session.world, session.data, player, merc_id, tick=0)
        except mercenary.MercenaryError:
            continue  # 门槛/钱/名额不满足就跳过，别让验收流程崩掉
    if codex_kills:
        book = player.get(Codex)
        if book is not None:
            for species in codex.rows(session.data, book)[:int(codex_kills)]:
                codex.record_kill(book, species["species"])
    if not learn_all:
        return
    for skill_id, spec in sorted(session.data.skills["skills"].items()):
        if spec["class"] != class_id or int(spec["level_req"]) > prog.level:
            continue
        player.get(Bag).add(spec["book"], 1)
        try:
            learn(player, session.data, skill_id)
        except SkillError:
            continue  # 资源/门槛不满足就跳过，别让验收流程崩掉


def launch(*, seed: int = 20261019, map_id: str = "biqi_field",
           player_class: str = "warrior", width: int = 1280, height: int = 720,
           shot_path: str | None = None, max_frames: int | None = None,
           level: int = 1, learn: bool = False, gear: bool = False,
           panel: str | None = None, siege: str | None = None,
           gold: int = 0, mercs: tuple[str, ...] = (),
           codex_kills: int = 0, bag: bool = False,
           wear_left: int | None = None,
           dead: bool = False, hurt: bool = False,
           potions: bool = False, mine: bool = False,
           ore: bool = False) -> None:
    """装配会话 + 开窗（CLI 与测试共用入口）。

    ``panel`` 只用于验收截图：开窗时就打开某个面板（``character``/``mercenary``/
    ``codex``/``auction``/``shop``/``warehouse``），截图里能直接看到面板长什么样。

    ``siege`` 是验收开关：传战役 id（如 ``sabak``）时装配完立刻
    :func:`mirsolo.siege.campaign.start_siege`——它内部会换图并铺现场，
    因此 ``map_id`` 会被战役的 ``map`` 覆盖（不覆盖就会"人还在比奇城却挂着
    沙巴克状态条"）。
    """
    session = build_session(seed=seed, map_id=map_id)
    populate(session, player_class=player_class)
    prepare_character(session, class_id=player_class, level=level,
                      learn_all=learn, gear=gear, gold=gold, mercs=mercs,
                      codex_kills=codex_kills, bag=bag, wear_left=wear_left,
                      dead=dead, hurt=hurt, potions=potions,
                      mine=mine, ore=ore)
    if siege is not None:
        start_siege(session, siege)
    window = MirsoloWindow(session=session, width=width, height=height)
    if panel is not None:
        window.panel = panel
    try:
        window.run(shot_path=shot_path, max_frames=max_frames)
    finally:
        session.shutdown()


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """欧氏距离（HUD/调试用；战斗判定一律用切比雪夫网格距离）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])
