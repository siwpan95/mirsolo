"""GL 渲染器 —— 唯一碰 OpenGL 的模块（除 ``app.py`` 的窗口外）。

分层选择：着色器用 **pyglet 的 ``ShaderProgram``**（2.1.16 实测可用：
``program.attributes['a_pos']['location']`` 拿属性位置、``program['u_mvp'] = Mat4(...)``
直接赋 uniform），顶点缓冲仍走原始 GL（``glBufferData`` + ``glVertexAttribPointer``）。
这样既避开 ctypes 传 ``char**`` 的指针层级坑，也不用依赖 pyglet 更高层的
``vertex_list`` 绑定签名（2.0→2.1 改过两次）。

几何策略（零外部素材）：

- **地面**：一整块四边形，片元着色器按**世界坐标**算格子色与格线，
  4096 格也只 1 次 draw call；
- **角色/怪物**：每帧把实例拼成 36 顶点/个的三角形列表（面亮度烘进顶点色），
  同帧一次上传、1 次 draw call 画完所有方块；
- **HUD**：交给 pyglet 的 ``shapes`` / ``text``（2D 管线），本模块只画 3D。
"""

from __future__ import annotations

import ctypes

import pyglet.gl as gl
from pyglet.graphics.shader import Shader, ShaderProgram
from pyglet.math import Mat4

from mirsolo.presentation.iso_camera import IsoCamera
from mirsolo.presentation.scene import Instance

# ── 着色器 ──────────────────────────────────────────

PROP_VS = """
#version 330 core
in vec3 a_pos;
in vec3 a_color;
uniform mat4 u_mvp;
out vec3 v_color;
void main() {
    v_color = a_color;
    gl_Position = u_mvp * vec4(a_pos, 1.0);
}
"""

PROP_FS = """
#version 330 core
in vec3 v_color;
out vec4 o_color;
void main() {
    o_color = vec4(v_color, 1.0);
}
"""

FLOOR_VS = """
#version 330 core
in vec3 a_pos;
uniform mat4 u_mvp;
out vec3 v_world;
void main() {
    v_world = a_pos;
    gl_Position = u_mvp * vec4(a_pos, 1.0);
}
"""

FLOOR_FS = """
#version 330 core
in vec3 v_world;
uniform vec3 u_base;
uniform float u_seed;
out vec4 o_color;

float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21) + u_seed);
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

void main() {
    vec2 tile = floor(v_world.xy);
    float h = hash21(tile);
    vec3 base = u_base * (0.94 + 0.12 * h);

    // 格线：距格边 0.035 以内压暗，等距视角下形成菱形网格
    vec2 f = fract(v_world.xy);
    float edge = min(min(f.x, 1.0 - f.x), min(f.y, 1.0 - f.y));
    float lit = smoothstep(0.0, 0.035, edge);
    base = mix(base * 0.76, base, lit);

    o_color = vec4(base, 1.0);
}
"""

#: 面亮度：把"简易光照"烘进顶点色，片元就不必算法线
#: 顺序：+X, -X, +Y, -Y, +Z(顶), -Z(底)
FACE_SHADE = (0.82, 0.78, 0.90, 0.86, 1.00, 0.55)


def _cube_triangles(inst: Instance) -> list[tuple[float, ...]]:
    """把一个实例烘成 36 个顶点（每顶点 pos3 + color3）。"""
    x0, x1 = inst.x - inst.sx * 0.5, inst.x + inst.sx * 0.5
    y0, y1 = inst.y - inst.sy * 0.5, inst.y + inst.sy * 0.5
    z0, z1 = inst.z, inst.z + inst.sz
    r, g, b = inst.color[0], inst.color[1], inst.color[2]

    faces = (
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),  # +X
        ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),  # -X
        ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),  # +Y
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),  # -Y
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),  # +Z
        ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)),  # -Z
    )
    out: list[tuple[float, ...]] = []
    for corners, shade in zip(faces, FACE_SHADE):
        a, bb, c, d = corners
        for vertex in (a, bb, c, a, c, d):
            out.append((vertex[0], vertex[1], vertex[2], r * shade, g * shade, b * shade))
    return out


class GLRenderer:
    """3D 渲染器：地面 + 方块实例。

    必须在 GL 上下文就绪后构造（窗口 ``switch_to()`` 之后）。
    """

    def __init__(self) -> None:
        self.prop_program = ShaderProgram(Shader(PROP_VS, "vertex"), Shader(PROP_FS, "fragment"))
        self.floor_program = ShaderProgram(Shader(FLOOR_VS, "vertex"),
                                           Shader(FLOOR_FS, "fragment"))

        self.prop_vao, self.prop_vbo = self._make_buffer(
            self.prop_program, (("a_pos", 3), ("a_color", 3)))
        self.floor_vao, self.floor_vbo = self._make_buffer(
            self.floor_program, (("a_pos", 3),))

        self._prop_capacity = 0
        self._prop_buffer = None
        self._floor_buffer = None
        self._floor_key: tuple | None = None

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

    # ── 资源 ────────────────────────────────────────

    @staticmethod
    def _make_buffer(program: ShaderProgram, attrs: tuple[tuple[str, int], ...]) -> tuple[int, int]:
        locations = program.attributes
        vao = ctypes.c_uint(0)
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        gl.glBindVertexArray(vao)
        vbo = ctypes.c_uint(0)
        gl.glGenBuffers(1, ctypes.byref(vbo))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        stride = sum(size for _, size in attrs) * 4
        offset = 0
        for name, size in attrs:
            if name not in locations:
                raise RuntimeError(f"着色器缺少属性 {name}")
            loc = locations[name]["location"]
            gl.glEnableVertexAttribArray(loc)
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            offset += size * 4
        gl.glBindVertexArray(0)
        return vao.value, vbo.value

    # ── 每帧 ────────────────────────────────────────

    def begin_frame(self, clear_color: tuple[float, float, float] = (0.05, 0.06, 0.07)) -> None:
        gl.glClearColor(*clear_color, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)

    def draw_floor(self, camera: IsoCamera, map_spec: dict, map_id: str,
                   base_color: tuple[float, float, float]) -> None:
        """画整块地面（1 次 draw call，格子在片元里算）。"""
        width, height = map_spec.get("size", (1, 1))
        key = (float(width), float(height))
        if key != self._floor_key:
            # 两个三角形（6 顶点）拼满整张地图——4 顶点只够画 1 个三角形，
            # 画面会缺一半（首帧验收截图抓到过）
            verts = (0.0, 0.0, 0.0,
                     float(width), 0.0, 0.0,
                     float(width), float(height), 0.0,
                     0.0, 0.0, 0.0,
                     float(width), float(height), 0.0,
                     0.0, float(height), 0.0)
            self._floor_buffer = (ctypes.c_float * len(verts))(*verts)
            self._floor_key = key
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.floor_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, ctypes.sizeof(self._floor_buffer),
                            self._floor_buffer, gl.GL_STATIC_DRAW)

        self.floor_program.use()
        self.floor_program["u_mvp"] = Mat4(*camera.view_proj())
        self.floor_program["u_base"] = tuple(float(c) for c in base_color)
        self.floor_program["u_seed"] = float(sum(map(ord, map_id)) % 1000) * 0.37
        gl.glBindVertexArray(self.floor_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)

    def draw_instances(self, camera: IsoCamera, instances: list[Instance]) -> None:
        """画所有方块实例（1 次 draw call）。"""
        if not instances:
            return
        flat: list[float] = []
        for inst in instances:
            for vertex in _cube_triangles(inst):
                flat.extend(vertex)
        count = len(flat) // 6
        if count > self._prop_capacity:
            self._prop_capacity = count
            self._prop_buffer = (ctypes.c_float * len(flat))()

        buf = self._prop_buffer
        assert buf is not None
        ctypes.memmove(buf, (ctypes.c_float * len(flat))(*flat),
                       ctypes.sizeof(ctypes.c_float) * len(flat))

        self.prop_program.use()
        self.prop_program["u_mvp"] = Mat4(*camera.view_proj())
        gl.glBindVertexArray(self.prop_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.prop_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, ctypes.sizeof(buf), buf, gl.GL_DYNAMIC_DRAW)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, count)

    def end_3d(self) -> None:
        """切回 2D（HUD）前关掉 3D 状态，否则标签会被深度测试吃掉。"""
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        self.prop_program.stop()
        self.floor_program.stop()

    def close(self) -> None:
        self.prop_program.delete()
        self.floor_program.delete()
