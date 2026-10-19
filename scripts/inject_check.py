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
    ("攻城推进不让上一波退场（旧城门+残兵堆在旗台下）",
     ROOT / "mirsolo/siege/campaign.py",
     "    # 先让上一波退场（城门 + 守军），再铺下一波\n    _retire_wave(world, state)",
     "    # 先让上一波退场（城门 + 守军），再铺下一波\n    pass",
     "tests.test_siege"),
    ("攻城推进不锁非当前波次目标（没打下城门就能砍旗台）",
     ROOT / "mirsolo/siege/campaign.py",
     "            objective.invulnerable = not current",
     "            objective.invulnerable = False",
     "tests.test_siege"),
    ("增援封顶失效（max 不生效）",
     ROOT / "mirsolo/siege/campaign.py",
     "            if tick - state.last_reinforce_tick >= every and state.reinforced < int(reinforce[\"max\"]):",
     "            if tick - state.last_reinforce_tick >= every:",
     "tests.test_siege"),
    ("战役目标改成 kind=monster（死亡奖励重复发/尸体不清）",
     ROOT / "mirsolo/siege/campaign.py",
     "    entity.add(Identity(name=wave[\"name\"], kind=\"guard\", faction=1,",
     "    entity.add(Identity(name=wave[\"name\"], kind=\"monster\", faction=1,",
     "tests.test_siege"),
    ("战役状态条忽略 elapsed（时钟永远满格、永不标红）",
     ROOT / "mirsolo/presentation/hud.py",
     "    elapsed = int(status.get(\"elapsed\", 0))",
     "    elapsed = 0",
     "tests.test_presentation"),
    ("赤月退回祖玛换皮怪（后段图又变成祖玛翻版）",
     ROOT / "data/maps.json",
     '"monster": "chiyue_mozhu",',
     '"monster": "zumma_weishi",',
     "tests.test_data"),
    ("怪等级远低于地图带（50 级图刷 15 级怪）",
     ROOT / "data/monsters.json",
     '"name": "赤月魔蛛",\n      "level": 50,',
     '"name": "赤月魔蛛",\n      "level": 15,',
     "tests.test_data"),
]


def _match(original: str, needle: str) -> tuple[str, str] | None:
    """在文件正文里找注入点，**换行符归一后再比**。

    坑：本仓库的文件是 CRLF（Windows），而 ``INJECTIONS`` 里的多行 needle
    写的是 ``\\n``。之前只在 LF 文件上碰巧命中，换成 CRLF 文件就静默 SKIP——
    注入验证"什么都没注入还报 SKIP"，等于这条门禁自己失效。
    这里做的是：把 needle 的 ``\\n`` 先试 LF，再试 CRLF。
    """
    if needle in original:
        return needle, needle.count("\n")
    crlf = needle.replace("\n", "\r\n")
    if crlf in original:
        return crlf, needle.count("\n")
    return None


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
        matched = _match(text, old)
        if matched is None:
            print(f"[SKIP] {label}: 找不到注入点（代码已变，请更新本脚本）")
            failures += 1
            continue
        needle, _ = matched
        new_text = new.replace("\n", "\r\n") if "\r\n" in needle else new
        if needle not in text:
            print(f"[SKIP] {label}: 找不到注入点（代码已变，请更新本脚本）")
            failures += 1
            continue
        path.write_text(text.replace(needle, new_text, 1), encoding="utf-8", newline="")
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
