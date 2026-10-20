"""存档文件层 —— 放哪、怎么安全落地、怎么列出与删除。

只有三件事，但每一件都有个不显眼但致命的细节：

**一、原子写**。直接 ``open(path, "w")`` 再 ``json.dump``，进程在写到一半时被
关掉（玩家按 Alt+F4 / 蓝屏 / 笔记本没电）就留下一个**截断的存档**——而它
旁边那个好档已经被覆盖了。所以一律写 ``<name>.tmp`` 再 ``os.replace``：
``os.replace`` 在同一卷上是原子的，要么旧档完整、要么新档完整，没有中间态。

**二、损坏可分辨**。``bad_json`` / ``bad_version`` / ``bad_checksum`` /
``missing_field`` 必须分开报——玩家看到"存档损坏"只会重开一局；
看到"版本是 2，本程序是 1"才知道该升级游戏还是开新档。

**三、槽位是字符串不是路径**。``slot`` 只允许 ``[A-Za-z0-9_-]``，
拼出 ``saves/slot1.json``。这样"从 UI 传个槽名进来"永远不会变成
``../../etc/passwd``——单机也不给自己留这种口子。

目录默认 ``saves/``（已在 ``.gitignore``，存档不进版本库）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from mirsolo.save import codec, schema

#: 槽名合法字符（挡路径穿越）
_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: 手动槽位数
MANUAL_SLOTS = 3

#: 自动存档的槽名（覆盖写）
AUTO_SLOT = "autosave"

#: 默认存档目录（相对工作目录；已在 .gitignore）
DEFAULT_DIR = Path("saves")


def manual_slot(index: int) -> str:
    """1..3 → ``"slot1"``（越界直接报错，不做静默夹取）。"""
    if not 1 <= int(index) <= MANUAL_SLOTS:
        raise ValueError(f"手动槽下标必须是 1..{MANUAL_SLOTS}，得到 {index}")
    return f"slot{int(index)}"


def all_slots() -> list[str]:
    return [manual_slot(i) for i in range(1, MANUAL_SLOTS + 1)] + [AUTO_SLOT]


class SaveStore:
    """一个存档目录。``dir`` 不存在时读写都会自动建。"""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.dir = Path(directory) if directory else DEFAULT_DIR

    # ── 路径 ────────────────────────────────────────

    def path_of(self, slot: str) -> Path:
        name = str(slot)
        if not _SLOT_RE.match(name):
            raise ValueError(f"非法槽名 {slot!r}（只允许字母数字与 -_）")
        # 槽位号必须落在 MANUAL_SLOTS 里：`slot0` / `slot4` 字符上合法，
        # 但它们根本不是合法槽位——放过去就会在目录里长出一堆没人认得的档
        if name.startswith("slot") and name[4:].isdigit():
            index = int(name[4:])
            if not 1 <= index <= MANUAL_SLOTS:
                raise ValueError(f"手动槽下标必须是 1..{MANUAL_SLOTS}，得到 {index}")
        return self.dir / f"{name}{schema.SAVE_SUFFIX}"

    def exists(self, slot: str) -> bool:
        return self.path_of(slot).is_file()

    def slots(self) -> list[str]:
        """目录里实际存在的槽名（按 手动槽 → 自动槽 的顺序）。"""
        return [slot for slot in all_slots() if self.exists(slot)]

    # ── 写 ──────────────────────────────────────────

    def write(self, session, slot: str, *, label: str = "") -> dict:
        """把会话写进 ``slot``，返回摘要。**原子写**（同卷 tmp + replace）。"""
        target = self.path_of(slot)
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = codec.dump_session(
            session, label=label or slot,
            saved_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        sealed = codec.seal(payload)
        text = json.dumps(sealed, ensure_ascii=False, indent=1, sort_keys=True)

        # 同目录建 tmp：跨卷 replace 会退化成 copy+delete，不再原子
        fd, tmp_name = tempfile.mkstemp(prefix=f".{slot}.", suffix=".tmp", dir=str(self.dir))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            # 任何失败都不能留下半个 tmp 或半个存档
            tmp.unlink(missing_ok=True)
            raise
        return codec.summary(sealed)

    # ── 读 ──────────────────────────────────────────

    def read(self, slot: str) -> dict:
        """读一份存档并校验，返回 payload（不合格抛 ``SaveError``）。"""
        return read_file(self.path_of(slot))

    def load(self, session, slot: str) -> dict:
        """把 ``slot`` 应用到一个**已经 populate 过**的会话上，返回摘要。"""
        return codec.load_session(session, self.read(slot))

    # ── 管理 ────────────────────────────────────────

    def peek(self, slot: str) -> dict | None:
        """读摘要；坏档返回 ``{"slot":…, "broken":<code>}`` 而不是抛。

        列表 UI 必须能显示"这一格坏了"——否则一个坏档会让整页列表打不开，
        玩家想删都删不掉。
        """
        try:
            return {"slot": slot, **codec.summary(self.read(slot))}
        except codec.SaveError as exc:
            return {"slot": slot, "broken": exc.code,
                    "error": str(exc), "saved_at": "", "label": slot}
        except FileNotFoundError:
            return None

    def delete(self, slot: str) -> bool:
        target = self.path_of(slot)
        if not target.is_file():
            return False
        target.unlink()
        return True


def read_file(path: str | os.PathLike) -> dict:
    """读并校验任意一个存档文件（``--load PATH`` 用得上）。"""
    file = Path(path)
    raw = file.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise codec.SaveError("bad_json", f"{file.name} 不是合法 JSON：{exc}") from exc
    return codec.verify(payload)


def write_file(session, path: str | os.PathLike, *, label: str = "") -> dict:
    """写到任意路径（同上原子写）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = codec.dump_session(
        session, label=label or target.stem,
        saved_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    sealed = codec.seal(payload)
    text = json.dumps(sealed, ensure_ascii=False, indent=1, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}.",
                                    suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return codec.summary(sealed)
