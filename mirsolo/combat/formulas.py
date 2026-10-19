"""战斗数学 —— 纯函数、纯整数、可单点复算。

三条硬约束：

1. **纯函数**：不读全局、不碰 ECS、不写文件；输入属性 + 随机流 → 输出结算。
   因此单测可以逐公式钉值，回放可以对任意一帧单独复算。
2. **纯整数**：所有中间量都是 int（万分比代替小数），与 ``determinism`` 同源，
   跨平台逐位一致。
3. **每次 roll 都记账**：结算结果带 ``rolls``，道歉要道得清楚——
   "这刀为什么只打 3 点"能还原到具体几个随机数。

当前公式基于 1.76 公开口径的**近似模型**（原版服务端公式未公开，
Crystal 等开源实现亦各有权重差异）。凡是近似处都标 ``CALIBRATE``，
P1 用实测战斗日志回归校准，见 ``docs/数据口径.md``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mirsolo.determinism import Rng

# ── 命中率校准（**万分比 BP**，0..10000） ────────────────────
#
# ⚠️ 本项目有两套"比率"尺度，混用会导致命中率差 10 倍（曾真的发生过）：
#   * **概率** → 万分比 BP，值域 0..10000，掷点也用 ``rand_int(1, 10000)``；
#   * **倍率** → 千分比 permille，1000 = 100%（``power_permille``）。
# 凡是"概率"一律带 ``_BP`` 后缀，凡"倍率"一律带 ``_PERMILLE`` 后缀。

HIT_BASE_BP = 7500            # 基准命中率 75.00%
HIT_PER_POINT_BP = 300        # 准确 - 敏捷 每差 1 点 ±3.00%
HIT_MIN_BP = 2000             # 命中下限 20.00%
HIT_MAX_BP = 9700             # 命中上限 97.00%
HIT_ROLL_DENOMINATOR = 10000  # 掷点分母：必须与上面的 BP 同尺度
MIN_DAMAGE = 1                 # 命中即至少 1 点（原版"最小伤害"）
MAX_DAMAGE_CAP = 4_000_000     # 单次伤害上限，防数值溢出（远高于原版上限）

# ── 幸运（P2-S3） ────────────────────────────────────────────
#
# 传奇口径：幸运来自**武器 + 项链**两个槽，合计封顶 :data:`LUCK_CAP` = 9；
# 合计到 9 就是玩家口中的"幸运 9，刀刀上限"。
#
# 实现不是"概率打上限"，而是把 roll 区间的**下限往上抬**：
#
#     narrowed_lo = hi − (hi − lo) × (LUCK_CAP − luck) // LUCK_CAP
#
# 于是两个端点都精确落在传奇口径上：
#
#   * ``luck = 0``  → ``(lo, hi)``，与没穿幸运装逐位一致（不会偷偷改基线）；
#   * ``luck = 9``  → ``(hi, hi)``，刀刀就是上限，不需要特判。
#
# 选"收窄区间"而不是"按概率打上限"的理由：**不额外掷点**。
# 于是同一把武器在幸运 0..8 之间切换，每次结算消耗的随机数个数完全相同
#（``tests/test_luck.py`` 用"同一条流的尾数相同"钉住），换装备不会让回放错位。
#
# 唯一的例外是 ``luck == LUCK_CAP``：此时区间塌成单点，和任何天生就是
# ``(x, x)`` 的区间一样走 ``Rng.rand_int`` 的免抽快路径，少一次抽数。
# 这不是特例而是沿用既有行为（`tests/test_luck.py` 里有一条专门对齐它）。
#
# ``CALIBRATE``：原版服务端公式未公开，线性收窄是近似；末端大概率是分段表
# 或加权抽样。本模型真正能赌咒的有两个部分——两个端点，由
# :mod:`tests.test_luck` 用字面量钉住。
#
# 单机化的两处取舍（都写进了 ``docs/数据口径.md``）：
#
#   1. 三职业都吃幸运：原版幸运只在武器上、等价于给物理攻击服务；这里对
#      ``dc`` / ``mc`` / ``sc`` 三条 roll 一视同仁，否则祝福油对法/道毫无意义。
#   2. 治疗/护盾不吃幸运：它们没有"上下限抽取"这件事，硬套会把治愈术变成
#      每次都刷满，属于把一个好想法推过了头。

LUCK_CAP = 9  # 幸运合计上限（到这个点数就是"刀刀上限"）


def clamp_luck(luck: int) -> int:
    """把幸运值钳进 ``[0, LUCK_CAP]``（数据表写超了也不会打穿上限）。"""
    return max(0, min(LUCK_CAP, int(luck)))


def lucky_range(value: tuple[int, int], luck: int = 0) -> tuple[int, int]:
    """按幸运收窄 ``(下限, 上限)``；``luck`` 为 0 时原样返回。

    >>> lucky_range((22, 36), 0)
    (22, 36)
    >>> lucky_range((22, 36), 6)
    (32, 36)
    >>> lucky_range((22, 36), 9)
    (36, 36)
    """
    lo, hi = value
    if lo > hi:
        lo, hi = hi, lo
    luck = clamp_luck(luck)
    if luck <= 0:
        return (lo, hi)
    return (hi - (hi - lo) * (LUCK_CAP - luck) // LUCK_CAP, hi)


def hit_chance_bp(accuracy: int, agility: int) -> int:
    """命中率（万分比 ``BP``，0..10000）。``CALIBRATE``：线性近似，原版为分段表。

    >>> hit_chance_bp(5, 5)
    7500
    """
    raw = HIT_BASE_BP + (accuracy - agility) * HIT_PER_POINT_BP
    return max(HIT_MIN_BP, min(HIT_MAX_BP, raw))


def roll_damage_range(value: tuple[int, int], rng: Rng) -> int:
    """从 ``(下限, 上限)`` 里 roll 一个整数（区间倒挂时自动交换）。"""
    lo, hi = value
    if lo > hi:
        lo, hi = hi, lo
    return rng.rand_int(lo, hi)


@dataclass
class AttackOutcome:
    """一次攻击的完整结算记录（可直接进回放/日志）。"""

    hit: bool = False
    damage: int = 0
    crit: bool = False
    rolls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "hit": self.hit,
            "damage": self.damage,
            "crit": self.crit,
            "rolls": dict(self.rolls),
        }


def resolve_physical(
    *,
    dc: tuple[int, int],
    accuracy: int,
    ac: tuple[int, int],
    agility: int,
    rng: Rng,
    power_permille: int = 1000,
    luck: int = 0,
) -> AttackOutcome:
    """物理攻击结算（普攻 / 战士技能共用）。

    流程与 1.76 口径一致：**先判命中，再 roll 攻防**。

    - 命中：``准确 vs 敏捷`` → ``hit_chance_bp``（万分比）
    - 攻击：``rand(幸运收窄后的 DC)``，技能按 ``power_permille`` 缩放
    - 防御：``rand(AC下限, AC上限)`` 抵扣，最低 :data:`MIN_DAMAGE`

    参数：
        power_permille: 技能倍率（1000 = 100%）。刺杀/烈火等技能 >1000。
        luck: 攻击者幸运（见 :data:`LUCK_CAP`）；0 = 不吃幸运。
    """
    luck = clamp_luck(luck)
    outcome = AttackOutcome()
    chance = hit_chance_bp(accuracy, agility)
    hit_roll = rng.rand_int(1, HIT_ROLL_DENOMINATOR)
    outcome.rolls["hit"] = hit_roll
    outcome.rolls["hit_chance"] = chance
    outcome.rolls["luck"] = luck
    if hit_roll > chance:
        return outcome

    outcome.hit = True
    atk_lo, atk_hi = lucky_range(dc, luck)
    atk = roll_damage_range((atk_lo, atk_hi), rng)
    outcome.rolls["atk"] = atk
    if luck:
        outcome.rolls["atk_floor"] = atk_lo
    if power_permille != 1000:
        atk = atk * power_permille // 1000
        outcome.rolls["atk_powered"] = atk

    defense = roll_damage_range(ac, rng)
    outcome.rolls["def"] = defense

    outcome.damage = max(MIN_DAMAGE, min(MAX_DAMAGE_CAP, atk - defense))
    return outcome


def resolve_magic(
    *,
    mc: tuple[int, int],
    ac: tuple[int, int],
    mac: tuple[int, int],
    rng: Rng,
    power_permille: int = 1000,
    ignore_mac: bool = False,
    luck: int = 0,
) -> AttackOutcome:
    """魔法攻击结算：魔法力 roll × 技能倍率 − 魔御 roll。

    传奇里魔法攻击**不看准确/敏捷**（必中），但被 ``MAC`` 抵扣；
    物理防御 ``AC`` 对魔法无效。``ignore_mac=True`` 用于"无视魔御"类技能。

    魔法同样吃幸运（``mc`` 区间被收窄）——见 :data:`LUCK_CAP` 的注释，
    这是单机化的刻意取舍，否则法师的幸运装与祝福油毫无意义。
    """
    luck = clamp_luck(luck)
    outcome = AttackOutcome(hit=True)
    outcome.rolls["luck"] = luck
    mc_lo, mc_hi = lucky_range(mc, luck)
    mc_roll = roll_damage_range((mc_lo, mc_hi), rng)
    outcome.rolls["mc"] = mc_roll
    if luck:
        outcome.rolls["mc_floor"] = mc_lo
    powered = mc_roll * power_permille // 1000
    outcome.rolls["mc_powered"] = powered
    if ignore_mac:
        outcome.rolls["mac"] = 0
        outcome.damage = max(MIN_DAMAGE, min(MAX_DAMAGE_CAP, powered))
        return outcome
    mac_roll = roll_damage_range(mac, rng)
    outcome.rolls["mac"] = mac_roll
    outcome.damage = max(MIN_DAMAGE, min(MAX_DAMAGE_CAP, powered - mac_roll))
    return outcome


def resolve_taoist(
    *,
    sc: tuple[int, int],
    mac: tuple[int, int],
    rng: Rng,
    power_permille: int = 1000,
    ignore_mac: bool = False,
    luck: int = 0,
) -> AttackOutcome:
    """道术结算（灵魂火符 / 施毒）：道术 roll × 倍率 − 魔御 roll。

    与魔法同构但走 ``sc``。治疗类技能不走这里——治疗不 roll 抵扣，
    直接 ``min(上限, roll)`` 见 :func:`heal_amount`；**也不吃幸运**。
    """
    luck = clamp_luck(luck)
    outcome = AttackOutcome(hit=True)
    outcome.rolls["luck"] = luck
    sc_lo, sc_hi = lucky_range(sc, luck)
    sc_roll = roll_damage_range((sc_lo, sc_hi), rng)
    outcome.rolls["sc"] = sc_roll
    if luck:
        outcome.rolls["sc_floor"] = sc_lo
    powered = sc_roll * power_permille // 1000
    outcome.rolls["sc_powered"] = powered
    if ignore_mac:
        outcome.rolls["mac"] = 0
        outcome.damage = max(MIN_DAMAGE, min(MAX_DAMAGE_CAP, powered))
        return outcome
    mac_roll = roll_damage_range(mac, rng)
    outcome.rolls["mac"] = mac_roll
    outcome.damage = max(MIN_DAMAGE, min(MAX_DAMAGE_CAP, powered - mac_roll))
    return outcome


def heal_amount(sc: tuple[int, int], rng: Rng, power_permille: int = 1000) -> int:
    """治疗量：``rand(SC) × 倍率``，不抵扣（治疗不吃魔御）。"""
    base = roll_damage_range(sc, rng)
    return max(0, base * power_permille // 1000)


def attack_interval_seconds(attack_speed: int) -> float:
    """按"攻速档位"折算出手间隔。

    传奇攻速极快（默认约 1 秒一刀），单机里用离散档位保证确定性：
    档位 1 → 1.40s，每升 1 档减 0.10s，最低 0.40s。
    """
    tier = max(1, min(11, attack_speed))
    return round(1.50 - 0.10 * tier, 2)
