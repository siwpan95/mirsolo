# -*- coding: utf-8 -*-
"""P4-S1 法道 dps 数值：负重 / mp 池 / 施毒 / 武器数值 + 召唤随 sc 缩放。

每条都是"改前红改后绿"：
* 负重与 mp 池：改前 L34 法师 53 负重 / 210 mp、道士 55 / 142——
  带不动 mp 药（Q3 洞穴经济负收益的根因）、蓝池只够 15 次雷电；
* 施毒：改前每跳 2 点 × 6 跳 = 12 总伤，纯负收益技能；
* 武器：改前 30/40 档法道 [6,11]/[9,15]，只有战士同档（[10,20]/[15,24]）的一半；
* 召唤：改前 L11 骷髅 dc[2,5] 在 33+ 图每刀被 ac 吃到 1 点，
  37 次召唤 × 12 mp = 444 mp 换 153 总伤——mp 黑洞。
"""
from __future__ import annotations

from mirsolo.bootstrap import build_session, populate
from mirsolo.components import (
    Attributes,
    Identity,
    Position,
    Progression,
    SummonOwner,
    Vitals,
    Wallet,
)
from mirsolo.data.loader import GameData
from mirsolo.progression.exp import apply_growth, grow_attributes
from mirsolo.skills import can_cast, learn

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_mage_taoist_weight_capacity():
    """法道负重成长 1→3：L34 法师 119、道士 121（改前 53 / 55）。

    改前的 53 负重减装备 16 只剩 37 余量，红蓝药各 10 瓶（40 重）都装不下——
    这正是 Q3 洞穴经济"法道负收益"的根因之一。
    """
    for cls, want in (("mage", 119), ("taoist", 121)):
        g = grow_attributes(DATA.classes["classes"][cls], 34)
        assert g["max_weight"] == want, (cls, g["max_weight"])


@case
def test_mage_taoist_mp_pool():
    """mp 池成长：法师 6→8、道士 4→6：L34 = 276 / 208（改前 210 / 142）。"""
    for cls, want in (("mage", 276), ("taoist", 208)):
        g = grow_attributes(DATA.classes["classes"][cls], 34)
        assert g["mp_max"] == want, (cls, g["mp_max"])


@case
def test_shidu_poison_damage():
    """施毒每跳 6（改前 2）：6 跳共 36 伤，仍低于普攻但值得挂。"""
    assert int(DATA.skills["skills"]["shidu"]["poison_damage"]) == 6


@case
def test_tier3040_mage_taoist_weapons():
    """30/40 档法道武器数值（改前 [6,11]/[9,15] = 战士同档的一半）。

    调后仍低于战士（30 档 [10,20]、40 档 [15,24]），保职业梯度。
    """
    items = DATA.items["items"]
    want = {"yinshe_jian": ("sc", [8, 14]), "wuji_jian": ("sc", [12, 19]),
            "guyu_fazhang": ("mc", [8, 14]), "longwen_fazhang": ("mc", [12, 19])}
    for iid, (field, val) in want.items():
        assert items[iid][field] == val, (iid, items[iid][field])


@case
def test_summon_scales_with_caster_sc():
    """召唤物 dc 随施法者 sc 缩放：dc += (sc_min//2, sc_max)。

    改前：骷髅恒为 kulou 条目 dc[2,5]，在 33+ 图每刀 1 点（实测 153 刀总伤 153）。
    """
    session = build_session(data=DATA, seed=3, map_id="biqi_field")
    try:
        player = populate(session, player_class="taoist")["player"]
        spec = DATA.classes["classes"]["taoist"]
        prog = player.get(Progression)
        prog.level = 34
        apply_growth(player, spec)
        book = DATA.skills["skills"]["zhaohuankulou"]["book"]
        from mirsolo.components import Bag
        player.get(Bag).add(book, 1)
        learn(player, DATA, "zhaohuankulou")
        session.step(1)
        ok, code = can_cast(player, DATA, "zhaohuankulou")
        assert ok, f"召唤放不出：{code}"
        session.cast_skill(player, "zhaohuankulou", player)
        session.step(1)
        summons = [e for e in session.world.each(SummonOwner)
                   if e.get(SummonOwner).owner_id == player.id]
        assert len(summons) == 1, [e.eid for e in summons]
        attr = summons[0].get(Attributes)
        player_sc = player.get(Attributes).sc
        base = DATA.monsters["monsters"]["kulou"]["dc"]
        expect = (base[0] + player_sc[0] // 2, base[1] + player_sc[1])
        assert attr.dc == expect, f"骷髅 dc {attr.dc}，期望 {expect}（基础 {base} × sc {player_sc}）"
    finally:
        session.shutdown()


def run() -> tuple[int, int]:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    return passed, failed


if __name__ == "__main__":
    p, f = run()
    print(f"test_p4s1: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
