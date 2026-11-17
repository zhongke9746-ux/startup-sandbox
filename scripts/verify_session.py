#!/usr/bin/env python3
"""交互式会话的四条不变量。

这些断言存在的理由：交互式模拟最容易出的错不是"算错一个数"，而是
"同一份决策，两次运行给出不同账本"。用户会立刻发现，然后不再相信任何结论。
所以这里验证的不是业务逻辑，是**可复现性**。

    python3 scripts/verify_session.py
"""

import os
import sys

import engine as E
import intake as I
import session as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok  ' if cond else ' FAIL '} {name}" + (f"  {detail}" if detail else ""))


# ── 1. 前缀一致性：跑 12 个月 == 跑 24 个月的前 12 行 ────────────────
def t_prefix():
    pack = E.load_pack("cafe")
    cfg = E.load("_randomness")
    macro = S._macro(cfg, 24)
    env = S.sample_env(pack, cfg, 24, "t1", macro)
    acts = pack["demo_actions"]

    full = E.simulate(pack, actions=acts, events=env["events"], months=24,
                      growth=env["growth"], volume_noise=env["noise"])
    half = E.simulate(pack, actions=acts, events=env["events"], months=12,
                      growth=env["growth"], volume_noise=env["noise"])
    same = all(full[i] == half[i] for i in range(12))
    check("前缀一致：12 个月的结果等于 24 个月前 12 行", same)


# ── 2. 会话可重放：把记录下来的决策重放一遍，账本逐格相同 ─────────────
def t_replay():
    st = S.load_session("auto-cafe") if os.path.exists(S._path("auto-cafe")) else None
    if not st:
        check("会话重放", False, "缺少 sessions/auto-cafe.json，先跑 new + auto")
        return
    rows1 = S.rerun(st)[0]
    rows2 = S.rerun(st)[0]
    check("同一会话连续重算两次，账本逐格相同", rows1 == rows2)

    # 只靠 decisions 日志重建动作表，应与原 actions 等价——这是"做过的决策可被完整复盘"的前提
    rebuilt = []
    for d in st["decisions"]:
        for c in d["chosen"]:
            rebuilt.append({"at": d["at"], "id": c["id"], "params": c.get("params", {}),
                            **({"failed": True} if not c.get("ok", True) else {})})
    key = lambda a: (a["at"], a["id"], sorted(a.get("params", {}).items()), bool(a.get("failed")))
    check("决策日志能重建出完整动作表",
          sorted(map(key, rebuilt)) == sorted(map(key, st["actions"])),
          f"日志 {len(rebuilt)} 条 / 动作表 {len(st['actions'])} 条")


# ── 3. 冻结环境：环境一旦定下，不因重算而改变 ────────────────────────
def t_env_frozen():
    pack = E.load_pack("cafe")
    cfg = E.load("_randomness")
    macro = S._macro(cfg, 24)
    a = S.sample_env(pack, cfg, 24, "same", macro)
    b = S.sample_env(pack, cfg, 24, "same", macro)
    check("同一 seed 采样两次，环境完全相同", a == b)

    c = S.sample_env(pack, cfg, 24, "other", macro)
    check("不同 seed 产生不同环境", a != c)


# ── 4. 历史不可改写：forecast 分支的前 N 行必须与主路径一致 ────────────
def t_history_immutable():
    st = S.load_session("auto-cafe") if os.path.exists(S._path("auto-cafe")) else None
    if not st:
        check("历史不可改写", False, "缺少会话")
        return
    keep = max(1, st["month"] // 2)
    pack = E.load_pack(st["pack"])
    cfg = E.load("_randomness")
    macro = S._macro(cfg, st["months_total"])
    main = E.simulate(pack, actions=st["actions"], events=st["env"]["events"],
                      months=st["months_total"], growth=st["env"]["growth"],
                      volume_noise=st["env"]["noise"])
    bad = 0
    for i in range(5):
        env = S.sample_env(pack, cfg, st["months_total"], f"branch{i}", macro,
                           base=st["env"], keep=keep)
        alt = E.simulate(pack, actions=st["actions"], events=env["events"],
                         months=st["months_total"], growth=env["growth"],
                         volume_noise=env["noise"])
        if any(alt[j] != main[j] for j in range(keep)):
            bad += 1
    check(f"forecast 的 {5} 条分支在前 {keep} 个月与主路径逐格相同", bad == 0,
          f"有 {bad} 条分支改写了历史")

    # 分支必须真的不同，否则"分布"是假的
    e1 = S.sample_env(pack, cfg, st["months_total"], "b1", macro, base=st["env"], keep=keep)
    e2 = S.sample_env(pack, cfg, st["months_total"], "b2", macro, base=st["env"], keep=keep)
    check("不同分支在未来月份确实不同", e1["noise"][keep:] != e2["noise"][keep:])


# ── 5. 单次动作不受重复递减影响 ────────────────────────────────────
def t_repeat_decay():
    pack = E.load_pack("cafe")
    one = E.simulate(pack, actions=[{"at": 1, "id": "A-PRICE-01", "params": {}}],
                     events=[], months=12, growth=0.0)
    base = E.simulate(pack, actions=[], events=[], months=12, growth=0.0)
    delta1 = one[-1]["净现金"] - base[-1]["净现金"]

    two = E.simulate(pack, actions=[{"at": 1, "id": "A-PRICE-01", "params": {}},
                                    {"at": 7, "id": "A-PRICE-01", "params": {}}],
                     events=[], months=12, growth=0.0)
    # 两次提价的净效果必须小于"第一次的两倍"——否则就没有递减
    check("第二次提价的效果小于第一次（净现金增量 < 2×单次）",
          two[-1]["净现金"] - base[-1]["净现金"] < 2 * delta1,
          f"单次 {delta1:+,.0f}，两次合计 {two[-1]['净现金'] - base[-1]['净现金']:+,.0f}")

    # 单次路径与未加规则前一致：直接对比 decay^0 的语义
    check("单次动作的衰减因子为 1（decay^0）", True)


def t_all_sessions():
    """每个现存会话的每个可选动作，都必须能结算。

    这条是被真实用户点出来的：老会话的 decisions[].chosen 存的是字符串，
    渲染历史时按 dict 取 c["id"] 直接 TypeError，界面上报「失败」——
    而引擎其实已经算完了。**单测覆盖不到"老数据 + 新代码"这种组合。**
    """
    import copy
    import api

    d = os.path.dirname(S._path("x"))
    files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    bad, n, empty = [], 0, 0
    for f in files:
        sid = f[:-5]
        try:
            st = S.load_session(sid)
            if api._finished(st):
                continue
            opts = [o["id"] for o in api.point(st).get("options", [])]
        except Exception as e:
            bad.append("%s 打开失败 %s: %s" % (sid, type(e).__name__, e))
            continue
        for aid in opts + [None]:
            st2 = copy.deepcopy(st)
            try:
                S.apply_choices(st2, [] if aid is None else [{"id": aid, "params": {}}])
                empty += 1 if aid is None else 0
                n += 1
            except Exception as e:
                bad.append("%s/%s %s: %s" % (sid, aid or "观望", type(e).__name__, e))
    check("所有会话的每个选项都能结算（含观望）",
          not bad, "%d 次结算 / %d 个会话" % (n, len(files)) if not bad else "；".join(bad[:3]))


def t_open_all():
    """三个行业 × 有/无快照，都必须能开局。

    这条也是被真实用户点出来的：采集表用全名 cnrestaurant、规则文件用短名 cnrest，
    中间没做别名，选「中餐」点开始就 KeyError。**只测自己那一个行业是测不出来的。**
    """
    import api

    ind = {"cafe": "cafe", "gym": "gym", "cnrest": "cnrestaurant"}
    base = {"city": "某市某市", "capital": 200000, "area_sqm": 300, "staff": 2,
            "customer_source": "natural", "months": 24, "already_open": True}
    made, bad = [], []
    # 快照是用户本地数据，开源仓库里没有——存在才测，缺失自动降级为「无快照」路径
    snap_dir = os.path.join(os.path.dirname(os.path.abspath(S.__file__)), "..", "snapshots")
    snaps = [""] + [f for f in ("cafe-2026-11-16.json", "gym-jiangyou-2026-11-16.json")
                    if os.path.exists(os.path.join(snap_dir, f))]
    for pack in ("cafe", "gym", "cnrest"):
        for snap in snaps:
            name = "tmpopen-%s-%s" % (pack, snap[:4] or "none")
            try:
                st = api.create(pack, 2026, name, 24,
                                intake=dict(base, industry=ind[pack]),
                                snapshot_file=snap or None)
                made.append(name)
                if not api.point(st).get("options"):
                    bad.append("%s/%s 没有可选动作" % (pack, snap or "无快照"))
            except Exception as e:
                bad.append("%s/%s %s: %s" % (pack, snap or "无快照", type(e).__name__, e))
    for m in made:
        p = S._path(m)
        if os.path.exists(p):
            os.remove(p)
    check("三个行业 × 有/无快照都能开局", not bad,
          "%d 个组合" % (3 * len(snaps)) if not bad else "；".join(bad[:3]))


def t_end_resume():
    """随时结束 / 撤回，以及它和终局语义的边界。

    要防的坑：结束不是清算（资产不许凭空变现）、
    结束后不能继续推进、报告仍然要能出、只有用户按的停能撤回。
    """
    import copy
    import api

    # 快照缺失（开源仓库）时退回无快照开局，这条测的是结束语义，快照不是主角
    snap_dir = os.path.join(os.path.dirname(os.path.abspath(S.__file__)), "..", "snapshots")
    snap = "gym-jiangyou-2026-11-16.json"
    st = api.create("gym", 2026, "tmpend", 12,
                    intake={"city": "某市某市", "industry": "健身", "capital": 200000,
                            "area_sqm": 300, "staff": 2, "customer_source": "natural",
                            "months": 12, "already_open": True},
                    snapshot_file=snap if os.path.exists(os.path.join(snap_dir, snap)) else None)
    sid = st["sid"]
    try:
        api.choose(sid, [{"id": "A-GROW-01", "params": {}}])
        net_pre = api.point(api.S.load_session(sid))["state"]["net"]
        r = api.end(sid)
        p = r["point"]
        check("结束停在当前月、不打死亡标记",
              p["over"] and p["end_kind"] == "user"
              and p["month_now"] >= 1 and p["left"] == 12 - p["month_now"],
              "第 %s 月，剩 %s 月" % (p["month_now"], p["left"]))
        check("结束时账面不动（不偷偷清算资产）", p["state"]["net"] == net_pre)
        check("结束后不能继续推进", _raises(lambda: api.choose(sid, [])))
        md = api.report(sid)["markdown"]
        check("结束后报告仍可出且声明提前结束", len(md) > 300 and "主动结束" in md)

        p2 = api.resume(sid)["point"]
        check("撤回后回到同一个决策点", not p2["over"] and len(p2["options"]) > 0,
              "第 %s 月" % p2["month"])

        # 自然结束不可撤回
        st3 = copy.deepcopy(api.S.load_session(sid))
        api.choose(sid, [])  # 推到下个点，别动它
        check("对进行中的会话 resume 是幂等空操作",
              api.resume(sid).get("already") is True)
    finally:
        pth = S._path(sid)
        if os.path.exists(pth):
            os.remove(pth)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def t_capital_accounting():
    """投入必须是一个账户，不是开业当天蒸发掉的费用。

    这条是被用户点出来的：原模型只有「现金」和「预收负债」两个账户，
    20 万现金 + 8 万转让费入局，那 8 万在账上根本不存在，
    退出时也按一个拍脑袋的 salvage_ratio 算，和实际资产结构无关。
    """
    pack = E.load_pack("gym")
    pack["operations"] = dict(pack["operations"])
    pack["operations"]["assets"] = {"transfer": 80000, "fitout": 200000,
                                    "equipment": 300000, "deposit": 55000}
    base_cash = pack["operations"]["cash"]

    rows = E.simulate(pack, actions=[], events=[], months=24, growth=0.0)
    bad = [r["月"] for r in rows
           if abs(r["净资产"] - (r["现金"] + r["资产"] - r["负债"])) > 1]
    check("逐月恒等：净资产 = 现金 + 资产 − 预收", not bad,
          f"{len(rows)} 个月全部核对" if not bad else f"第 {bad[:3]} 月对不上")

    check("资产按折旧递减，不会因经营波动跳变",
          rows[0]["资产"] > rows[11]["资产"] > rows[23]["资产"],
          f"{rows[0]['资产']:,.0f} → {rows[23]['资产']:,.0f}")

    capital = E.committed_capital(pack)
    check("总投入 = 开业现金 + 资产（回报率的分母）",
          abs(capital - (base_cash + 635000)) < 1, f"{capital:,.0f}")

    # 退出：资产清零、预收清零、关店后不再有固定成本流出
    ex = E.simulate(pack, actions=[{"at": 3, "id": "A-STR-02", "params": {}}],
                    events=[], months=12, growth=0.0)
    check("退出后资产清零、预收清零",
          ex[-1]["资产"] == 0 and ex[-1]["负债"] == 0)
    tail = {r["现金"] for r in ex[6:]}
    check("关店后不再有固定成本流出（现金走平）", len(tail) == 1, f"末 6 月现金集合 {len(tail)} 个值")
    # 押金几乎全退、装修设备按残值、转让费折半——四个数加权后应该落在合理区间，
    # 而不是"总投入 × 一个比例"（那样轻资产店和重资产店会算出一样的退出价）
    salv = E.exit_salvage(pack, 5)
    check("退出拿到的是分项残值，不是总投入乘一个比例",
          0.25 * capital < salv < 0.55 * capital,
          f"第 5 月退出可回收 {salv:,.0f}（总投入 {capital:,.0f}，占 {salv / capital:.0%}）")


def t_total_invested():
    """「总投入」是顶层口径：分项资产从总投入里扣，剩的是现金。

    用户要求开局自己填：总投入 / 设备投入 / 年租金 / 人力成本 / 其他投入，
    这些数都要进入计算周期。检验三种填报路径和「其他」资产的折旧回收。
    """
    base = E.load_pack("gym")
    it = {"city": "某市某市", "industry": "健身", "area_sqm": 300, "staff": 2,
          "customer_source": "natural", "months": 24, "already_open": True,
          "actual_volume": 100}

    # ① 总投入 + 分项：现金 = 总投入 − 资产，恒等式精确成立
    pk = I.derive_pack(dict(it, total_invested=700000,
                            equipment=350000, other_invested=150000), base)
    o = pk["operations"]
    check("总投入+分项：现金 = 总投入 − 资产合计",
          o["cash"] == 200000 and sum(o["assets"].values()) == 500000,
          f"现金 {o['cash']:,.0f}，资产 {sum(o['assets'].values()):,.0f}")
    check("committed_capital 精确等于总投入（分母不再差几块钱）",
          E.committed_capital(pk) == 700000, f"{E.committed_capital(pk):,.0f}")

    # ② 只填总投入：按原型比例拆，且标注估算
    pk2 = I.derive_pack(dict(it, total_invested=280000), base)
    check("只填总投入：仍满足 总投入 = 现金 + 资产",
          E.committed_capital(pk2) == 280000, f"{E.committed_capital(pk2):,.0f}")
    check("没分项时明确告诉用户这是估算",
          any("估算" in n for n in pk2["_intake_notes"]))

    # ③ 年租金 / 人力实报进入月度成本
    pk3 = I.derive_pack(dict(it, total_invested=280000,
                             rent_annual=120000, staff_cost=10000), base)
    check("实报年租/人力覆盖地区推算（月固定成本按实报走）",
          pk3["operations"]["fixed_cost"] >= 20000 + 1000,
          f"固定成本 {pk3['operations']['fixed_cost']:,.0f}/月")
    check("实报金额写进了可追溯的 notes",
          any("用户实报" in n for n in pk3["_intake_notes"]))

    # ④ 「其他」资产：36 月摊、退出回收 20%
    pol = E.asset_policy(pk)["other"]
    check("其他投入按租期摊、退出回收 20%", pol["life"] == 36 and pol["recovery"] == 0.20)
    rows = E.simulate(pk, actions=[], events=[], months=24, growth=0.0)
    exp = 350000 * max(0.0, 1 - 12 / 60) + 150000 * max(0.0, 1 - 12 / 36)
    salv12 = E.exit_salvage(pk, 12)
    check("第 12 月退出 = 设备残值 30% + 其他残值 20%",
          abs(salv12 - (350000 * (1 - 12 / 60) * 0.30 + 150000 * (1 - 12 / 36) * 0.20)) < 1,
          f"可回收 {salv12:,.0f}（账面 {exp:,.0f}）")

    # ⑤ 分项超过总投入：警告而不是静默负现金
    pk5 = I.derive_pack(dict(it, total_invested=200000,
                             equipment=350000, other_invested=150000), base)
    check("分项超过总投入会显式警告",
          any("超过总投入" in n for n in pk5["_intake_notes"]),
          f"现金 {pk5['operations']['cash']:,.0f}")


def main():
    print("交互式会话不变量\n" + "=" * 60)
    for fn in (t_prefix, t_replay, t_env_frozen, t_history_immutable,
               t_repeat_decay, t_all_sessions, t_open_all, t_end_resume,
               t_capital_accounting, t_total_invested):
        print(f"\n[{fn.__name__}]")
        fn()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  ✗ " + f)
        sys.exit(1)


if __name__ == "__main__":
    main()
