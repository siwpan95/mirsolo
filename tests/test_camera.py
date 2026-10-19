"""等距相机：投影换算往返一致性、跟随/缩放/钳制。

核心不变量：**投出去再投回来要回到原点**（``world_to_screen`` ↔
``pixel_to_ground``），这条不成立，点地移动/点怪选中就会偏。
"""

from __future__ import annotations

from mirsolo.presentation.iso_camera import TRUE_ISOMETRIC_PITCH, IsoCamera

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _camera(**kw) -> IsoCamera:
    defaults = dict(center=(32.0, 32.0), width=1280, height=720)
    defaults.update(kw)
    return IsoCamera(**defaults)


@case
def test_center_projects_to_screen_center():
    cam = _camera()
    px, py = cam.world_to_screen(*cam.center)
    assert abs(px - cam.width / 2) < 1e-6, px
    assert abs(py - cam.height / 2) < 1e-6, py


@case
def test_ground_roundtrip():
    """地面点投影 → 反投影，必须回到原点（浮点容差 1e-3 格）。"""
    cam = _camera()
    for x, y in ((32, 32), (10, 50), (0, 0), (63, 63), (32.5, 31.25)):
        px, py = cam.world_to_screen(x, y)
        gx, gy = cam.pixel_to_ground(px, py)
        assert abs(gx - x) < 1e-3 and abs(gy - y) < 1e-3, (x, y, gx, gy)


@case
def test_default_pitch_is_true_isometric():
    """默认俯角必须是真等距 35.264°（否则格子不是 2:1 菱形）。"""
    assert _camera().pitch_deg == TRUE_ISOMETRIC_PITCH
    assert abs(TRUE_ISOMETRIC_PITCH - 35.264) < 0.01


@case
def test_view_proj_is_invertible():
    cam = _camera()
    from world3d import mat4_inverse, mat4_mul

    inv = mat4_inverse(cam.view_proj())
    assert inv is not None
    identity = mat4_mul(cam.view_proj(), inv)
    for i in range(16):
        expect = 1.0 if i % 5 == 0 else 0.0
        assert abs(identity[i] - expect) < 1e-4, (i, identity[i])


@case
def test_follow_lerp_semantics():
    """lerp=1 瞬间对齐；lerp=0.5 半程；lerp=0 不动。"""
    cam = _camera(follow_lerp=1.0)
    cam.follow(40, 40)
    assert cam.center == (40.0, 40.0)

    cam = _camera(center=(0, 0), follow_lerp=0.5)
    cam.follow(10, 10)
    assert cam.center == (5.0, 5.0)

    cam = _camera(center=(3, 4), follow_lerp=0.0)
    cam.follow(10, 10)
    assert cam.center == (3.0, 4.0)


@case
def test_zoom_is_clamped():
    cam = _camera(view_height=10.0)
    cam.zoom(0.001)
    assert cam.view_height == 6.0, "缩太近要钳制"
    cam.zoom(1e9)
    assert cam.view_height == 40.0, "拉太远要钳制"


@case
def test_clamp_to_map():
    cam = _camera(center=(-5, 999))
    cam.clamp_to_map(64, 64, margin=2.0)
    assert cam.center == (2.0, 62.0)


@case
def test_ground_point_above_camera_height_is_rejected_safely():
    """俯角下视线永远与地面相交；退化（dz≈0）时回退 near 点而不是崩。"""
    cam = _camera(pitch_deg=0.0)  # 水平视线：与 z=0 平面平行
    gx, gy = cam.pixel_to_ground(640, 360)
    assert isinstance(gx, float) and isinstance(gy, float)


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
    print(f"test_camera: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
