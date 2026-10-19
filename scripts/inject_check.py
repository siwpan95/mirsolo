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
     "    unique_eid = eid or next_entity_id(world, species)",
     "    unique_eid = eid or f\"{species}#{world.entity_count() + 1}\"",
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
    ("幸运收窄失效（lucky_range 原样返回，幸运 9 不出刀刀上限）",
     ROOT / "mirsolo/combat/formulas.py",
     "    luck = clamp_luck(luck)\n    if luck <= 0:\n        return (lo, hi)\n"
     "    return (hi - (hi - lo) * (LUCK_CAP - luck) // LUCK_CAP, hi)",
     "    luck = clamp_luck(luck)\n    if luck <= 0 or True:\n        return (lo, hi)\n"
     "    return (hi - (hi - lo) * (LUCK_CAP - luck) // LUCK_CAP, hi)",
     "tests.test_luck"),
    ("幸运改成每点掷一次 lucky（随机数个数随幸运变化）",
     ROOT / "mirsolo/combat/formulas.py",
     "    luck = clamp_luck(luck)\n    outcome = AttackOutcome()",
     "    luck = clamp_luck(luck)\n"
     "    _dummy = rng.rand_int(1, 1000) if luck else 0\n    outcome = AttackOutcome()",
     "tests.test_luck"),
    ("resolver 不把幸运递给公式（三职业幸运装等于没穿）",
     ROOT / "mirsolo/combat/resolver.py",
     "            power_permille=power_permille,\n            luck=atk_attr.luck,",
     "            power_permille=power_permille,",
     "tests.test_luck"),
    ("recompute 不写 Attributes.luck（装备有幸运、实际不生效）",
     ROOT / "mirsolo/items/equipment.py",
     "    attr.luck = clamp_luck(luck)",
     "    attr.luck = 0",
     "tests.test_luck"),
    ("幸运改成概率打上限（每刀多一次掷点，回放错位）",
     ROOT / "mirsolo/combat/formulas.py",
     "    atk_lo, atk_hi = lucky_range(dc, luck)\n    atk = roll_damage_range((atk_lo, atk_hi), rng)",
     "    atk_lo, atk_hi = lucky_range(dc, luck)\n"
     "    atk = atk_hi if (luck and rng.rand_int(1, LUCK_CAP) <= luck) else "
     "roll_damage_range((atk_lo, atk_hi), rng)",
     "tests.test_luck"),
    ("resolver 拿不到数据表（护身/复活/麻痹/火焰全线哑火）",
     ROOT / "mirsolo/combat/resolver.py",
     "        data = self.ctx.game_data\n        if data is not None and amount > 0:",
     "        data = None\n        if data is not None and amount > 0:",
     "tests.test_rings"),
    ("护身扣了蓝却不减伤（两边都扣，等于白扣蓝）",
     ROOT / "mirsolo/items/rings.py",
     "    mp_cost = min(int(vitals.mp), covered)\n    return mp_cost, amount - mp_cost",
     "    mp_cost = min(int(vitals.mp), covered)\n    return mp_cost, amount",
     "tests.test_rings"),
    ("复活判定写成 amount > hp（伤害正好等于剩余血量时漏救）",
     ROOT / "mirsolo/combat/resolver.py",
     "        if data is not None and amount > 0 and amount >= int(vitals.hp):",
     "        if data is not None and amount > 0 and amount > int(vitals.hp):",
     "tests.test_rings"),
    ("复活戒指不扣额度（无限复活）",
     ROOT / "mirsolo/items/rings.py",
     "    state.charges[item_id] = left - 1",
     "    pass",
     "tests.test_rings"),
    ("AttackSystem 忽略麻痹（被麻痹还能出手）",
     ROOT / "mirsolo/actor/systems.py",
     "            if _is_paralyzed(attacker, tick):",
     "            if False:",
     "tests.test_rings"),
    ("MovementSystem 忽略麻痹（被麻痹还能走路）",
     ROOT / "mirsolo/actor/systems.py",
     "        if _is_paralyzed(player, tick):",
     "        if False:",
     "tests.test_rings"),
    ("麻痹只掷点不挂定身时长（until_tick 等于当前刻）",
     ROOT / "mirsolo/items/rings.py",
     "    buff.until_tick = int(tick) + max(1, int(found[2].get(\"ticks\", 1)))",
     "    buff.until_tick = int(tick)",
     "tests.test_rings"),
    ("隐身戒指把 recover_ticks 当成 0（出手后立刻又隐回去）",
     ROOT / "mirsolo/items/rings.py",
     "    _state(attacker).cloak_at = int(tick) + max(0, int(found[2].get(\"recover_ticks\", 0)))",
     "    _state(attacker).cloak_at = int(tick)",
     "tests.test_rings"),
    ("传送戒指不查冷却（CD 形同虚设）",
     ROOT / "mirsolo/items/rings.py",
     "    if cooldown and int(tick) - int(state.last_travel_tick) < cooldown:",
     "    if False:",
     "tests.test_rings"),
    ("特戒挪用了主伤害流的随机数（换戒指回放错位）",
     ROOT / "mirsolo/combat/resolver.py",
     "        rng = Rng(self.ctx.seed, _stream(\"physical\", attacker, defender, self.ctx.tick))\n"
     "        outcome = formulas.resolve_physical(",
     "        rng = Rng(self.ctx.seed, _stream(\"physical\", attacker, defender, self.ctx.tick))\n"
     "        if self.ctx.game_data is not None and rings.ring_effect(self.ctx.game_data, attacker):\n"
     "            rng.rand_int(1, 10000)\n"
     "        outcome = formulas.resolve_physical(",
     "tests.test_rings"),
    ("角色面板不显示戒指剩余次数（复活用完玩家看不见）",
     ROOT / "mirsolo/presentation/panels.py",
     "            if left_charges is not None:\n                line += f\"　剩余 {left_charges} 次\"",
     "            if False:\n                line += f\"　剩余 {left_charges} 次\"",
     "tests.test_rings"),

    # ── P2-S5 拍卖行 ───────────────────────────────
    ("挂单不扣物品（挂着还能穿、还能卖）",
     ROOT / "mirsolo/economy/auction.py",
     "    bag.remove(item_id, count)\n    book = _book(entity)",
     "    book = _book(entity)",
     "tests.test_auction"),
    ("要价上限失效（挂 100 万金也照成交）",
     ROOT / "mirsolo/economy/auction.py",
     "    if ask < floor or ask >= cap:",
     "    if ask < floor:",
     "tests.test_auction"),
    ("成交概率不衰减（高价与底价一样好卖）",
     ROOT / "mirsolo/economy/auction.py",
     "    return max(0, demand * (cap - ask) // span)",
     "    return max(0, demand)",
     "tests.test_auction"),
    ("撤单不退物品（挂单凭空消失）",
     ROOT / "mirsolo/economy/auction.py",
     "        book.listings.pop(index)\n        bag.add(item_id, int(listing.get(\"count\", 1)))",
     "        book.listings.pop(index)",
     "tests.test_auction"),
    ("撮合随机流只按轮次命名（多挂一张单会挪动别的单）",
     ROOT / "mirsolo/economy/auction.py",
     "        rng = Rng(seed, f\"auction:{seq}@{round_index}\")",
     "        rng = Rng(seed, f\"auction:{round_index}\")",
     "tests.test_auction"),

    # ── P2-S6 佣兵 ─────────────────────────────────
    ("雇佣不扣钱（白嫖战力）",
     ROOT / "mirsolo/actor/mercenary.py",
     "    wallet.gold -= price",
     "    price = 0",
     "tests.test_mercenary"),
    ("等级门槛失效（1 级就能雇 50 级侍卫）",
     ROOT / "mirsolo/actor/mercenary.py",
     "    if int(prog.level) < level_req:",
     "    if False:",
     "tests.test_mercenary"),
    ("全局随从上限失效（能带一个班）",
     ROOT / "mirsolo/actor/mercenary.py",
     "    if len(squad(world, owner_id)) >= SQUAD_LIMIT:",
     "    if False:",
     "tests.test_mercenary"),
    ("佣兵不挂归属（击杀奖励与图鉴归随从）",
     ROOT / "mirsolo/actor/factory.py",
     "        entity.add(SummonOwner(owner_id=owner_id))",
     "        pass  # injected: 不挂归属",
     "tests.test_mercenary"),
    ("佣兵到期判定永不到期（花钱买永久随从）",
     ROOT / "mirsolo/components.py",
     "    def expired(self, tick: int) -> bool:\n        return self.until_tick <= tick",
     "    def expired(self, tick: int) -> bool:\n        return False",
     "tests.test_mercenary"),

    # ── P2-S6 图鉴 ─────────────────────────────────
    ("图鉴每次击杀都发奖励（变成经验副本）",
     ROOT / "mirsolo/actor/systems.py",
     "            if not codex.record_kill(book, species):\n                continue",
     "            codex.record_kill(book, species)",
     "tests.test_codex"),
    ("图鉴不做归属解析（雇兵开图一格不亮）",
     ROOT / "mirsolo/actor/systems.py",
     "        killer = resolve_owner(world, killer)\n        self._pending.append({",
     "        self._pending.append({",
     "tests.test_codex"),
    ("图鉴行不排序（每次打开顺序都不一样）",
     ROOT / "mirsolo/actor/codex.py",
     "    out.sort(key=lambda r: (r[\"level\"], r[\"species\"]))",
     "    pass  # injected: 不排序",
     "tests.test_codex"),
    ("页脚「关闭」提示左对齐（整行甩出面板外）",
     ROOT / "mirsolo/presentation/panels.py",
     '(0.6, 0.62, 0.66, 1.0), anchor_x="right"))',
     '(0.6, 0.62, 0.66, 1.0))',
     "tests.test_panels"),
    # ── P3-S1 磨损与修理 ───────────────────────────
    ("命中不记磨损（打一辈子也不坏）",
     ROOT / "mirsolo/combat/resolver.py",
     "            self._wear_strike(attacker, defender)",
     "            pass  # injected: 不记磨损",
     "tests.test_wear"),
    ("磨穿的装备仍在给属性加成（recompute 不摘）",
     ROOT / "mirsolo/items/equipment.py",
     "        if wear.is_broken(data, wear_book, item_id):\n            continue",
     "        if False:\n            continue",
     "tests.test_wear"),
    ("修理不扣钱（白嫖满耐久）",
     ROOT / "mirsolo/economy/shop.py",
     "    wallet.gold -= cost\n    wear.reset(wear_book, item_id)",
     "    wear.reset(wear_book, item_id)",
     "tests.test_wear"),
    ("买入不重置磨损（花 120 金买回一把废剑）",
     ROOT / "mirsolo/economy/shop.py",
     '    wear.reset(entity.get(Wear), item_id)',
     '    pass  # injected: 新买的继承旧磨损',
     "tests.test_wear"),
    ("挨打连武器一起磨（出手/受击两本账混成一本）",
     ROOT / "mirsolo/items/wear.py",
     'ARMOR_ONLY_EXCLUDE = ("weapon",)',
     "ARMOR_ONLY_EXCLUDE = ()",
     "tests.test_wear"),

    # ── P3-S2 死亡惩罚 ─────────────────────────────
    ("死亡不掉经验（惩罚只剩摆设）",
     ROOT / "mirsolo/actor/penalty.py",
     "        result[\"exp_lost\"] = loss\n",
     "        result[\"exp_lost\"] = 0\n",
     "tests.test_penalty"),
    ("掉经验可以掉级（与「绝不掉级」的承诺冲突）",
     ROOT / "mirsolo/actor/penalty.py",
     "    return min(exp_loss(data, prog), max(0, int(prog.exp)))",
     "    return exp_loss(data, prog)",
     "tests.test_penalty"),
    ("原地复活白嫖（全额惩罚永远触发不到）",
     ROOT / "mirsolo/actor/penalty.py",
     "        wallet.gold = int(wallet.gold) - cost\n        result[\"paid\"] = cost",
     "        result[\"paid\"] = cost",
     "tests.test_penalty"),
    ("原地复活连经验一起掉（花钱买不到减损）",
     ROOT / "mirsolo/actor/penalty.py",
     "    if in_place:\n        cost = revive_cost(prog)",
     "    if False:\n        cost = revive_cost(prog)",
     "tests.test_penalty"),
    ("死亡不磨装备（唯一不可回避的那件也没了）",
     ROOT / "mirsolo/actor/penalty.py",
     "            if wear.add_wear(data, book, item_id, DEATH_WEAR_POINTS):",
     "            if False:",
     "tests.test_penalty"),
    ("掉了身上穿的装备（单机丢装备惩罚过重）",
     ROOT / "mirsolo/actor/penalty.py",
     "        bag = player.get(Bag)\n        if bag is not None and rng.rand_int(1, 10000) <= BAG_DROP_BP:",
     "        bag = player.get(Bag)\n        if bag is not None and True:",
     "tests.test_penalty"),
    ("倒地后不自动回城（headless 永远躺着）",
     ROOT / "mirsolo/actor/penalty.py",
     "DOWN_TICKS = 30",
     "DOWN_TICKS = 10 ** 9",
     "tests.test_penalty"),
    ("怪物继续盯着倒地的玩家出刀",
     ROOT / "mirsolo/actor/systems.py",
     "            if tvitals is not None and not tvitals.alive:\n                state.target_id = None\n                continue\n",
     "",
     "tests.test_penalty"),
    ("死亡账本不记账（读档就能撤销惩罚）",
     ROOT / "mirsolo/actor/penalty.py",
     "        ledger.count += 1\n",
     "        pass\n",
     "tests.test_penalty"),
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
