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
from mirsolo.components import (
    Bag,
    Cooldowns,
    Identity,
    Position,
    Progression,
    Shield,
    Skills,
    Vitals,
    Wallet,
)
from mirsolo.economy.shop import TradeError, buy, deposit, sell, withdraw
from mirsolo.items.equipment import EquipError, equip, unequip
from mirsolo.presentation.hud import MAX_SKILL_SLOTS, HudLayout, build_hud
from mirsolo.presentation.iso_camera import IsoCamera
from mirsolo.presentation.panels import Panel, Row, build_panel
from mirsolo.presentation.renderer import GLRenderer
from mirsolo.presentation.scene import MAP_PALETTE, build_instances
from mirsolo.progression.exp import apply_growth
from mirsolo.skills import EVT_SKILL_CAST, SELF_KINDS, SkillError, learn
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

#: 面板开关键 → 面板名。放在这里而不是散在 if 里：加面板只改这张表
PANEL_KEYS = {
    pyglet.window.key.C: "character",
    pyglet.window.key.B: "shop",
    pyglet.window.key.V: "warehouse",
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
        self.panel: str | None = None
        self.panel_scroll = 0
        self._panel: Panel | None = None

        bus = session.world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_SKILL_CAST, self._on_skill_cast)
            bus.subscribe("skill:rejected", self._on_skill_rejected)
            bus.subscribe(EVT_MAP_CHANGED, self._on_map_changed)

        player = session.world.get_entity("player")
        if player is not None:
            pos = player.get(Position)
            self.camera.center = (pos.x, pos.y)

    # ── 反馈浮字 ────────────────────────────────────

    def _say(self, text: str) -> None:
        """一行浮字提示（1.2 秒后自动消失）。"""
        self._toast = text
        self._toast_until = time.time() + TOAST_SECONDS

    def _on_map_changed(self, payload: dict) -> None:
        maps = self.session.data.maps["maps"]
        name = maps.get(payload.get("to"), {}).get("name", payload.get("to"))
        self._say(f"进入 {name}")
        self.panel_scroll = 0
        player = self.session.world.get_entity("player")
        if player is not None:  # 传送是瞬移，相机不要插值过去（否则会飞越大半张图）
            pos = player.get(Position)
            self.camera.center = (pos.x, pos.y)

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
                got = buy(player, data, row.item)
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
            else:  # "none"：不可操作的条目（技能书等），点了给句解释
                self._say(f"{row.text}：这个不能直接使用")
        except EquipError as exc:
            self._say(f"{row.text}：{EQUIP_ERRORS.get(exc.code, exc.code)}")
        except TradeError as exc:
            self._say(f"{row.text}：{TRADE_ERRORS.get(exc.code, exc.code)}")

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
        if player is None:
            return
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
        self.session.world.set_data("move_intent", self._collect_intent())
        if pyglet.window.key.SPACE in self.pressed:
            target = self._target_in_range()
            if target is not None:
                self.session.queue_attack(self.session.world.get_entity("player"), target)
        self.session.engine.advance(self._last_real_dt)

        player = self.session.world.get_entity("player")
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
                                     build_instances(self.session.world, self.session.data))
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

        self._hud_labels.append(Label(
            "WASD/方向键移动　空格攻击　1-5 技能　C 角色　B 商店　V 仓库　滚轮缩放　ESC 退出",
            x=self.width / 2, y=8, font_size=11, font_name=FONT,
            anchor_x="center", color=(170, 176, 186, 255)))

        # 施放/拒绝反馈（1.2 秒后自动消失）
        if self._toast and time.time() < self._toast_until:
            self._hud_labels.append(Label(
                self._toast, x=self.width / 2, y=self.height - 64, font_size=15,
                font_name=FONT, anchor_x="center", color=(255, 228, 150, 255)))

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
                            scroll=self.panel_scroll)
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


def prepare_character(session: GameSession, *, class_id: str, level: int = 1,
                      learn_all: bool = False, gear: bool = False) -> None:
    """验收辅助：提等级 / 发书学技能 / 发一套装备穿上。

    刻意**复用游戏内路径**（``apply_growth`` + ``learn`` + ``equip`` + 真发
    技能书），而不是直接往 ``Skills.known`` / ``Loadout.slots`` 里塞——
    否则验收版本与真实成长会悄悄分叉。
    """
    player = session.world.get_entity("player")
    if player is None:
        return
    prog = player.get(Progression)
    if level > prog.level:
        prog.level = min(50, level)
        prog.exp = 0
        apply_growth(player, session.data.classes["classes"][class_id])
    if gear:
        for item_id in best_gear(session.data, class_id, prog.level):
            player.get(Bag).add(item_id, 1)
            try:
                equip(player, session.data, item_id)
            except EquipError:
                continue  # 负重不够就跳过，别让验收流程崩掉
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
           panel: str | None = None) -> None:
    """装配会话 + 开窗（CLI 与测试共用入口）。

    ``panel`` 只用于验收截图：开窗时就打开某个面板（``character``/``shop``/
    ``warehouse``），截图里能直接看到面板长什么样。
    """
    session = build_session(seed=seed, map_id=map_id)
    populate(session, player_class=player_class)
    prepare_character(session, class_id=player_class, level=level,
                      learn_all=learn, gear=gear)
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
