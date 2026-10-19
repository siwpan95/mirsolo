"""headless 冒烟 CLI —— 不开窗口、不依赖 GL，纯逻辑跑一遍战斗-成长-掉落闭环。

用法::

    python -m mirsolo.cli.smoke                 # 默认 3000 刻、种子 20261019
    python -m mirsolo.cli.smoke --ticks 6000 --seed 7
    python -m mirsolo.cli.smoke --json          # 机器可读摘要（门禁/CI 用）

退出码：0 = 闭环跑通；2 = 数据表校验失败；3 = 推演后无任何击杀（说明协议断了）。

它只做一件事：**证明"打怪 → 掉装 → 升级"这条链在无窗口环境下是通的**。
真正的可玩性交给表现层，逻辑正确性交给 ``tests/``。
"""

from __future__ import annotations

import argparse
import json
import sys

from mirsolo.bootstrap import build_session, populate
from mirsolo.components import Bag, Identity, Position, Progression, Vitals, Wallet
from mirsolo.data.loader import GameData


MELEE_RANGE = 1  # 近战接触距离（切比雪夫网格距离）


def _nearest_alive_monster(session, player):
    """最近的活怪（直接扫世界，因此刷怪系统补的怪也能被找到）。"""
    ppos = player.get(Position)
    best, best_dist = None, 10**9
    for mob in session.world.each(Identity, Vitals, Position):
        ident = mob.get(Identity)
        if ident.kind != "monster" or not mob.get(Vitals).alive:
            continue
        mpos = mob.get(Position)
        dist = int(max(abs(mpos.x - ppos.x), abs(mpos.y - ppos.y)))
        if dist < best_dist:
            best, best_dist = mob, dist
    return best, best_dist


def _step_grid(pos, target_pos) -> None:
    """八向网格走一格（纯整数，无浮点，回放可复算）。"""
    dx = target_pos.x - pos.x
    dy = target_pos.y - pos.y
    if dx:
        pos.x += 1 if dx > 0 else -1
    if dy:
        pos.y += 1 if dy > 0 else -1


def run_simulation(*, ticks: int, seed: int, map_id: str, player_class: str) -> dict:
    """跑 ``ticks`` 个固定步，返回结构化摘要（确定性：同参数同结果）。

    驱动策略（这是 P0 的"临时玩家"，不是正式输入层）：

    1. 选最近活怪，八向走到相邻格；相邻则出刀。
    2. 怪物只在相邻时反击（每 3 刻一次）。
    3. 玩家死亡即回城复活（满血、回到地图中心），并计数——不做掉落惩罚。
    """
    data = GameData.load()
    if not data.ok:
        return {"ok": False, "errors": data.errors}

    session = build_session(data=data, seed=seed, map_id=map_id)
    spawned = populate(session, player_class=player_class)
    player, monsters = spawned["player"], spawned["monsters"]

    map_spec = data.maps["maps"][map_id]
    home = (float(map_spec["size"][0] // 2), float(map_spec["size"][1] // 2))
    deaths = 0
    spawned_count = len(monsters)

    for tick in range(ticks):
        vitals = player.get(Vitals)
        if not vitals.alive:
            # 回城复活：满血回地图中心（单机不做掉经验惩罚，P1 再议）
            vitals.hp = vitals.max_hp
            vitals.mp = vitals.max_mp
            ppos = player.get(Position)
            ppos.x, ppos.y = home
            deaths += 1
            continue

        target, dist = _nearest_alive_monster(session, player)
        if target is not None:
            ppos = player.get(Position)
            tpos = target.get(Position)
            if dist <= MELEE_RANGE:
                session.queue_attack(player, target)
            else:
                _step_grid(ppos, tpos)
            # 怪物反击：仅在相邻时（隔着半个地图也能打人那是 bug，不是 AI）
            if dist <= MELEE_RANGE and tick % 3 == 0:
                session.queue_attack(target, player)
        session.step(1)

    prog = player.get(Progression)
    wallet = player.get(Wallet)
    bag = player.get(Bag)
    rewards = session.rewards()
    summary = {
        "ok": True,
        "seed": seed,
        "map": map_id,
        "class": player_class,
        "ticks": ticks,
        "final_tick": session.tick,
        "level": prog.level,
        "exp": prog.exp,
        "hp": player.get(Vitals).hp,
        "max_hp": player.get(Vitals).max_hp,
        "gold": wallet.gold,
        "bag": dict(sorted(bag.items.items())),
        "deaths": deaths,
        "kills": sum(1 for r in rewards if r.get("monster")),
        "reward_events": len(rewards),
        "spawned": spawned_count,
        "entities_left": session.world.entity_count(),
        "kill_counts": dict(sorted(session.kill_counts().items())),
    }
    session.shutdown()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mirsolo-smoke", description="单机复刻 headless 冒烟")
    parser.add_argument("--ticks", type=int, default=3000, help="固定步数（10Hz，默认 3000 = 300 秒）")
    parser.add_argument("--seed", type=int, default=20261019, help="世界种子（决定刷新与掉落）")
    parser.add_argument("--map", default="biqi_field", help="地图 id（见 data/maps.json）")
    parser.add_argument("--class", dest="player_class", default="warrior",
                        choices=("warrior", "mage", "taoist"), help="职业")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = parser.parse_args(argv)

    summary = run_simulation(ticks=args.ticks, seed=args.seed,
                             map_id=args.map, player_class=args.player_class)
    if not summary.get("ok"):
        print("[FAIL] 数据表校验未通过：", file=sys.stderr)
        for err in summary.get("errors", []):
            print("  -", err, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("=== mirsolo 冒烟 ===")
        print(f"地图 {summary['map']} / 职业 {summary['class']} / 种子 {summary['seed']}")
        print(f"推演 {summary['ticks']} 刻（末刻 {summary['final_tick']}）")
        print(f"等级 {summary['level']}  经验 {summary['exp']}  "
              f"HP {summary['hp']}/{summary['max_hp']}  金币 {summary['gold']}")
        print(f"击杀 {summary['kills']}  奖励流水 {summary['reward_events']}  "
              f"场上实体 {summary['entities_left']}")
        print(f"背包 {summary['bag'] or '空'}")

    if summary["kills"] == 0:
        print("[FAIL] 推演期间无任何击杀——战斗链路可能断了", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
