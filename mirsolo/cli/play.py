"""可玩入口 —— 开窗跑等距斜俯视版本。

用法::

    python -m mirsolo.cli.play                  # 开窗游玩
    python -m mirsolo.cli.play --map biqi_mine_1 --seed 7
    python -m mirsolo.cli.play --shot shots/field.png   # 渲染一帧存图后退出（验收用）
    python -m mirsolo.cli.play --frames 120 --shot x.png  # 先跑 120 帧再存图
    python -m mirsolo.cli.play --class mage --level 35 --learn   # 验收：满技能法师
    python -m mirsolo.cli.play --level 20 --gear --panel character --shot x.png
    python -m mirsolo.cli.play --level 50 --gear --siege sabak --shot x.png  # 验收：沙巴克攻城
    python -m mirsolo.cli.play --level 50 --gold 200000 --merc gubing huwei \
        --panel mercenary --shot x.png     # 验收：佣兵面板（带队伍与倒计时）
    python -m mirsolo.cli.play --codex 6 --panel codex --shot x.png          # 验收：图鉴面板
    python -m mirsolo.cli.play --gold 200000 --bag --panel auction --shot x.png
        # 验收：拍卖行面板（背包栏得有货才看得出寄卖行的版式）

按键：WASD / 方向键移动，空格攻击最近的相邻怪，1-5 施放技能，滚轮缩放，
C 角色 / M 佣兵 / J 图鉴 / N 拍卖行 / B 商店 / V 仓库（点行操作），
ESC 关面板 / 再按退出。走上传送点自动换图。

``--level`` / ``--learn`` / ``--gear`` / ``--gold`` / ``--merc`` / ``--codex`` /
``--bag`` / ``--panel`` 都是**验收开关**（不是作弊菜单）：升级走 ``apply_growth``、
学技能走 ``learn`` + 真发技能书、穿装备走 ``equip``（含职业/等级/负重四道门）、
雇佣兵走 ``mercenary.hire``（含等级/名额/金币三道门），与游戏内路径完全同一条。

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
    parser.add_argument("--level", type=int, default=1,
                        help="验收用：直接把角色提到该等级（走 apply_growth）")
    parser.add_argument("--learn", action="store_true",
                        help="验收用：发放本职业技能书并学会当前等级可学的全部技能")
    parser.add_argument("--gear", action="store_true",
                        help="验收用：发放并把该等级能穿的最好一套装备穿上（走 equip）")
    parser.add_argument("--panel",
                        choices=("character", "mercenary", "codex", "auction",
                                 "shop", "warehouse"),
                        default=None, help="验收用：开窗即打开该面板（截图看版式）")
    parser.add_argument("--gold", type=int, default=0,
                        help="验收用：给一笔金币（否则买不起/雇不起的行全灰，看不出版式）")
    parser.add_argument("--merc", nargs="*", default=(), metavar="ID",
                        help="验收用：开窗即雇好这些佣兵（走 mercenary.hire）")
    parser.add_argument("--codex", type=int, default=0, metavar="N",
                        help="验收用：图鉴记 N 种已解锁（只记账，不发首杀奖励）")
    parser.add_argument("--bag", action="store_true",
                        help="验收用：往背包塞几件可寄卖装备（不穿），拍卖行/商店截图用")
    parser.add_argument("--siege", default=None, metavar="ID",
                        help="验收用：开窗即开战（如 sabak），会按战役自己的 map 换图")
    args = parser.parse_args(argv)

    from mirsolo.presentation.app import launch

    try:
        launch(seed=args.seed, map_id=args.map, player_class=args.player_class,
               width=args.width, height=args.height,
               shot_path=args.shot, max_frames=args.frames,
               level=args.level, learn=args.learn, gear=args.gear,
               panel=args.panel, siege=args.siege, gold=args.gold,
               mercs=tuple(args.merc), codex_kills=args.codex, bag=args.bag)
    except Exception as exc:  # noqa: BLE001 - 开窗失败要给可执行的提示，而不是堆栈
        print(f"[FAIL] 无法启动窗口: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("提示：无显示器/无 GL 时请用 `python -m mirsolo.cli.smoke` 做逻辑验收。",
              file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
