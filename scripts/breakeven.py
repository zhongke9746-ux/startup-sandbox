#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保本测算器：给定成本结构与定价，算出

  ① 月度固定成本明细
  ② 单人边际贡献（月费 − 单人变动成本）
  ③ 保本活跃会员数（订阅制）/ 保本日客流（到店制）
  ④ 稳态维持保本线所需月获客（= 保本会员数 × 月流失率）
  ⑤ 从当前会员数爬到保本线，需要在多少个月内做到多少月获客
  ⑥ 价格 / 人力的敏感性
  ⑦ 达到不同会员规模后的年化回报（对照 70 万投资）

注意三件事，脚本会显式提示：
  · 总投资额不影响月度保本（装修/设备是沉没），只影响退出清算与现金跑道
  · 预付制行业「现金 ≠ 你的钱」，要扣预收负债
  · 健身房水电不能用「租金+人力的一成」估，它是独立硬项
"""
import argparse
import sys


def money(v):
    return f"{v:,.0f}"


def breakeven_members(fixed, contribution):
    return fixed / max(1e-9, contribution)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", type=float, default=300)
    ap.add_argument("--rent-year", type=float, default=100000, help="年租金（元）")
    ap.add_argument("--staff-cost", type=float, default=6000, help="月人力总成本（元）")
    ap.add_argument("--utilities", type=float, default=3000, help="月水电物业（元）")
    ap.add_argument("--other", type=float, default=1200, help="月其他固定（系统/保险/杂费）")
    ap.add_argument("--price", type=float, default=118, help="月费/ARPU（元/人/月）")
    ap.add_argument("--var-per-member", type=float, default=6,
                    help="单人月变动成本（耗材/洗浴/支付通道，元）")
    ap.add_argument("--cash", type=float, default=0, help="手上可动用现金（元），0=不算跑道")
    ap.add_argument("--liability", type=float, default=0, help="预收负债（元）")
    ap.add_argument("--current", type=float, default=0, help="当前活跃会员数")
    ap.add_argument("--churn", type=float, default=0.021, help="月流失率（默认 2.1%%，约年 25%%）")
    ap.add_argument("--invest", type=float, default=0, help="总投资（元），用于算回报率")
    a = ap.parse_args()

    rent = a.rent_year / 12.0
    fixed = rent + a.staff_cost + a.utilities + a.other
    contrib = a.price - a.var_per_member
    be = breakeven_members(fixed, contrib)

    print(f"## 保本测算 · {a.area:.0f}㎡")
    print()
    print("### 一、月度固定成本")
    print()
    print("| 项目 | 金额 | 说明 |")
    print("|---|---|---|")
    print(f"| 房租 | {money(rent)} | 年租 {money(a.rent_year)} ÷ 12 = {rent / max(1e-9, a.area):.1f} 元/㎡·月 |")
    print(f"| 人力 | {money(a.staff_cost)} | 员工 + 保洁 |")
    print(f"| 水电物业 | {money(a.utilities)} | 健身房独立硬项（空调/热水/淋浴） |")
    print(f"| 其他固定 | {money(a.other)} | 系统/保险/维修备件/杂费 |")
    print(f"| **合计** | **{money(fixed)}** | |")
    print()

    print("### 二、单个会员的边际贡献")
    print()
    print(f"- 月费（ARPU）：{a.price:.1f} 元")
    print(f"- 单人月变动成本：{a.var_per_member:.1f} 元（耗材/洗浴/支付通道）")
    print(f"- **边际贡献：{contrib:.1f} 元/人/月**（边际贡献率 {contrib / a.price:.0%}）")
    print()

    print("### 三、保本线")
    print()
    print(f"**保本活跃会员数 = {money(fixed)} ÷ {contrib:.1f} = {be:.0f} 人**")
    print()
    print(f"- 现在 {a.current:.0f} 人时：月{'净利' if a.current >= be else '亏损'} "
          f"{a.current * contrib - fixed:+,.0f} 元"
          f"（安全边际 {(a.current - be) / max(1e-9, a.current):+.0%}）" if a.current else "")
    print(f"- 每天平均要卖 {be / 30:.1f} 张月卡才能维持这个规模")
    print()

    print("### 四、月获客要求（两种口径，别混）")
    print()
    steady = be * a.churn
    print(f"**① 稳态维持保本线**：月新增 ≥ **{steady:.1f} 人**")
    print(f"   算法：{be:.0f} 人 × 月流失 {a.churn:.1%}。这只是「不缩水」，不产生增长。")
    print()
    gap = max(0.0, be - a.current)
    print("**② 爬到保本线**：")
    print()
    print("| 目标月数 | 需要月获客（毛） | 说明 |")
    print("|---|---|---|")
    for t in (3, 6, 9, 12, 18, 24):
        decay = (1 - a.churn) ** t
        need = gap * a.churn / max(1e-9, 1 - decay) if gap > 0 else steady
        print(f"| {t} 个月 | **{need:.1f} 人/月** | 净增 {gap:.0f} 人，按 {a.churn:.1%}/月流失折损 |")
    print()

    # 逐月模拟：给定月获客，看多久到保本、期间最多烧多少
    print("### 五、不同获客力度下的现金曲线")
    print()
    print("| 月获客 | 到保本月数 | 期间最大累计亏损 | 第 12 月会员 | 第 12 月月净利 |")
    print("|---|---|---|---|---|")
    for acq in (10, 15, 20, 30, 40, 60):
        n = a.current
        cum, worst, hit = 0.0, 0.0, None
        for m in range(1, 37):
            n = n * (1 - a.churn) + acq
            profit = n * contrib - fixed
            cum += profit
            worst = min(worst, cum)
            if hit is None and profit >= 0:
                hit = m
        n12 = a.current
        for _ in range(12):
            n12 = n12 * (1 - a.churn) + acq
        print(f"| {acq} 人 | {('第 %d 月' % hit) if hit else '36 月内未达'} | "
              f"{money(worst)} | {n12:.0f} | {n12 * contrib - fixed:+,.0f} |")
    print()

    if a.cash or a.liability:
        net_cash = a.cash - a.liability
        print("### 六、现金与跑道")
        print()
        print(f"- 账面现金 {money(a.cash)}｜预收负债 {money(a.liability)}｜**净现金 {money(net_cash)}**")
        if a.current < be:
            burn = fixed - a.current * contrib
            print(f"- 当前月亏损 {money(burn)} → 不改善的话跑道 **{net_cash / max(1e-9, burn):.1f} 个月**")
        print()

    print("### 七、定价敏感性")
    print()
    print("| 月费 | 边际贡献 | 保本会员数 | 稳态维持月获客 |")
    print("|---|---|---|---|")
    for p in (88, 99, 118, 138, 158, 198):
        c = p - a.var_per_member
        print(f"| {p} | {c:.0f} | {breakeven_members(fixed, c):.0f} 人 | "
              f"{breakeven_members(fixed, c) * a.churn:.1f} 人 |")
    print()

    if a.invest:
        print("### 八、达到不同规模后的回报（对照总投资）")
        print()
        print(f"总投资 {money(a.invest)}（含首年租金与工资，其中装修改备是沉没资产）")
        print()
        print("| 会员规模 | 月净利 | 年净利 | 对总投资的年化 |")
        print("|---|---|---|---|")
        for n in (be, be * 1.5, be * 2, be * 2.5, be * 3):
            ann = (n * contrib - fixed) * 12
            print(f"| {n:.0f} 人 | {n * contrib - fixed:+,.0f} | {ann:+,.0f} | "
                  f"{ann / max(1e-9, a.invest):+.1%} |")
        print()
        print("> 注意：年化分÷总投资是粗口径。装修改备如果能在转让时收回一部分，"
              "真实回报会比这更高；如果收不回，它就是这个数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
