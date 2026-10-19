"""数据表：加载、结构校验、引用校验——以及**校验器自己能不能抓到坏数据**。

最后一组用例是重点：只在好数据上跑绿的校验器等于装饰品，
所以这里专门造四种坏表（缺文件 / 死引用 / 曲线断档 / 传送点越界），
断言校验器逐条报出来。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from mirsolo.data.loader import GameData

CASES = []
DATA = GameData.load()


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_shipped_data_is_clean():
    assert DATA.errors == [], DATA.errors
    assert DATA.ok


@case
def test_seven_tables_are_present():
    """结构表必须齐全（P2 加了 ``sieges``，这里是按名字点验不是按数量）。"""
    for name in ("exp", "classes", "skills", "monsters", "items", "drops", "maps", "sieges"):
        table = getattr(DATA, name)
        assert isinstance(table, dict) and table, f"{name}.json 为空"


@case
def test_p1_map_bands_cover_1_to_52():
    """地图等级带必须首尾相接覆盖 1-52（练级不能断档）。

    P2 加了 ``chiyue_valley``(50-51) / ``niumo_cave``(51-52)，上限从 50 抬到 52。
    """
    bands = sorted((s["level_range"][0], s["level_range"][1], mid)
                   for mid, s in DATA.maps["maps"].items())
    assert bands[0][0] == 1, bands[0]
    assert bands[-1][1] == 52, bands[-1]
    for prev, cur in zip(bands, bands[1:]):
        assert cur[0] <= prev[1] + 1, f"{prev[2]}→{cur[2]} 等级断档"


@case
def test_every_monster_has_drops_and_spawn_somewhere():
    """每只怪都要有掉落表、并且至少在某张图刷出来（否则是死数据）。"""
    spawned = {s["monster"] for spec in DATA.maps["maps"].values()
               for s in spec.get("spawns", [])}
    for mid in DATA.monsters["monsters"]:
        assert mid in DATA.drops["drops"], f"{mid} 没有掉落表"
        assert mid in spawned, f"{mid} 没有在任何地图刷新（写了等于没写）"


@case
def test_bosses_are_elite_and_slow_respawn():
    """教主/精英必须走 elite 档位，且补刷间隔与小怪分层（数据与行为同源）。"""
    monsters = DATA.monsters["monsters"]
    bosses = {mid for mid, s in monsters.items() if s.get("ai") == "elite"}
    assert {"woma_jiaozhu", "zumma_jiaozhu"} <= bosses, sorted(bosses)
    for map_id, spec in DATA.maps["maps"].items():
        for spawn in spec.get("spawns", []):
            mid = spawn["monster"]
            seconds = spawn["respawn_seconds"]
            if mid in ("woma_jiaozhu", "zumma_jiaozhu"):
                assert spawn["count"] == 1, f"{map_id} 的教主刷了 {spawn['count']} 只"
                assert seconds >= 300, (map_id, mid, seconds)
            elif monsters[mid].get("ai") == "elite":      # 尸王这类精英
                assert spawn["count"] == 1, f"{map_id} 的精英刷了 {spawn['count']} 只"
                assert seconds >= 180, (map_id, mid, seconds)
            else:
                assert seconds <= 120, (map_id, mid, seconds)


@case
def test_exp_covers_1_to_52():
    """经验表必须覆盖到地图等级带的顶（P2 延到 52）。

    地图带扩到 52 而经验表还停在 50，玩家在赤月/牛魔会"升不动级"——
    这个断言就是防止两头脱节。
    """
    assert DATA.max_level() == 52
    for level in range(1, 53):
        assert str(level) in DATA.exp_levels
    # 只有封顶级允许 0（loader 校验）；51 级必须还要经验才能到 52
    assert int(DATA.exp_levels["51"]) > 0
    assert int(DATA.exp_levels["52"]) == 0


@case
def test_missing_table_raises():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _copy_tables(base)
        (base / "drops.json").unlink()
        try:
            GameData.load(base)
        except FileNotFoundError as exc:
            assert "drops.json" in str(exc)
            return
        raise AssertionError("缺表未抛 FileNotFoundError")


def _copy_tables(target: Path) -> None:
    """把数据目录里**所有**表拷进 ``target``。

    以前这里是写死的七张表名单——加了 ``sieges.json`` 之后，破坏测试的临时
    目录就缺表，于是"构造一个坏表看校验报不报"的测试全部退化成
    "FileNotFoundError"。改成扫目录，加表不必再改测试。
    """
    for path in DATA.data_dir.glob("*.json"):
        shutil.copy(path, target / path.name)


def _mutated(name: str, mutate) -> Path:
    """复制一份数据目录并就地改坏一张表，返回临时目录（调用方负责清理）。"""
    tmp = Path(tempfile.mkdtemp())
    _copy_tables(tmp)
    payload = json.loads((tmp / f"{name}.json").read_text(encoding="utf-8"))
    mutate(payload)
    (tmp / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp


@case
def test_dangling_drop_item_is_caught():
    tmp = _mutated("drops", lambda p: p["drops"]["ji"]["groups"][0]["entries"][0].update(
        {"item": "nonexistent_sword"}))
    try:
        bad = GameData.load(tmp)
        assert not bad.ok
        assert any("nonexistent_sword" in e for e in bad.errors), bad.errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_exp_curve_gap_is_caught():
    tmp = _mutated("exp", lambda p: p["levels"].pop("5"))
    try:
        bad = GameData.load(tmp)
        assert any("不连续" in e for e in bad.errors), bad.errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_out_of_bounds_portal_is_caught():
    """调小地图尺寸后忘改坐标的经典事故——必须被结构校验拦住。"""
    tmp = _mutated("maps", lambda p: p["maps"]["biqi_field"]["portals"][0].update(
        {"at": [999, 6]}))
    try:
        bad = GameData.load(tmp)
        assert any("越界" in e for e in bad.errors), bad.errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_dangling_spawn_monster_is_caught():
    tmp = _mutated("maps", lambda p: p["maps"]["biqi_mine_1"]["spawns"].append(
        {"monster": "dragon_king", "count": 1, "respawn_seconds": 60}))
    try:
        bad = GameData.load(tmp)
        assert any("dragon_king" in e for e in bad.errors), bad.errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_skill_class_must_exist():
    tmp = _mutated("skills", lambda p: p["skills"]["gongsha"].update({"class": "ninja"}))
    try:
        bad = GameData.load(tmp)
        assert any("ninja" in e for e in bad.errors), bad.errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_endgame_maps_have_their_own_monsters():
    """后段图不能只是"祖玛换皮"——必须有自家怪种。

    P2 之前赤月/牛魔刷的是祖玛卫士 + 恶魔弓箭手，玩家会觉得
    "换张图但怪一模一样"。
    """
    expected = {
        "chiyue_valley": {"chiyue_mozhu", "xuejiangshi", "chiyue_emo"},
        "niumo_cave": {"niumo_zhanshi", "niumo_fashi", "niumo_wang"},
    }
    for map_id, own in expected.items():
        spawns = {s["monster"] for s in DATA.maps["maps"][map_id]["spawns"]}
        assert own <= spawns, (map_id, sorted(own - spawns))
        assert "zumma_weishi" not in spawns, f"{map_id} 还在复用祖玛卫士"


@case
def test_endgame_gear_is_wearable_within_top_level_band():
    """终局装备的等级门槛必须够得着，否则"打到 52 级也穿不上"。

    最贵的那几件门槛设在 48/50，正好是赤月/牛魔地图带的起始位。
    """
    top_band = max(s["level_range"][1] for s in DATA.maps["maps"].values())
    for iid in ("chiyue_mojian", "chiyue_mozhang", "chiyue_daojian",
                "chiyue_zhanjia", "niumo_zhanfu", "niumo_toukui"):
        req = DATA.items["items"][iid]["level_req"]
        assert req <= top_band, (iid, req, top_band)


@case
def test_monster_levels_fall_in_their_map_band():
    """怪的等级要大体落在它刷新的那张图的等级带里。

    两类都要抓：
    - **高出图的上限** → 低等级图里藏着高级怪，路过即秒；
    - **远低于图的下限** → 50 级的图刷一群 30 级怪，打了没收益。

    下界留 2 级窗口是因为相邻练级带本来就该交叠（石墓阵 40-44 里
    刷 38 级的沃玛卫士是刻意保留的过渡）；这条断言抓的是尸王那种
    整整差 11 级的**真错位**（15 级尸王曾刷在 26-33 的半兽古墓里）。
    """
    TRANSITION_WINDOW = 2
    for map_id, spec in DATA.maps["maps"].items():
        low, high = spec["level_range"]
        for spawn in spec.get("spawns", []):
            mid = spawn["monster"]
            level = DATA.monsters["monsters"][mid]["level"]
            assert level <= high, (map_id, mid, level, (low, high))
            assert level >= low - TRANSITION_WINDOW, (map_id, mid, level, (low, high))


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
    print(f"test_data: {p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
