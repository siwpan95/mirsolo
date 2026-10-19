"""等距斜俯视相机 —— 纯数学，零 GL、零 pyglet。

坐标约定（与 ECS 的 ``Position`` 一致）：

- 地面是 ``z = 0`` 平面，``x`` / ``y`` 是**地图格子坐标**（1 格 = 1 世界单位）；
- ``z`` 向上；人物的"高度"就是 z（方块人站在地面 z=0 上）；
- 相机绕目标点以固定 ``yaw`` / ``pitch`` 环绕，**正交投影**——
  原版传奇就是固定俯角的斜视，正交没有近大远小，格子看起来一样大。

矩阵全部走 3dworld 的列主序约定（``mat4_mul(a, b)`` 即 a·b，
见 ``world3d/engine/mathkit.py:70``），不自己发明一套索引。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from world3d import mat4_mul, look_at_matrix

#: 真等距（isometric）俯角：atan(1/√2) ≈ 35.264°，配合 45° 偏航得到 2:1 菱形网格
TRUE_ISOMETRIC_PITCH = 35.264


@dataclass
class IsoCamera:
    """正交等距相机。

    用法::

        cam = IsoCamera(width=1280, height=720)
        cam.follow(32, 32)                 # 镜头中心对准玩家所在格
        mvp = cam.view_proj()              # 喂给着色器
        px, py = cam.world_to_screen(32, 32)
    """

    center: tuple[float, float] = (0.0, 0.0)
    yaw_deg: float = 45.0
    pitch_deg: float = TRUE_ISOMETRIC_PITCH
    distance: float = 60.0
    view_height: float = 14.0   # 正交视口在屏幕竖直方向覆盖的世界单位数（越小越"近"）
    near: float = 0.1
    far: float = 400.0
    width: int = 1280
    height: int = 720
    #: 跟随平滑系数（0 = 完全不跟随，1 = 瞬间对齐）；表现层每帧调用 follow
    follow_lerp: float = 1.0

    # ── 派生量 ──────────────────────────────────────

    @property
    def aspect(self) -> float:
        return self.width / max(1, self.height)

    def offset_dir(self) -> tuple[float, float, float]:
        """从目标点指向相机的单位向量（yaw/pitch 决定）。"""
        yaw = math.radians(self.yaw_deg)
        pitch = math.radians(self.pitch_deg)
        horizontal = math.cos(pitch)
        return (math.cos(yaw) * horizontal,
                math.sin(yaw) * horizontal,
                math.sin(pitch))

    def eye(self) -> tuple[float, float, float]:
        dx, dy, dz = self.offset_dir()
        return (self.center[0] + dx * self.distance,
                self.center[1] + dy * self.distance,
                dz * self.distance)

    # ── 矩阵 ────────────────────────────────────────

    def view_matrix(self) -> tuple:
        ex, ey, ez = self.eye()
        return look_at_matrix((ex, ey, ez), (self.center[0], self.center[1], 0.0), (0.0, 0.0, 1.0))

    def proj_matrix(self) -> tuple:
        """正交投影（列主序）。上下按 ``view_height`` 对称展开。"""
        half_h = max(0.01, self.view_height)
        half_w = half_h * self.aspect
        near, far = self.near, self.far
        return (
            1.0 / half_w, 0.0, 0.0, 0.0,
            0.0, 1.0 / half_h, 0.0, 0.0,
            0.0, 0.0, -2.0 / (far - near), 0.0,
            0.0, 0.0, -(far + near) / (far - near), 1.0,
        )

    def view_proj(self) -> tuple:
        return mat4_mul(self.proj_matrix(), self.view_matrix())

    # ── 投影换算 ────────────────────────────────────

    @staticmethod
    def _apply(m: tuple, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x, y, z, w = v
        return (
            m[0] * x + m[4] * y + m[8] * z + m[12] * w,
            m[1] * x + m[5] * y + m[9] * z + m[13] * w,
            m[2] * x + m[6] * y + m[10] * z + m[14] * w,
            m[3] * x + m[7] * y + m[11] * z + m[15] * w,
        )

    def world_to_ndc(self, x: float, y: float, z: float = 0.0) -> tuple[float, float, float]:
        vx, vy, vz, vw = self._apply(self.view_proj(), (x, y, z, 1.0))
        w = vw or 1.0
        return vx / w, vy / w, vz / w

    def world_to_screen(self, x: float, y: float, z: float = 0.0) -> tuple[float, float]:
        """世界坐标 → 像素坐标（pyglet 的 y 向上，左下角为原点）。"""
        nx, ny, _ = self.world_to_ndc(x, y, z)
        return ((nx * 0.5 + 0.5) * self.width, (ny * 0.5 + 0.5) * self.height)

    def unproject(self, px: float, py: float, ndc_z: float) -> tuple[float, float, float]:
        """像素 + NDC 深度 → 世界坐标。"""
        from world3d import mat4_inverse

        inv = mat4_inverse(self.view_proj())
        if inv is None:
            raise ValueError("view_proj 不可逆（相机参数非法）")
        ndc_x = px / max(1, self.width) * 2.0 - 1.0
        ndc_y = py / max(1, self.height) * 2.0 - 1.0
        wx, wy, wz, ww = self._apply(inv, (ndc_x, ndc_y, ndc_z, 1.0))
        w = ww or 1.0
        return wx / w, wy / w, wz / w

    def pixel_to_ground(self, px: float, py: float) -> tuple[float, float]:
        """屏幕像素 → 地面（z=0）交点。用于"点地移动 / 点怪选中"。"""
        near_pt = self.unproject(px, py, -1.0)
        far_pt = self.unproject(px, py, 1.0)
        dz = far_pt[2] - near_pt[2]
        if abs(dz) < 1e-9:
            return near_pt[0], near_pt[1]
        t = -near_pt[2] / dz
        return (near_pt[0] + (far_pt[0] - near_pt[0]) * t,
                near_pt[1] + (far_pt[1] - near_pt[1]) * t)

    # ── 跟随与缩放 ──────────────────────────────────

    def follow(self, x: float, y: float) -> None:
        if self.follow_lerp >= 1.0:
            self.center = (x, y)
            return
        t = max(0.0, min(1.0, self.follow_lerp))
        self.center = (self.center[0] + (x - self.center[0]) * t,
                       self.center[1] + (y - self.center[1]) * t)

    def zoom(self, factor: float, min_height: float = 6.0, max_height: float = 40.0) -> None:
        """滚轮缩放：调 ``view_height``（越小越近）。"""
        self.view_height = max(min_height, min(max_height, self.view_height * factor))

    def clamp_to_map(self, width: int, height: int, margin: float = 2.0) -> None:
        """镜头中心不许跑出地图（避免黑边）。"""
        self.center = (
            max(margin, min(width - margin, self.center[0])),
            max(margin, min(height - margin, self.center[1])),
        )
