"""测试聚合 runner —— ``python -m tests.run_tests``。

口径与 3dworld 一致：每个测试模块暴露 ``run() -> (passed, failed)``，
本 runner 汇总并在最后打印机器可读标记 ``ALL_MIRSOLO_TESTS_PASSED``。
CI 与 pre-commit 认这个标记，不认"看起来跑完了"。

注意：``MODULES`` 与 ``tests/test_*.py`` 的双向一致性由
``scripts/verify.py`` 的门禁 ``tests-registered`` 钉住——
新加测试文件忘了登记，门禁会红。
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

# 允许直接 `python tests\run_tests.py`（不依赖 editable 安装）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULES = [
    "test_determinism",
    "test_formulas",
    "test_exp",
    "test_drops",
    "test_data",
    "test_bootstrap",
    "test_camera",
    "test_presentation",
    "test_movement",
    "test_skills",
]

PASS_MARKER = "ALL_MIRSOLO_TESTS_PASSED"


def main() -> int:
    total_passed = total_failed = 0
    started = time.perf_counter()
    print("=== mirsolo 测试 ===")
    for name in MODULES:
        module = importlib.import_module(f"tests.{name}")
        print(f"\n[{name}]")
        passed, failed = module.run()
        total_passed += passed
        total_failed += failed
        print(f"  -> {passed} passed, {failed} failed")

    elapsed = (time.perf_counter() - started) * 1000
    print("\n" + "=" * 46)
    print(f"合计 {len(MODULES)} 个模块：{total_passed} passed, {total_failed} failed"
          f"（{elapsed:.0f} ms）")
    if total_failed == 0:
        print(PASS_MARKER)
        return 0
    print("SOME_MIRSOLO_TESTS_FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
