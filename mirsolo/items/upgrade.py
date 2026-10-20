"""武器升级（P3-S5）—— 用黑铁矿石把武器一级一级顶上去。

三条与前面几批一脉相承的纪律：

1. **升级等级按 ``item_id`` 记账**（与 :mod:`mirsolo.items.wear` 同一条）。
   背包是堆叠的、没有实物实例 id，所以"这把斩马刀 +3"记成
   "斩马刀这一类里最好的那把 +3"：卖掉再买一把同款，账本照旧——
   想洗掉只有**把武器脱手**这一条路。
2. **尝试次数只增不减**：随机流名是 ``upgrade:{item_id}:{attempt}``，
   ``attempt`` 是累计尝试次数。同一存档点重来拿到的是**同一个**随机数
   （读档刷不了），正常连点则每次不同。与 ``drops`` 的 ``kill_index``
   是同一条防线，也是 P3-S6 存档必须带上的东西。
3. **成功率是万分比**（``bp``）、**金币是千分比**（``gold_permille``）。
   两套尺度在 P2 混过一次、代价是 10 倍误差，所以名字继续带后缀。

单机化取舍：升级只作用于**当前穿着的那把武器**（不做背包选择列表）——
"你手上这把刀"是唯一没有歧义的对象，选列表只会多出一层"我到底升了哪把"。
失败惩罚是**材料全损 + 降一级**，不是原版的"武器直接碎掉"：
单机丢一把 +7 是毁灭性打击，而且会逼出"先备份存档再升级"的读档流。
"""

from __future__ import annotations

from allin_engine import Entity

from mirsolo.components import Bag, Loadout, Upgrade, Wallet
from mirsolo.data.loader import GameData
from mirsolo.determinism import Rng, splitmix64
from mirsolo.items import mining

#: 升级档位表里"每一级"的字段（loader 已校验齐全，这里只是取用顺序）
_LEVEL_KEYS = ("from", "to", "min_purity", "ore", "gold_permille")


class UpgradeError(ValueError):
    """升级被拒。``code`` 是稳定的机器可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _spec(data: GameData) -> dict:
    return data.upgrades.get("upgrade") or {}


def max_level(data: GameData) -> int:
    """升级上限（数据表口径：7，即 +7）。"""
    return int(_spec(data).get("max_level", 0))


def bonus_per_level(data: GameData) -> tuple[int, int]:
    """每升一级加多少 ``(下限, 上限)`` —— 数据口径是 (1, 2)。

    区间因此会**变宽**：伤害波动变大。它与幸运 9（把区间收窄）正好互补，
    一把 +7 且幸运 9 的武器是"下限极高且刀刀上限"。
    """
    bonus = _spec(data).get("bonus") or {}
    return (int(bonus.get("lo", 0)), int(bonus.get("hi", 0)))


def step_bp(data: GameData) -> int:
    """每升一级从成功率里扣掉的万分比。"""
    return int(_spec(data).get("step_bp", 0))


def min_success_bp(data: GameData) -> int:
    """成功率地板：永远留一线，否则高等级配低纯度就是纯浪费。"""
    return int(_spec(data).get("min_success_bp", 0))


def purity_bp_of(data: GameData, purity: int) -> int:
    """纯度 → 成功率基准（万分比）。不认识的纯度返回 0（= 不能用来升级）。"""
    table = _spec(data).get("purity_bp") or {}
    return int(table.get(str(int(purity)), 0))


def levels(data: GameData) -> list[dict]:
    """升级档位表（按数据表的顺序，索引即"从这一级往上"）。"""
    return list(_spec(data).get("levels") or [])


def row_for(data: GameData, level: int) -> dict | None:
    """当前等级 ``level`` 升到下一级的档位；已满级或越界返回 ``None``。"""
    for row in levels(data):
        if int(row.get("from", -1)) == int(level):
            return row
    return None


# ── 账本 ────────────────────────────────────────────────

def level_of(book, item_id: str) -> int:
    """这件武器现在 +几（没有账本 = +0）。"""
    if book is None:
        return 0
    return int(book.levels.get(item_id, 0))


def attempts_of(book, item_id: str) -> int:
    """这件武器累计尝试过多少次（**只增不减**）。"""
    if book is None:
        return 0
    return int(book.attempts.get(item_id, 0))


def _bump_attempts(book, item_id: str) -> int:
    before = attempts_of(book, item_id)
    if book is not None:
        book.attempts[item_id] = before + 1
    return before


# ── 资格 ────────────────────────────────────────────────

def main_stat(data: GameData, item_id: str) -> str | None:
    """这件武器吃哪一条属性：``"atk"`` / ``"mc"`` / ``"sc"``，都不是返回 ``None``。

    数据上三者互斥（由 ``forge-and-blessing`` 门禁钉住），
    所以"该加在哪一条"不需要玩家或调用方操心。
    """
    spec = data.items["items"].get(item_id) or {}
    for key in ("atk", "mc", "sc"):
        if spec.get(key):
            return key
    return None


def is_upgradable(data: GameData, item_id: str) -> bool:
    """这件东西能不能升级：武器 + **不是工具** + 有主属性。

    鹤嘴锄有 ``tool="pick"``、占 weapon 槽，但它不是拿来打的——
    给它加攻击力等于让"挖矿要腾出武器槽"这个代价凭空消失。
    """
    spec = data.items["items"].get(item_id) or {}
    if spec.get("kind", "equip") != "equip":
        return False
    if spec.get("slot") != "weapon":
        return False
    if spec.get("tool"):
        return False
    return main_stat(data, item_id) is not None


def wielded_weapon(entity: Entity) -> str | None:
    """当前穿着的武器（升级/祝福的唯一作用对象）。"""
    loadout = entity.get(Loadout)
    if loadout is None:
        return None
    return loadout.slots.get("weapon")


# ── 代价与成功率 ─────────────────────────────────────────

def purity_gate(data: GameData, level: int) -> int:
    """升这一级**最低**要什么纯度的黑铁矿石（达不到就不能升）。"""
    row = row_for(data, level)
    return int(row.get("min_purity", 0)) if row else 0


def ore_need(data: GameData, level: int) -> int:
    """升这一级要几块矿石（数据口径：恒为 1）。"""
    row = row_for(data, level)
    return int(row.get("ore", 0)) if row else 0


def gold_cost(data: GameData, item_id: str, level: int) -> int:
    """升这一级要多少金币 = 买入价 × 该档千分比（**服务费，成败都收**）。"""
    row = row_for(data, level)
    if row is None:
        return 0
    price = max(1, int((data.items["items"].get(item_id) or {}).get("price", 1)))
    return max(1, price * int(row.get("gold_permille", 0)) // 1000)


def success_bp(data: GameData, level: int, purity: int) -> int:
    """这一次的成功率（万分比）：``纯度基准 − 当前等级 × step_bp``，夹到地板。

    注意减的是**当前等级**而不是目标等级：+0 那一级最便宜也最容易，
    越往上越难——"顶到 +7"要的就是这种坡度。
    """
    base = purity_bp_of(data, purity)
    if base <= 0:
        return 0
    return max(min_success_bp(data), base - int(level) * step_bp(data))


def pick_ore(data: GameData, bag: Bag | None, gate: int) -> str | None:
    """从背包里挑一块**够门槛的最高纯度**矿石，返回 item_id。

    为什么不挑"够用的里面最低的"（药水那条纪律）：矿石既不能合成、
    也不能按纯度分开买，"省下高纯度的"根本没有出口；而纯度直接决定成功率，
    用最高的就是唯一合理的策略——让玩家手动挑只是徒增点击。
    """
    best: tuple[int, str] | None = None
    if bag is None:
        return None
    for item_id, count in sorted(bag.items.items()):
        if int(count) <= 0 or not mining.is_ore(data, item_id):
            continue
        purity = int(mining.purity_of(data, item_id))
        if purity < int(gate):
            continue
        if best is None or purity > best[0]:
            best = (purity, item_id)
    return best[1] if best else None


def plan(data: GameData, entity: Entity) -> dict:
    """"再升一级"这件事的全貌（面板数据源 + :func:`upgrade` 的前置校验共用）。

    ``ok`` 为假时 ``code`` 给原因（``no_weapon`` / ``not_upgradable`` /
    ``max_level`` / ``purity_too_low`` / ``no_gold``）；为真时
    ``ore`` / ``gold`` / ``bp`` / ``purity`` 都是这一次真正会用上的数。
    """
    item_id = wielded_weapon(entity)
    if not item_id:
        return {"ok": False, "code": "no_weapon", "item": "", "level": 0}
    if not is_upgradable(data, item_id):
        return {"ok": False, "code": "not_upgradable", "item": item_id, "level": 0}
    book = entity.get(Upgrade)
    level = level_of(book, item_id)
    if level >= max_level(data):
        return {"ok": False, "code": "max_level", "item": item_id, "level": level}
    gate = purity_gate(data, level)
    ore = pick_ore(data, entity.get(Bag), gate)
    if ore is None:
        return {"ok": False, "code": "purity_too_low", "item": item_id,
                "level": level, "gate": gate}
    cost = gold_cost(data, item_id, level)
    wallet = entity.get(Wallet)
    gold = int(wallet.gold) if wallet is not None else 0
    purity = int(mining.purity_of(data, ore))
    return {
        "ok": gold >= cost,
        "code": "" if gold >= cost else "no_gold",
        "item": item_id,
        "level": level,
        "next": level + 1,
        "ore": ore,
        "ore_count": ore_need(data, level),
        "purity": purity,
        "gate": gate,
        "gold": cost,
        "bp": success_bp(data, level, purity),
    }


def upgrade(entity: Entity, data: GameData, seed: int) -> dict:
    """给当前穿着的武器升一级（成功 +1 / 失败 −1，+0 时失败不降）。

    顺序是**先扣材料、再掷点**：不然"失败不消耗"会被算成空子——
    每次失败都退回矿石的话，升级就退化成"一直点到成功为止"。
    金币是服务费，成败都收（它买的是"这一次尝试"，不是"这一次成功"）。
    """
    info = plan(data, entity)
    if not info["ok"]:
        raise UpgradeError(info["code"], _message(data, info))
    item_id = str(info["item"])
    book = entity.get(Upgrade)
    bag = entity.get(Bag)
    wallet = entity.get(Wallet)
    level = int(info["level"])
    ore = str(info["ore"])

    bag.remove(ore, int(info["ore_count"]))
    wallet.gold -= int(info["gold"])

    attempt = _bump_attempts(book, item_id)
    stream = f"upgrade:{item_id}:{attempt}"
    rng = Rng(seed ^ splitmix64(1), stream)
    rolled = rng.rand_int(1, 10000)
    won = rolled <= int(info["bp"])
    after = level + 1 if won else max(0, level - 1)
    if book is not None:
        book.levels[item_id] = after
    # 改完账本必须立刻重算属性：recompute 是唯一的属性出口，
    # 不重算的话 "+3" 要等到下一次战斗磨损触发 recompute 才生效，
    # 玩家看到的就是"点完了但攻击力没动"。
    # 延迟导入：equipment 反过来 import 本模块（recompute 要读升级账本）。
    from mirsolo.items.equipment import recompute

    recompute(entity, data)
    return {
        "ok": won,
        "item": item_id,
        "level": after,
        "from": level,
        "ore": ore,
        "purity": int(info["purity"]),
        "paid": int(info["gold"]),
        "gold": int(wallet.gold),
        "bp": int(info["bp"]),
        "roll": rolled,
    }


def _message(data: GameData, info: dict) -> str:
    name = (data.items["items"].get(info.get("item", "")) or {}).get(
        "name", info.get("item", ""))
    return {
        "no_weapon": "手上没有武器",
        "not_upgradable": f"{name} 不能升级",
        "max_level": f"{name} 已经满级",
        "purity_too_low": f"需要纯度不低于 {info.get('gate', 0)} 的黑铁矿石",
        "no_gold": f"金币不够（需要 {info.get('gold', 0)} 金）",
    }.get(str(info.get("code")), "现在升不了级")


def bonus_of(data: GameData, book, item_id: str) -> tuple[int, int]:
    """这件武器的升级加成 ``(下限, 上限)``；没升级过返回 ``(0, 0)``。"""
    lo, hi = bonus_per_level(data)
    level = level_of(book, item_id)
    return (level * lo, level * hi)
