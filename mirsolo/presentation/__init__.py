"""表现层：等距斜俯视 3D + HUD。

分层（依赖单向，便于零 GL 单测）::

    scene.py / hud.py / iso_camera.py   ← 纯数学与数据（可无窗口测试）
    renderer.py                          ← 原始 GL 绘制
    app.py                               ← 窗口 / 输入 / 主循环
"""
