"""持久化存档（P3-S6）。

三层，各管一件事：

:mod:`~mirsolo.save.schema`
    登记表 —— 每个组件归哪一类（真相 / 派生 / 运行时 / 部分），
    以及真相组件逐字段的类型码。**唯一**一张"什么进档"的表。

:mod:`~mirsolo.save.codec`
    会话 ↔ 纯 JSON 字典。含校验和（FNV-1a，不是 ``hash()``）与
    严格的类型 round-trip，以及"先派生、后真相、最后钳血"的读档顺序。

:mod:`~mirsolo.save.store`
    文件层 —— 槽位命名、原子写（tmp + ``os.replace``）、坏档可分辨。

:mod:`~mirsolo.save.autosave`
    自动存档 —— 哪四个节点、为什么是轮询而不是订阅事件、
    以及"读档绝不会引发连锁写盘"这道闸门。
"""

from __future__ import annotations

from mirsolo.save.autosave import AutoSaver
from mirsolo.save.codec import (
    PLAYER_EID,
    SaveError,
    checksum,
    dump_player,
    dump_session,
    dump_world,
    load_player,
    load_session,
    load_world,
    seal,
    summary,
    verify,
)
from mirsolo.save.schema import SCHEMA_VERSION
from mirsolo.save.store import (
    AUTO_SLOT,
    DEFAULT_DIR,
    MANUAL_SLOTS,
    SaveStore,
    all_slots,
    manual_slot,
    read_file,
    write_file,
)

__all__ = [
    "AUTO_SLOT",
    "AutoSaver",
    "DEFAULT_DIR",
    "MANUAL_SLOTS",
    "PLAYER_EID",
    "SCHEMA_VERSION",
    "SaveError",
    "SaveStore",
    "all_slots",
    "checksum",
    "dump_player",
    "dump_session",
    "dump_world",
    "load_player",
    "load_session",
    "load_world",
    "manual_slot",
    "read_file",
    "seal",
    "summary",
    "verify",
    "write_file",
]
