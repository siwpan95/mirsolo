"""构建 mirsolo 的 Windows 发行版 exe。

用法::

    .venv\\Scripts\\python scripts\\build_exe.py              # 构建全部三个目标
    .venv\\Scripts\\python scripts\\build_exe.py --target play   # 只构建窗口版

产出（``dist/``，构建临时目录 ``build/`` 均已 .gitignore）：

==============  ==============  ============================================
文件名           形态           用途
==============  ==============  ============================================
mirsolo.exe     windowed       发行主产物：双击开玩，无黑窗
mirsolo-smoke.exe  console     发行附带：headless 冒烟（命令行验收）
mirsolo-console.exe  console   仅自检：窗口入口的控制台变体（拿退出码验证）
==============  ==============  ============================================

设计要点：

- 数据表 ``data/*.json`` 通过 ``--add-data`` 打进 ``_MEIPASS/data``；
  ``mirsolo/data/loader.py`` 的 ``parents[2] / "data"`` 定位逻辑在 frozen
  环境下恰好命中（``__file__`` = ``_MEIPASS/mirsolo/data/loader.py``）。
  用户也可用环境变量 ``MIRSOLO_DATA`` 指到外部数据目录覆盖。
- 依赖按真实 import 分析：逻辑层不 import numpy/scipy/sounddevice，
  表现层只用 pyglet 的窗口/GL/文字（无 media），因此 exe 只收 pyglet。
- windowed 版拿不到退出码，故另打一个与 windowed 完全同配置的 console
  变体（mirsolo-console.exe）做发布前自检。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
VERSION_FILE = Path(__file__).resolve().parent / "version_info.txt"
SEP = os.pathsep  # Windows ';'，Linux ':'

# 三个目标共享的参数（--windowed 是唯一差异点）
_COMMON = [
    "--noconfirm",
    "--clean",
    "--onefile",
    "--add-data", f"data{SEP}data",
    "--version-file", str(VERSION_FILE),
    "--distpath", str(DIST),
    "--workpath", str(REPO / "build" / "pyinstaller"),
    "--specpath", str(REPO / "build" / "pyinstaller"),
]

TARGETS = {
    "play": {
        "name": "mirsolo",
        "windowed": True,
        "script": "mirsolo/cli/play.py",
    },
    "smoke": {
        "name": "mirsolo-smoke",
        "windowed": False,
        "script": "mirsolo/cli/smoke.py",
    },
    "console": {
        "name": "mirsolo-console",
        "windowed": False,
        "script": "mirsolo/cli/play.py",
    },
}


def build(target: str) -> None:
    spec = TARGETS[target]
    args = _COMMON + ["--name", spec["name"], str(REPO / spec["script"])]
    if spec["windowed"]:
        args.append("--windowed")
    print(f"[build] {spec['name']} ({'windowed' if spec['windowed'] else 'console'})")
    PyInstaller.__main__.run(args)
    exe = DIST / f"{spec['name']}.exe"
    if not exe.exists():
        raise SystemExit(f"[FAIL] 产物缺失: {exe}")
    print(f"[ok] {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 mirsolo 发行版 exe")
    parser.add_argument("--target", choices=sorted(TARGETS), default=None,
                        help="只构建某个目标（默认全部）")
    args = parser.parse_args()

    targets = [args.target] if args.target else list(TARGETS)
    for t in targets:
        build(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
