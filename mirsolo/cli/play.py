"""可玩入口 —— 开窗跑等距斜俯视版本。

用法::

    python -m mirsolo.cli.play                  # 开窗游玩
    python -m mirsolo.cli.play --map biqi_mine_1 --seed 7
    python -m mirsolo.cli.play --shot shots/field.png   # 渲染一帧存图后退出（验收用）
    python -m mirsolo.cli.play --frames 120 --shot x.png  # 先跑 120 帧再存图

按键：WASD / 方向键移动，空格攻击最近的相邻怪，滚轮缩放，ESC 退出。

为什么先设 ``dpi_scaling='real'``：3dworld 实测过 pyglet 2.1 在
Windows 150% 缩放下 ``platform`` 模式会让 viewport 被乘两遍导致画面裁切
（见 3dworld README「已知平台坑」）。表现层同样吃这个坑，提前绕开。
"""

from __future__ import annotations

import argparse
import sys

import pyglet

pyglet.options["dpi_scaling"] = "real"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mirsolo-play", description="mirsolo 可玩窗口")
    parser.add_argument("--seed", type=int, default=20261019, help="世界种子")
    parser.add_argument("--map", default="biqi_field", help="地图 id（见 data/maps.json）")
    parser.add_argument("--class", dest="player_class", default="warrior",
                        choices=("warrior", "mage", "taoist"))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--shot", metavar="PATH", help="渲染一帧存图后退出")
    parser.add_argument("--frames", type=int, default=None, help="最多跑多少帧后退出")
    args = parser.parse_args(argv)

    from mirsolo.presentation.app import launch

    try:
        launch(seed=args.seed, map_id=args.map, player_class=args.player_class,
               width=args.width, height=args.height,
               shot_path=args.shot, max_frames=args.frames)
    except Exception as exc:  # noqa: BLE001 - 开窗失败要给可执行的提示，而不是堆栈
        print(f"[FAIL] 无法启动窗口: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("提示：无显示器/无 GL 时请用 `python -m mirsolo.cli.smoke` 做逻辑验收。",
              file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
