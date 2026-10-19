"""窗口与主循环 —— 等距斜俯视可玩版本。

职责边界：

- **不写游戏规则**：移动/攻击都通过 ``world.set_data`` 的意图键交给 ECS 系统，
  与 headless 冒烟走的是**同一条结算路径**（这点很关键——否则"看得见的版本"
  与"测过的版本"会悄悄分叉）；
- **只管表现**：相机跟随、HUD、配色、输入采集。

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
from mirsolo.presentation.hud import MAX_SKILL_SLOTS, HudLayout, build_hud
from mirsolo.presentation.iso_camera import IsoCamera
from mirsolo.presentation.renderer import GLRenderer
from mirsolo.presentation.scene import MAP_PALETTE, build_instances
from mirsolo.progression.exp import apply_growth
from mirsolo.skills import EVT_SKILL_CAST, SELF_KINDS, SkillError, learn

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

        bus = session.world.get_data("bus")
        if bus is not None:
            bus.subscribe(EVT_SKILL_CAST, self._on_skill_cast)
            bus.subscribe("skill:rejected", self._on_skill_rejected)

        player = session.world.get_entity("player")
        if player is not None:
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
        self._toast = f"{spec.get('name', payload.get('skill'))} {detail}".strip()
        self._toast_until = time.time() + TOAST_SECONDS

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
        self._toast = f"{name}：{reasons.get(first.get('code'), first.get('code'))}"
        self._toast_until = time.time() + TOAST_SECONDS

    # ── 事件 ────────────────────────────────────────

    def on_resize(self, width: int, height: int) -> None:
        super().on_resize(width, height)
        self.camera.width = width
        self.camera.height = height

    def on_key_press(self, symbol: int, modifiers: int) -> None:
        key = pyglet.window.key
        self.pressed.add(symbol)
        if symbol == key.ESCAPE:
            self.close()
        # 技能栏 1..5（数字键与数字小键盘都收）：按一次放一次，
        # 长按不连发——连发交给冷却门控反而会让节奏变乱。
        if symbol in SKILL_KEYS:
            self._cast_slot(SKILL_KEYS.index(symbol) % MAX_SKILL_SLOTS)

    def on_key_release(self, symbol: int, modifiers: int) -> None:
        self.pressed.discard(symbol)

    def on_mouse_scroll(self, x: int, y: int, scroll_x: float, scroll_y: float) -> None:
        self.camera.zoom(0.9 if scroll_y > 0 else 1.1)

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
            anchor = "right" if text.x > self.width * 0.6 else "left"
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
            "WASD/方向键移动　空格攻击　1-5 技能　滚轮缩放　ESC 退出",
            x=self.width / 2, y=8, font_size=11, font_name=FONT,
            anchor_x="center", color=(170, 176, 186, 255)))

        # 施放/拒绝反馈（1.2 秒后自动消失）
        if self._toast and time.time() < self._toast_until:
            self._hud_labels.append(Label(
                self._toast, x=self.width / 2, y=self.height - 64, font_size=15,
                font_name=FONT, anchor_x="center", color=(255, 228, 150, 255)))

        # pyglet 不会自动画 shape/label——必须显式 draw（首帧截图验证抓到过）
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


def prepare_character(session: GameSession, *, class_id: str, level: int = 1,
                      learn_all: bool = False) -> None:
    """验收辅助：提等级 / 发书学技能。

    刻意**复用游戏内路径**（``apply_growth`` + ``learn`` + 真发技能书），
    而不是直接往 ``Skills.known`` 里塞——否则验收版本与真实成长会悄悄分叉。
    """
    player = session.world.get_entity("player")
    if player is None:
        return
    prog = player.get(Progression)
    if level > prog.level:
        prog.level = min(50, level)
        prog.exp = 0
        apply_growth(player, session.data.classes["classes"][class_id])
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
           level: int = 1, learn: bool = False) -> None:
    """装配会话 + 开窗（CLI 与测试共用入口）。"""
    session = build_session(seed=seed, map_id=map_id)
    populate(session, player_class=player_class)
    prepare_character(session, class_id=player_class, level=level, learn_all=learn)
    window = MirsoloWindow(session=session, width=width, height=height)
    try:
        window.run(shot_path=shot_path, max_frames=max_frames)
    finally:
        session.shutdown()


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """欧氏距离（HUD/调试用；战斗判定一律用切比雪夫网格距离）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])
