#!/usr/bin/env python3
"""动作库跨行业动态校验
三件事：
  1) 三个行业各自的 被动 vs 主动 结局
  2) 每个动作单独执行的边际贡献（决定"性价比排序"是否跨行业成立）
  3) 三个新增字段的敏感度（不建模这三点，答案会错多少）
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

KEYS = ("cafe", "gym", "cnrest")
MONTHS = 24


def run(pack, actions=None, months=MONTHS):
    return E.simulate(pack, actions=actions, months=months)


def line(rows, pack):
    s = E.summary(rows, pack)
    return s


def main():
    packs = {k: E.load_pack(k) for k in KEYS}
    base, act = {}, {}

    print("=" * 84)
    print("① 被动 vs 主动（同一组外部冲击，唯一区别是有没有做动作）")
    print("=" * 84)
    print(f"  {'行业':<13}{'场景':<6}{'期末现金':>11}{'期末负债':>10}{'净现金':>11}{'累计现金流':>11}{'亏损月':>7}{'现金归零':>9}")
    print("  " + "-" * 80)
    for k in KEYS:
        p = packs[k]
        b = run(p)
        a = run(p, actions=p.get("demo_actions", []))
        base[k], act[k] = b, a
        for tag, rows in (("被动", b), ("主动", a)):
            s = line(rows, p)
            print(f"  {p['meta']['name'][:11]:<13}{tag:<6}"
                  f"{s['期末现金']:>+11,}{rows[-1]['负债']:>10,}{s['期末净现金']:>+11,}"
                  f"{s['累计现金流']:>+11,}{s['亏损月数']:>6}/24{s['现金归零月']!s:>9}")
        db = E.summary(a, p)["期末净现金"] - E.summary(b, p)["期末净现金"]
        print(f"  {'':<19}→ 动作带来的净现金差异: {db:+,}")

    print()
    print("=" * 84)
    print("② 单动作贡献（各自独立执行，其余不变）")
    print("=" * 84)
    for k in KEYS:
        p = packs[k]
        b0 = E.summary(base[k], p)["期末净现金"]
        print(f"\n  【{p['meta']['name']}】基准净现金 {b0:+,}")
        print(f"    {'动作':<12}{'执行月':>7}{'净现金变化':>14}{'排名':>6}")
        contrib = []
        for a in p.get("demo_actions", []):
            rows = run(p, actions=[a])
            d = E.summary(rows, p)["期末净现金"] - b0
            contrib.append((a["id"], a["name"], a["at"], d))
        for i, (aid, name, at, d) in enumerate(sorted(contrib, key=lambda x: -x[3]), 1):
            mark = "  ← 负贡献" if d < 0 else ""
            print(f"    {aid:<12}{at:>7}{d:>+14,}{i:>6}{mark}")
        tot = sum(c[3] for c in contrib)
        both = E.summary(act[k], p)["期末净现金"] - b0
        print(f"    {'合计':<12}{'':>7}{tot:>+14,}")
        print(f"    {'实际组合':<12}{'':>7}{both:>+14,}   (叠加损耗 {(both - tot):+,})")

    print()
    print("=" * 84)
    print("③ 四个新增字段的敏感度：不建模它们，结论会错多少")
    print("=" * 84)

    # 3.1 A-COST-02 的量级高估：库写的是 fixed_cost × -reduction
    print("\n  [3.1] A-COST-02 重谈租约（统一按降 12% 计）：库里打的是 fixed_cost 全量，实际只降房租")
    print(f"    {'行业':<14}{'房租占固定成本':>14}{'库声明降本':>13}{'实际降本':>12}{'高估倍数':>10}")
    for k in KEYS:
        p = packs[k]
        o, bb = p["operations"], p["baseline"]
        rev = o["price"] * o["volume"] * o["days"]
        rent = rev * bb.get("rent_cost_share", 0)
        share = rent / o["fixed_cost"]
        print(f"    {p['meta']['name'][:12]:<14}{share:>13.0%}{o['fixed_cost']*0.12:>+13,.0f}"
              f"{o['fixed_cost']*0.12*share:>+12,.0f}{1/share:>9.1f}x")

    # 3.2 关键人依赖：裁员在中餐店会额外掉品质
    print("\n  [3.2] A-COST-01 裁员 15%：不建模 key_person_dependency 会漏掉品质损失（数字为贡献，非绝对值）")
    for k in KEYS:
        p = packs[k]
        a = [{"at": 10, "id": "A-COST-01", "name": "裁 15%", "params": {"cut_ratio": 0.15}}]
        p0 = copy.deepcopy(p)
        p0["baseline"]["key_person_dependency"] = 0.0
        b_with = E.summary(run(p), p)["期末净现金"]
        b_without = E.summary(run(p0), p0)["期末净现金"]
        d_with = E.summary(run(p, actions=a), p)["期末净现金"] - b_with
        d_without = E.summary(run(p0, actions=a), p0)["期末净现金"] - b_without
        print(f"    {p['meta']['name'][:12]:<14} 建模={d_with:>+10,}  不建模={d_without:>+10,}  "
              f"漏掉 {d_without - d_with:+,}")

    # 3.3 产能天花板：扩产在面积受限行业会撞墙，且"加产能不自动来客"
    print("\n  [3.3] A-CAP-01 扩产：加产能不会自动造出需求，只在饱和时才转化")
    print(f"    {'行业':<14}{'月均产能利用率':>14}{'现况下扩产贡献':>15}{'饱和(+40%客流)后':>17}")
    for k in KEYS:
        p = packs[k]
        o = p["operations"]
        util = o["volume"] / max(1e-9, o["capacity"] * p["baseline"]["capacity_ceiling"])
        a = [{"at": 6, "id": "A-CAP-01", "name": "扩产 30%", "params": {"capacity_gain": 0.30}}]
        d_now = E.summary(run(p, actions=a), p)["期末净现金"] - E.summary(run(p), p)["期末净现金"]
        p2 = copy.deepcopy(p)
        p2["operations"]["volume"] *= 1.4
        d_hot = E.summary(run(p2, actions=a), p2)["期末净现金"] - E.summary(run(p2), p2)["期末净现金"]
        print(f"    {p['meta']['name'][:12]:<14}{util:>13.0%}{d_now:>+15,}{d_hot:>+17,}")

    # 3.4 预付负债：健身房的现金有多少是别人的钱
    print("\n  [3.4] 预收款：账面现金 vs 真实可动用现金")
    for k in KEYS:
        p = packs[k]
        r = base[k]
        print(f"    {p['meta']['name'][:12]:<14} 期初负债 {p['operations'].get('liability',0):>+9,}  "
              f"期末账面现金 {r[-1]['现金']:>+9,}  期末负债 {r[-1]['负债']:>+9,}  "
              f"净现金 {r[-1]['净现金']:>+9,}")


if __name__ == "__main__":
    main()
