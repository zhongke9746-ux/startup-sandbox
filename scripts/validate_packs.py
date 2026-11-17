#!/usr/bin/env python3
"""动作库跨行业静态校验
回答三个问题：
  1) 行业包本身有没有结构性错误（成本占比不等于 1、槽位没覆盖全）
  2) 动作库与引擎有没有漂移（YAML 里 19 个动作，引擎是否都实现了）
  3) 每个动作在三个行业里"有没有用力点" —— 杠杆矩阵
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

SLOT_ALL = ["fixed_cost", "unit_cost", "variable_cost", "volume",
            "price", "churn", "capacity", "revenue", "cash", "valuation"]

# 每个动作的"主杠杆"：靠哪个基线参数发力，以及及格线
LEVER = {
    "A-PRICE-01": ("price_margin", 0.20, "提价空间（= 1 - 价格弹性）"),
    "A-PRICE-02": ("price_margin", 0.20, "降价换量空间"),
    "A-COST-01": ("labor_cost_share", 0.12, "人力占比"),
    "A-COST-02": ("rent_cost_share", 0.10, "房租占比"),
    "A-COST-03": ("material_cost_share", 0.08, "食材/物料占比"),
    "A-COST-04": ("other_cost_share", 0.05, "可削项占比"),
    "A-GROW-01": ("gross_margin", 0.30, "毛利空间"),
    "A-GROW-02": ("channel_concentration", 0.40, "单一渠道依赖度"),
    "A-GROW-03": ("gross_margin", 0.25, "客单提升空间"),
    "A-GROW-04": ("baseline_churn", 0.10, "月度流失率"),
    "A-CAP-01": ("capacity_headroom", 0.20, "产能余量（= ceiling - 1）"),
    "A-CAP-02": ("capacity_idle", 0.15, "产能闲置率（= 1 - 量/产能）"),
    "A-FIN-01": ("fundability", 0.50, "资本可融资性"),
    "A-FIN-02": ("asset_intensity", 0.30, "可抵押资产占比"),
    "A-FIN-03": ("prepaid_ratio", 0.15, "预付/储值占比"),
    "A-RISK-01": ("regulatory_intensity", 0.50, "监管强度"),
    "A-RISK-02": ("trust_sensitivity", 0.50, "信任敏感度"),
    "A-STR-01": ("multi_line", 0.30, "是否有可砍的亏损业务线"),
    "A-STR-02": ("always", 0.0, "永远可用（退出）"),
}

# 融资可及性、业务线数量、资本密度曾经各有一份手写常量表。
# 现在前两项改由 _applicability.yaml 的原型能力向量提供（单一真源），
# 资本密度从「资产账面合计 / 年营收」反推——常量表会和行业包互相矛盾。
CAP_ACCESS_SCORE = {"none": 0.02, "debt_only": 0.20, "equity": 1.00}


def metrics(pack, key):
    b, o = pack["baseline"], pack["operations"]
    rev = o["price"] * o["volume"] * o["days"]
    annual = max(1e-9, rev * 12)
    gross = 1 - (o["unit_cost"] * o["volume"] * o["days"] + rev * o["var_rate"]) / max(1e-9, rev)
    caps = E.capabilities(pack)
    return {
        "price_margin": 1 - b.get("price_elasticity", 1.0),
        "labor_cost_share": b.get("labor_cost_share", 0),
        "rent_cost_share": b.get("rent_cost_share", 0),
        "material_cost_share": b.get("material_cost_share", 0),
        "other_cost_share": b.get("other_cost_share", 0),
        "gross_margin": gross,
        "channel_concentration": b.get("channel_concentration", 0),
        "baseline_churn": b.get("baseline_churn", 0),
        "capacity_headroom": b.get("capacity_ceiling", 1.0) - 1.0,
        "capacity_idle": max(0.0, 1 - pack["operations"]["volume"] / max(1e-9, pack["operations"]["capacity"])),
        "fundability": CAP_ACCESS_SCORE.get(caps.get("capital_access"), 0.02),
        "asset_intensity": min(1.0, sum((o.get("assets") or {}).values()) / annual),
        "prepaid_ratio": b.get("prepaid_ratio", 0),
        "regulatory_intensity": b.get("regulatory_intensity", 0),
        "trust_sensitivity": b.get("trust_sensitivity", 0),
        "multi_line": 0.60 if caps.get("business_lines", 1) >= 2 else 0.15,
    }


def grade(score, baseline):
    if baseline == "always":
        return "常备"
    score = round(score, 3)
    baseline = round(baseline, 3)
    if score < 0:
        return "反向"        # 做了比不做好
    if score >= baseline * 2.5:
        return "强"
    if score >= baseline:
        return "可用"
    if score >= baseline * 0.5:
        return "弱"
    return "空转"


def main():
    E.build(force=True)
    actions = E.load("_actions")
    yaml_ids = {a["id"] for a in actions["actions"]}

    # 引擎已实现的动作（从 compile_action 源码里静态提取动作 ID）
    import inspect
    import re
    src = inspect.getsource(E.compile_action)
    impl_ids = set(re.findall(r'"(A-[A-Z]+-\d+)"', src))

    print("=" * 78)
    print("① 行业包结构校验")
    print("=" * 78)
    packs = {}
    ok = True
    for key in ("cafe", "gym", "cnrest"):
        p = E.load_pack(key)
        packs[key] = p
        b = p["baseline"]
        shares = ["rent_cost_share", "labor_cost_share", "material_cost_share",
                  "other_cost_share", "platform_cost_share", "equipment_cost_share"]
        s = sum(b.get(x, 0) for x in shares)
        covered = set(p["slot_map"].keys())
        miss = set(SLOT_ALL) - covered
        # 判据修正：*_cost_share 是"占收入比"，合计 = 1 - 净利率，不是必须等于 1
        # 合计 = 1.000 意味着这个行业包隐含假设"零净利"，必须显式披露
        implied_margin = 1 - s
        flag = "OK" if s <= 1.001 and not miss else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"  {p['meta']['name']:<16} 成本合计 {s:.3f}  隐含净利率 {implied_margin:+.1%}  "
              f"槽位 {len(covered)}/10  未覆盖 {sorted(miss) or '无'}  [{flag}]")
        # 成本占比明细（用于发现"漏算一项"）
        print("      明细: " + "  ".join(f"{x.replace('_cost_share','')}={b.get(x,0):.2f}" for x in shares))

    # 1b. 基线字段完整性：缺字段 → 相关动作只能粗算，必须显式告警
    req = E.load("_mechanisms").get("baseline_required_fields", {})
    need = list(req.get("core", [])) + list(req.get("v0_2_added", {}).keys())
    print("\n  基线字段完整性（缺字段 = 相关动作降级为粗算）")
    for key in ("cafe", "gym", "cnrest"):
        p = packs[key]
        miss = [f for f in need if f not in p["baseline"]]
        print(f"    {p['meta']['name'][:12]:<14} {len(need)-len(miss):>2}/{len(need)}  "
              f"缺: {miss or '无'}")

    print()
    print("=" * 78)
    print("② 动作库 ↔ 引擎 一致性")
    print("=" * 78)
    print(f"  YAML 动作数: {len(yaml_ids)}   引擎实现数: {len(impl_ids)}")
    only_yaml = yaml_ids - impl_ids
    only_impl = impl_ids - yaml_ids
    if only_yaml:
        ok = False
        print(f"  ✗ 库里声明但引擎未实现: {sorted(only_yaml)}")
    if only_impl:
        print(f"  ! 引擎实现但库里未声明: {sorted(only_impl)}  ← 私有扩展，应回写 YAML")
    if not only_yaml:
        print("  ✓ 库中每个动作都有可执行实现，无漂移")

    print()
    print("=" * 78)
    print("③ 杠杆矩阵：这个动作在这个行业里有没有用力点")
    print("=" * 78)
    keys = ("cafe", "gym", "cnrest")
    head = "  " + f"{'动作':<14}" + "".join(f"{packs[k]['meta']['name'][:12]:>14}" for k in keys)
    print(head)
    print("  " + "-" * (14 + 14 * len(keys)))
    empty_by_pack = {k: [] for k in keys}
    for aid in sorted(yaml_ids):
        lev, base, _ = LEVER.get(aid, ("always", 0.0, ""))
        row = f"  {aid:<14}"
        for k in keys:
            # 不存在的动作不评杠杆：给"融资"打"空转"分，等于默认它可以做
            if E.applicability(packs[k], aid)["tier"] == "impossible":
                row += f"{'不存在':>14}"
                continue
            m = metrics(packs[k], k)
            score = 0.0 if lev == "always" else m.get(lev, 0.0)
            g = grade(score, base if lev != "always" else "always")
            row += f"{g + f'({score:+.2f})':>14}"
            if g in ("空转", "反向"):
                empty_by_pack[k].append((aid, g))
        print(row)

    print()
    print("=" * 78)
    print("④ 结论：每个行业里使不上劲的动作")
    print("=" * 78)
    for k in keys:
        bad = empty_by_pack[k]
        print(f"  {packs[k]['meta']['name']}: {len(bad)} 个")
        for aid, g in bad:
            print(f"      {aid:<14} {g}")
    print()
    print("  合计: " + ", ".join(f"{packs[k]['meta']['name']}={len(empty_by_pack[k])}" for k in keys))

    # ── ⑤ 适用性：这个动作在这个形态里存在吗 ──
    print()
    print("=" * 78)
    print("⑤ 适用性校验（archetype 是否有效、override 是否超限、demo 是否含幻觉动作）")
    print("=" * 78)
    app = E.load("_applicability")
    limit = app["governance"]["override_limit"]
    for k in keys:
        p = packs[k]
        arch = p.get("archetype")
        ov = p.get("capability_overrides", {}) or {}
        known = arch in app["archetypes"]
        dims_ok = all(d in app["capability_schema"] for d in ov)
        menu = E.action_menu(p)
        n_ok = sum(1 for m in menu if m["tier"] != "impossible")
        bad_demo = [a["id"] for a in p.get("demo_actions", [])
                    if E.applicability(p, a["id"])["tier"] == "impossible"]
        flags = []
        if not known:
            flags.append("archetype 不存在")
        if len(ov) > limit:
            flags.append(f"override {len(ov)}>{limit} 应新建原型")
        if not dims_ok:
            flags.append("override 含未知维度")
        if bad_demo:
            flags.append(f"demo 含不可能动作 {bad_demo}")
        if flags:
            ok = False
        print(f"  {p['meta']['name'][:12]:<14} {arch:<20} override {len(ov)}/{limit}  "
              f"菜单 {n_ok}/19  [{'OK' if not flags else ' / '.join(flags)}]")
    print("  说明：impossible 动作不进菜单；用户强行选择时引擎硬拒绝（不掷骰、不部分生效）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
