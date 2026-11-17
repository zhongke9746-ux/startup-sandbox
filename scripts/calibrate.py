#!/usr/bin/env python3
"""动作成功率校准器 v1 —— 补 GAP-MC-04。

做的事很少，但每一件都必须做对：
  1. 把 _calibration.yaml 的 (p, n0) 编码成 Beta(α0, β0) 先验
  2. 读 data/observations.jsonl 的真实观测，做 Beta-Binomial 共轭更新
  3. 输出后验均值与 95% 区间；导出 build/calibration.json 供蒙特卡洛层采样

不引第三方库（环境无 numpy/scipy），Beta 分位数用数值积分求。

用法：
    python3 scripts/calibrate.py                 # 报告
    python3 scripts/calibrate.py --add A-COST-02 success --industry cafe
    python3 scripts/calibrate.py --export        # 只导出 JSON
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OBS_FILE = os.path.join(DATA, "observations.jsonl")
BUILD = os.path.join(ROOT, "build")


# ────────────────────────── 观测存储 ──────────────────────────
def load_observations(path=None):
    path = path or OBS_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_observation(rec):
    os.makedirs(DATA, exist_ok=True)
    with open(OBS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ────────────────────────── Beta 数学 ──────────────────────────
def beta_pdf(x, a, b, ln_beta=None):
    import math
    if x <= 0 or x >= 1:
        return 0.0
    if ln_beta is None:
        ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    return math.exp((a - 1) * math.log(x) + (b - 1) * math.log(1 - x) - ln_beta)


def beta_quantiles(a, b, qs=(0.025, 0.5, 0.975), grid=4000):
    """数值积分求分位：够用且不需要 scipy。"""
    import math
    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    xs = [(i + 0.5) / grid for i in range(grid)]
    pdf = [beta_pdf(x, a, b, ln_beta) for x in xs]
    total = sum(pdf)
    if total <= 0:
        return [a / (a + b)] * len(qs)
    cum, out, i = 0.0, [], 0
    for q in qs:
        target = q * total
        while i < len(pdf) - 1 and cum + pdf[i] < target:
            cum += pdf[i]
            i += 1
        out.append(xs[i])
    return out


def encode_prior(p, n0):
    return p * n0, (1 - p) * n0


# ────────────────────────── 后验 ──────────────────────────
def posteriors(reg, obs):
    """返回 {action_id: {...}}；无门的动作直接标记 p_locked。"""
    by_action = {}
    for o in obs:
        by_action.setdefault(o["action_id"], []).append(o)

    out = {}
    for a in reg["actions"]:
        aid = a["id"]
        gate = a["gate_type"]
        p0 = a["current_p"]
        n0 = a.get("n0", 0)
        rows = by_action.get(aid, [])

        if gate in ("none", "execution"):
            out[aid] = {
                "gate_type": gate, "prior_p": p0, "n0": n0,
                "n": len(rows), "successes": sum(1 for r in rows if r["outcome"] == "success"),
                "posterior_mean": 1.0, "ci": [1.0, 1.0], "locked": True,
                "note": a.get("verdict", ""),
            }
            continue

        a0, b0 = encode_prior(p0, n0)
        s = sum(1 for r in rows if r["outcome"] == "success")
        f = len(rows) - s
        A, B = a0 + s, b0 + f
        lo, mid, hi = beta_quantiles(A, B)
        out[aid] = {
            "gate_type": gate, "prior_p": p0, "n0": n0, "alpha0": round(a0, 2), "beta0": round(b0, 2),
            "n": len(rows), "successes": s, "failures": f,
            "alpha": round(A, 2), "beta": round(B, 2),
            "posterior_mean": A / (A + B), "ci": [round(lo, 3), round(hi, 3)],
            "locked": False,
            "moved": abs(A / (A + B) - p0),
            "enough": len(rows) >= max(1, n0),
            "industries": sorted({r.get("industry", "?") for r in rows}),
        }
    return out


# ────────────────────────── 报告 ──────────────────────────
def report(reg, post):
    typed = {}
    for a in reg["actions"]:
        typed.setdefault(a["gate_type"], []).append(a)

    n_none = len(typed.get("none", []))
    n_exec = len(typed.get("execution", []))
    n_real = len(typed.get("negotiation", [])) + len(typed.get("market", []))
    print("=" * 92)
    print(f"  动作成功率校准报告 · 登记 {len(reg['actions'])} 个动作")
    print("=" * 92)
    print(f"  无门（伪概率，应删除）: {n_none}    执行型（p 应=1.0）: {n_exec}    真门（需校准）: {n_real}")

    print(f"\n  {'动作':<12}{'类型':<12}{'先验 p':>8}{'n0':>4}{'n':>4}{'成功':>5}"
          f"{'后验均值':>10}{'95% 区间':>18}{'状态':>10}")
    print("  " + "-" * 88)
    for aid, d in post.items():
        name = next(a["name"] for a in reg["actions"] if a["id"] == aid)[:10]
        if d["locked"]:
            state = "已锁定 1.0"
            ci = "—"
            pm = "1.000"
        else:
            pm = f"{d['posterior_mean']:.3f}"
            ci = f"[{d['ci'][0]:.2f}, {d['ci'][1]:.2f}]"
            if d["n"] == 0:
                state = "纯先验"
            elif d["enough"]:
                state = f"已更新 Δ{d['moved']:+.3f}"
            else:
                state = f"样本不足 {d['n']}/{d['n0']}"
        print(f"  {name:<12}{d['gate_type']:<12}{d['prior_p']:>7.2f}{d['n0']:>4}"
              f"{d['n']:>4}{d['successes']:>5}{pm:>9}{ci:>16}  {state}")

    # 反馈覆盖度
    print(f"\n  反馈覆盖：{sum(1 for d in post.values() if not d['locked'] and d['n'] > 0)}/{n_real} "
          f"个真门动作已有观测")
    thin = [aid for aid, d in post.items() if not d["locked"] and d["n"] < d["n0"]]
    if thin:
        print(f"  仍以先验为主（n < n0）：{', '.join(thin)}")
    print()


def export(post):
    os.makedirs(BUILD, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "actions": {
            aid: {"alpha": d.get("alpha", 1e6), "beta": d.get("beta", 1e-6),
                  "mean": d["posterior_mean"], "locked": d["locked"]}
            for aid, d in post.items()
        },
    }
    path = os.path.join(BUILD, "calibration.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  已导出 {os.path.relpath(path, ROOT)}（{len(payload['actions'])} 个动作）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", nargs=2, metavar=("ACTION_ID", "OUTCOME"),
                    help="追加一条观测：success | failure")
    ap.add_argument("--industry", default="cafe")
    ap.add_argument("--source", default="user_feedback")
    ap.add_argument("--note", default="")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--obs", default=None, help="观测文件路径（默认 data/observations.jsonl）")
    args = ap.parse_args()

    reg = E.load("_calibration")
    ids = {a["id"] for a in reg["actions"]}

    if args.add:
        aid, outcome = args.add
        if aid not in ids:
            sys.exit(f"未知动作 {aid}")
        if outcome not in ("success", "failure"):
            sys.exit("outcome 只能是 success / failure")
        append_observation({
            "action_id": aid, "outcome": outcome, "industry": args.industry,
            "source": args.source, "date": time.strftime("%Y-%m-%d"), "note": args.note,
        })
        print(f"  已记录：{aid} = {outcome}（{args.industry}）")

    post = posteriors(reg, load_observations(args.obs))
    if args.export:
        export(post)
    else:
        report(reg, post)
        export(post)


if __name__ == "__main__":
    main()
