#!/usr/bin/env python3
"""咖啡店行业包 · 24 个月演算 v2
新增：动作库支持（_actions.yaml 的 4 个动作在此实现）
用途：验证"店主主动采取动作"能否改变结局，以及改变多少
仅用标准库
"""

# ── 初始条件 ──
INIT = {
    "cash": 300000,
    "rent": 25000,
    "labor": 24000,
    "other": 10000,
    "material_rate": 0.36,
    "ticket": 26.0,
    "orders": 120,
    "days": 30,
    "elasticity": 0.70,      # baseline.price_elasticity
}
RAMP = {1: 80, 2: 100, 3: 115}

EVENTS = [
    {"id": "R-CAFE-LOCAL-01", "name": "道路施工封路", "start": 4,  "dur": 3,  "orders_mul": 0.75},
    {"id": "R-CAFE-MKT-01",   "name": "连锁品牌入驻", "start": 8,  "dur": 24, "ticket_mul": 0.92},
    {"id": "R-CAFE-COST-01",  "name": "咖啡豆涨价",   "start": 10, "dur": 24, "material_mul": 1.055},
    {"id": "R-CAFE-POL-01",   "name": "食安检查被罚", "start": 12, "dur": 2,  "orders_mul": 0.90, "cash_hit": 30000},
    {"id": "M-COST-04",       "name": "平台佣金上调", "start": 14, "dur": 24, "other_add": 1500},
    {"id": "R-CAFE-LOCAL-01", "name": "地铁施工",     "start": 16, "dur": 6,  "orders_mul": 0.85},
]

# ── 场景 B：第 7 个月开始动作（带宽 2 个/月，符合规则约束）──
ACTION_PLAN = [
    {"at": 7, "id": "A-COST-02", "name": "重谈租约", "kind": "rent_cut",
     "reduction": 0.12, "neg_cost": 15000, "delay": 1},
    {"at": 7, "id": "A-PRICE-01", "name": "提价 8%", "kind": "price_up",
     "delta": 0.08, "delay": 0},
    {"at": 9, "id": "A-GROW-02", "name": "开外卖渠道", "kind": "new_channel",
     "gain": 0.18, "channel_fee": 0.18, "pack_cost": 1.2,
     "setup_cost": 12000, "delay": 2},
    {"at": 15, "id": "A-COST-04", "name": "削减非核心支出 15%", "kind": "cut_spend",
     "cut": 0.15, "delay": 1},
]


def build_effects(plan, months=24):
    """把动作计划编译成逐月的乘数数组"""
    n = months + 2
    arr = {k: [1.0] * n for k in ("rent", "labor", "other", "material", "ticket", "orders")}
    extra_ch = [0.0] * n
    cash_hits = {}
    labels = {}

    for a in plan:
        m0, kind, delay = a["at"], a["kind"], a.get("delay", 0)
        start = m0 + delay

        if kind == "rent_cut":
            for m in range(start, n):
                arr["rent"][m] *= (1 - a["reduction"])
            cash_hits[m0] = cash_hits.get(m0, 0) + a.get("neg_cost", 0)
            labels.setdefault(start, []).append(f"{a['name']}(生效)")

        elif kind == "price_up":
            loss = a["delta"] * INIT["elasticity"]
            for m in range(m0, n):
                arr["ticket"][m] *= (1 + a["delta"])
            for m in range(m0 + 1, n):
                progress = 0.5 if m == m0 + 1 else 1.0
                arr["orders"][m] *= (1 - loss * progress)
            labels.setdefault(m0, []).append(f"{a['name']}(当月)")

        elif kind == "new_channel":
            gain = a["gain"]
            for i, m in enumerate(range(start, n)):
                factor = min(1.0, (i + 1) / 3)
                extra_ch[m] = max(extra_ch[m], gain * factor)
            cash_hits[m0] = cash_hits.get(m0, 0) + a.get("setup_cost", 0)
            labels.setdefault(start, []).append(f"{a['name']}(生效)")

        elif kind == "cut_spend":
            for m in range(start, n):
                arr["other"][m] *= (1 - a["cut"])
            labels.setdefault(start, []).append(f"{a['name']}(生效)")

    return arr, extra_ch, cash_hits, labels


def run(growth=0.0, plan=None, months=24):
    plan = plan or []
    arr, extra_ch, cash_hits, labels = build_effects(plan, months)
    cash = INIT["cash"]
    rows = []

    for m in range(1, months + 1):
        orders = RAMP[m] if m in RAMP else INIT["orders"] * ((1 + growth) ** (m - 4))
        orders *= arr["orders"][m]
        ticket = INIT["ticket"] * arr["ticket"][m]
        mat = INIT["material_rate"] * arr["material"][m]
        other = INIT["other"] * arr["other"][m]
        rent = INIT["rent"] * arr["rent"][m]
        labor = INIT["labor"] * arr["labor"][m]
        cash_hit = cash_hits.get(m, 0)
        active = []

        for e in EVENTS:
            if e["start"] <= m < e["start"] + e["dur"]:
                active.append(e["name"])
                orders *= e.get("orders_mul", 1.0)
                ticket *= e.get("ticket_mul", 1.0)
                mat *= e.get("material_mul", 1.0)
                other += e.get("other_add", 0)
                if m == e["start"]:
                    cash_hit += e.get("cash_hit", 0)

        extra = extra_ch[m]
        base_rev = orders * ticket * INIT["days"]
        extra_rev = base_rev * extra
        revenue = base_rev + extra_rev
        cogs = revenue * mat + extra * orders * INIT["days"] * 1.2
        commission = extra_rev * 0.18 if extra > 0 else 0
        variable = cogs + commission
        fixed = rent + labor + other
        profit = revenue - variable - fixed - cash_hit
        cash += profit

        rows.append({
            "月": m, "日单量": round(orders * (1 + extra)), "客单价": round(ticket, 1),
            "月营收": round(revenue), "变动成本": round(variable),
            "固定成本": round(fixed + cash_hit), "净利润": round(profit),
            "现金": round(cash),
            "事件": "、".join(sorted(set(active))) or "—",
            "动作": "、".join(labels.get(m, [])) or "",
        })
    return rows


def show(rows, title):
    print(f"\n### {title}\n")
    print("| 月 | 日单量 | 客单价 | 月营收 | 净利 | 现金余额 | 事件 | 动作 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['月']} | {r['日单量']} | {r['客单价']} | {r['月营收']:,} | "
              f"{r['净利润']:+,} | {r['现金']:,} | {r['事件']} | {r['动作'] or '—'} |")


def summary(rows):
    return {
        "期末现金": rows[-1]["现金"],
        "累计净利": sum(r["净利润"] for r in rows),
        "亏损月数": sum(1 for r in rows if r["净利润"] < 0),
        "最低现金": min(r["现金"] for r in rows),
        "首次转负": next((r["月"] for r in rows if r["现金"] < 0), "未发生"),
    }


if __name__ == "__main__":
    base = run()
    act = run(plan=ACTION_PLAN)

    show(base, "场景 A · 什么都不做（被动挨打）")
    show(act, "场景 B · 第 7 个月起采取动作")

    a, b = summary(base), summary(act)
    print("\n### 对比\n")
    print("| 指标 | A 被动 | B 主动 | 差异 |")
    print("|---|---|---|---|")
    for k in ("期末现金", "累计净利", "亏损月数", "最低现金", "首次转负"):
        va, vb = a[k], b[k]
        if isinstance(va, (int, float)):
            print(f"| {k} | {va:+,} | {vb:+,} | {vb - va:+,} |")
        else:
            print(f"| {k} | {va} | {vb} | — |")
