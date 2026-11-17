#!/usr/bin/env python3
"""P1-A 校验：动作适用性（哪些动作"存在"）与"幻觉动作"的代价。

三个问题，按顺序回答：
  1) 19 个动作在 3 个行业包里，分别属于 core / weak / impossible？
  2) 每个行业包真正可用的菜单有多大？被剔除的是哪些、为什么？
  3) 如果一个系统不做适用性判定（gating=False），给一家单店配上
     "融资/扩产/预售/收缩业务线" 这类原型错误的动作，结局会被抬高多少？

第 3 问是这一步真正的价值：它衡量的是"选项集污染"的代价，
而不是某个概率准不准。

用法：
    python3 scripts/gate_demo.py
    python3 scripts/gate_demo.py --runs 2000
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
import monte_carlo as M

KEYS = ["cafe", "gym", "cnrest"]
MARK = {"core": "●", "weak": "◐", "impossible": "×"}

# 一家 3 人咖啡店在"外行 AI"手里最容易被选中的四个动作：
# 它们在任何 MBA 框架里都成立，但在这个经营形态里根本不存在。
FANTASY = [
    {"at": 6, "id": "A-FIN-01", "name": "融资 9 个月跑道", "params": {"amount_months": 9}},
    {"at": 7, "id": "A-CAP-01", "name": "扩产 30%", "params": {"capacity_gain": 0.30}},
    {"at": 8, "id": "A-FIN-03", "name": "卖储值卡", "params": {"amount_months": 1.5}},
    {"at": 9, "id": "A-STR-01", "name": "收缩业务线 20%", "params": {"cut_share": 0.20}},
]


def dwidth(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def cut(s, w):
    """按显示宽度截断（CJK 计 2 列）。"""
    out, used = "", 0
    for c in str(s):
        cw = 2 if ord(c) > 0x2E80 else 1
        if used + cw > w:
            break
        out += c
        used += cw
    return out


def pad(s, w, align="l"):
    s = str(s)
    gap = max(0, w - dwidth(s))
    return s + " " * gap if align == "l" else " " * gap + s


SHORT = {"cafe": "咖啡店", "gym": "健身房", "cnrest": "中餐店"}


def print_matrix(packs):
    lib = E.load("_actions")
    print("=" * 74)
    print("① 动作适用性矩阵（● core / ◐ weak / × impossible）")
    print("=" * 74)
    print(pad("动作", 28) + "".join(pad(SHORT[k], 15) for k in KEYS))
    print("-" * 74)
    for a in lib["actions"]:
        name = cut(f"{a['id']} {a['name']}", 26)
        row = pad(name, 28)
        for k in KEYS:
            ap = E.applicability(packs[k], a["id"])
            row += pad(f"{MARK[ap['tier']]} {ap['tier']}", 15)
        print(row)


def print_menus(packs):
    print()
    print("=" * 88)
    print("② 每个行业包的实际菜单：哪 19 个动作里，有几个是真的可以做的")
    print("=" * 88)
    for k in KEYS:
        p = packs[k]
        menu = E.action_menu(p)
        n_core = sum(1 for m in menu if m["tier"] == "core")
        n_weak = sum(1 for m in menu if m["tier"] == "weak")
        n_no = sum(1 for m in menu if m["tier"] == "impossible")
        print(f"\n  {p['meta']['name']}（原型 {p['archetype']}）")
        print(f"    可用 {n_core + n_weak}/19：core {n_core} + weak {n_weak}，剔除 {n_no}")
        print(f"    被剔除：")
        for m in menu:
            if m["tier"] == "impossible":
                print(f"      × {m['id']} {m['name']} —— {m['reasons'][0]}")
        weak = [m for m in menu if m["tier"] == "weak"]
        if weak:
            print(f"    被削弱：")
            for m in weak:
                print(f"      ◐ {m['id']} {m['name']} —— {m['reasons'][0]}")


def print_contamination(packs, runs):
    """核心实验：不做 gating 时，幻觉动作能把结局抬高多少。"""
    cfg = M.load_cfg()
    probs = M.action_prob_table()
    seed = cfg["mc"]["seed"]
    p = packs["cafe"]
    print()
    print("=" * 88)
    print(f"③ 选项集污染的代价（{p['meta']['name']}，N={runs}，同种子配对）")
    print("=" * 88)
    print("  四个「原型错误」的动作：融资 / 扩产 / 预售 / 收缩业务线")
    print("  它们在任何商业框架里都成立，但在单店形态里根本不存在。\n")

    rows = []
    pa, ac, d = M.run_mc(p, cfg, probs, runs, seed)                      # 真实 demo 动作
    rows.append(("被动（不做动作）", pa, None))
    rows.append(("真实菜单（demo 4 个动作）", ac, d))

    pu, au, du = M.run_mc(p, cfg, probs, runs, seed, actions=FANTASY, gating=False)
    rows.append(("幻觉动作 · 不做 gating", au, du))

    pg, ag, dg = M.run_mc(p, cfg, probs, runs, seed, actions=FANTASY, gating=True)
    rows.append(("幻觉动作 · 做 gating", ag, dg))

    print(f"  {pad('场景', 26)}{pad('存活率', 10, 'r')}{pad('p5 期末', 14, 'r')}"
          f"{pad('p50 期末', 14, 'r')}{pad('p95 期末', 14, 'r')}")
    print("  " + "-" * 76)
    for name, agg, _ in rows:
        fn = agg["final_net"]
        surv = f"{agg['survival_rate']:.1%}"
        v5, v50, v95 = f"{fn['p5']:+,}", f"{fn['p50']:+,}", f"{fn['p95']:+,}"
        print(f"  {pad(name, 26)}{pad(surv, 10, 'r')}"
              f"{pad(v5, 14, 'r')}{pad(v50, 14, 'r')}{pad(v95, 14, 'r')}")

    # 硬校验：gating 后必须与"什么都不做"完全相等——证明它是硬拒绝而非打折
    same = (ag["survival_rate"] == pa["survival_rate"]
            and ag["final_net"] == pa["final_net"]
            and ag["insolvency_rate"] == pa["insolvency_rate"])
    print()
    verdict = "是（硬拒绝，不是打折）" if same else "否 ← 缺陷"
    print(f"  gating 后是否与「被动」逐项相等：{verdict}")
    lift = au["survival_rate"] - pa["survival_rate"]
    d_p50 = au["final_net"]["p50"] - pa["final_net"]["p50"]
    d_mean = au["final_net_mean"] - pa["final_net_mean"]
    print(f"  不做 gating 时：存活率 {lift * 100:+.1f}pp，期末 p50 {d_p50:+,}，"
          f"均值 {d_mean:+,.0f} 元")
    print(f"  也就是说：给不存在的动作一个 p，等于给系统开了一条免费输血通道。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=500)
    args = ap.parse_args()
    packs = {k: E.load_pack(k) for k in KEYS}
    print_matrix(packs)
    print_menus(packs)
    print_contamination(packs, args.runs)


if __name__ == "__main__":
    main()
