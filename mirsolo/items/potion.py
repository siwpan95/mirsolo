"""药水与续航（P3-S3）—— 把"打怪 → 掉药 → 喝药"这条链补上。

在此之前玩家只有一条回血路：``RegenSystem`` 每 20 刻回 1 点。30 级战士
136 HP 从空回满要 272 秒，法师 186 MP 要 372 秒 —— 那不叫续航，叫干等。

时序（都在 ``PotionSystem``，priority 8 = 战斗之前）::

    每 tick
      ├─ 1) 消费手动意图  world 数据键 potion_intent = "hp" | "mp"
      └─ 2) 自动喝药      HP < 50% 喝红 / MP < 30% 喝蓝
    两条路共用**同一个公共冷却**（``POTION_COOLDOWN_TICKS`` 刻）

为什么公共冷却而不是"每种药各算各的"：否则背包里小中大齐全时，一个 tick
里连灌三瓶，冷却形同虚设。"喝药要一口一口来"是传奇的手感底线。

为什么自动喝药的阈值**写死**不开放调整：口径只有一份、改动有门禁盯着
（``verify.py`` 的 ``potion-and-sustain``）。开放可调就要多一份可变状态、
一份面板控件、一份存档字段，而"低于一半就喝"这件事二十年没变过。

选药策略（不浪费）：在能补得上缺口的候选里挑 **amount 最小的**；
一个都不够就挑 **amount 最大的**。背包里小中大齐全时，缺 20 点喝小的、
缺 100 点喝大的，不会拿超级药去补 3 点血。遍历按 ``(amount, item_id)``
升序，结果与背包字典的插入顺序无关。

已知差异（原版对照，记在 docs/数据口径.md）：

* 原版**没有**自动喝药（那是外挂功能）。本作单机没有外挂，自动喝药常开；
* 原版药水按"格"堆叠（一包若干瓶），本作只受**负重**约束 —— ``Bag`` 按
  种类计容量的语义一行都没动，理由是引入"每格几瓶"会牵动穿脱、存取、
  寄卖与面板滚动，回归面太大，收益只是更像原版；
* 原版药水没有等级门槛。本作给超级档加了 ``level_req``，但**只约束商店
  购买**（面板会写"需 20 级"），**不约束喝** —— 打 BOSS 掉一瓶超级药
  是意外之喜，不该因为等级被锁在背包里。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Bag, Body, PotionState, Vitals
from mirsolo.data.loader import POTION_EFFECTS, GameData

#: 数据表缺 ``consume.cooldown_ticks`` 时的兜底（正常不会发生，loader 已校验）。
#: 10 刻 = 1 秒：既不允许连灌，也不至于让人觉得"按了没反应"。
POTION_COOLDOWN_FALLBACK = 10

#: 自动喝药的阈值（千分比）。HP 低于 50% 喝红药，MP 低于 30% 喝蓝药。
#: 蓝药阈值比红药低，是因为 MP 只在"放不出技能"时才碍事，而 HP 见底就要命。
AUTO_HP_PERMILLE = 500
AUTO_MP_PERMILLE = 300

#: 商店点一次买几瓶。一瓶一瓶点太折磨，而"补满"又要先算负重余量——
#: 固定捆数是两者之间最省事又最可预测的选择。
BUY_BUNDLE = 10


class PotionError(ValueError):
    """喝药被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData, item_id: str) -> dict:
    spec = data.items["items"].get(item_id)
    if spec is None:
        raise PotionError("unknown_item", f"未知物品 {item_id!r}")
    return spec


def is_potion(data: GameData, item_id: str) -> bool:
    """这件东西是不是**能喝**的药水。

    ⚠️ 不能只看 ``kind == "consumable"``：祝福油（P3-S5）也是消耗品，
    但它不是喝下去的东西——只看 kind 的话，"药水负重"会把它算进去，
    商店补货的余量凭空少一块。真正的口径是"带一条合法的 ``consume`` 段"。
    """
    spec = data.items["items"].get(item_id) or {}
    if spec.get("kind") != "consumable":
        return False
    consume = spec.get("consume")
    return isinstance(consume, dict) and consume.get("effect") in POTION_EFFECTS


def consume_of(data: GameData, item_id: str) -> dict:
    """``consume`` 段（非药水返回空字典——调用方据此判定"喝不了"）。"""
    spec = data.items["items"].get(item_id) or {}
    if spec.get("kind") != "consumable":
        return {}
    consume = spec.get("consume")
    return consume if isinstance(consume, dict) else {}


def gains(data: GameData, item_id: str) -> tuple[int, int]:
    """这瓶药回多少 (HP, MP)。

    ``hp`` → (amount, 0)；``mp`` → (0, amount)；``both`` → (amount, amount)。
    """
    consume = consume_of(data, item_id)
    if not consume:
        return (0, 0)
    amount = max(0, int(consume.get("amount", 0)))
    effect = consume.get("effect")
    if effect == "hp":
        return (amount, 0)
    if effect == "mp":
        return (0, amount)
    if effect == "both":
        return (amount, amount)
    return (0, 0)


def cooldown_of(data: GameData, item_id: str) -> int:
    """喝这瓶药之后的公共冷却刻数（就是数据面声明的那个值）。

    刻意**不做** ``max(1, ...)`` 之类的兜底：真读到一个 0，说明数据被改坏了，
    该由 ``potion-and-sustain`` 门禁把它拦下来；运行时悄悄补个 1 把错误糊过去，
    门禁就永远绿、永远发现不了。loader 已在加载时校验它为正整数。
    """
    consume = consume_of(data, item_id)
    return int(consume.get("cooldown_ticks", POTION_COOLDOWN_FALLBACK))


def is_cooling(state: PotionState | None, tick: int) -> bool:
    """公共冷却是不是还没走完。没有状态组件 = 没喝过 = 不冷却。"""
    return state is not None and tick < int(state.ready_at)


def candidates(bag: Bag | None, data: GameData, want: str) -> list[tuple[int, str]]:
    """背包里能补 ``want``（hp / mp）的药水，按 ``(回复量, id)`` 升序。

    结果不含数量为 0 的条目，也不含补不了 ``want`` 的药（魔法药不会出现在
    想回血的候选里）；太阳水 ``both`` 两边都算。
    """
    if bag is None or want not in ("hp", "mp"):
        return []
    out: list[tuple[int, str]] = []
    for item_id, count in bag.items.items():
        if int(count) <= 0:
            continue
        hp, mp = gains(data, item_id)
        amount = hp if want == "hp" else mp
        if amount <= 0:
            continue
        out.append((amount, item_id))
    out.sort()
    return out


def pick(bag: Bag | None, data: GameData, want: str, need: int = 1) -> str | None:
    """挑一瓶最合适的药：够用的里面最小的，都不够就用最大的。

    ``need`` 是缺口点数（还差多少才满）。缺省 1 = "只要能补就行"，
    此时会选中回复量最小的那瓶。
    """
    rows = candidates(bag, data, want)
    if not rows:
        return None
    for amount, item_id in rows:
        if amount >= need:
            return item_id
    return rows[-1][1]


def auto_want(vitals: Vitals | None) -> str | None:
    """自动喝药该补什么：``"hp"`` / ``"mp"`` / ``None``（都不用补）。

    全整数比较（``hp * 1000 < max_hp * permille``），不引入浮点——
    同一 tick 的存档要能对得上账。``max_*`` 为 0 时那一路恒不成立，
    所以战士（``max_mp == 0``）永远不会自动灌蓝药。
    """
    if vitals is None or not vitals.alive:
        return None
    if vitals.max_hp > 0 and vitals.hp * 1000 < vitals.max_hp * AUTO_HP_PERMILLE:
        return "hp"
    if vitals.max_mp > 0 and vitals.mp * 1000 < vitals.max_mp * AUTO_MP_PERMILLE:
        return "mp"
    return None


def need_of(vitals: Vitals | None, want: str) -> int:
    """还差多少才满（自动喝药挑档用）。"""
    if vitals is None or want not in ("hp", "mp"):
        return 0
    if want == "hp":
        return max(0, int(vitals.max_hp) - int(vitals.hp))
    return max(0, int(vitals.max_mp) - int(vitals.mp))


def auto_pick(entity: Entity, data: GameData) -> str | None:
    """自动喝药选一瓶（不扣、不喝，只挑）。挑不到返回 ``None``。"""
    vitals = entity.get(Vitals)
    want = auto_want(vitals)
    if want is None:
        return None
    return pick(entity.get(Bag), data, want, need_of(vitals, want))


def drink(entity: Entity, data: GameData, item_id: str, *, tick: int = 0) -> dict:
    """喝掉**一瓶**药水并返回这一口的账。

    校验顺序（每一步都有独立拒绝码，绝不做"悄悄少喝一点"）：

    1. 是不是药水；
    2. 活着吗（倒地期间不能喝——能喝的话死亡惩罚就只剩掉东西了）；
    3. 公共冷却走完了吗；
    4. 背包里真的有吗。

    回血走 ``Vitals.heal``（与 ``RegenSystem`` 同源，不经过战斗结算器——
    那条"唯一改血入口"的纪律管的是**伤害**，回血另有两处既有实现）。
    """
    consume = consume_of(data, item_id)
    if not consume:
        raise PotionError("not_a_potion", f"{_spec(data, item_id).get('name', item_id)} 不是药水")
    vitals = entity.get(Vitals)
    bag = entity.get(Bag)
    if vitals is None or bag is None:
        raise PotionError("missing_components", "缺少 Vitals/Bag 组件")
    if not vitals.alive:
        raise PotionError("dead", "倒地期间喝不了药")
    state = entity.get(PotionState)
    if is_cooling(state, tick):
        raise PotionError("cooling", "药水冷却中")
    if int(bag.items.get(item_id, 0)) <= 0:
        raise PotionError("none_left", f"背包里没有 {_spec(data, item_id).get('name', item_id)}")

    hp, mp = gains(data, item_id)
    if hp <= 0 and mp <= 0:
        raise PotionError("nothing_to_do", f"{_spec(data, item_id).get('name', item_id)} 喝了没用")

    bag.remove(item_id, 1)
    healed = int(vitals.heal(hp))
    restored = 0
    if mp > 0:
        before = int(vitals.mp)
        vitals.mp = min(int(vitals.max_mp), before + mp)
        restored = int(vitals.mp) - before
    if state is not None:
        state.ready_at = tick + cooldown_of(data, item_id)
        state.last = item_id
    return {
        "item": item_id,
        "hp": healed,
        "mp": restored,
        "ready_at": int(state.ready_at) if state is not None else tick,
        "left": int(bag.items.get(item_id, 0)),
        "tick": tick,
    }


def stock_choice(data: GameData, want: str, cap: int) -> str | None:
    """商店"补满"挑哪一档：回复量不超过 ``cap`` 的最大档，连最小的都超就取最小的。

    只看**纯药**（``effect`` 恰好等于 ``want``）——太阳水不进补满建议，
    它是应急品，贵，玩家想买会自己点。
    """
    rows: list[tuple[int, str]] = []
    for item_id, spec in data.items["items"].items():
        if spec.get("kind") != "consumable":
            continue
        consume = spec.get("consume") or {}
        if consume.get("effect") != want:
            continue
        rows.append((max(0, int(consume.get("amount", 0))), item_id))
    if not rows:
        return None
    rows.sort()
    for amount, item_id in rows:
        if amount >= cap:
            return item_id
    return rows[0][1]


def carried_potion_weight(entity: Entity, data: GameData) -> int:
    """背包里药水的总重量（"补满"算余量时用）。"""
    bag = entity.get(Bag)
    if bag is None:
        return 0
    total = 0
    for item_id, count in bag.items.items():
        if is_potion(data, item_id):
            total += int(data.items["items"][item_id].get("weight", 0)) * int(count)
    return total


def free_weight(entity: Entity, data: GameData, carried: int) -> int:
    """还能再背多少重量（``Body`` 缺失时返回 0 = 一件也别加）。"""
    body = entity.get(Body)
    if body is None:
        return 0
    return max(0, int(body.max_weight) - int(carried))
