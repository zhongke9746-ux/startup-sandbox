#!/usr/bin/env python3
"""P1-B 校验：宏观状态层（系统性风险）到底改变了什么。

三件事，按顺序：
  1) 链的实现是否正确——状态占用率是否复现按窗口算出的权重；
  2) 是否只加了相关性、没加漂移——p50/均值不应系统性移动；
  3) 左尾是否变厚——存活率、p5、最差路径里的收缩态占比。

用法：
    python3 scripts/regime_ab.py
    python3 scripts/regime_ab.py --runs 2000
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
import monte_carlo as M

KEYS = ["cafe", "gym", "cnrest"]
SHORT = {"cafe": "咖啡店", "gym": "健身房", "cnrest": "中餐店"}


def chain_check(macro, runs, seed, months):
    """实测状态占用 vs 解析权重。差得远 = 转移矩阵实现错了。"""
    cnt = {n: 0 for n in macro["names"]}
    for i in range(runs):
        st = M.sample_states(macro, random.Random((seed + i) ^ 0xC0FFEE), months)
        for s in st:
            cnt[s] += 1
    tot = runs * months
    return {n: cnt[n] / tot for n in macro["names"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1000)
    args = ap.parse_args()

    cfg = M.load_cfg()
    probs = M.action_prob_table()
    seed = cfg["mc"]["seed"]
    months = 24
    macro = M.prep_macro(cfg, months)
    if macro is None:
        sys.exit("_randomness.yaml 里 macro_regime.enabled 为 false")

    print("=" * 96)
    print(f"① 链实现检查（N={args.runs}）")
    print("=" * 96)
    emp = chain_check(macro, args.runs, seed, months)
    for n in macro["names"]:
        print(f"  {n:<12} 解析权重 {macro['weights'][n]:.3f}   实测占用 {emp[n]:.3f}   "
              f"差 {emp[n] - macro['weights'][n]:+.3f}")
    print(f"  收缩态期望持续月数 = 1/(1-{macro['trans']['contraction']['contraction']:.2f}) "
          f"= {1 / (1 - macro['trans']['contraction']['contraction']):.1f} 个月")

    print()
    print("=" * 96)
    print("② 宏观状态开关 A/B（同种子配对，主动 = 行业包 demo 动作）")
    print("=" * 96)
    res = {}
    for k in KEYS:
        p = E.load_pack(k)
        off, off_a, d_off = M.run_mc(p, cfg, probs, args.runs, seed, months, macro=None)
        on, on_a, d_on = M.run_mc(p, cfg, probs, args.runs, seed, months, macro=macro)
        res[k] = (off, on, off_a, on_a, d_off, d_on)

    print(f"  {'行业':<10}{'被动存活率':>18}{'p5':>24}{'p50':>24}{'均值':>24}")
    print("  " + "-" * 92)
    for k in KEYS:
        off, on, off_a, on_a, _, _ = res[k]
        a, b = off["final_net"], on["final_net"]
        print(f"  {SHORT[k]:<8}"
              f"  {off['survival_rate']:>6.1%} → {on['survival_rate']:<6.1%}"
              f"  {a['p5']:>+9,} → {b['p5']:>+9,}"
              f"  {a['p50']:>+9,} → {b['p50']:>+9,}"
              f"  {off['final_net_mean']:>+9,.0f} → {on['final_net_mean']:>+9,.0f}")

    print()
    print("=" * 96)
    print("③ 左尾与系统性风险")
    print("=" * 96)
    print(f"  {'行业':<10}{'主动存活率':>20}{'动作反变差占比':>18}{'最差5%路径收缩态占比':>24}")
    print("  " + "-" * 92)
    for k in KEYS:
        off, on, off_a, on_a, d_off, d_on = res[k]
        r = on.get("regime") or {}
        print(f"  {SHORT[k]:<8}"
              f"  {off_a['survival_rate']:>6.1%} → {on_a['survival_rate']:<6.1%}"
              f"  {d_off['worse_rate']:>7.1%} → {d_on['worse_rate']:<7.1%}"
              f"  全体 {r['contract_all']:.1%} → 最差5% {r['contract_worst5']:.1%}")
        print(f"  {'':<10}  事件数 全体 {r['events_all']:.1f} → 最差5% {r['events_worst5']:.1f} 起")


if __name__ == "__main__":
    main()
