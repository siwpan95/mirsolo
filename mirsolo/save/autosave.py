"""自动存档 —— "关键节点"到底指哪几个时刻，以及为什么是这四个。

四个节点（对应 P3-S6 定档的"手动 F5 存 / F9 读 + 关键节点自动"）：

===========  ==========================  ===========================
节点          观察的东西                   卡在系统之后的原因
===========  ==========================  ===========================
换图          ``current_map`` 变化          新图的怪已经铺好，坐标已是新图坐标
升级          ``last_level_up`` 的 tick    ``apply_growth`` + ``recompute`` 都跑完了
死亡结算      ``last_death`` 的 tick       惩罚已经扣完（延迟结算的关键）
锻造一次      ``upgrade`` / ``bless`` 的结果  ``attempts`` 已自增、材料已扣
===========  ==========================  ===========================

为什么四条**都是轮询**而不是订阅事件
------------------------------------
项目里只有"死亡那一下"（``EVT_DEATH``）是事件；换图虽有 ``EVT_MAP_CHANGED``，
但它在 ``travel`` 里**换图之前**就发了（``app._on_map_changed`` 得自己回头读
新图名），升级与死亡结算都只写世界键。若这里订阅事件、那里轮询，两套节奏就会
在"读档时"打架——**最要命的是 ``load_world`` 把 tick 拨回去的时候**：事件型
钩子会在读档过程中被触发，把刚读进来的整局原地存回一个半读状态。

而 :meth:`_AutoSaver.observe` 有一条硬门禁 ``tick > self._floor``：
读档后第一件事就是把 ``_last_tick`` 抬到存档 tick，于是"tick 倒退"这件事
**永远**不可能被当成节点，读档绝不会引发连锁写盘。

为什么每个节点都要求"tick 严格变大"
----------------------------------
1 刻是 0.1 秒。同一个 tick 里连着发生两件事（打死怪 → 升级 → 掉落）时只应该
存一次盘；反过来，如果只判"值变了"，读档把 ``last_level_up`` 写成 ``None``
也会算一次变化。用 tick 单调性当唯一闸门，两个坑一起堵住。

为什么写盘失败只记一句、不抛
----------------------------
自动存是**副作用**，不是玩法。磁盘满 / 目录只读的时候，不该让游戏当场崩掉——
那会把"存档写不进去"降级成"这一局白玩"。失败信息留在 ``last_error`` 里，
由表现层上屏（玩家至少知道自动存没成，可以手动 F5 到别处）。
"""

from __future__ import annotations

from pathlib import Path

from mirsolo.save.store import AUTO_SLOT, SaveStore

#: 观察用的世界键
KEY_LEVEL_UP = "last_level_up"
KEY_LAST_DEATH = "last_death"
KEY_MAP = "current_map"

#: "没见过任何流水"的哨兵。用 ``-1`` 而不是 ``0``：第 0 刻的流水在含义上
#: 与"没有流水"不同，虽然实战里 tick 从 0 起、第一条流水不会落在 0。
NEVER = -1


def _event_tick(world, key: str) -> int:
    """读某条世界流水的 tick；没有 / 不是 dict / 没有 tick 一律算"没见过"。"""
    if world is None:
        return NEVER
    evt = world.get_data(key)
    if not isinstance(evt, dict):
        return NEVER
    at = evt.get("tick")
    return int(at) if isinstance(at, int) and not isinstance(at, bool) else NEVER


class AutoSaver:
    """盯住四个节点，必要时写自动槽。窗口版每帧调一次 :meth:`observe`。"""

    def __init__(self, store: SaveStore, *, slot: str = AUTO_SLOT) -> None:
        self.store = store
        self.slot = slot
        self.writes = 0
        #: 上一次观察到的世界 tick（读档/初始化时抬到当前 tick）
        self._last_tick = 0
        #: 已经处理过的节点流水刻度（-1 = 还没见过任何一条）
        self._seen = {"level": -1, "death": -1}
        self._seen_map = ""
        self._seen_forge = 0
        #: 读档后要把闸门抬到这里（= 存档里的 tick）
        self._floor = -1
        #: 最近一次写盘失败的原因（成功即清空）
        self.last_error = ""

    # ── 闸门 ────────────────────────────────────────

    def reset(self, *, tick: int, map_id: str = "", forge_seq: int = 0,
              world=None) -> None:
        """把观察基线抬到 ``tick`` 之后再观察。

        读档与开局都要调：不调的话"上次 tick 是 0、这次是 2000"会被当成
        一次换图/升级，刚进游戏就写一个自动档（把玩家可能想手动读的那格盖掉）。
        """
        self._last_tick = int(tick)
        self._floor = int(tick)
        self._seen_map = str(map_id)
        self._seen_forge = int(forge_seq)
        # 基线要以**当下已发生过的事**为起点，不能简单置 -1：
        # ``last_level_up`` 可能在第 1 刻就写了、而 reset 发生在第 2 刻
        # （开局 / 读档都可能是这个次序）。置 -1 会让"过去的事"看起来比基线新，
        # 读档第一帧就白写一个自动档。这里直接把已存在的流水刻度当作"已见过"。
        self._seen["level"] = _event_tick(world, KEY_LEVEL_UP)
        self._seen["death"] = _event_tick(world, KEY_LAST_DEATH)

    def bump(self, *, tick: int, map_id: str = "", forge_seq: int = 0,
             world=None) -> None:
        """同 :meth:`reset`：表现层自己扣完的节点（例如按 R 原地复活）用它抬基线。"""
        self.reset(tick=tick, map_id=map_id, forge_seq=forge_seq, world=world)

    # ── 观察 ────────────────────────────────────────

    def observe(self, session, *, forge_seq: int = 0) -> str:
        """每帧调一次，返回本次**触发**的节点名（没触发返回 ``""``）。

        ``forge_seq`` 是"这一局已经锻造过几次"的单调计数：锻造结果没有世界键，
        由表现层累加历史里的 ``last_forge`` 得到。它**不落盘**——读档后从 0
        重新数，配合 :meth:`reset` 的基线，绝不会因为"读档时它归零"而误触发。
        """
        world = session.world
        tick = int(world.get_data("tick", 0))
        if tick <= self._floor:
            # 读档把时间拨回去了 / 同一刻的重入：什么都不做，也不改基线
            return ""
        # 两种次序都要能认出来：
        #   * 事件在 tick 内先落、observe 后跑 —— 常规情形；
        #   * ``reset`` 早于事件写入（开局时 ``populate`` 先铺怪、第 1 刻才升级，
        #     或读档把旧流水一并搬回来）—— 此时 ``_seen`` 落后于世界状态。
        # 判据统一为"**这条流水的刻度比基线新**"：靠流水自己的 tick，不靠前帧的 tick。
        reason = ""
        map_id = str(world.get_data(KEY_MAP) or "")
        if self._seen_map and map_id and map_id != self._seen_map:
            reason = "map"
        elif _event_tick(world, KEY_LEVEL_UP) > self._seen["level"]:
            reason = "level"
        elif _event_tick(world, KEY_LAST_DEATH) > self._seen["death"]:
            reason = "death"
        elif forge_seq > self._seen_forge:
            reason = "forge"

        # 基线无条件跟上：即使没触发，也要记住"我已经看过这一格了"。
        # **不做** ``max()``：读档后计数会归零，此刻必须**跟着降**；
        # 能出错的那种情形（前一局数到 5、新局从 0 起）由 tick 闸门先拦住——
        # 只要 tick 没变大，:meth:`observe` 早在上面就 return 了。
        self._last_tick = tick
        self._seen_map = map_id or self._seen_map
        self._seen_forge = int(forge_seq)
        if not reason:
            return ""
        self._remember(reason, world)
        self.save(session, label=reason)
        return reason

    def _remember(self, reason: str, world) -> None:
        if reason == "level":
            self._seen["level"] = _event_tick(world, KEY_LEVEL_UP)
        elif reason == "death":
            self._seen["death"] = _event_tick(world, KEY_LAST_DEATH)

    # ── 写盘 ────────────────────────────────────────

    def save(self, session, *, label: str = "") -> bool:
        """写自动槽，成功返回 True。失败**不抛**（见模块 docstring 末段）。"""
        try:
            self.store.write(session, self.slot, label=f"自动·{label}" if label else "自动")
        except (OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = ""
        self.writes += 1
        return True

    def exists(self) -> bool:
        try:
            return self.store.exists(self.slot)
        except (OSError, ValueError):
            return False


def autosave_path(store: SaveStore | None = None) -> Path:
    """自动槽的绝对路径（日志/提示用）。"""
    target = store or SaveStore()
    return target.path_of(AUTO_SLOT)


def remove_autosave(store: SaveStore | None = None) -> bool:
    """删掉自动槽（--new-game 之类的外壳用得上）。"""
    try:
        return (store or SaveStore()).delete(AUTO_SLOT)
    except (OSError, ValueError):
        return False


__all__ = ["AutoSaver", "autosave_path", "remove_autosave", "KEY_LEVEL_UP",
           "KEY_LAST_DEATH", "KEY_MAP"]
