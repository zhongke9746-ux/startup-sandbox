#!/usr/bin/env python3
"""交互式经营会话 —— 把"决策"还给用户，引擎只负责结算后果。

三层分工：
    engine.py   纯函数：动作列表 + 环境 → 月度账本。无状态、无随机、无行业分支
    session.py  有状态：冻结环境、记录决策、逐月推进、生成决策点、给出反馈
    forecast    从当前状态重跑 N 条未来 → (存活率, p50, p5) 三元组，代替单一评分

为什么要"每次全期重算"而不是增量推进：
    动作效果按绝对月份定位（start=m0）。用同一份决策列表从头重算，第 m 月的结果
    与一次跑满 24 月的第 m 行逐格相同（verify_session.py 有断言）。而增量推进需要
    手工维护 cash/liab/cum_g 等 6 个累积量，任何一处漏更新都会静默漂移——这种 bug
    在账本上表现为"算错了"，在体验上表现为"我明明没做这个动作，钱怎么少了"。

为什么决策点是"打断"而不是"每月一问"：
    真实经营者的瓶颈是注意力。24 个月问 24 次，用户三次之后就只会点"跳过"。
    所以只在四种时刻打断：外部冲击落地、现金越过警戒线、机会窗打开、静默太久。

用法：
    python3 scripts/session.py new cafe --seed 42 --name "街角咖啡"
    python3 scripts/session.py choose <sid>            # 观望（不动作）
    python3 scripts/session.py choose <sid> A-PRICE-01:price_delta=0.08
    python3 scripts/session.py forecast <sid> -n 400
    python3 scripts/session.py log <sid>
"""

import argparse
import copy
import json
import os
import random
import sys
import zlib

import engine as E

ROOT = E.ROOT
SESS = os.path.join(ROOT, "sessions")

# 动作的"人话"呈现层文案。这里是产品语言，不是结算逻辑——
# 结算只认 _actions.yaml 与 engine.compile_action，两者不许在这里重复。
BLURB = {
    "A-PRICE-01":   "涨价救毛利，但会掉量",
    "A-PRICE-02":   "降价换客流，先止血后遗症",
    "A-COST-01":    "裁人最快降本，最伤组织",
    "A-COST-02":    "重谈房租/长约，不伤团队但要看筹码",
    "A-COST-03":    "换便宜供应商，赌客人尝不出来",
    "A-COST-04":    "砍营销/福利，立刻变好看，伤未来",
    "A-GROW-01":    "加大投放，只有单位经济为正才成立",
    "A-GROW-02":    "开新渠道，来量快但要交过路费",
    "A-GROW-03":    "上新品提客单，成功率大约一半",
    "A-GROW-04":    "会员/复购：会员价让 3% 换留存，两个月后才见效",
    "A-CAP-01":     "扩产，利用率没到 85% 就是纯加成本",
    "A-CAP-02":     "收缩产能，承认失败但止血",
    "A-FIN-01":     "融资，要 4 个月才有钱到账",
    "A-FIN-02":     "借贷，不稀释股权但把刚性拉满",
    "A-FIN-03":     "卖预付卡，今天拿到钱，明天背着债",
    "A-RISK-01":    "合规加固，买保险，短期难看",
    "A-RISK-02":    "危机公关，黄金窗口只有一个月",
    "A-STR-01":     "砍掉亏损业务线，收入掉、成本掉更多",
    "A-STR-02":     "退出/转让，把剩下的资产变现",
}

FAMILY_LABEL = {"pricing": "价格", "cost": "成本", "growth": "增长",
                "capacity": "产能", "finance": "资金", "risk": "风控",
                "strategy": "战略"}

# 成功率不显示数字。真实经营者不知道 p，只知道"有没有把握"。
# 这不是美化，是防止用户学会"按 p 挑动作"——那是在玩概率表，不是在经营。
def confidence(p):
    if p >= 0.95:
        return "基本会成"
    if p >= 0.75:
        return "多半能成"
    if p >= 0.5:
        return "五五开"
    return "没把握"


# ────────────────────────── 确定性随机 ──────────────────────────
# 不用 random.Random(str)：str 的 hash 受 PYTHONHASHSEED 影响，换个进程就复现不了。
def _rng(*parts):
    key = "|".join(str(p) for p in parts)
    return random.Random(zlib.crc32(key.encode()))


# ────────────────────────── 环境采样（可续采）──────────────────────────
def _macro(cfg, months):
    mc = cfg.get("macro_regime") or {}
    if not mc.get("enabled", False):
        return None
    names = list(mc["states"].keys())
    trans, init = mc["transition"], mc.get("init", "normal")

    # 窗口内期望占用（与 monte_carlo.prep_macro 同一口径，理由见那里的注释：
    # 24 个月走不到平稳分布，按平稳分布居中会让窗口内平均增长系统性偏低）
    v = {n: 1.0 if n == init else 0.0 for n in names}
    acc = {n: 0.0 for n in names}
    for _ in range(months):
        for n in names:
            acc[n] += v[n]
        v = {b: sum(v[a] * trans[a].get(b, 0.0) for a in names) for b in names}
    w = {n: acc[n] / months for n in names}

    st = mc["states"]
    means = {k: sum(w[n] * st[n][k] for n in names)
             for k in ("growth_shift", "noise_offset", "event_occ_mult")}
    return {
        "names": names, "trans": trans, "init": init,
        "growth_shift": {n: st[n]["growth_shift"] - means["growth_shift"] for n in names},
        "noise_offset": {n: st[n]["noise_offset"] - means["noise_offset"] for n in names},
        "sigma_mult": {n: st[n]["sigma_mult"] for n in names},
        "occ_mult": {n: st[n]["event_occ_mult"] / means["event_occ_mult"] for n in names},
        "raw_mean": means,
    }


def _states(macro, seed, months, base=None, keep=0):
    """逐月宏观状态。base 存在时前 keep 个月沿用（历史不可改写），
    第 keep 个月的状态作为续采起点。"""
    if not macro:
        return None
    trans, names = macro["trans"], macro["names"]
    rng = random.Random(zlib.crc32(f"{seed}|states".encode()))
    out = []
    cur = macro["init"]
    for m in range(1, months + 1):
        if base and m <= keep:
            out.append(base["states"][m - 1])
            cur = base["states"][m - 1]
            continue
        if m > 1:
            r = rng.random()
            acc, cur_next = 0.0, names[-1]
            for n in names:
                acc += trans[cur].get(n, 0.0)
                if r < acc:
                    cur_next = n
                    break
            cur = cur_next
        out.append(cur)
    return out


def _events(pack, cfg, months, macro, states, seed, base=None, keep=0, snap=None):
    """事件：是否发生、烈度、时间。

    snap（环境快照）存在时，事件目录来自创建时刻的真实抓取（window 时间窗 +
    delta 力度区间 + group 联动），否则退回 pack 里的 demo 目录（start 单点 +
    三角抖动）。keep>0 时：已经发生（start<=keep）的事件原样保留——"上周已经
    封路"是事实，重算未来不能把它抹掉；其余事件重新掷骰，但若落到 keep 之前
    则丢弃（不许追溯改写）。
    """
    occ_p = cfg["event_occurrence"]["default_p"]
    mag = cfg["event_magnitude"]
    jit = cfg["event_start_jitter"]
    rng = random.Random(zlib.crc32(f"{seed}|events".encode()))
    snap_cat = (((snap or {}).get("env") or {}).get("event_catalog"))
    demo = snap_cat if snap_cat else (pack.get("demo_events", []) or [])

    happened = {}
    if base:
        for e in base["events"]:
            if e.get("src") is not None and e["start"] <= keep:
                happened[e["src"]] = e

    out = []
    groups = {}
    for i, ev in enumerate(demo):
        if i in happened:
            out.append(copy.deepcopy(happened[i]))
            continue
        p = ev.get("p", occ_p)
        if macro and states:
            anchor = ev.get("start") or ev.get("window", [1])[0]
            idx = min(months, max(1, anchor)) - 1
            p = min(1.0, p * macro["occ_mult"][states[idx]])
        # 同组事件共享一次发生判定与同一个落地月（如：食安检查与罚款联动）
        g = ev.get("group")
        if g is not None:
            if g not in groups:
                lo, hi = ev.get("window", [ev.get("start", 1)] * 2)
                groups[g] = {"roll": rng.random(),
                             "start": max(1, min(months, rng.randint(lo, min(hi, months))))}
            roll = groups[g]["roll"]
        else:
            roll = rng.random()
        if roll >= p:
            continue
        e = copy.deepcopy(ev)
        e["src"] = i
        # 时间：快照目录给 window 区间（均匀抽样），demo 目录给 start + 抖动
        if g is not None:
            e["start"] = groups[g]["start"]
        elif "window" in ev:
            lo, hi = ev["window"]
            # 窗口整体落在赛季之外（例如只跑 6 个月、事件的窗口是 8–14 月）时，
            # 不能直接 randint：lo > months 会抛 empty range。把它压到最后一两个月。
            lo2, hi2 = min(lo, months - 1 if months > 1 else 1), min(hi, months)
            e["start"] = max(1, min(months, rng.randint(min(lo2, hi2), hi2)))
        else:
            e["start"] = max(jit["clamp"][0], min(months, e["start"] + rng.randint(jit["min"], jit["max"])))
        # 力度：快照目录给 [lo,hi] 区间（均匀），demo 目录给单点 × 三角
        d = ev["delta"]
        e["delta"] = rng.uniform(d[0], d[1]) if isinstance(d, list) \
            else d * rng.triangular(mag["min"], mag["max"], mag["mode"])
        if keep and e["start"] <= keep:
            continue
        for k in ("window", "group", "p"):
            e.pop(k, None)
        out.append(e)
    return out


def _growth(pack, months, macro, states, seed, snap=None):
    # 快照存在时，增长区间来自创建时刻抓取的行业数据，而不是 pack 里的占位值
    g = (((snap or {}).get("env") or {}).get("growth_assumption")
         or pack["baseline"].get("growth_assumption", [0.0, 0.0]))
    rng = random.Random(zlib.crc32(f"{seed}|growth".encode()))
    base = rng.uniform(g[0], g[1])
    if macro and states:
        return [base + macro["growth_shift"][states[m - 1]] for m in range(1, months + 1)]
    return [base] * months


def _noise(pack, cfg, months, macro, states, seed):
    sigma = pack["baseline"].get("demand_volatility", 0.05)
    lo, hi = cfg["volume_noise"]["clip"]
    rng = random.Random(zlib.crc32(f"{seed}|noise".encode()))
    out = []
    for m in range(1, months + 1):
        if macro and states:
            s = states[m - 1]
            sg, off = sigma * macro["sigma_mult"][s], macro["noise_offset"][s]
        else:
            sg, off = sigma, 0.0
        out.append(max(lo, min(hi, 1.0 + off + rng.gauss(0.0, sg))))
    return out


def sample_env(pack, cfg, months, seed, macro, base=None, keep=0, snap=None):
    """base 存在 = 续采：前 keep 月沿用既成事实，之后按新 seed 重新展开。
    snap = 创建时刻冻结的环境快照（真实抓取），接管事件目录与增长区间。"""
    k = keep if base else 0
    states = _states(macro, seed, months, base, k)
    ev = _events(pack, cfg, months, macro, states, seed, base, k, snap)
    gr = _growth(pack, months, macro, states, seed, snap)
    nz = _noise(pack, cfg, months, macro, states, seed)
    if base and keep:
        gr = base["growth"][:keep] + gr[keep:]
        nz = base["noise"][:keep] + nz[keep:]
    return {"events": ev, "growth": gr, "noise": nz, "states": states}


# ────────────────────────── 会话读写 ──────────────────────────
def _path(sid):
    return os.path.join(SESS, f"{sid}.json")


def _migrate(st):
    """兼容老会话：决定日志里 chosen 早期存的是动作 id 字符串，后来才改成
    {id, params, ok}。在读取这一层统一掉，比让每个消费者各自 isinstance 检查可靠。

    不写回磁盘——只在内存里规整，老文件保持原样（可重放性靠原始日志）。
    """
    for d in st.get("decisions") or []:
        ch = d.get("chosen")
        if ch and any(not isinstance(c, dict) for c in ch):
            d["chosen"] = [c if isinstance(c, dict) else {"id": str(c), "params": {}}
                           for c in ch]
    return st


def load_session(sid):
    p = _path(sid)
    # 抛异常而不是 sys.exit：这个函数会被 Web 层在子线程里调用，
    # sys.exit 在非主线程只会静默杀掉那个线程，客户端收到空响应。
    if not os.path.exists(p):
        raise FileNotFoundError(f"没有这个会话：{sid}")
    with open(p, encoding="utf-8") as f:
        return _migrate(json.load(f))


def save_session(st):
    os.makedirs(SESS, exist_ok=True)
    with open(_path(st["sid"]), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ────────────────────────── 主动结束 / 继续 ──────────────────────────
def end_session(st, reason=None):
    """用户随时按停：停在当前已结算的月份，不做资产清算。

    和 A-STR-02「退出/转让」是两件事，别混：
    - A-STR-02 是**经营决策**——变现资产、留 2 个月延迟、成功率 70%，进账后终局；
    - 这个只是**停止推演**——账就停在这里，报告按"现状 + 未来推演"给结论。
      如果用户真想退出，该在结束前先走 A-STR-02，那样剩余资产才会回到现金。

    可撤销：网页上误触很常见，所以只打 end_kind="user" 标记。
    pending 保留不动，继续时同一个决策点会原样回来。
    """
    if st.get("over"):
        return st
    m = st["month"]
    left = max(0, (st["months_total"] or 0) - m)
    st["over"] = True
    st["end_kind"] = "user"
    st["end_month"] = m
    st["end_reason"] = reason or (
        f"你在第 {m} 个月主动收手（还剩 {left} 个月没跑）" if m
        else "你在开局前就结束了，还没有任何月份被结算")
    return st


def resume_session(st):
    """撤销"主动结束"。只能撤销用户自己按的停——死亡和跑满赛季不可复活。"""
    if not st.get("over"):
        return st
    if st.get("end_kind") != "user":
        raise ValueError(f"这一局是自然结束的（{st.get('end_reason')}），不能继续")
    st["over"] = False
    for k in ("end_kind", "end_month", "end_reason", "death_month"):
        st.pop(k, None)
    return st


# ────────────────────────── 账本重算 ──────────────────────────
def _pack(st):
    """这家店的参数包：开局信息采集派生的定制包优先，否则读规则库原型包。"""
    return st.get("pack_data") or E.load_pack(st["pack"])


def rerun(st):
    """用当前决策列表把全期重算一遍。返回 (rows_full, monthly_trace, meta)。"""
    pack = _pack(st)
    env = st["env"]
    trace = {}
    rows = E.simulate(pack, actions=st["actions"], events=env["events"],
                      months=st["months_total"], growth=env["growth"],
                      volume_noise=env["noise"], gating=True, trace=trace)
    return rows, trace.get("monthly", []), {"refused": trace.get("refused", []),
                                            "weak": trace.get("weak", [])}


# ────────────────────────── 状态读出 ──────────────────────────
def snapshot(st):
    """当前月末的经营状态（用户能看懂的那几个数）。"""
    rows, mon, _ = rerun(st)
    m = st["month"]
    pack = _pack(st)
    burn = E._burn(pack)
    o = pack["operations"]
    if m == 0:
        cash, net = o["cash"], o["cash"] - o.get("liability", 0)
        month_row = None
        util = o["volume"] / max(1e-9, o["capacity"])
        profit = 0.0
        vol, price = o["volume"], o["price"]
        assets_book = E.asset_book(pack, 0)
        net_worth = cash + assets_book - o.get("liability", 0)
    else:
        r = rows[m - 1]
        cash, net, profit = r["现金"], r["净现金"], r["净利"]
        month_row = r
        mm = mon[m - 1]
        util = mm["利用率"]
        vol, price = mm["成交"], mm["价"]
        assets_book = r.get("资产", 0)
        net_worth = r.get("净资产", net)
    runway = (cash / burn) if burn > 0 else 99.0
    return {
        "month": m, "cash": cash, "net": net, "profit": profit,
        "runway": max(0.0, runway), "util": util, "vol": vol, "price": price,
        "burn": burn, "row": month_row,
        # 投资口径（和上面的流动性口径是两件事，别混）：
        # net = 现金 − 该退的预收，回答"明天会不会没钱"；
        # net_worth = 现金 + 资产 − 预收，回答"我投的钱现在值多少"。
        "assets": assets_book, "net_worth": net_worth,
        "capital": E.committed_capital(pack), "salvage": E.exit_salvage(pack, m),
    }


# ────────────────────────── 决策点触发 ──────────────────────────
def triggers(st, rows, mon, m):
    """第 m 月是否需要用户做决策。返回 [(代码, 人话)]。"""
    out = []
    pack = _pack(st)
    burn = E._burn(pack)
    prev_cash = rows[m - 2]["现金"] if m >= 2 else pack["operations"]["cash"]
    cash, net = rows[m - 1]["现金"], rows[m - 1]["净现金"]

    # T0 开局：第一笔钱花在哪，决定前三个月的形状
    if m == 1 and not st["decisions"]:
        out.append(("T0", "开局：这笔钱先花在哪，会决定前三个月的形状"))

    # T1 外部冲击：本月有新事件落地，或正在生效
    hits = [e for e in st["env"]["events"] if e["start"] == m]
    for e in hits:
        out.append(("T1", f"{e.get('name', e['id'])} 本月落地"))

    # T2 现金警戒：跑道 < 3 个月
    if cash / burn < 3.0 <= prev_cash / burn:
        out.append(("T2", f"现金只够 {cash / burn:.1f} 个月了"))

    # T3 账面已透支
    if cash < 0 < prev_cash:
        out.append(("T3", "现金已经为负，付不出下个月的账"))
    elif net < 0 <= (rows[m - 2]["净现金"] if m >= 2 else 0):
        out.append(("T3", "净资产转负——欠的钱比有的钱多"))

    # T4 机会窗：产能打满或持续赚钱且有跑道。
    # 两个排除：开业头两个月（利用率是装修期惯性）；正在挨打的时候
    # （外面在修路、刚被通报，此刻谈扩张是把钱扔进坑里）。
    under_fire = [e for e in st["env"]["events"]
                  if e["slot"] == "volume" and e["delta"] < 0
                  and e["start"] <= m <= e["start"] + (e.get("dur") or 99)]
    # T4 只看已结算的上个月（mon[m-2]）：用未发生月份的预测需求触发，
    # 等于把需求噪声的骰子提前翻给用户看——那是剧透，不是机会窗。
    if m >= 3 and not under_fire and mon[m - 2]["利用率"] >= 0.90:
        out.append(("T4", f"上月产能利用 {mon[m - 2]['利用率']:.0%}，装不下了"))
    elif m >= 3 and not under_fire and rows[m - 1]["净利"] > 0 and cash / burn >= 6:
        if rows[m - 2]["净利"] > 0:
            out.append(("T4", "连续两个月赚钱，跑道也够"))

    # T5 静默期满：三个月没人管过
    last = max([d["at"] for d in st["decisions"]] or [0])
    if m - last >= 3 and m < st["months_total"]:
        out.append(("T5", f"距上次决策已 {m - last} 个月"))

    return out


def next_point(st, rows, mon):
    """从不晚于"当前+1"的月份里，找出第一个该被打断的月份。
    已经走到最后一月时返回 None —— 赛季结束，不再生成决策点。"""
    if st["month"] >= st["months_total"]:
        return None, [("T6", "赛季结束")]
    start = st["month"] + 1
    cadence = st["cadence"]
    for m in range(start, st["months_total"] + 1):
        if rows[m - 1]["现金"] < 0:
            return m, [("T3", "现金归零")]
        if m == st["months_total"]:
            return m, [("T6", "第 %d 个月，赛季结束" % m)]
        t = triggers(st, rows, mon, m)
        if cadence == "monthly":
            return m, t or [("T7", "月度例会")]
        # 带宽被上一个重动作锁死时，就算有事也推不动——此时打断只是在
        # 让用户看一个只能点"观望"的菜单。危机除外：坏消息必须通知，
        # 哪怕这一秒什么也做不了。
        crisis = any(c in ("T2", "T3") for c, _ in t)
        if _bandwidth(st, m)[0] <= 0 and not crisis:
            continue
        if t and not (cadence == "quarterly" and [x for x in t if x[0] in ("T1", "T2", "T3")] == []
                      and m % 3 != 0):
            return m, t
        if cadence == "quarterly" and m % 3 == 0:
            return m, t or [("T7", "季度复盘")]
    return st["months_total"], []


# ────────────────────────── 选项生成 ──────────────────────────
def _last_month(st, aid):
    ms = [a["at"] for a in st["actions"] if a["id"] == aid and not a.get("failed")]
    return max(ms) if ms else None


def _bandwidth(st, month):
    cfg = E.load("_actions")["bandwidth"]
    for lock in st.get("locks", []):
        if lock["from"] <= month <= lock["until"]:
            return 0, f"{lock['reason']} 占用"
    used = sum(1 for a in st["actions"] if a["at"] == month)
    return max(0, cfg["monthly_action_slots"] - used), ""


def build_options(st, month, snap):
    """本月可选的动作 + 每个动作的真实代价与警告。"""
    lib = E.load("_actions")
    pack = _pack(st)
    meta = {a["id"]: a for a in lib["actions"]}
    heavy = set(lib["bandwidth"]["heavy_actions"])
    slots, lock_reason = _bandwidth(st, month)

    opts, blocked = [], []
    snap_adj = ((_snap(st) or {}).get("action_adjustments") or {})
    for aid, a in meta.items():
        ap = E.applicability(pack, aid)
        rec = {"id": aid, "name": a["name"], "family": a["family"],
               "family_label": FAMILY_LABEL.get(a["family"], a["family"]),
               "blurb": BLURB.get(aid, ""), "tier": ap["tier"],
               "confidence": confidence(_eff_p(st, aid)),
               "reversibility": a.get("reversibility", "medium"),
               "params_hint": a.get("params", {}), "warnings": []}

        # 快照情报：这次抓取里和这个动作直接相关的现实证据
        intel = (snap_adj.get("success_adjust") or {}).get(aid, {}).get("evidence")
        pd_spec = (snap_adj.get("param_defaults") or {}).get(aid, {})
        if pd_spec:
            evs = [v.get("evidence", "") for v in pd_spec.values() if isinstance(v, dict)]
            intel = (intel + "；" if intel else "") + "；".join(e for e in evs if e)
        rec["intel"] = intel

        if ap["tier"] == "impossible":
            blocked.append({**rec, "why": ap["reasons"][0] if ap["reasons"] else "本业态不具备"})
            continue

        # 退出不是随时可用的选项：跑道还宽的时候谈退出，等于把上行空间白送
        if aid == "A-STR-02":
            loss_run = 0
            for r in reversed(rerun(st)[0][:max(0, month - 1)]):
                if r["净利"] < 0:
                    loss_run += 1
                else:
                    break
            if not (snap["runway"] < 4 or loss_run >= 3):
                blocked.append({**rec, "why": "现金流还没到需要考虑退出的地步"})
                continue

        # 时效性前置：危机公关只在事件后 1 个月内有效（_actions.yaml 的 requires）
        if aid == "A-RISK-02" and not [e for e in st["env"]["events"]
                                       if month - 1 <= e["start"] <= month]:
            blocked.append({**rec, "why": "最近一个月没有需要回应的负面事件"})
            continue

        cd = a.get("cooldown_months", 0)
        last = _last_month(st, aid)
        if last is not None and month - last < cd:
            blocked.append({**rec, "why": f"冷却中，还要 {cd - (month - last)} 个月"})
            continue

        if slots <= 0:
            blocked.append({**rec, "why": f"本月没有管理带宽了（{lock_reason or '额度用尽'}）"})
            continue

        # 真实一次性成本：直接问引擎，不在交互层另立一份算法
        try:
            _, once, _ = E.compile_action(
                {"id": aid, "params": _snap_defaults(st).get(aid, {})}, pack, month)
        except KeyError:
            once = 0.0
        rec["one_time"] = round(once)
        rec["affordable"] = once <= max(0.0, snap["cash"])
        if not rec["affordable"]:
            rec["warnings"].append(f"一次性支出约 ¥{once:,.0f}，超过手上现金")

        # 守卫：引擎不拦，但要说清"这个方向在你的结构里是负杠杆"
        e = pack["baseline"].get("price_elasticity", 0.0)
        if aid in ("A-PRICE-01", "A-PRICE-02") and e >= 1.0:
            rec["warnings"].append(f"价格弹性 {e:.2f} ≥ 1：量对价的反应比价本身还大，这是负杠杆")
        if aid == "A-CAP-01" and snap["util"] < 0.85:
            rec["warnings"].append(f"当前利用率 {snap['util']:.0%}，新增产能不会变成成交，只会变成成本")
        if aid == "A-COST-01" and pack["baseline"].get("key_person_dependency", 0) >= 0.5:
            rec["warnings"].append(f"关键人依赖 {pack['baseline']['key_person_dependency']:.2f}：走的人会把品质带走")
        if aid == "A-COST-03" and pack["baseline"].get("quality_visibility", 0) >= 0.7:
            rec["warnings"].append("客人尝得出差别，降规格会被记住")
        if aid in heavy:
            rec["warnings"].append("重动作：占满当月带宽，并锁住后 2 个月")

        opts.append(rec)

    # 按情境排序：缺钱先看现金，装不下先看供给，赚钱先看增长。
    # 注意 A-CAP-02（收缩产能）不能因为"利用率高"就得高分——忙的时候收缩是自杀。
    def score(o):
        aid = o["id"]
        s = 0.0
        if snap["runway"] < 3:
            s += {"finance": 30, "cost": 24, "capacity": 6, "risk": 6, "strategy": 8}.get(o["family"], 0)
            if aid in ("A-FIN-02", "A-FIN-03", "A-COST-02", "A-COST-04"): s += 10
            if aid == "A-CAP-02": s += 4
        elif snap["util"] >= 0.90:
            if aid == "A-CAP-01": s += 40
            if aid in ("A-PRICE-01", "A-GROW-02", "A-GROW-03"): s += 22
            if aid == "A-CAP-02": s -= 25
            if aid in ("A-PRICE-02", "A-GROW-01"): s -= 10
        elif snap["profit"] > 0:
            if aid in ("A-GROW-01", "A-GROW-02", "A-GROW-03", "A-GROW-04"): s += 20
            if aid == "A-PRICE-01": s += 12
            if aid == "A-CAP-02": s -= 15
        else:
            if aid in ("A-COST-02", "A-COST-04", "A-PRICE-01", "A-GROW-03"): s += 16
            if aid in ("A-GROW-01", "A-CAP-01"): s -= 8
        if not o["warnings"]:
            s += 6
        if o["confidence"] == "基本会成":
            s += 8
        if o["reversibility"] == "low":
            s -= 6
        return -s

    opts.sort(key=score)
    return opts, blocked, slots


# ────────────────────────── 环境快照（L0/L1 层）──────────────────────────
def _snap(st):
    return st.get("snapshot")


def _eff_p(st, aid):
    """动作的生效成功率：规则库基础值 + 快照修正（如「今年是租客市场，重谈房租更可能成」）。
    修正值带证据，存在快照文件里，不是拍脑袋。"""
    lib = E.load("_actions")
    p = next((a.get("success_probability", 1.0) for a in lib["actions"] if a["id"] == aid), 1.0)
    adj = (((_snap(st) or {}).get("action_adjustments") or {}).get("success_adjust") or {}).get(aid)
    if adj:
        p = max(0.05, min(0.99, p + adj["delta"]))
    return p


def _snap_defaults(st):
    """快照给出的参数默认值（如外卖综合抽成从 18% 改为 14%）。
    用户显式传的参数永远覆盖默认值。"""
    out = {}
    for aid, spec in (((_snap(st) or {}).get("action_adjustments") or {}).get("param_defaults") or {}).items():
        out[aid] = {k: (v["value"] if isinstance(v, dict) else v) for k, v in spec.items()}
    return out


def render_brief(snap):
    """创建时刻的情报简报：告诉玩家这个世界是从哪些真实信息冻结来的。"""
    m = snap["meta"]
    L = [f"### 环境快照（截至 {m['asof']} · {m.get('region', '')}）", ""]
    L.append(f"本局的世界来自 {len(m['sources'])} 个真实来源的抓取，**已冻结**——"
             f"接下来 {24} 个月里它不会变，就像现实里你看不到明天的新闻。")
    L.append("")
    for b in snap.get("brief", []):
        L.append(f"- {b}")
    adj = snap.get("action_adjustments") or {}
    sa, pd_ = adj.get("success_adjust") or {}, adj.get("param_defaults") or {}
    if sa or pd_:
        L.append("")
        L.append("**抓取结果直接改了三件事：**")
        for aid, a in sa.items():
            L.append(f"- {aid} 成功率 {'+' if a['delta'] >= 0 else ''}{a['delta']:.0%}（{a['evidence'].split('：')[0]}）")
        for aid, spec in pd_.items():
            for k, v in spec.items():
                val = v["value"] if isinstance(v, dict) else v
                L.append(f"- {aid} 的 {k} 默认改为 {val}（{(v.get('evidence') if isinstance(v, dict) else '').split('：')[0]}）")
    nc = snap.get("not_changed", [])
    if nc:
        L.append("")
        L.append("**抓到了但没敢用的：** " + "；".join(x.split("：")[0] for x in nc[:4])
                 + "。证据不足以量化到单店，宁可不动也不假装精确。")
    return "\n".join(L)


# ────────────────────────── 掷骰（一次定终身）──────────────────────────
def roll_action(st, aid, month, idx):
    """动作成败在"选的那一刻"就定了，不能反悔重掷。
    同一会话同一动作同一月，永远返回同一个结果——这是可复现性的要求。"""
    p = _eff_p(st, aid)
    if p >= 1.0:
        return True, p
    r = _rng(st["sid"], month, idx, aid).random()
    return r < p, p


# ────────────────────────── 叙事 ──────────────────────────
def narrate(st, prev_mon, cur_mon, prev_row, cur_row, res):
    """把"为什么变了"讲清楚。取变化最大的两个成本项 + 量价变化。"""
    bits = []
    if prev_mon and cur_mon:
        d = {k: cur_mon[k] - prev_mon[k] for k in ("营收", "货品", "变动", "固定")}
        money = [(k, v) for k, v in d.items() if abs(v) >= 200]
        money.sort(key=lambda x: -abs(x[1]))
        for k, v in money[:2]:
            bits.append(f"{k}{'多' if v > 0 else '少'}了 ¥{abs(v):,.0f}")
        dv = cur_mon["成交"] - prev_mon["成交"]
        if abs(dv) >= max(3, prev_mon["成交"] * 0.02):
            bits.append(f"客流{'涨' if dv > 0 else '跌'} {abs(dv):.0f} 单/天")
        dp = cur_mon["价"] - prev_mon["价"]
        if abs(dp) >= 0.3:
            bits.append(f"客单{'上调' if dp > 0 else '下调'} ¥{abs(dp):.1f}")

    chosen = [a for a in st["actions"] if a["at"] == res["month"]]
    if chosen:
        names = "、".join(a.get("name") or a["id"] for a in chosen)
        bits.append(f"本月动作：{names}")

    evs = [e.get("name", e["id"]) for e in st["env"]["events"] if e["start"] == res["month"]]
    if evs:
        bits.insert(0, "外部：" + "、".join(evs))

    return "；".join(bits) if bits else "本月无明显变化，账本按惯性走。"


# ────────────────────────── 推进 ──────────────────────────
def apply_choices(st, choices):
    """choices: [{"id":.., "params":{..}}]。返回本月结算结果。"""
    # 未知 id 必须在入口挡住：后面按 id 查动作库是 next(...) 无默认值，
    # 漏一个就会抛 StopIteration，在 HTTP 里表现成一次没有任何说明的 500。
    known = {a["id"] for a in E.load("_actions")["actions"]}
    unknown = [str(c.get("id")) for c in (choices or []) if c.get("id") not in known]
    if unknown:
        raise ValueError("未知动作 id：" + "、".join(unknown))

    month = st["month"] + 1
    pack = _pack(st)
    before_cash = (rerun(st)[0][month - 2]["现金"] if month >= 2 else pack["operations"]["cash"])
    prev_mon = (rerun(st)[1][month - 2] if month >= 2 else None)
    prev_row = rerun(st)[0][month - 2] if month >= 2 else None

    accepted, rejected = [], []
    slots, lock_reason = _bandwidth(st, month)
    heavy = set(E.load("_actions")["bandwidth"]["heavy_actions"])

    for i, c in enumerate(choices):
        aid = c["id"]
        ap = E.applicability(pack, aid)
        if ap["tier"] == "impossible":
            rejected.append({"id": aid, "why": ap["reasons"][0] if ap["reasons"] else "不可行"})
            continue
        cd = next(a.get("cooldown_months", 0) for a in E.load("_actions")["actions"] if a["id"] == aid)
        last = _last_month(st, aid)
        if last is not None and month - last < cd:
            rejected.append({"id": aid, "why": f"冷却中（{cd} 个月）"})
            continue
        if slots <= 0:
            rejected.append({"id": aid, "why": f"带宽不足（{lock_reason or '已用满'}）"})
            continue

        ok, p = roll_action(st, aid, month, i)
        lib_a = next(a for a in E.load("_actions")["actions"] if a["id"] == aid)
        # 快照给的默认值垫在用户参数下面（如外卖抽成按抓取的 14% 而非 18%）
        params = {**_snap_defaults(st).get(aid, {}), **c.get("params", {})}
        st["actions"].append({
            "at": month, "id": aid, "name": lib_a["name"], "params": params,
            **({"failed": True} if not ok else {}),
        })
        slots -= 1
        accepted.append({"id": aid, "name": lib_a["name"], "ok": ok, "p": p})
        if aid in heavy:
            st.setdefault("locks", []).append(
                {"from": month, "until": month + 2, "reason": f"{lib_a['name']}"})

    rows, mon, meta = rerun(st)
    cur_row, cur_mon = rows[month - 1], mon[month - 1]
    st["month"] = month

    res = {
        "month": month,
        "accepted": accepted, "rejected": rejected,
        "cash_before": before_cash, "cash_after": cur_row["现金"],
        "profit": cur_row["净利"], "net": cur_row["净现金"],
        "util": cur_mon["利用率"], "vol": cur_mon["成交"],
        "story": narrate(st, prev_mon, cur_mon, prev_row, cur_row, {"month": month}),
    }

    # 终局判定：真死了就停，不要继续算一个已经不存在的主体
    if cur_row["现金"] < 0:
        st["over"] = True
        st["end_reason"] = "流动性死亡：现金为负，付不出下一笔账"
        st["death_month"] = month
    elif any(a["id"] == "A-STR-02" and a["at"] == month and not a.get("failed") for a in st["actions"]):
        st["over"] = True
        st["end_reason"] = "主动退出：把剩余资产变现离场"
        st["death_month"] = month
    return res


# ────────────────────────── 结算评分（三元组）──────────────────────────
def _final_worth(row):
    """一个月的终局价值：净资产口径。

    「净现金」只回答"还有多少活钱"；一家店的价值还包括装修、设备、押金、
    转让费这些已经沉在里面的钱。用净现金当终局，等于把开业投入当成烧掉了。
    """
    return row.get("净资产", row["净现金"])


def forecast(st, n=400):
    """从当前状态往前推：不再有新决策，只有未来的不确定性。
    返回 (存活率, p50, p5) —— 而不是一个分数。"""
    pack = _pack(st)
    cfg = E.load("_randomness")
    months = st["months_total"]
    macro = _macro(cfg, months)
    keep = st["month"]
    if keep >= months:
        return None
    finals, dead = [], 0
    for i in range(n):
        env = sample_env(pack, cfg, months, f"{st['sid']}|f{i}", macro,
                         base=st["env"], keep=keep, snap=_snap(st))
        rows = E.simulate(pack, actions=st["actions"], events=env["events"], months=months,
                          growth=env["growth"], volume_noise=env["noise"], gating=True)
        # 未来任何一个月现金归零，就算没活下来
        alive = all(r["现金"] >= 0 for r in rows[keep:])
        if not alive:
            dead += 1
        finals.append(_final_worth(rows[-1]))
    finals.sort()

    def q(p):
        return finals[max(0, min(len(finals) - 1, int(p * (len(finals) - 1))))]

    return {
        "n": n, "survival": 1 - dead / n,
        "p5": q(0.05), "p50": q(0.50), "p95": q(0.95),
        "worst": finals[0], "best": finals[-1],
    }


def forecast_paths(st, n=400):
    """forecast 的明细版：每条未来路径的终局、死亡月、激活事件。
    结论层（report）需要归因——不只问"死没死"，还要问"被什么杀死的"。"""
    pack = _pack(st)
    cfg = E.load("_randomness")
    months = st["months_total"]
    macro = _macro(cfg, months)
    keep = st["month"]
    if keep >= months:
        return None
    out = []
    for i in range(n):
        env = sample_env(pack, cfg, months, f"{st['sid']}|f{i}", macro,
                         base=st["env"], keep=keep, snap=_snap(st))
        rows = E.simulate(pack, actions=st["actions"], events=env["events"], months=months,
                          growth=env["growth"], volume_noise=env["noise"], gating=True)
        death = next((r["月"] for r in rows[keep:] if r["现金"] < 0), None)
        out.append({"final": _final_worth(rows[-1]), "death": death,
                    "rows": rows, "events": env["events"]})
    return out


def _pct(vals, q):
    vals = sorted(vals)
    return vals[max(0, min(len(vals) - 1, int(q * (len(vals) - 1))))]


# ────────────────────────── 结论层（report）──────────────────────────
# 所有结论必须能从模拟数据里算出来，并写明计算依据。
# 不给"感觉上应该"，只给"在这 N 条未来里，这样做比不这样做好多少"。
def levers(st, paths_base, n=120):
    """把现在能做的动作逐个加进未来，看哪个真的改变终局分布。
    成败两种情形按快照修正后的成功率加权——不给 risky 动作白送成功。"""
    month = st["month"] + 1
    snap0 = snapshot(st)
    opts, _, _ = build_options(st, month, snap0)
    cands = [o for o in opts if o["affordable"] and not any("负杠杆" in w for w in o["warnings"])]
    base_p50 = _pct([p["final"] for p in paths_base], 0.5)
    base_surv = 1 - sum(1 for p in paths_base if p["death"]) / len(paths_base)
    pack = _pack(st)
    cfg = E.load("_randomness")
    months = st["months_total"]
    macro = _macro(cfg, months)
    keep = st["month"]

    out = []
    for o in cands[:12]:
        params = _snap_defaults(st).get(o["id"], {})
        p = _eff_p(st, o["id"])
        finals, dead = [], 0
        for i in range(n):
            env = sample_env(pack, cfg, months, f"{st['sid']}|L{o['id']}{i}", macro,
                             base=st["env"], keep=keep, snap=_snap(st))
            for ok in ((True, p), (False, 1 - p)):
                if ok[1] <= 0:
                    continue
                acts = st["actions"] + [{"at": month, "id": o["id"], "params": params,
                                         **({"failed": True} if not ok[0] else {})}]
                rows = E.simulate(pack, actions=acts, events=env["events"], months=months,
                                  growth=env["growth"], volume_noise=env["noise"], gating=True)
                finals.append((_final_worth(rows[-1]), ok[1]))
                if any(r["现金"] < 0 for r in rows[keep:]):
                    dead += ok[1]
        p50 = _pct([f for f, _ in finals], 0.5)
        surv = 1 - dead / max(1e-9, sum(w for _, w in finals))
        out.append({"id": o["id"], "name": o["name"], "p": p,
                    "d_p50": p50 - base_p50, "d_surv": surv - base_surv})
    out.sort(key=lambda x: -(x["d_p50"] + x["d_surv"] * 50000))
    return out


def risk_register(st, paths):
    """风险登记册：三类来源，全部量化，全部给对冲手段。
    ① 结构脆弱（baseline 暴露度）② 快照事件（概率×力度）③ 死亡路径归因。"""
    pack = _pack(st)
    b = pack["baseline"]
    reg = []

    struct = [
        ("channel_concentration", "客流集中度", "单一渠道/商圈一抖你就抖",
         "开新渠道（A-GROW-02）或会员体系（A-GROW-04）"),
        ("key_person_dependency", "关键人依赖", "走一个人，品质跟着走",
         "提前做配方/SOP 固化；裁员类动作是反向操作"),
        ("quality_visibility", "品质可察觉度", "降规格会被客人记住",
         "换供应商（A-COST-03）在你这里是高危动作"),
        ("regulatory_intensity", "监管强度", "被检查频次高，违规成本大",
         "合规加固（A-RISK-01）买的是不出事"),
        ("competition_intensity", "竞争密度", "替代品就在隔壁，提价空间被压死",
         "差异化（新品/会员）比提价更安全"),
    ]
    for k, label, why, hedge in struct:
        v = b.get(k)
        if v is not None and v >= 0.55:
            reg.append({"src": "结构", "name": label, "level": v, "why": why, "hedge": hedge})

    snap_ev = (((_snap(st) or {}).get("env") or {}).get("event_catalog")) or []
    # 槽位的"坏方向"不同：量/价/产能是跌坏，成本/费率是涨坏，一次性现金取负坏。
    # 不分方向全列进风险册，会把政府补贴这种好事也当成风险。
    BAD_NEG = {"volume", "price", "revenue", "capacity", "cash", "valuation"}
    BAD_POS = {"unit_cost", "var_rate", "fixed_cost", "churn"}
    for e in snap_ev:
        d = e.get("delta")
        mag = (abs(d[0]) + abs(d[1])) / 2 if isinstance(d, list) else abs(d)
        sign = ((d[0] + d[1]) / 2 if isinstance(d, list) else d)
        slot = e.get("slot")
        is_bad = (slot in BAD_NEG and sign < 0) or (slot in BAD_POS and sign > 0) \
            or (slot == "cash_once" and sign < 0)
        exp = e.get("p", 0.5) * mag
        if is_bad and exp >= 0.03 and slot != "cash_once":
            reg.append({"src": "环境", "name": e["name"], "level": min(1.0, exp * 5),
                        "why": f"概率 {e.get('p', 0.5):.0%} × 力度约 {mag:.0%}（{e.get('evidence', '').split('：')[0]}）",
                        "hedge": "见下方动作建议"})

    deaths = [p for p in paths if p["death"]]
    if deaths:
        from collections import Counter
        c = Counter()
        for pth in deaths:
            for e in pth["events"]:
                if abs(e["start"] - pth["death"]) <= 2:
                    c[e.get("name", e["id"])] += 1
        top = "、".join(f"{n}（{cnt / len(deaths):.0%}）" for n, cnt in c.most_common(3))
        reg.append({"src": "归因", "name": f"{len(deaths)} 条死亡路径的直接凶器",
                    "level": min(1.0, len(deaths) / len(paths) * 3),
                    "why": f"死亡前两个月内最常在场的冲击：{top}", "hedge": "跑道 ≥ 6 个月是通用解"})
    reg.sort(key=lambda x: -x["level"])
    return reg


def verdict(st, paths, lev):
    """投资结论。规则全部写在这里，数字全部来自模拟——可以不同意规则，
    但不能说结论是拍脑袋。"""
    keep = st["month"]
    months = st["months_total"]
    remain = max(1, months - keep)
    snap = snapshot(st)
    # 两套口径，别混：
    #   worth_now —— 投资口径（现金 + 资产 − 预收）：这笔钱现在值多少
    #   cash_now  —— 流动性口径：明天会不会断粮
    worth_now = snap["net_worth"]
    cash_now = snap["cash"]
    capital = max(1.0, snap["capital"])     # 回报率的分母 = 你一共占用了多少钱
    finals = [p["final"] for p in paths]
    p50, p5 = _pct(finals, 0.5), _pct(finals, 0.05)
    surv = 1 - sum(1 for p in paths if p["death"]) / len(paths)
    gain = p50 - worth_now
    # 年化必须除以总投入（含装修设备转让费），不能除以账上现金——
    # 除以现金会算出几百个点的年化，那是会计错误，不是好生意。
    ann = (p50 - capital) / capital / (remain / 12)
    BENCH = 0.03          # 机会成本基准：无风险理财年化（假设，可调）
    tail_loss = max(0.0, (worth_now - p5)) / max(1.0, cash_now)
    util = snap["util"]

    lines = []
    if surv < 0.6:
        lines.append(f"**不建议继续投入**：未来 {remain} 个月只有 {surv:.0%} 的路径能活。"
                     "先解决活下去的问题（现金、跑道），再谈盈利。")
    elif gain < 0:
        lines.append(f"**照常经营是在销毁价值**：中位路径收在 {money(p50)}，"
                     f"比现在的 {money(worth_now)} 还少 {money(-gain)}。"
                     "要么做改变结构的动作，要么认真考虑退出（A-STR-02）。")
    elif ann < BENCH:
        lines.append(f"**能活，但不赚钱**：存活率 {surv:.0%}，中位年化回报 {ann:.1%}，"
                     f"跑不赢 {BENCH:.0%} 的无风险基准。这家店目前更像一份给自己发工资的工作，"
                     "不是一笔好投资——除非你能执行右边那几个正杠杆动作。")
    else:
        lines.append(f"**结构是健康的**：存活率 {surv:.0%}，中位年化回报 {ann:.1%}，"
                     f"超过 {BENCH:.0%} 基准，值得继续做。")
        if util >= 0.9 and tail_loss < 0.3:
            lines.append(f"产能利用率 {util:.0%} 已顶满、左尾可控——"
                         "**追加产能类投资是可取的**（它把装不下的需求变成钱）。")
        else:
            lines.append(f"但**不建议现在追加固定资产**"
                         f"{'（产能还没打满，加了也是白背成本）' if util < 0.9 else '（左尾太大，先降风险）'}："
                         "把钱花在轻资产杠杆上，见第三节。")
    if tail_loss >= 0.3:
        lines.append(f"⚠️ 左尾警告：5% 的坏世界里，剩下的 {remain} 个月会亏掉 "
                     f"{money(worth_now - p5)}（现金的 {tail_loss:.0%}）。"
                     "如果这笔钱亏不起，现在的敞口就太大了。")
    if ann > 1.0:
        lines.append(f"⚠️ 关于那个 {ann:.0%} 的年化：分母是总投入 {money(capital)}，"
                     "如果这个数比你以为的小（比如漏了装修设备），年化就被抬高了——"
                     "先把投入结构填准，再看回报率。")
    lines.append(f"投入口径：总占用资金 {money(capital)}（现金 + 资产），"
                 f"现在净资产 {money(worth_now)}，其中资产账面 {money(snap['assets'])}、"
                 f"此刻退出可回收约 {money(snap['salvage'])}。")
    if lev and lev[0]["d_p50"] > 0:
        top = lev[0]
        lines.append(f"当前最值得做的一手：**{top['name']}**——在模拟里它把中位终局抬了 "
                     f"{money(top['d_p50'])}（成功率加权后），是测量出来的，不是猜的。")
    return {"surv": surv, "p50": p50, "p5": p5, "ann": ann,
            "tail_loss": tail_loss, "lines": lines,
            "capital": capital, "worth": worth_now,
            "assets": snap["assets"], "salvage": snap["salvage"]}


def _capital_section(st, pack, sec_no):
    """这笔钱去哪了：投入结构、现在的净资产、以及此刻退出能收回多少。

    这一段存在的理由：只报「现金从 30 万变成 16 万」，用户会以为亏了 14 万；
    但那 30 万里可能有 20 万变成了装修设备和押金，它们没消失，只是换了形态。
    反过来也一样：如果投入结构里根本没有装修，说明这家店的装修是别人出的钱，
    那份便宜迟早要在转让费或者别处还回去。
    """
    o = pack["operations"]
    a = o.get("assets") or {}
    m = st["month"]
    snap = snapshot(st)
    pol = E.asset_policy(pack)
    lib = {"transfer": "转让费", "fitout": "装修", "equipment": "设备",
           "deposit": "押金", "other": "其他"}
    L = [f"### {sec_no[0]}、这笔钱在哪"]
    if not a:
        L.append("")
        L.append("- 这一局没有登记资产结构，以下只按现金流口径。"
                 "（开局时填「转让费/装修/设备/押金」，才能算出退出能收回多少。）")
        L.append(f"- 现金 {money(snap['cash'])}，扣预收负债 = 净现金 {money(snap['net'])}")
        return L

    L.append("")
    L.append("| 投入项 | 原始投入 | 现在账面 | 退出回收率 | 现在变现 |")
    L.append("|---|---|---|---|---|")
    tot_book = tot_salv = 0.0
    for k, v in a.items():
        life = (pol.get(k) or {}).get("life", 0)
        left = 1.0 if life <= 0 else max(0.0, 1.0 - m / float(life))
        book = float(v) * left
        rec = (pol.get(k) or {}).get("recovery", 0.3)
        tot_book += book
        tot_salv += book * rec
        L.append(f"| {lib.get(k, k)} | {money(v)} | {money(book)} | {rec:.0%} | {money(book * rec)} |")
    cap_assets = sum(float(v) for v in a.values())
    L.append(f"| **合计** | **{money(cap_assets)}** | **{money(tot_book)}** | — | **{money(tot_salv)}** |")
    L.append("")
    capital = E.committed_capital(pack)
    worth = snap["net_worth"]
    # 开局那一刻的净资产：现金 + 资产 − 该退的预收。这是"你买到的东西值多少"。
    open_worth = float(o.get("cash", 0)) + E.asset_book(pack, 0) - float(o.get("liability", 0) or 0)
    price_paid = open_worth - capital          # 负数 = 你付的钱超过拿到的净资产
    run_delta = worth - open_worth             # 开局之后经营造成的增减
    L.append(f"- 总占用资金：**{money(capital)}**（开业现金 {money(o.get('cash', 0))} + 资产 {money(cap_assets)}）"
             "——这是回报率的分母")
    liab_now = snap["cash"] - snap["net"]      # 当前还没履约的预收余额（不是开业时的那个）
    L.append(f"- 现在净资产：现金 {money(snap['cash'])} + 资产 {money(snap['assets'])}"
             f" − 预收 {money(liab_now)} = **{money(worth)}**")
    if abs(liab_now - float(o.get("liability", 0) or 0)) > 1:
        L.append(f"  （预收从开业的 {money(o.get('liability', 0) or 0)} 摊到 {money(liab_now)}："
                 "收到的钱在逐月变成收入，负债在减少）")
    L.append(f"- 开局那天净资产 {money(open_worth)}"
             + (f"——比你付的 {money(capital)} 少 {money(-price_paid)}："
                "这部分不是亏损，是你为位置/客流/装修付的价，或者接过来的隐性负债。"
                if price_paid < -1 else "——和你的投入基本对得上。"))
    cash_delta = snap["cash"] - float(o.get("cash", 0))
    dep_delta = snap["assets"] - E.asset_book(pack, 0)          # 折旧（负数）
    liab_delta = float(o.get("liability", 0) or 0) - liab_now    # 预收摊销（正数 = 净资产增加）
    L.append(f"- **经营至今（第 0 月 → 现在）：{money(run_delta)}**"
             f"（{run_delta / max(1.0, open_worth):+.1%}）"
             + ("　这是纯经营成绩，不含你付的溢价。" if abs(price_paid) > 1 else ""))
    L.append(f"  = 现金 {money(cash_delta)} + 资产折旧 {money(dep_delta)}"
             + (f" + 预收摊销 {money(liab_delta)}" if abs(liab_delta) > 1 else "")
             + "　——所以「净利」为正是可能的：它不含折旧，而折旧是真的损失")
    refund_rate = (pack.get("prepaid") or {}).get("refund_on_exit", 0.0)
    refund = liab_now * refund_rate
    net_exit = snap["salvage"] - refund
    L.append(f"- 此刻整体退出：资产变现 {money(snap['salvage'])} − 退预收 {money(refund)}"
             f" = **净回收 {money(net_exit)}**，加上手上现金后能拿回 "
             f"{money(snap['cash'] + net_exit)}（占投入的 {(snap['cash'] + net_exit) / max(1.0, capital):.0%}）")
    L.append("")
    L.append("> 折旧不进利润表：这个沙盒的「净利」是现金流口径，装修款在开业那天就付掉了。"
             "所以别拿净利去和会计利润比，也别把「资产账面」当成能马上变现的钱。")
    return L


def render_report(st):
    pack = _pack(st)
    o = pack["operations"]
    snap0 = snapshot(st)
    paths = forecast_paths(st, 400)
    L = [f"## 经营评估报告 · {pack['meta']['name']}", ""]
    L.append(f"*第 {st['month']}/{st['months_total']} 个月，基于 400 条可能的未来*")
    L.append("")
    if st.get("end_kind") == "user":
        left = max(0, st["months_total"] - st["month"])
        L.append(f"> 本局在第 {st['month']} 个月由你主动结束，剩下 {left} 个月没有跑。"
                 "下面的数字分两类：**账面**是已经发生的事实，**未来分布**是按现状"
                 "继续经营会怎样——后者仍然是推演，不是承诺。")
        L.append("")

    # ⓿ 这笔钱在哪：用户第一个要问的问题，不是"我赚了多少"，是"我投的钱去哪了"
    sec_no = [1]
    L.extend(_capital_section(st, pack, sec_no))
    L.append("")

    # ① 当前盈利率
    sec_no[0] += 1
    L.append(f"### {sec_no[0]}、盈利率（当前环境）")
    rows, mon, _ = rerun(st)
    if st["month"] >= 1:
        r, mt = rows[st["month"] - 1], mon[st["month"] - 1]
        margin = r["净利"] / max(1.0, r["营收"])
        L.append(f"- 上月净利率：**{margin:.1%}**（净利 {r['净利']:+,.0f} / 营收 {r['营收']:,.0f}）")
        L.append(f"- 毛利率：{(r['营收'] - mt['货品']) / max(1.0, r['营收']):.1%}，"
                 f"固定成本吃掉营收的 {mt['固定'] / max(1.0, r['营收']):.1%}")
    if paths:
        med_margin = _pct([sum(x["营收"] and (x["净利"] / x["营收"]) for x in p["rows"][st["month"]:])
                           / max(1, st["months_total"] - st["month"]) for p in paths], 0.5)
        L.append(f"- 稳态净利率（未来中位）：**{med_margin:.1%}**"
                 f"——{'健康' if med_margin >= 0.10 else '偏薄，抗风险能力弱' if med_margin >= 0 else '亏损结构'}")
    L.append("")

    if not paths:
        L.append("赛季已结束，看 audit（反事实审计）复盘每一手的价值。")
        return "\n".join(L)

    # ② 未来分布
    v = verdict(st, paths, None)
    sec_no[0] += 1
    L.append(f"### {sec_no[0]}、未来分布（400 条路径）")
    L.append(f"- 存活率 **{v['surv']:.0%}**｜中位结局 {money(v['p50'])}｜最差 5% {money(v['p5'])}")
    L.append(f"- 中位年化回报 **{v['ann']:.1%}**（基准：无风险理财 3%）")
    L.append("")

    # ③ 杠杆
    lev = levers(st, paths)
    sec_no[0] += 1
    L.append(f"### {sec_no[0]}、增加盈利：此刻最管用的三手（模拟测量，非建议清单）")
    L.append("")
    L.append("| 动作 | 中位终局变化 | 存活率变化 | 把握 |")
    L.append("|---|---|---|---|")
    for x in lev[:3]:
        L.append(f"| {x['name']} | {x['d_p50']:+,.0f} | {x['d_surv']:+.0%} | {confidence(x['p'])} |")
    L.append("")

    # ④ 风险
    reg = risk_register(st, paths)
    sec_no[0] += 1
    L.append(f"### {sec_no[0]}、风险点（按严重度排序）")
    L.append("")
    L.append("| 来源 | 风险 | 为什么危险 | 怎么对冲 |")
    L.append("|---|---|---|---|")
    for r in reg[:6]:
        L.append(f"| {r['src']} | {r['name']} | {r['why']} | {r['hedge']} |")
    L.append("")

    # ⑤ 结论的脆弱性：哪些输入是估的，结论就对哪些数字敏感
    fallbacks = [n for n in pack.get("_intake_notes", [])
                 if "兜底表" in n or "默认条款" in n or "待抓取覆盖" in n]
    if fallbacks or not _snap(st):
        sec_no[0] += 1
        L.append(f"### {sec_no[0]}、这些结论对什么敏感（诚实声明）")
        L.append("")
        if not _snap(st):
            L.append("- 本局**没有联网抓取**：事件与增长来自内部模型，"
                     "不代表你所在城市此刻的真实环境。")
        for f in fallbacks:
            L.append(f"- {f.split('；')[0]} —— 这是兜底估计，不是抓到的当地数据；"
                     "如果真实值差得远，上面的盈利率和结论都要重算。")
        L.append("")

    # ⑥ 结论
    v = verdict(st, paths, lev)
    sec_no[0] += 1
    L.append(f"### {sec_no[0]}、结论：这笔钱该不该继续投")
    L.append("")
    for ln in v["lines"]:
        L.append(f"- {ln}")
    return "\n".join(L)


def cmd_report(args):
    st = load_session(args.sid)
    print(render_report(st))


def cmd_end(args):
    st = load_session(args.sid)
    if st.get("over"):
        sys.exit(f"这一局已经结束了：{st.get('end_reason')}")
    end_session(st, args.reason)
    save_session(st)
    print(f"## 已结束\n\n**{st['end_reason']}**\n")
    print(render_forecast(forecast(st, 300), st["months_total"]))
    print("\n想看结论：python3 scripts/session.py report \"" + st["sid"] + "\"")


def cmd_resume(args):
    st = load_session(args.sid)
    try:
        resume_session(st)
    except ValueError as e:
        sys.exit(str(e))
    save_session(st)
    print(f"## 继续\n\n从第 {st['month']} 个月之后接上\n")
    print(render_point(st))


# ────────────────────────── 渲染 ──────────────────────────
def money(v):
    return f"¥{v:,.0f}"


def _lex(pack):
    """业态话术：订阅制（健身房，days=1）按会员/月费说，到店制按客流/客单说。"""
    if pack["operations"].get("days", 30) == 1:
        return {"vol": "活跃会员", "vol_unit": "会员", "price": "月费", "material": "耗材",
                "be_unit": "会员"}
    return {"vol": "日客流", "vol_unit": "单/天", "price": "客单", "material": "食材",
            "be_unit": "单/天"}


def render_intro(pack):
    """开局简报：只报老板本来就知道的事实（房租占比、客流、几个脆弱点），
    不报任何"将会发生什么"——那是剧透，不是信息。"""
    b, o = pack["baseline"], pack["operations"]
    lx = _lex(pack)
    rev = o["price"] * o["volume"] * o["days"]
    be = E.breakeven(pack)
    L = ["### 你接手的是这样一家店", ""]
    L.append(f"- 月营收约 ¥{rev:,.0f}，月固定成本 ¥{o['fixed_cost']:,.0f}，手上现金 ¥{o['cash']:,.0f}")
    L.append(f"- 成本结构：房租占收入 {b.get('rent_cost_share', 0):.0%}、"
             f"人力 {b.get('labor_cost_share', 0):.0%}、"
             f"{lx['material']} {b.get('material_cost_share', 0):.0%}")
    L.append(f"- 盈亏平衡 {be:.0f} {lx['be_unit']}，现在 {o['volume']} {lx['be_unit']}"
             f"（安全边际 {(o['volume'] - be) / max(1e-9, o['volume']):+.0%}）")
    if o.get("liability", 0) > 0:
        L.append(f"- ⚠️ 预收负债 ¥{o['liability']:,.0f}：已收未履约的年卡钱，"
                 f"现金 ¥{o['cash']:,.0f} 里有一部分不完全是你的")
    L.append(f"- 已知的脆弱处：客流靠单一商圈 {b.get('channel_concentration', 0):.0%}、"
             f"关键人依赖 {b.get('key_person_dependency', 0):.0%}、"
             f"客人能察觉品质变化 {b.get('quality_visibility', 0):.0%}")
    return "\n".join(L)


def render_point(st):
    """输出当前决策点（Markdown，直接给人看）。"""
    pack = _pack(st)
    snap = snapshot(st)
    month = st.get("pending") or (st["month"] + 1)
    opts, blocked, slots = build_options(st, month, snap)
    # 触发理由每次都重算：它是纯函数，缓存下来只会在改规则后显示旧文案
    if st.get("pending"):
        rows, mon, _ = rerun(st)
        st["pending_triggers"] = triggers(st, rows, mon, st["pending"])
    trig = st.get("pending_triggers", [])

    L = []
    L.append(f"## 第 {month} 个月 · {pack['meta']['name']}")
    if st["month"] == 0 and not st["decisions"]:
        L.append("")
        L.append(render_intro(pack))
    if trig:
        L.append("")
        for code, why in trig:
            L.append(f"> **{why}**")
    L.append("")
    L.append(f"现金 {money(snap['cash'])} · 净现金 {money(snap['net'])} · "
             f"跑道 {snap['runway']:.1f} 个月")
    lx = _lex(pack)
    L.append(f"上月：{lx['vol']} {snap['vol']:.0f} · {lx['price']} ¥{snap['price']:.0f} · "
             f"产能利用 {snap['util']:.0%}")
    if snap["month"]:
        p = snap["profit"]
        L.append(f"上月净利 {'+' if p >= 0 else '−'}{money(abs(p))}")
        # 距上次决策隔了几个月，用户可能已经忘了结构变了什么。
        # 这里只报"你自己能看见"的三件事：客单、单杯货品成本、月固定成本。
        last = max([d["at"] for d in st["decisions"]] or [1])
        if snap["month"] > last:
            _, mon, _ = rerun(st)
            a, b = mon[last - 1], mon[snap["month"] - 1]
            bits = []
            lxs = _lex(pack)
            if abs(b["价"] - a["价"]) >= 0.3:
                bits.append(f"{lxs['price']} ¥{a['价']:.0f}→¥{b['价']:.0f}")
            dys = pack["operations"]["days"]
            ua = a["货品"] / max(1e-9, a["成交"] * dys)
            ub = b["货品"] / max(1e-9, b["成交"] * dys)
            if abs(ub - ua) / max(1e-9, ua) >= 0.03:
                bits.append(f"单{lxs['be_unit']}{lxs['material']}成本 ¥{ua:.2f}→¥{ub:.2f}")
            if abs(b["固定"] - a["固定"]) / max(1e-9, a["固定"]) >= 0.03:
                bits.append(f"月固定成本 ¥{a['固定']:,.0f}→¥{b['固定']:,.0f}")
            if bits:
                L.append("自上次决策：" + "、".join(bits))
    L.append("")
    L.append(f"**本月你能推 {slots} 个动作。** 排序只反映「当下更相关」，不代表更划算。")
    L.append("")
    L.append("| # | 动作 | 族 | 代价 | 把握 | 一句话 |")
    L.append("|---|---|---|---|---|---|")
    for i, o in enumerate(opts, 1):
        cost = money(o["one_time"]) if o["one_time"] > 0 else "—"
        warn = " ⚠️" if o["warnings"] else ""
        L.append(f"| {i} | {o['name']}{warn} | {o['family_label']} | {cost} | "
                 f"{o['confidence']} | {o['blurb']} |")
    L.append("| 0 | 什么都不做 | — | — | — | 观望也是一种决策 |")
    if blocked:
        L.append("")
        L.append("**这些动作你现在做不了：** " + "；".join(
            f"{b['name']}（{b['why']}）" for b in blocked[:6]))
    warns = [f"{o['name']}：{'、'.join(o['warnings'])}" for o in opts if o["warnings"]]
    if warns:
        L.append("")
        for w in warns[:4]:
            L.append(f"- ⚠️ {w}")
    intels = [f"{o['name']}：{o['intel']}" for o in opts if o.get("intel")]
    if intels:
        L.append("")
        L.append("**这次抓取到的、和动作直接相关的现实：**")
        for t in intels:
            L.append(f"- {t}")
    return "\n".join(L)


def render_month(res):
    L = [f"### 第 {res['month']} 个月结算", ""]
    L.append(f"- {res['story']}")
    if res["accepted"]:
        for a in res["accepted"]:
            L.append(f"- {a['name']}：{'成了' if a['ok'] else '**失败了**（付了成本，效果没来）'}")
    for r in res["rejected"]:
        L.append(f"- {r['id']} 未执行：{r['why']}")
    L.append(f"- 本月净利 {'+' if res['profit'] >= 0 else '−'}{money(abs(res['profit']))}"
             f"　现金 {money(res['cash_after'])}（{'−' if res['cash_after'] < res['cash_before'] else '+'}"
             f"{money(abs(res['cash_after'] - res['cash_before']))}）")
    return "\n".join(L)


def render_forecast(fc, months=24):
    if fc is None:
        return "已经走到最后一个月，没有「未来」可算了。"
    return (f"**从现在起走到第 {months} 个月，{fc['n']} 条可能的未来：**\n\n"
            f"- 活下来：{fc['survival']:.0%}\n"
            f"- 中位结局：{money(fc['p50'])} 净现金\n"
            f"- 最差 5%：{money(fc['p5'])}\n"
            f"- 最好 5%：{money(fc['p95'])}\n\n"
            f"不要只看中位数。一家店有 5% 的概率收在 {money(fc['p5'])}，"
            f"这个数字才是你晚上睡不着的原因。")


def state_line(st):
    snap = snapshot(st)
    return (f"第 {st['month']}/{st['months_total']} 月 · 现金 {money(snap['cash'])} · "
            f"净现金 {money(snap['net'])} · 跑道 {snap['runway']:.1f} 月")


# ────────────────────────── AI 经营策略 ──────────────────────────
# 只看用户也能看到的东西（现金、跑道、利用率、警告），不看未来账本。
# 这是"AI 在沙盒里替你经营"的最小实现：策略公开、可替换、可被用户随时接管。
def pick(st, opts, snap):
    ok = [o for o in opts if o["affordable"] and not o["warnings"]]
    if not ok:
        return []
    ids = {o["id"]: o for o in ok}

    def first(*cands):
        for c in cands:
            if c in ids:
                return {"id": c, "params": {}}
        return None

    plan = []
    if snap["runway"] < 3:
        plan = [first("A-COST-02", "A-COST-04", "A-FIN-02", "A-COST-01")]
    elif snap["util"] >= 0.90:
        plan = [first("A-CAP-01", "A-PRICE-01", "A-GROW-02")]
    else:
        plan = [first("A-GROW-02", "A-GROW-03", "A-PRICE-01")]

    # 第二个带宽位用来做不冲突的加固：增长期顺便降本，危机期顺便保客
    if snap["runway"] < 3:
        plan.append(first("A-COST-04", "A-PRICE-01"))
    else:
        plan.append(first("A-COST-04", "A-GROW-04"))

    out, seen = [], set()
    for c in plan:
        if c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out[:2]


def cmd_auto(args):
    st = load_session(args.sid)
    step = 0
    while not st.get("over") and step < 60:
        step += 1
        snap = snapshot(st)
        month = st.get("pending") or (st["month"] + 1)
        opts, _, _ = build_options(st, month, snap)
        choices = [] if args.policy == "none" else pick(st, opts, snap)
        if st["month"] + 1 != month:
            st["month"] = month - 1
        res = apply_choices(st, choices)
        st["decisions"].append(_decision(st, res, choices))
        picked = "、".join(a["name"] + ("" if a["ok"] else "(失败)") for a in res["accepted"]) or "观望"
        print(f"第 {res['month']:>2} 月 | {picked:<28} | 净利 {res['profit']:+9,.0f} | "
              f"现金 {res['cash_after']:>10,.0f} | 利用 {res['util']:.0%}")
        if st.get("over"):
            break
        rows, mon, _ = rerun(st)
        m, trig = next_point(st, rows, mon)
        if m is None:
            break
        if rows[m - 1]["现金"] < 0:
            st["month"], st["over"], st["death_month"] = m, True, m
            st["end_reason"] = f"流动性死亡：第 {m} 个月现金归零"
            break
        st["pending"], st["pending_triggers"] = m, trig
        save_session(st)

    save_session(st)
    print()
    if st.get("over"):
        print(f"**结束**：{st['end_reason']}（第 {st.get('death_month', st['month'])} 个月）")
    else:
        print(f"**跑到第 {st['month']} 个月**")
    print()
    print(render_forecast(forecast(st, 400), st["months_total"]))


# ────────────────────────── 命令 ──────────────────────────
def cmd_new(args):
    E.build()
    pack = E.load_pack(args.pack)
    cfg = E.load("_randomness")
    months = args.months
    # --snapshot：创建那一刻的"真实世界"。读进来就冻结，存进会话文件，
    # 之后 24 个月的事件目录、增长区间、动作修正全部从它采样，不再联网。
    snap = None
    if getattr(args, "snapshot", None):
        with open(args.snapshot, encoding="utf-8") as f:
            snap = json.load(f)
        assert snap.get("meta", {}).get("asof"), "快照缺少 meta.asof"

    # --intake：开局信息采集。用户只回答事实，这里把事实派生成定制包，
    # 替换掉 demo 原型——店是用户的店，不是演示店。
    intake = None
    if getattr(args, "intake", None):
        import intake as I
        intake = I.load_intake(args.intake)
        arch = I.ARCHETYPE.get(intake.get("industry", ""), args.pack)
        base = E.load_pack(arch)
        derived = (snap or {}).get("derived") or {}
        pack = I.derive_pack(intake, base, derived)
        args.pack = arch
        months = pack.pop("_intake_months", months)

    macro = _macro(cfg, months)
    env = sample_env(pack, cfg, months, f"{args.pack}|{args.seed}", macro, snap=snap)

    st = {
        "sid": args.name or f"{args.pack}-{args.seed}",
        "pack": args.pack, "pack_name": pack["meta"]["name"],
        "seed": args.seed, "months_total": months, "month": 0,
        "cadence": args.cadence, "env": env, "actions": [], "decisions": [], "locks": [],
        "over": False, "created_at": None,
        **({"snapshot": snap} if snap else {}),
        **({"intake": intake, "pack_data": pack} if intake else {}),
    }
    rows, mon, _ = rerun(st)
    m, trig = next_point(st, rows, mon)
    # 第一次决策点之前的日子按"什么都没做"先过完。经营是从半路开始的，
    # 不是从第 1 个月递推出来的——用户拿到的是一个已经在跑的店。
    st["month"] = m - 1
    st["pending"] = m
    st["pending_triggers"] = trig
    save_session(st)
    if intake:
        print("### 你的店是这样算出来的\n")
        for nt in pack.get("_intake_notes", []):
            print(f"- {nt}")
        print()
    if snap:
        print(render_brief(snap))
        print()
    if st["month"] > 0:
        print(f"*（第 1–{st['month']} 个月按现状经营，时间快进。）*\n")
    print(render_point(st))
    return st


def cmd_show(args):
    st = load_session(args.sid)
    if st.get("over"):
        print(f"**已结束**：{st['end_reason']}（第 {st['death_month']} 个月）")
        return
    # 重算下一个真正的决策点：改完规则后，之前缓存的那个点可能已经不成立了
    rows, mon, _ = rerun(st)
    m, trig = next_point(st, rows, mon)
    if m and m > st["month"]:
        skipped_from = st["month"] + 1
        st["month"] = m - 1          # 中间月份按"不动作"走完，状态行才对得上
        st["pending"], st["pending_triggers"] = m, trig
        save_session(st)
        if m > skipped_from:
            span = f"第 {skipped_from} 个月" if m - 1 == skipped_from else f"第 {skipped_from}–{m - 1} 个月"
            print(f"*（{span}没有值得打断你的事，时间快进。）*\n")
    print(render_point(st))


def parse_choice(tok):
    """A-PRICE-01:price_delta=0.08,reduction=0.12 → {"id":.., "params":{..}}"""
    if ":" in tok:
        aid, rest = tok.split(":", 1)
    else:
        aid, rest = tok, ""
    params = {}
    for kv in [x for x in rest.split(",") if x]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    return {"id": aid, "params": params}


def _decision(st, res, choices):
    """一条决策日志必须足以完整重放：只存动作 id 不够，参数不同就是另一个决策。"""
    return {
        "at": res["month"], "triggers": st.get("pending_triggers", []),
        "chosen": [{"id": a["id"],
                    "params": next((c.get("params", {}) for c in choices if c["id"] == a["id"]), {}),
                    "ok": a["ok"]} for a in res["accepted"]],
        "rejected": res["rejected"],
        "cash": res["cash_after"], "profit": res["profit"],
    }


def cmd_choose(args):
    st = load_session(args.sid)
    if st.get("over"):
        sys.exit(f"这个会话已经结束：{st['end_reason']}。开一个新的吧。")

    target = st.get("pending") or (st["month"] + 1)
    # 把用户的选择挂到目标月；中间未被打断的月份按"不动作"自动走完
    choices = [] if args.none else [parse_choice(t) for t in args.actions]
    if st["month"] + 1 != target:
        st["month"] = target - 1        # 快进到决策月前一个月

    res = apply_choices(st, choices)
    st["decisions"].append(_decision(st, res, choices))
    print(render_month(res))
    print()

    if st.get("over"):
        print(f"## 结束\n\n**{st['end_reason']}**\n")
        print(render_forecast(forecast(st, 300), st["months_total"]))
        save_session(st)
        return

    rows, mon, _ = rerun(st)
    m, trig = next_point(st, rows, mon)
    if m is None:
        save_session(st)
        print(f"## 走完了 {st['months_total']} 个月\n")
        print("| 月 | 现金 | 净现金 | 净利 |\n|---|---|---|---|")
        for r in rows[-3:]:
            print(f"| {r['月']} | {money(r['现金'])} | {money(r['净现金'])} | {r['净利']:+,.0f} |")
        return
    # 死亡可能发生在"没被打断的那几个月"里：不是你没决策，是你没机会决策
    if rows[m - 1]["现金"] < 0:
        st["month"], st["over"], st["death_month"] = m, True, m
        st["end_reason"] = f"流动性死亡：第 {m} 个月现金归零。中间几个月没有值得打断你的事，但账还是走完了"
        save_session(st)
        print(f"## 结束\n\n**{st['end_reason']}**\n")
        print(render_forecast(forecast(st, 300), st["months_total"]))
        return
    skipped_from = st["month"] + 1
    st["month"] = m - 1              # 中间月份按"不动作"走完
    st["pending"], st["pending_triggers"] = m, trig
    save_session(st)
    if m > skipped_from:
        evs = [e.get("name", e["id"]) for e in st["env"]["events"]
               if skipped_from <= e["start"] < m]
        if evs:
            span = f"第 {skipped_from} 个月" if m - 1 == skipped_from else f"第 {skipped_from}–{m - 1} 个月"
            print(f"*（{span}你被上一件事锁着，其间落地：{'、'.join(evs)}。）*\n")
        else:
            print(f"*（第 {skipped_from}–{m - 1} 个月没有值得打断你的事，时间快进。）*\n")
    print(render_point(st))


def cmd_forecast(args):
    st = load_session(args.sid)
    print(render_forecast(forecast(st, args.n), st["months_total"]))


def audit(st):
    """反事实审计：同一份冻结环境里，把每个决策单独拿掉，看终局差多少。
    这是沙盒最该有的功能——不是"你得了多少分"，而是"你的哪一手真的起作用"。
    注意：动作之间有交互（拿掉一个会改变后续状态），所以这些边际贡献
    加起来不等于总差；这本身就是一条结论：决策不可加。"""
    pack = _pack(st)
    env = st["env"]

    def run(acts):
        return E.simulate(pack, actions=acts, events=env["events"], months=st["months_total"],
                          growth=env["growth"], volume_noise=env["noise"])

    base = run([])
    full = run(st["actions"])
    out = []
    for i, a in enumerate(st["actions"]):
        rest = [x for j, x in enumerate(st["actions"]) if j != i]
        r = run(rest)
        out.append({"name": a.get("name") or a["id"], "id": a["id"], "at": a["at"],
                    "failed": bool(a.get("failed")),
                    "delta": full[-1]["净现金"] - r[-1]["净现金"]})
    return base, full, out


def render_audit(st, base, full, rows):
    o = _pack(st)["operations"]
    start_net = o["cash"] - o.get("liability", 0)
    L = []
    L.append("### 反事实审计\n")
    L.append(f"- 什么都不做：{money(base[-1]['净现金'])}")
    L.append(f"- 你这一路：{money(full[-1]['净现金'])}")
    L.append(f"- 差额：**{full[-1]['净现金'] - base[-1]['净现金']:+,.0f}**"
             f"（起点 {money(start_net)}）\n")
    L.append("| 月 | 决策 | 结果 | 单独拿掉会怎样 |")
    L.append("|---|---|---|---|")
    for r in sorted(rows, key=lambda x: -abs(x["delta"])):
        tag = "失败" if r["failed"] else "成了"
        sign = "+" if r["delta"] >= 0 else "−"
        L.append(f"| {r['at']} | {r['name']} | {tag} | {sign}{money(abs(r['delta']))} |")
    L.append("")
    L.append("> 这些数加起来不等于总差额——决策之间会互相影响。"
             "拿掉「加员工」，后面「重谈房租」的意义就变了。"
             "**决策不可加**，这是它和成本项最大的区别。")
    return "\n".join(L)


def cmd_audit(args):
    st = load_session(args.sid)
    base, full, rows = audit(st)
    print(render_audit(st, base, full, rows))


def cmd_log(args):
    st = load_session(args.sid)
    print(f"## {st['pack_name']} · 决策复盘\n")
    print(f"共 {len(st['decisions'])} 个决策点，{len(st['actions'])} 个已执行动作。\n")
    print("| 月 | 触发 | 决定 | 月末现金 | 净利 |")
    print("|---|---|---|---|---|")
    for d in st["decisions"]:
        t = "、".join(x[1] for x in d["triggers"]) or "例行"
        c = "、".join(d["chosen"]) or "观望"
        print(f"| {d['at']} | {t} | {c} | {money(d['cash'])} | {d['profit']:+,.0f} |")
    if st["actions"]:
        print("\n**执行过的动作**\n")
        for a in st["actions"]:
            print(f"- 第 {a['at']} 月 · {a['name']}{'（失败）' if a.get('failed') else ''}")
    print()
    print(render_forecast(forecast(st, args.n), st["months_total"]))


def _guard(fn, *args, **kw):
    """CLI 入口：把"会话不存在"翻回人类可读的一行，不再抛栈。"""
    try:
        return fn(*args, **kw)
    except FileNotFoundError as e:
        sys.exit(str(e))


def main():
    ap = argparse.ArgumentParser(description="交互式经营会话")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="开一个新会话")
    p.add_argument("pack", choices=sorted(E.PACKS))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--months", type=int, default=24)
    p.add_argument("--cadence", choices=["event", "monthly", "quarterly"], default="event")
    p.add_argument("--name", default=None)
    p.add_argument("--snapshot", default=None,
                   help="环境快照 JSON（创建时刻抓取并冻结的真实世界，如 snapshots/cafe-2026-11-16.json）")
    p.add_argument("--intake", default=None,
                   help="开局信息采集 JSON（地点/资金/面积/客源/加盟…，派生定制行业包）")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("show", help="看当前决策点")
    p.add_argument("sid")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("choose", help="做出选择并推进")
    p.add_argument("sid")
    p.add_argument("actions", nargs="*", help="如 A-PRICE-01:price_delta=0.08")
    p.add_argument("--none", action="store_true", help="本月不动作")
    p.set_defaults(fn=cmd_choose)

    p = sub.add_parser("auto", help="让 AI 按公开策略自动经营到底")
    p.add_argument("sid")
    p.add_argument("--policy", choices=["greedy", "none"], default="greedy")
    p.set_defaults(fn=cmd_auto)

    p = sub.add_parser("forecast", help="从当前状态重跑未来")
    p.add_argument("sid")
    p.add_argument("-n", type=int, default=400)
    p.set_defaults(fn=cmd_forecast)

    p = sub.add_parser("audit", help="反事实审计：每个决策单独值多少")
    p.add_argument("sid")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("report", help="经营评估报告：盈利率/杠杆/风险点/投资结论")
    p.add_argument("sid")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("end", help="随时按停：停在当前月，不再推演（可撤回）")
    p.add_argument("sid")
    p.add_argument("--reason", default=None, help="写进报告的结束原因")
    p.set_defaults(fn=cmd_end)

    p = sub.add_parser("resume", help="撤销「主动结束」，从原来的决策点继续")
    p.add_argument("sid")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("log", help="决策复盘 + 三元组")
    p.add_argument("sid")
    p.add_argument("-n", type=int, default=400)
    p.set_defaults(fn=cmd_log)

    args = ap.parse_args()
    _guard(args.fn, args)


if __name__ == "__main__":
    main()
