"""mirsolo 实机视频录制 —— 离屏循环出帧 + ffmpeg 编码。

复用表现层渲染管线（与 ``python -m mirsolo.cli.play --shot`` 同源），
但用 AI 策略驱动玩家自动操作（走位/攻击/放技能），逐帧出 PNG 后再编码成 mp4。

用法::

    python record_gameplay.py --class warrior --level 35 --map biqi_field \
        --seconds 20 --fps 30 --out gameplay.mp4

依赖：本机已装 mirsolo（editable）+ pyglet + ffmpeg（在 PATH 或脚本同目录）。
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys

import pyglet

pyglet.options["dpi_scaling"] = "real"

from mirsolo.bootstrap import build_session, populate
from mirsolo.presentation.app import MirsoloWindow, prepare_character
from mirsolo.save import SaveStore
from mirsolo.components import (
    Downed, Identity, Position, Progression, Skills, Vitals,
)
from mirsolo.combat.resolver import MELEE_RANGE, chebyshev


def _sign(x: float) -> int:
    return 0 if x == 0 else (1 if x > 0 else -1)


class RecordWindow(MirsoloWindow):
    """关掉可见窗口（避免录制时闪窗），并注入 AI 策略。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_visible(False)
        self._ai_last_skill = -1000
        self._wander = (1, 0)

    # ── AI 策略 ─────────────────────────────────────
    def _map_size(self):
        return self.session.data.maps["maps"][self.session.map_id]["size"]

    def _ai_pick(self):
        """挑目标：只考虑不贴边的怪（边缘外是虚空，追过去画面穿帮）。

        中央区没怪就返回 None（上层会回中游走），绝不回退到贴边怪——
        否则 AI 会把玩家一路带到地图边上打，镜头照出一角虚空。
        """
        W, H = self._map_size()
        margin = 14
        player = self.session.world.get_entity("player")
        if player is None:
            return None, 10 ** 9
        faction = player.get(Identity).faction
        ppos = player.get(Position)
        best, best_d = None, 10 ** 9
        for mob in self.session.world.each(Identity, Position, Vitals):
            ident = mob.get(Identity)
            if ident.faction == faction or not mob.get(Vitals).alive:
                continue
            epos = mob.get(Position)
            if epos.x < margin or epos.y < margin or epos.x > W - margin or epos.y > H - margin:
                continue
            d = chebyshev(mob.get(Position), ppos)
            if d < best_d:
                best, best_d = mob, d
        if best is None:
            return None, 10 ** 9
        return best, best_d

    def _ai_policy(self):
        player = self.session.world.get_entity("player")
        if player is None or player.has(Downed):
            return (0, 0)
        W, H = self._map_size()
        margin = 14
        ppos = player.get(Position)
        # 贴边检查必须最先做：否则"贴脸站定输出"会把玩家钉在边上打
        near_edge = (ppos.x < margin or ppos.y < margin
                     or ppos.x > W - margin or ppos.y > H - margin)
        if near_edge:
            dx, dy = W / 2.0 - ppos.x, H / 2.0 - ppos.y
            return (_sign(dx) or 1, _sign(dy) or 1)
        enemy, dist = self._ai_pick()
        if enemy is None:
            # 中央区没怪：回中央游走区
            dx, dy = W / 2.0 - ppos.x, H / 2.0 - ppos.y
            if abs(dx) < 2 and abs(dy) < 2:
                self._wander = (-self._wander[0], -self._wander[1])
                return self._wander
            return (_sign(dx), _sign(dy))
        if dist <= MELEE_RANGE:
            return (0, 0)  # 贴脸就站定输出
        epos = enemy.get(Position)
        return (_sign(epos.x - ppos.x), _sign(epos.y - ppos.y))

    def _ai_act(self):
        player = self.session.world.get_entity("player")
        if player is None or player.has(Downed):
            return
        enemy, dist = self._ai_pick()
        if enemy is None:
            return
        if dist <= MELEE_RANGE:
            self.session.queue_attack(player, enemy)
        # 周期性放一个攻击型技能，展示技能特效（含半月 AOE）
        if self.session.tick - self._ai_last_skill >= 45 and dist <= 4:
            order = self._skill_order()
            for sid in order:
                spec = self.session.data.skills["skills"].get(sid) or {}
                kind = spec.get("kind")
                if kind in ("physical", "magical", "physical_aoe"):
                    rng = int(spec.get("range", MELEE_RANGE))
                    if rng >= dist:
                        self.session.cast_skill(player, sid, enemy)
                        self._ai_last_skill = self.session.tick
                        break

    def tick(self, real_dt):
        self._last_real_dt = min(0.25, max(0.0, real_dt))
        player = self.session.world.get_entity("player")
        stalled = player is not None and player.has(Downed)
        move_vec = (0, 0) if stalled else self._ai_policy()
        self.session.world.set_data("move_intent", move_vec)
        if not stalled:
            self._ai_act()
        self.session.engine.advance(self._last_real_dt)
        self._report_potion()
        self._report_ore()
        self.autosaver.observe(self.session, forge_seq=self.forge_attempts)
        if player is not None:
            pos = player.get(Position)
            self.camera.follow(pos.x, pos.y + 1.0)
            map_spec = self.session.data.maps["maps"][self.session.map_id]
            W, H = map_spec["size"]
            # 等距投影下视野沿对角线半宽 ~18 格（实测），clamp 23 才保证画面
            # 不照到地图外的虚空；AI margin 14 让玩家在中央 36x36 活动
            self.camera.clamp_to_map(W, H, margin=23)


def record(*, seed, map_id, player_class, level, gear, learn,
           width, height, fps, seconds, frame_dir):
    session = build_session(seed=seed, map_id=map_id)
    populate(session, player_class=player_class)
    prepare_character(session, class_id=player_class, level=level,
                      learn_all=learn, gear=gear, gold=0)
    window = RecordWindow(session=session, width=width, height=height,
                          saves=SaveStore())
    window.camera.zoom(0.72)  # 拉近：角色更大、单屏内容更密，贴边虚空也更少
    window.switch_to()
    total = max(1, int(fps * seconds))
    dt = 1.0 / fps
    os.makedirs(frame_dir, exist_ok=True)
    for i in range(total):
        window.dispatch_events()
        window.tick(dt)
        window.on_draw()
        pyglet.image.get_buffer_manager().get_color_buffer().save(
            os.path.join(frame_dir, f"frame_{i:05d}.png"))
        if (i + 1) % 60 == 0:
            print(f"  出帧 {i + 1}/{total}  "
                  f"击杀={sum(session.kill_counts().values())} "
                  f"Lv{session.world.get_entity('player').get(Progression).level}",
                  flush=True)
    window.close()
    session.shutdown()
    return total


def _ffmpeg_bin():
    candidates = [
        r"E:\ffmpeg\bin\ffmpeg.exe",
        "/e/ffmpeg/bin/ffmpeg",
        "ffmpeg",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "ffmpeg"


def encode(frame_dir, out_path, fps):
    pattern = os.path.join(frame_dir, "frame_%05d.png")
    env = dict(os.environ)
    ffmpeg_dir = r"E:\ffmpeg\bin"
    if os.path.isdir(ffmpeg_dir):
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    cmd = [
        _ffmpeg_bin(), "-y", "-framerate", str(fps), "-i", pattern,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
        "-movflags", "+faststart", out_path,
    ]
    subprocess.run(cmd, check=True, env=env)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261019)
    ap.add_argument("--map", default="biqi_field")
    ap.add_argument("--class", dest="player_class", default="warrior")
    ap.add_argument("--level", type=int, default=35)
    ap.add_argument("--gear", action="store_true", default=True)
    ap.add_argument("--no-gear", dest="gear", action="store_false")
    ap.add_argument("--learn", action="store_true", default=True)
    ap.add_argument("--no-learn", dest="learn", action="store_false")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--frame-dir", default=None)
    ap.add_argument("--out", default="gameplay.mp4")
    args = ap.parse_args(argv)

    frame_dir = args.frame_dir or (os.path.splitext(args.out)[0] + "_frames")
    print(f"[rec] 录制 {args.player_class} L{args.level} @ {args.map} "
          f"{args.seconds}s @{args.fps}fps -> {args.out}", flush=True)
    n = record(seed=args.seed, map_id=args.map, player_class=args.player_class,
               level=args.level, gear=args.gear, learn=args.learn,
               width=args.width, height=args.height, fps=args.fps,
               seconds=args.seconds, frame_dir=frame_dir)
    print(f"[rec] 出帧完成 {n} 张，编码中…", flush=True)
    encode(frame_dir, args.out, args.fps)
    size = os.path.getsize(args.out)
    print(f"[rec] 完成：{args.out}  ({size/1e6:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
