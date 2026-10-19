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

import pyglet
from pyglet import shapes
from pyglet.text import Label

from mirsolo.bootstrap import GameSession, build_session, populate
from mirsolo.components import Bag, Identity, Position, Progression, Vitals, Wallet
from mirsolo.presentation.hud import HudLayout, build_hud
from mirsolo.presentation.iso_camera import IsoCamera
from mirsolo.presentation.renderer import GLRenderer
from mirsolo.presentation.scene import MAP_PALETTE, build_instances

FONT = "Microsoft YaHei"
MELEE_RANGE = 1


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

        player = session.world.get_entity("player")
        if player is not None:
            pos = player.get(Position)
            self.camera.center = (pos.x, pos.y)

    # ── 事件 ────────────────────────────────────────

    def on_resize(self, width: int, height: int) -> None:
        super().on_resize(width, height)
        self.camera.width = width
        self.camera.height = height

    def on_key_press(self, symbol: int, modifiers: int) -> None:
        self.pressed.add(symbol)
        if symbol == pyglet.window.key.ESCAPE:
            self.close()

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

    def _target_in_range(self):
        player = self.session.world.get_entity("player")
        if player is None:
            return None
        ppos = player.get(Position)
        best, best_dist = None, 10**9
        for mob in self.session.world.each(Identity, Position, Vitals):
            ident = mob.get(Identity)
            if ident.kind != "monster" or not mob.get(Vitals).alive:
                continue
            mpos = mob.get(Position)
            dist = int(max(abs(mpos.x - ppos.x), abs(mpos.y - ppos.y)))
            if dist < best_dist:
                best, best_dist = mob, dist
        return best if best is not None and best_dist <= MELEE_RANGE else None

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

        self._hud_labels.append(Label(
            "WASD/方向键移动　空格攻击　滚轮缩放　ESC 退出",
            x=self.width / 2, y=8, font_size=11, font_name=FONT,
            anchor_x="center", color=(170, 176, 186, 255)))

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


def launch(*, seed: int = 20261019, map_id: str = "biqi_field",
           player_class: str = "warrior", width: int = 1280, height: int = 720,
           shot_path: str | None = None, max_frames: int | None = None) -> None:
    """装配会话 + 开窗（CLI 与测试共用入口）。"""
    session = build_session(seed=seed, map_id=map_id)
    populate(session, player_class=player_class)
    window = MirsoloWindow(session=session, width=width, height=height)
    try:
        window.run(shot_path=shot_path, max_frames=max_frames)
    finally:
        session.shutdown()


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """欧氏距离（HUD/调试用；战斗判定一律用切比雪夫网格距离）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])
