#!/usr/bin/env python3
"""宏观状态层的通道分解：涨落到底是从哪来的？

三个调制通道（增长/需求噪声/事件概率）共用一个状态链。开启后分布变宽是预期的，
但如果中位数也系统性移动，说明"只加相关性、不加漂移"这条承诺没做到——
必须定位到具体通道，而不是笼统地接受"宽了"。

做法：固定同一组随机种子，逐个关掉通道，看哪个通道关掉后 p50 回到基线。

用法：python3 scripts/regime_decomp.py [cafe|gym|cnrest] [--runs N]
"""

import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
import monte_carlo as M


def variant(cfg, growth=True, noise=True, events=True):
    c = copy.deepcopy(cfg)
    for st in c["macro_regime"]["states"].values():
        if not growth:
            st["growth_shift"] = 0.0
        if not noise:
            st["noise_offset"] = 0.0
            st["sigma_mult"] = 1.0
        if not events:
            st["event_occ_mult"] = 1.0
    return M.prep_macro(c)


def row(name, pa, base=None):
    fn = pa["final_net"]
    d50 = "" if base is None else f"{fn['p50'] - base['final_net']['p50']:+,}"
    print(f"  {name:<22}{pa['survival_rate']:>8.1%}{fn['p5']:>+12,}"
          f"{fn['p50']:>+12,}{fn['p95']:>+12,}{pa['final_net_mean']:>+12,.0f}{d50:>12}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", nargs="?", default="cafe")
    ap.add_argument("--runs", type=int, default=1000)
    args = ap.parse_args()

    cfg = M.load_cfg()
    probs = M.action_prob_table()
    seed = cfg["mc"]["seed"]
    pack = E.load_pack(args.pack)

    cases = [
        ("基线（宏观关）", None),
        ("全开", variant(cfg)),
        ("只开增长通道", variant(cfg, noise=False, events=False)),
        ("只开噪声通道", variant(cfg, growth=False, events=False)),
        ("只开事件通道", variant(cfg, growth=False, noise=False)),
    ]

    print(f"\n{'=' * 84}")
    print(f"  {pack['meta']['name']} · 宏观状态通道分解（N={args.runs}，同一组种子，被动场景）")
    print(f"{'=' * 84}")
    print(f"  {'配置':<22}{'存活率':>8}{'p5':>12}{'p50':>12}{'p95':>12}{'均值':>12}{'p50变化':>12}")
    print("  " + "-" * 92)
    base = None
    for name, macro in cases:
        pa, _, _ = M.run_mc(pack, cfg, probs, args.runs, seed, macro=macro)
        row(name, pa, base)
        if base is None:
            base = pa
    print()
    print("  读法：哪个通道关掉后 p50 回到基线，漂移就来自那个通道；")
    print("        三个通道单独开都接近基线、全开才偏低 = 交互效应。")


    print()
    print("  注：均值与中位同步小幅下移 = 分布整体略降（居中有残余）；仅中位下移 = 左偏。")
    print("      本表用于判断「哪个通道」而非「是否该下移」，后者见 10-宏观状态.md 的残差说明。")


if __name__ == "__main__":
    main()
