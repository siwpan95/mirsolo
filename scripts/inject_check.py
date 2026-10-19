"""注入验证：把生产代码改坏，确认新回归真的会红。

每次只改一处、跑完立刻恢复（用原文字节备份，恢复失败会明显报错）。
"永远为绿的门禁等于装饰品"——测试同理。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

INJECTIONS = [
    ("传送点判定放宽到「附近一格」",
     ROOT / "mirsolo/world/travel.py",
     "if int(at[0]) == tx and int(at[1]) == ty:",
     "if abs(int(at[0]) - tx) <= 1 and abs(int(at[1]) - ty) <= 1:",
     "tests.test_travel"),
    ("落点直接压在回程传送点上",
     ROOT / "mirsolo/world/travel.py",
     "    ax = px + (step if cx > px else -step if cx < px else 0)",
     "    ax = px",
     "tests.test_travel"),
    ("eid 退回 entity_count()+1",
     ROOT / "mirsolo/actor/factory.py",
     "    unique_eid = eid or next_entity_id(world, monster_id)",
     "    unique_eid = eid or f\"{monster_id}#{world.entity_count() + 1}\"",
     "tests.test_travel"),
    ("面板命中测试改用列号（滚动后错位）",
     ROOT / "mirsolo/presentation/panels.py",
     "        for row in self.rows:\n            if row.rect.contains(x, y):\n                return row",
     "        for row in self.rows:\n            if row.rect.contains(x, row.rect.y + row.rect.h / 2):\n                return row",
     "tests.test_panels"),
    ("面板显示回收价当买入价",
     ROOT / "mirsolo/presentation/panels.py",
     "        price = buy_price(data, item_id)",
     "        price = sell_price(data, item_id)",
     "tests.test_panels"),
]


def run(test_module: str) -> tuple[bool, str]:
    proc = subprocess.run([PY, "-m", test_module], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    tail = "\n".join(line for line in proc.stdout.splitlines() if "FAIL" in line)
    return proc.returncode == 0, tail


def main() -> int:
    failures = 0
    for label, path, old, new, module in INJECTIONS:
        original = path.read_bytes()
        text = original.decode("utf-8")
        if old not in text:
            print(f"[SKIP] {label}: 找不到注入点（代码已变，请更新本脚本）")
            failures += 1
            continue
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        try:
            green, tail = run(module)
        finally:
            path.write_bytes(original)  # 无论结果如何都恢复
        if green:
            print(f"[BAD ] {label}: 注入后测试仍然全绿 —— 这条回归抓不到 bug")
            failures += 1
        else:
            print(f"[OK  ] {label}: 测试变红")
            for line in tail.splitlines()[:3]:
                print(f"        {line.strip()}")
    print(f"\n{len(INJECTIONS) - failures}/{len(INJECTIONS)} 处注入被抓到")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
