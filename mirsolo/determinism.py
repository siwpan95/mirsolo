"""确定性随机数 —— 纯整数 splitmix64 流。

设计约束（与 3dworld 的"零浮点不定性"一致）：

1. **无全局状态**：每一次取值由 ``(seed, stream, index)`` 三元组决定，
   与调用顺序无关。同一场战斗无论先算谁，伤害序列都一样。
2. **纯整数运算**：不依赖 ``random`` 模块、不产出浮点中间值，
   跨平台/跨 Python 版本逐位一致（存档回放、防 save-scum 的地基）。
3. **流隔离**：用字符串流名（``"hit:player->mob_1"``）把不同用途的随机
   分开，避免"多加一次 roll 就把整条战斗序列改掉"的骨牌效应。

等价于 Java/JS 生态里常用的 splitmix64；取整用拒绝采样，无模偏差。
"""

from __future__ import annotations

MASK64 = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C15

#: FNV-1a 64 位偏移基与质数（用于把流名变成整数种子）
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3


def splitmix64(x: int) -> int:
    """splitmix64 单步混合函数：输入任意 64 位整数，输出雪崩后的 64 位整数。"""
    x = (x + GOLDEN) & MASK64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return (z ^ (z >> 31)) & MASK64


def fnv1a64(text: str) -> int:
    """FNV-1a 64 位字符串哈希：把流名稳定地折成整数（与 locale 无关）。"""
    h = _FNV_OFFSET
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * _FNV_PRIME) & MASK64
    return h


class Rng:
    """确定性随机流：``(seed, stream)`` 决定整条序列。

    用法::

        rng = Rng(seed=20261019, stream="drop:mob_zhanshi:3")
        lo, hi = rng.rand_int(1, 100), rng.rand_int(1, 100)

    同一个 ``Rng`` 实例按调用次数递进；需要"位置无关"的取值时，
    直接每次新建实例并把序号放进 stream（推荐在战斗/掉落里这样用）。
    """

    __slots__ = ("seed", "stream", "_state", "_index")

    def __init__(self, seed: int, stream: str = "") -> None:
        self.seed = int(seed) & MASK64
        self.stream = stream
        self._state = (self.seed ^ fnv1a64(stream)) & MASK64
        self._index = 0

    def next_u64(self) -> int:
        """取下个 64 位无符号整数。"""
        self._index += 1
        self._state = splitmix64(self._state)
        return self._state

    def rand_int(self, lo: int, hi: int) -> int:
        """闭区间 ``[lo, hi]`` 上的均匀整数（拒绝采样，无模偏差）。"""
        if hi < lo:
            raise ValueError(f"rand_int 区间非法: [{lo}, {hi}]")
        span = hi - lo + 1
        if span == 1:
            return lo
        limit = (1 << 64) - ((1 << 64) % span)
        while True:
            value = self.next_u64()
            if value < limit:
                return lo + value % span

    def chance(self, numerator: int, denominator: int = 10000) -> bool:
        """万分比判定：``chance(35)`` 即 0.35%。整数实现，不含浮点。"""
        if denominator <= 0:
            raise ValueError("denominator 必须为正")
        return self.rand_int(1, denominator) <= numerator

    def getstate(self) -> list:
        """序列化状态（存档用）。"""
        return [self.seed, self.stream, self._state, self._index]

    @classmethod
    def fromstate(cls, state: list) -> "Rng":
        """从 :meth:`getstate` 的产物恢复。"""
        seed, stream, raw_state, index = state
        rng = cls(seed, stream)
        rng._state = int(raw_state) & MASK64
        rng._index = int(index)
        return rng


def roll(seed: int, stream: str, index: int, lo: int, hi: int) -> int:
    """位置无关的定值 roll：结果只由 ``(seed, stream, index)`` 决定。

    战斗与掉落统一走这个入口——把"第几次"显式写进参数，
    就不再依赖调用顺序，回放与自检可以对任意单点复算。
    """
    rng = Rng(seed ^ splitmix64(index + 1), stream)
    return rng.rand_int(lo, hi)
