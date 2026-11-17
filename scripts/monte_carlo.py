#!/usr/bin/env python3
"""蒙特卡洛层 v1 —— 把"一个可能世界"变成"一千个可能世界的分布"。

架构位置：
    _randomness.yaml（分布声明）
        ↓ 本层抽样（纯输入侧扰动）
    engine.simulate（确定性结算，不认随机数）
        ↓ 每条路径产出 summary
    本层聚合（分位数 / 存活率 / 配对差值）

配对种子（common random numbers）：
    被动场景与主动场景的第 i 次运行使用同一个环境种子，事件、增长、噪声
    完全相同；唯一区别是动作成败掷骰（独立随机流）。这样"主动 - 被动"
    的差值才能归因于动作本身，而不是"主动那一组运气好"。

用法：
    python3 scripts/monte_carlo.py cafe               # 被动 vs 主动对比
    python3 scripts/monte_carlo.py gym --runs 2000
    python3 scripts/monte_carlo.py cnrest --seed 7
"""

import argparse
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E


# ────────────────────────── 配置读取 ──────────────────────────
def load_cfg():
    return E.load("_randomness")


def action_prob_table():
    """动作 id → success_probability（来自 _actions.yaml，不在此重复声明）"""
    lib = E.load("_actions")
    return {a["id"]: a.get("success_probability", 1.0) for a in lib["actions"]}


def load_posterior():
    """动作 id → (alpha, beta)；来自 calibrate.py 导出的 build/calibration.json。
    这是"认知不确定性"层：我们不知道 p 是多少，只知道它是某个 Beta 分布。"""
    path = os.path.join(E.BUILD, "calibration.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)["actions"]


def sample_beta(rng, a, b):
    """Beta 采样：两个 Gamma 之比。不用 numpy。"""
    x = rng.gammavariate(max(a, 1e-6), 1.0)
    y = rng.gammavariate(max(b, 1e-6), 1.0)
    return x / (x + y) if (x + y) > 0 else 0.5


# ────────────────────────── 宏观状态（系统性风险）──────────────────────────
# 事件独立抽样造不出"全都不顺的一年"，左尾因此被系统性低估。
# 这里用一条三态马尔可夫链逐月决定"今年是什么年头"，由它同时调制
# 增长率、需求水平与波动、事件发生概率——一个机制，两处相关。
def prep_macro(cfg, months=24):
    """把 YAML 的调制量居中，保证"只加相关性，不加漂移"。

    居中的权重必须是**窗口内期望占用**，不是平稳分布：
    一条 24 个月的路径从 normal 出发，根本走不到平稳分布（平稳分布里收缩态占 17%，
    而前 24 个月里只占到 12% 左右）。用平稳分布居中，会让窗口内平均增长被压低
    约 0.06pp/月；在盈亏平衡附近的生意上，这点漂移会被经营杠杆放大成上万元。
    """
    mc = cfg.get("macro_regime") or {}
    if not mc.get("enabled", False):
        return None
    names = list(mc["states"].keys())
    trans = mc["transition"]
    init = mc.get("init", "normal")

    # 平稳分布：幂迭代，不引第三方库
    pi = {n: 1.0 / len(names) for n in names}
    for _ in range(500):
        new = {n: 0.0 for n in names}
        for a in names:
            for b in names:
                new[b] += pi[a] * trans[a].get(b, 0.0)
        pi = new

    # 窗口内期望占用：π_t = e_init · P^(t-1)，对 t=1..months 取平均
    v = {n: 1.0 if n == init else 0.0 for n in names}
    acc = {n: 0.0 for n in names}
    for _ in range(months):
        for n in names:
            acc[n] += v[n]
        v = {b: sum(v[a] * trans[a].get(b, 0.0) for a in names) for b in names}
    w = {n: acc[n] / months for n in names}

    st = mc["states"]
    g_mean = sum(w[n] * st[n]["growth_shift"] for n in names)
    o_mean = sum(w[n] * st[n]["noise_offset"] for n in names)
    e_mean = sum(w[n] * st[n]["event_occ_mult"] for n in names)
    return {
        "names": names,
        "trans": trans,
        "pi": pi,
        "weights": w,
        "months": months,
        "init": init,
        "growth_shift": {n: st[n]["growth_shift"] - g_mean for n in names},
        "noise_offset": {n: st[n]["noise_offset"] - o_mean for n in names},
        "sigma_mult": {n: st[n]["sigma_mult"] for n in names},
        "occ_mult": {n: st[n]["event_occ_mult"] / e_mean for n in names},
        "raw_mean": {"growth": g_mean, "noise": o_mean, "occ": e_mean},
    }


def sample_states(macro, rng, months):
    """逐月状态序列。收缩态自持 70% → 平均持续 3.3 个月，坏年份才成得了形。"""
    cur = macro["init"]
    out = []
    for _ in range(months):
        out.append(cur)
        r = rng.random()
        acc = 0.0
        nxt = macro["names"][-1]
        for n in macro["names"]:
            acc += macro["trans"][cur].get(n, 0.0)
            if r < acc:
                nxt = n
                break
        cur = nxt
    return out


# ────────────────────────── 抽样 ──────────────────────────
def sample_events(events, cfg, rng, months, macro=None, states=None):
    """对编排事件做三件事：是否发生、烈度、时间抖动。
    开启宏观状态时，发生概率再乘状态系数（收缩态坏事更多）。"""
    occ_p = cfg["event_occurrence"]["default_p"]
    mag = cfg["event_magnitude"]
    jit = cfg["event_start_jitter"]
    out = []
    for ev in events:
        p = ev.get("p", occ_p)
        if macro and states:
            idx = min(months, max(1, ev["start"])) - 1
            p = min(1.0, p * macro["occ_mult"][states[idx]])
        if rng.random() >= p:
            continue                                   # 这次没发生
        e = copy.deepcopy(ev)
        e["delta"] = e["delta"] * rng.triangular(mag["min"], mag["max"], mag["mode"])
        e["start"] = max(jit["clamp"][0],
                         min(months, e["start"] + rng.randint(jit["min"], jit["max"])))
        out.append(e)
    return out


def sample_growth(pack, rng, macro=None, states=None, months=24):
    g = pack["baseline"].get("growth_assumption", [0.0, 0.0])
    base = rng.uniform(g[0], g[1])
    if not (macro and states):
        return base
    return [base + macro["growth_shift"][states[m - 1]] for m in range(1, months + 1)]


def sample_noise(pack, cfg, rng, months, macro=None, states=None):
    sigma = pack["baseline"].get("demand_volatility", 0.05)
    lo, hi = cfg["volume_noise"]["clip"]
    out = []
    for m in range(months):
        if macro and states:
            s = states[m]
            sg = sigma * macro["sigma_mult"][s]
            off = macro["noise_offset"][s]
        else:
            sg, off = sigma, 0.0
        out.append(max(lo, min(hi, 1.0 + off + rng.gauss(0.0, sg))))
    return out


def roll_actions(actions, probs, rng, posterior=None):
    """动作成败掷骰。失败 → failed 标记，引擎按 success_roll 结算。
    posterior 非空时，p 本身从 Beta 后验抽样（认知不确定性），否则用固定先验。"""
    out = []
    for a in actions:
        a2 = copy.deepcopy(a)
        aid = a2["id"]
        if posterior and aid in posterior and not posterior[aid].get("locked"):
            p = sample_beta(rng, posterior[aid]["alpha"], posterior[aid]["beta"])
        else:
            p = probs.get(aid, 1.0)
        if p < 1.0 and rng.random() >= p:
            a2["failed"] = True
        out.append(a2)
    return out


# ────────────────────────── 单条路径 ──────────────────────────
def run_path(pack, cfg, probs, run_seed, actions, months, posterior=None, gating=True,
             macro=None):
    rng_env = random.Random(run_seed)                  # 环境流（被动/主动共享）
    # 状态用独立随机流：关闭宏观时环境抽样序列完全不变，A/B 才干净
    states = sample_states(macro, random.Random(run_seed ^ 0xC0FFEE), months) if macro else None
    events = sample_events(pack.get("demo_events", []), cfg, rng_env, months, macro, states)
    growth = sample_growth(pack, rng_env, macro, states, months)
    noise = sample_noise(pack, cfg, rng_env, months, macro, states)

    rolled = None
    if actions:
        rng_act = random.Random(run_seed ^ 0x5EED)     # 动作流（独立）
        rolled = roll_actions(actions, probs, rng_act, posterior)

    rows = E.simulate(pack, actions=rolled, events=events, months=months,
                      growth=growth, volume_noise=noise, gating=gating)
    net = [r["净现金"] for r in rows]
    cash = [r["现金"] for r in rows]
    # 两种"死法"必须分开：
    #   流动性死亡（现金 < 0，付不出账单）——任何行业都是终点
    #   资不抵债（净现金 < 0）——对预收款行业是常态而非终点：
    #   年卡负债是逐月履约摊销的，账上净资产为负不等于活不下去
    bankrupt = next((r["月"] for r in rows if r["现金"] < 0), None)
    insolvent = next((r["月"] for r in rows if r["净现金"] < 0), None)
    contract = (sum(1 for s in states if s == "contraction") / len(states)) if states else 0.0
    expand = (sum(1 for s in states if s == "expansion") / len(states)) if states else 0.0
    return {
        "final_net": net[-1],
        "final_cash": cash[-1],
        "min_net": min(net),
        "min_cash": min(cash),
        "loss_months": sum(1 for r in rows if r["净利"] < 0),
        "bankrupt_month": bankrupt,
        "insolvent_month": insolvent,
        "survived": bankrupt is None,
        "contract_share": contract,
        "expand_share": expand,
        "event_count": len(events),
    }


# ────────────────────────── 聚合 ──────────────────────────
def pct(sorted_vals, q):
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def aggregate(results):
    finals = sorted(r["final_net"] for r in results)
    mins = sorted(r["min_net"] for r in results)
    min_cash = sorted(r["min_cash"] for r in results)
    losses = sorted(r["loss_months"] for r in results)
    n = len(results)
    bankrupt = [r for r in results if not r["survived"]]
    bk_months = sorted(r["bankrupt_month"] for r in bankrupt)
    insolvent_n = sum(1 for r in results if r["insolvent_month"] is not None)
    return {
        "runs": n,
        "survival_rate": (n - len(bankrupt)) / n,
        "bankruptcy_rate": len(bankrupt) / n,
        "insolvency_rate": insolvent_n / n,
        "bankrupt_month_p50": pct(bk_months, 0.5) if bk_months else None,
        "final_net": {k: pct(finals, q) for k, q in
                      [("p5", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95)]},
        "final_net_mean": sum(finals) / n,
        "min_net": {k: pct(mins, q) for k, q in [("p5", .05), ("p50", .5), ("p95", .95)]},
        "min_cash": {k: pct(min_cash, q) for k, q in [("p5", .05), ("p50", .5), ("p95", .95)]},
        "loss_months_p50": pct(losses, 0.5),
        "regime": regime_stats(results, finals),
    }


def regime_stats(results, sorted_finals):
    """系统性风险的证据：最差 5% 路径里，"收缩年"占的月份是否明显更多。"""
    n = len(results)
    if not any(r["contract_share"] for r in results):
        return None
    cut = pct(sorted_finals, 0.05)
    worst = [r for r in results if r["final_net"] <= cut]
    mean = lambda xs: sum(xs) / max(1, len(xs))
    return {
        "contract_all": mean([r["contract_share"] for r in results]),
        "contract_worst5": mean([r["contract_share"] for r in worst]) if worst else 0.0,
        "expand_all": mean([r["expand_share"] for r in results]),
        "events_all": mean([r["event_count"] for r in results]),
        "events_worst5": mean([r["event_count"] for r in worst]) if worst else 0.0,
        "worst_n": len(worst),
    }


def paired_delta(passive, active):
    """逐对差值：同一个环境种子下，主动 - 被动。"""
    deltas = sorted(a["final_net"] - p["final_net"]
                    for p, a in zip(passive, active))
    n = len(deltas)
    return {
        "p5": pct(deltas, .05), "p25": pct(deltas, .25), "p50": pct(deltas, .5),
        "p75": pct(deltas, .75), "p95": pct(deltas, .95),
        "mean": sum(deltas) / n,
        "positive_rate": sum(1 for d in deltas if d > 0) / n,
        "worse_rate": sum(1 for d in deltas if d < 0) / n,
    }


# ────────────────────────── 主流程 ──────────────────────────
def run_mc(pack, cfg, probs, runs, seed, months=24, posterior=None,
           actions=None, gating=True, macro=None):
    """返回 (被动聚合, 主动聚合, 配对差值)。
    actions=None 时用行业包的 demo_actions；gating=False 用于对照实验。
    注意：macro 必须按同一个 months 居中过，否则漂移会回来。"""
    if macro is not None and macro.get("months") != months:
        raise ValueError("macro 的居中窗口与 months 不一致——请用 prep_macro(cfg, months)")
    use = pack.get("demo_actions", []) if actions is None else actions
    passive, active = [], []
    for i in range(runs):
        s = seed + i                                   # 配对：两组共用同一环境种子
        passive.append(run_path(pack, cfg, probs, s, None, months, posterior, gating, macro))
        active.append(run_path(pack, cfg, probs, s, use, months, posterior, gating, macro))
    return aggregate(passive), aggregate(active), paired_delta(passive, active)


def fmt(v):
    return f"{v:+,}" if isinstance(v, (int, float)) else str(v)


def print_report(name, pa, ac, d, tag="", macro=None):
    print(f"\n{'=' * 78}")
    print(f"  {name} · 蒙特卡洛 {pa['runs']} 条路径 {tag}")
    print(f"{'=' * 78}")
    if macro:
        pi = "  ".join(f"{n}={macro['pi'][n]:.2f}" for n in macro["names"])
        occ = "  ".join(f"{n}={macro['weights'][n]:.2f}" for n in macro["names"])
        rm = macro["raw_mean"]
        print(f"  宏观状态平稳分布：{pi}")
        print(f"  {macro['months']} 个月窗口内期望占用：{occ}  ← 居中按此权重，不是平稳分布")
        print(f"  居中量（窗口加权均值，已扣除）：growth {rm['growth']:+.4f}  "
              f"noise {rm['noise']:+.3f}  occ {rm['occ']:.3f}")
    print(f"  {'指标':<16}{'被动（不做动作）':>22}{'主动（demo 动作组合）':>26}")
    print("  " + "-" * 72)
    print(f"  {'存活率(现金未归零)':<16}{pa['survival_rate']:>22.1%}{ac['survival_rate']:>26.1%}")
    print(f"  {'流动性破产率':<16}{pa['bankruptcy_rate']:>22.1%}{ac['bankruptcy_rate']:>26.1%}")
    print(f"  {'资不抵债率':<16}{pa['insolvency_rate']:>22.1%}{ac['insolvency_rate']:>26.1%}")
    bm_p = f"第 {pa['bankrupt_month_p50']} 月" if pa["bankrupt_month_p50"] else "—"
    bm_a = f"第 {ac['bankrupt_month_p50']} 月" if ac["bankrupt_month_p50"] else "—"
    print(f"  {'破产月中位数':<16}{bm_p:>22}{bm_a:>26}")
    print(f"  {'亏损月数中位':<16}{pa['loss_months_p50']:>22}{ac['loss_months_p50']:>26}")
    print()
    print(f"  期末净现金分布（元）")
    print(f"  {'分位':<8}{'被动':>14}{'主动':>14}{'差值(配对)':>14}")
    print("  " + "-" * 52)
    for k in ("p5", "p25", "p50", "p75", "p95"):
        print(f"  {k:<8}{pa['final_net'][k]:>+14,}{ac['final_net'][k]:>+14,}{d[k]:>+14,}")
    print(f"  {'均值':<8}{pa['final_net_mean']:>+14,.0f}{ac['final_net_mean']:>+14,.0f}{d['mean']:>+14,.0f}")
    print()
    print(f"  路径最低点（最深的坑）：净现金 / 现金")
    print(f"  {'':<8}{'被动净现金':>13}{'被动现金':>13}{'主动净现金':>13}{'主动现金':>13}")
    for k in ("p5", "p50", "p95"):
        print(f"  {k:<8}{pa['min_net'][k]:>+13,}{pa['min_cash'][k]:>+13,}"
              f"{ac['min_net'][k]:>+13,}{ac['min_cash'][k]:>+13,}")
    print()
    print(f"  配对结论：动作让期末更好的路径占 {d['positive_rate']:.1%}，"
          f"反而更差的占 {d['worse_rate']:.1%}")
    if pa.get("regime"):
        r = pa["regime"]
        print()
        print(f"  系统性风险证据（{r['worst_n']} 条最差路径 vs 全体）")
        print(f"    收缩态月份占比：全体 {r['contract_all']:.1%}  →  最差 5% 路径 "
              f"{r['contract_worst5']:.1%}   （扩张态 {r['expand_all']:.1%}）")
        print(f"    实际发生的事件数：全体 {r['events_all']:.1f}  →  最差 5% 路径 "
              f"{r['events_worst5']:.1f} 起")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", choices=list(E.PACKS.keys()))
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--uncertain-p", action="store_true",
                    help="成功率本身从 Beta 后验抽样（认知不确定性），而非固定先验")
    ap.add_argument("--macro", dest="macro", action="store_true", default=None,
                    help="强制开启宏观状态层（默认按 _randomness.yaml 的 enabled）")
    ap.add_argument("--no-macro", dest="macro", action="store_false",
                    help="关闭宏观状态层，用于与逐月独立版本 A/B 对比")
    args = ap.parse_args()

    cfg = load_cfg()
    runs = args.runs or cfg["mc"]["runs"]
    seed = args.seed if args.seed is not None else cfg["mc"]["seed"]

    pack = E.load_pack(args.pack)
    probs = action_prob_table()
    posterior = load_posterior() if args.uncertain_p else None
    if args.uncertain_p and posterior is None:
        sys.exit("未找到 build/calibration.json，先跑 python3 scripts/calibrate.py")

    macro = prep_macro(cfg, args.months)
    if args.macro is False:
        macro = None
    elif args.macro is True and macro is None:
        cfg = copy.deepcopy(cfg)
        cfg["macro_regime"]["enabled"] = True
        macro = prep_macro(cfg, args.months)

    pa, ac, d = run_mc(pack, cfg, probs, runs, seed, args.months, posterior,
                       macro=macro)
    tags = []
    if args.uncertain_p:
        tags.append("p 从后验抽样")
    tags.append("宏观状态：开" if macro else "宏观状态：关")
    print_report(pack["meta"]["name"], pa, ac, d, tag="（" + "；".join(tags) + "）", macro=macro)


if __name__ == "__main__":
    main()
