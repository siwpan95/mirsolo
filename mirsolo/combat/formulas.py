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
) -> AttackOutcome:
    """物理攻击结算（普攻 / 战士技能共用）。

    流程与 1.76 口径一致：**先判命中，再 roll 攻防**。

    - 命中：``准确 vs 敏捷`` → ``hit_chance_bp``（万分比）
    - 攻击：``rand(DC下限, DC上限)``，技能按 ``power_permille`` 缩放
    - 防御：``rand(AC下限, AC上限)`` 抵扣，最低 :data:`MIN_DAMAGE`

    参数：
        power_permille: 技能倍率（1000 = 100%）。刺杀/烈火等技能 >1000。
    """
    outcome = AttackOutcome()
    chance = hit_chance_bp(accuracy, agility)
    hit_roll = rng.rand_int(1, HIT_ROLL_DENOMINATOR)
    outcome.rolls["hit"] = hit_roll
    outcome.rolls["hit_chance"] = chance
    if hit_roll > chance:
        return outcome

    outcome.hit = True
    atk = roll_damage_range(dc, rng)
    outcome.rolls["atk"] = atk
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
) -> AttackOutcome:
    """魔法攻击结算：魔法力 roll × 技能倍率 − 魔御 roll。

    传奇里魔法攻击**不看准确/敏捷**（必中），但被 ``MAC`` 抵扣；
    物理防御 ``AC`` 对魔法无效。``ignore_mac=True`` 用于"无视魔御"类技能。
    """
    outcome = AttackOutcome(hit=True)
    mc_roll = roll_damage_range(mc, rng)
    outcome.rolls["mc"] = mc_roll
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
) -> AttackOutcome:
    """道术结算（灵魂火符 / 施毒）：道术 roll × 倍率 − 魔御 roll。

    与魔法同构但走 ``sc``。治疗类技能不走这里——治疗不 roll 抵扣，
    直接 ``min(上限, roll)`` 见 :func:`heal_amount`。
    """
    outcome = AttackOutcome(hit=True)
    sc_roll = roll_damage_range(sc, rng)
    outcome.rolls["sc"] = sc_roll
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
