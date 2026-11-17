#!/usr/bin/env python3
"""通用月度引擎 v1
设计约束：本文件不得出现任何行业名、店名、菜品名。
只认 6 个语义槽位 + 2 个账户（现金 / 预收负债），行业差异全部来自行业包。

为什么这一点很重要：如果引擎里出现 "if 是咖啡店"，那这次跨行业校验就退化成
"我又写了两份脚本"，验证不到动作库本身。

用法：
    python3 scripts/engine.py gym            # 单个行业
    python3 scripts/run_three.py             # 三个行业 + 单动作贡献
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
RULES = os.path.join(ROOT, "rules")

SLOTS = ("price", "volume", "unit_cost", "fixed_cost", "var_rate", "capacity", "volume_gated")
PACKS = {
    "cafe": "industry-cafe-demo",
    "gym": "industry-gym-demo",
    "cnrest": "industry-cnrestaurant-demo",
}

# 同一个行业有两个叫法：会话与规则文件里用短名 cnrest，采集表与前端用全名
# cnrestaurant。曾经因为只认短名，选「中餐」开局直接 KeyError——开局就失败，
# 而且只有选中餐的人会遇到。别名放在读取这一层，别让每个调用点各自兼容。
PACK_ALIAS = {"cnrestaurant": "cnrest"}


# ────────────────────────── 构建：YAML → JSON ──────────────────────────
def build(force=False):
    """确保 build/*.json 齐全。

    分发版里 build/ 已经预编译好了，运行完全不需要 ruby；
    只有改过 rules/*.yaml 才需要重新编译，那时才要求本机有 ruby。
    没有 ruby 时，只要 json 齐全就直接用现有 json（解压/复制会把时间戳弄乱，
    yaml 比 json 新是常态，不该为此崩掉）。
    """
    os.makedirs(BUILD, exist_ok=True)
    ruby = os.path.join(ROOT, "scripts", "yaml2json.rb")
    want = [f[:-5] for f in os.listdir(RULES) if f.endswith(".yaml")]
    missing = [n for n in want if not os.path.exists(os.path.join(BUILD, n + ".json"))]
    stale = force or bool(missing)
    if not stale:
        json_t = max((os.path.getmtime(os.path.join(BUILD, f)) for f in os.listdir(BUILD) if f.endswith(".json")), default=0)
        yaml_t = max((os.path.getmtime(os.path.join(RULES, f)) for f in os.listdir(RULES) if f.endswith(".yaml")), default=0)
        stale = yaml_t > json_t
    if not stale:
        return True
    exe = shutil.which("ruby")
    if not exe:
        if missing:
            raise RuntimeError(
                "缺少预编译规则：%s\n本机没有 ruby，无法从 rules/*.yaml 编译。"
                "请在开发机上先跑一次 scripts/yaml2json.rb，把 build/*.json 一起打包。"
                % "、".join(missing))
        return True
    r = subprocess.run([exe, ruby], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"YAML 编译失败：\n{r.stdout}\n{r.stderr}")
    return True


def load(name):
    build()
    path = os.path.join(BUILD, f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_pack(key):
    return load(PACKS[PACK_ALIAS.get(key, key)])


# ────────────────────────── 动作适用性 ──────────────────────────
# P0 只解决"p 是多少"；这一层解决"这个动作在不在"。
# 一家 3 人的咖啡店拿到 p=0.35 的融资，问题不是 0.35 准不准，
# 而是它根本不进入股权融资市场。不存在的选项不该有概率。
TIER_ORDER = {"core": 0, "weak": 1, "impossible": 2}


def capabilities(pack):
    """原型能力向量 + 行业包 ≤4 个 override。"""
    lib = load("_applicability")
    caps = dict(lib["archetypes"][pack["archetype"]]["capabilities"])
    caps.update(pack.get("capability_overrides", {}) or {})
    return caps


def _gate_pass(caps, when):
    v = caps.get(when["dim"])
    if "in" in when:
        return v in when["in"]
    if "gte" in when:
        return v is not None and v >= when["gte"]
    if "lte" in when:
        return v is not None and v <= when["lte"]
    if "eq" in when:
        return v == when["eq"]
    raise KeyError(f"未知的门条件：{when}")


def applicability(pack, action_id):
    """返回 {tier, reasons}。多个门取最坏档位。"""
    lib = load("_applicability")
    caps = capabilities(pack)
    gates = next((a["gates"] for a in lib["action_gates"] if a["id"] == action_id), [])
    tier, reasons = "core", []
    for g in gates:
        if _gate_pass(caps, g["when"]):
            continue
        if TIER_ORDER[g["on_fail"]] > TIER_ORDER[tier]:
            tier = g["on_fail"]
        reasons.append(g["reason"])
    return {"tier": tier, "reasons": reasons}


def action_menu(pack):
    """该行业包的动作菜单：core / weak 可用，impossible 被剔除。"""
    lib = load("_actions")
    out = []
    for a in lib["actions"]:
        ap = applicability(pack, a["id"])
        out.append({"id": a["id"], "name": a["name"], "tier": ap["tier"],
                    "reasons": ap["reasons"]})
    return out



# ────────────────────────── 时间曲线 ──────────────────────────
def _factor(m, e):
    """某条效果在第 m 月的生效比例（0~1）"""
    s = e["start"]
    dur = e.get("duration")
    prof = e.get("profile", "step")
    if m < s:
        return 0.0
    ramp = max(1, e.get("ramp", 2))
    if dur is None:                      # 永久效果
        if prof == "ramp":
            return min(1.0, (m - s + 1) / ramp)
        if prof == "pulse":
            return 0.5 ** ((m - s) / max(1, e.get("tau", 2)))
        return 1.0
    end = s + dur - 1                    # 有期限
    if m <= end:
        if prof == "ramp":
            return min(1.0, (m - s + 1) / ramp)
        return 1.0
    rec = e.get("recover", 0) or 0       # v0.1.1 的恢复期
    if rec <= 0:
        return 0.0
    return max(0.0, 1.0 - (m - end) / rec)


def build_timeline(effects, months):
    n = months + 2
    mult = {s: [1.0] * n for s in SLOTS}
    add = {s: [0.0] * n for s in SLOTS}
    once, liab_once, labels = {}, {}, {}
    for e in effects:
        slot = e["slot"]
        if slot == "cash_once":
            once[e["start"]] = once.get(e["start"], 0) + e["delta"]
            if e.get("label"):
                labels.setdefault(e["start"], []).append(e["label"])
            continue
        if slot == "liability_once":
            liab_once[e["start"]] = liab_once.get(e["start"], 0) + e["delta"]
            continue
        if slot in ("assets_zero", "close_at", "prepaid_refund"):
            continue                       # 这三个由主循环消费，不进槽位时间线
        op = e.get("op", "mul")
        for m in range(1, months + 1):
            f = _factor(m, e)
            if f == 0.0:
                continue
            if op == "mul":
                mult[slot][m] *= (1 + e["delta"] * f)
            elif op == "add":
                add[slot][m] += e["delta"] * f
        if e.get("label"):
            labels.setdefault(e["start"], []).append(e["label"])
    return mult, add, once, liab_once, labels


# ────────────────────────── 动作编译 ──────────────────────────
# 每个 action 在库里的声明（_actions.yaml）都在这里被翻译成"对槽位的操作"
# validate_packs.py 会断言：这里的 key 集合 == YAML 里的 id 集合（不许有未实现的动作）
def compile_action(a, pack, m0):
    """返回 (effects, one_time_cash, note)"""
    b = pack["baseline"]
    op = pack["operations"]
    pid, p = a["id"], a.get("params", {})
    ef, once, note = [], 0.0, ""

    if pid == "A-PRICE-01":                                    # 提价
        d = p.get("price_delta", 0.08)
        ef.append({"slot": "price", "delta": +d, "start": m0})
        ef.append({"slot": "volume", "delta": -(d * b["price_elasticity"]),
                   "start": m0 + 1, "profile": "ramp", "ramp": 2})

    elif pid == "A-PRICE-02":                                  # 促销降价
        d = abs(p.get("price_delta", 0.15))
        gain = min(b["price_elasticity"] * d * 1.5, 0.40)
        ef.append({"slot": "price", "delta": -d, "start": m0})
        ef.append({"slot": "volume", "delta": +gain, "start": m0, "profile": "ramp", "ramp": 1})
        ef.append({"slot": "volume", "delta": -0.10, "start": m0 + 3, "duration": 2})  # 透支

    elif pid == "A-COST-01":                                   # 裁员/缩编
        cut = p.get("cut_ratio", 0.15)
        lab = b["labor_cost_share"]
        ef.append({"slot": "fixed_cost", "delta": -(cut * lab), "start": m0 + 1})
        ef.append({"slot": "capacity", "delta": -(cut * 0.7), "start": m0 + 1})
        ef.append({"slot": "volume", "delta": -(cut * 0.3), "start": m0 + 2})
        # ★ 新增：关键人依赖会把"减人"放大成"掉品质"，这是本库原先没有的传导
        # 用连续量而非阈值：依赖 0.2 和 0.35 也有差别，只是小
        kd = b.get("key_person_dependency", 0.0)
        if kd > 0.0:
            ef.append({"slot": "volume", "delta": -(cut * kd), "start": m0 + 1,
                       "duration": 6, "recover": 4})
            if kd >= 0.4:
                note = f"关键人依赖 {kd:.2f}，裁员额外伤量 {cut*kd*100:.1f}%"
        once = cut * lab * op["fixed_cost"] * 1.5

    elif pid == "A-COST-02":                                   # 重谈固定成本
        r = p.get("reduction", 0.10)
        share = _rent_share(pack)
        ef.append({"slot": "fixed_cost", "delta": -(r * share), "start": m0 + 1})
        once = op["fixed_cost"] * share
        note = f"房租占固定成本 {share:.0%}，故实际降本仅为库中声明的 {share:.0%}"

    elif pid == "A-COST-03":                                   # 换供应商/降规格
        d = abs(p.get("unit_cost_delta", 0.08))
        q = abs(p.get("quality_delta", 0.05))
        ef.append({"slot": "unit_cost", "delta": -d, "start": m0 + 1})
        ef.append({"slot": "capacity", "delta": -0.10, "start": m0 + 1, "duration": 1})
        # ★ 新增：品质可感知性决定"降本"会不会变成"掉口碑"
        qv = b.get("quality_visibility", 0.0)
        if qv > 0.0:
            ef.append({"slot": "volume", "delta": -(q * qv * 2.0), "start": m0 + 2,
                       "duration": 8, "recover": 5})
            note = f"品质可感知度 {qv:.2f}，掉量 {q*qv*2*100:.1f}%"

    elif pid == "A-COST-04":                                   # 削减非核心支出
        cut = p.get("cut_ratio", 0.20)
        ef.append({"slot": "fixed_cost", "delta": -(cut * 0.35), "start": m0})
        ef.append({"slot": "var_rate", "delta": -(cut * 0.5), "start": m0})
        ef.append({"slot": "volume", "delta": -(cut * 0.3), "start": m0 + 2})

    elif pid == "A-GROW-01":                                   # 加大获客
        mult = p.get("spend_multiple", 2.0)
        ef.append({"slot": "var_rate", "delta": +(mult - 1) * 0.10, "start": m0})
        ef.append({"slot": "volume", "delta": +(mult - 1) * 0.6 * 0.3,
                   "start": m0 + 1, "profile": "ramp", "ramp": 2})

    elif pid == "A-GROW-02":                                   # 开新渠道
        g = p.get("volume_gain", 0.18)
        fee = p.get("channel_fee", 0.18)
        ef.append({"slot": "volume", "delta": +g, "start": m0 + 2, "profile": "ramp", "ramp": 3})
        ef.append({"slot": "var_rate", "delta": +(fee * g) / max(1e-6, 1), "start": m0 + 2})
        once = op["price"] * op["volume"] * op["days"] * 0.5

    elif pid == "A-GROW-03":                                   # 推新品
        t = p.get("ticket_gain", 0.08)
        v = p.get("volume_gain", 0.03)
        ef.append({"slot": "price", "delta": +t, "start": m0 + 1})
        ef.append({"slot": "volume", "delta": +v, "start": m0 + 2, "profile": "ramp", "ramp": 2})
        once = op["price"] * op["volume"] * op["days"] * 0.5

    elif pid == "A-GROW-04":                                   # 会员/复购
        cr = p.get("churn_reduction", 0.20)
        mg = p.get("margin_give_up", 0.03)
        gain = cr * b["baseline_churn"] * 5
        ef.append({"slot": "volume", "delta": +gain, "start": m0 + 2})
        ef.append({"slot": "price", "delta": -mg, "start": m0 + 1})

    elif pid == "A-CAP-01":                                    # 扩产
        g = p.get("capacity_gain", 0.30)
        ef.append({"slot": "capacity", "delta": +g, "start": m0 + 2})
        ef.append({"slot": "fixed_cost", "delta": +(g * 0.6), "start": m0 + 2})
        # ★ 修正：加产能不会自动造出需求。只有产能已被打满时，新增产能才转化为成交
        ef.append({"slot": "volume_gated", "delta": +(g * 0.5), "start": m0 + 3,
                   "profile": "ramp", "ramp": 2})
        once = op["fixed_cost"] * 3 * g

    elif pid == "A-CAP-02":                                    # 收缩产能
        c = p.get("capacity_cut", 0.30)
        ef.append({"slot": "fixed_cost", "delta": -(c * 0.8), "start": m0 + 1})
        ef.append({"slot": "volume", "delta": -(c * 0.3), "start": m0 + 1})
        once = op["fixed_cost"] * 0.5 * c

    elif pid == "A-FIN-01":                                    # 融资
        months_amt = p.get("amount_months", 9)
        ef.append({"slot": "cash_once", "delta": +(months_amt * _burn(pack)), "start": m0 + 4})
        once = 0.0

    elif pid == "A-FIN-02":                                    # 借贷
        amt = p.get("amount_months", 3)
        rate = p.get("interest", 0.10)
        ef.append({"slot": "cash_once", "delta": +(amt * _burn(pack)), "start": m0 + 1})
        ef.append({"slot": "fixed_cost", "delta": +(rate / 12 * amt * 0.9 * 0.5), "start": m0 + 1})

    elif pid == "A-FIN-03":                                    # ★ 新增：预售/储值/年卡
        amt = p.get("amount_months", 1.5) * _burn(pack)
        ef.append({"slot": "cash_once", "delta": +amt, "start": m0})
        ef.append({"slot": "liability_once", "delta": +amt, "start": m0})
        note = "现金 +负债 等额增加，净资产不变——这是原动作库缺失的一条"

    elif pid == "A-RISK-01":                                   # 合规加固
        spend = p.get("spend_months", 0.6)
        ef.append({"slot": "fixed_cost", "delta": +(0.03 + 0.09 * min(spend, 1.5) / 1.5), "start": m0 + 2})
        ef.append({"slot": "volume", "delta": -0.05, "start": m0 + 1, "duration": 2})
        once = spend * _burn(pack)

    elif pid == "A-RISK-02":                                   # 危机公关
        spend = p.get("spend_months", 0.2)
        boost = p.get("recovery_boost", 0.45)
        ef.append({"slot": "volume", "delta": +(boost * 0.15), "start": m0 + 1,
                   "duration": 4, "recover": 2})
        once = spend * _burn(pack)

    elif pid == "A-STR-01":                                    # 收缩业务线
        c = p.get("cut_share", 0.20)
        ef.append({"slot": "volume", "delta": -c, "start": m0 + 1})
        ef.append({"slot": "fixed_cost", "delta": -(c * 1.6), "start": m0 + 1})

    elif pid == "A-STR-02":                                    # 退出
        at = m0 + 2
        # 退出能收回多少，取决于**那时的资产账面值**，而不是"已投入资产 × 一个拍脑袋的比例"。
        # 押金/装修/设备/转让费的回收率差得很远：混成一个 salvage_ratio，
        # 会让一家轻资产店和一家重资产店的退出价值看起来一模一样。
        salvage = exit_salvage(pack, at)
        ef.append({"slot": "cash_once", "delta": +salvage, "start": at,
                   "label": f"退出变现 {salvage:,.0f}"})
        ef.append({"slot": "volume", "delta": -1.0, "start": at})
        ef.append({"slot": "assets_zero", "delta": 0, "start": at})
        # 预收年卡余额要退（监管要求 + 实际协商），不退就是拿别人的钱当自己的利润
        refund = (pack.get("prepaid") or {}).get("refund_on_exit", 0.0)
        if refund:
            ef.append({"slot": "prepaid_refund", "delta": 0, "start": at, "rate": refund})
        ef.append({"slot": "close_at", "delta": 0, "start": at})
        note = "终止动作：变现资产、退还预收、停止经营"

    else:
        raise KeyError(f"动作 {pid} 未在引擎中实现 —— 库与引擎已漂移")

    return ef, once, note


def _rent_share(pack):
    """房租在"固定成本"里的比重。
    动作库把'降租 X%'写成 fixed_cost × -X，这是错的：房租只是固定成本的一部分。
    这里按 rent_cost_share 反推真实比重，差额写进校验报告。
    """
    b = pack["baseline"]
    o = pack["operations"]
    rev = o["price"] * o["volume"] * o["days"]
    rent_amt = rev * b.get("rent_cost_share", 0.0)
    return max(0.05, min(1.0, rent_amt / max(1e-6, o["fixed_cost"])))


def op_fixed_total(pack):
    return pack["operations"]["fixed_cost"]


def _burn(pack):
    o = pack["operations"]
    rev = o["price"] * o["volume"] * o["days"]
    return max(1.0, o["fixed_cost"] + o["unit_cost"] * o["volume"] * o["days"] * 0.5 - rev * 0.25)


# ────────────────────────── 资本与资产 ──────────────────────────
# 开业投入不是费用，是**钱换了个形态**：现金 → 装修/设备/押金/转让费。
# 它只有在两种情况下才变成损失：折旧耗尽，或退出时折价变现。
# 所以资产必须是一个独立账户，跟现金并列演化，而不是开业当天从账上消失。
ASSET_KINDS = {
    "transfer":  {"life": 0,  "recovery": 0.50,
                  "note": "转让费：位置价值，不折旧，但转手要打折"},
    "fitout":    {"life": 36, "recovery": 0.25,
                  "note": "装修：按租期摊，退租时基本只剩拆改成本"},
    "equipment": {"life": 60, "recovery": 0.30,
                  "note": "设备：五年直线折旧，二手残值约三成"},
    "deposit":   {"life": 0,  "recovery": 0.95,
                  "note": "押金：不折旧，正常退租议退"},
    "other":     {"life": 36, "recovery": 0.20,
                  "note": "其他投入：家具杂项/开办杂费的混合桶，按租期摊，退出回收率低"},
}


def asset_policy(pack):
    """资产折旧与退出回收比例。行业包可用 asset_policy 覆盖（例如器械残值更高）。"""
    out = {}
    for k, d in ASSET_KINDS.items():
        out[k] = dict(d, **(pack.get("asset_policy", {}).get(k) or {}))
    return out


def asset_book(pack, month):
    """第 month 个月末的资产账面值（month=0 为开业当天）。线性折旧，**不进 P&L**。

    为什么折旧不算成本：这个沙盒的「净利」是现金流口径，装修款在开业那天
    就已经一次性付掉了，再摊进月度成本就是重复扣。会计利润和现金流
    是两套语言，混在一起会让账本自相矛盾。
    """
    o = pack["operations"]
    a = o.get("assets") or {}
    pol = asset_policy(pack)
    total = 0.0
    for k, v in a.items():
        life = (pol.get(k) or {}).get("life", 0)
        left = 1.0 if life <= 0 else max(0.0, 1.0 - month / float(life))
        total += float(v) * left
    return total


def exit_salvage(pack, month):
    """现在退出能收回多少现金：押金几乎全额，装修设备按残值，转让费按位置价值打折。"""
    o = pack["operations"]
    a = o.get("assets") or {}
    pol = asset_policy(pack)
    total = 0.0
    for k, v in a.items():
        life = (pol.get(k) or {}).get("life", 0)
        left = 1.0 if life <= 0 else max(0.0, 1.0 - month / float(life))
        total += float(v) * left * (pol.get(k) or {}).get("recovery", 0.3)
    return total


def committed_capital(pack):
    """这笔生意一共占用了多少钱：开业后投的现金 + 已经沉在店里的资产。

    这是回报率唯一正确的分母。用"账上现金"当分母会算出几百个点的年化——
    因为分母漏掉了装修、设备、转让费这些已经花掉的钱。
    """
    o = pack["operations"]
    return float(o.get("cash", 0)) + sum(float(v) for v in (o.get("assets") or {}).values())


# ────────────────────────── 主模拟 ──────────────────────────
def simulate(pack, actions=None, events=None, months=24, growth=None, verbose=False,
             volume_noise=None, gating=True, trace=None):
    """volume_noise: 长度为 months 的逐月乘数列表（蒙特卡洛层注入），None 则全 1。
    growth: 标量（恒定月增长率）或长度 months 的逐月增长率列表（宏观状态层注入）。
    actions 条目可带 "failed": True —— 按 _actions.yaml 的 success_roll 规则结算：
    效果不生效，但仍承担一半一次性成本（谈判失败有时间成本，招聘失败有面试成本）。
    gating=False 仅用于对照实验（模拟一个不做适用性判定的系统）。"""
    o = pack["operations"]
    b = pack["baseline"]
    events = events if events is not None else pack.get("demo_events", [])
    actions = actions or []
    ceiling = b.get("capacity_ceiling", 1.0)

    if growth is None:
        g = b.get("growth_assumption", [0.0, 0.0])
        growth = (g[0] + g[1]) / 2.0
    g_path = list(growth) if isinstance(growth, (list, tuple)) else [growth] * (months + 1)

    eff = []
    for e in events:
        eff.append({"slot": e["slot"], "delta": e["delta"], "start": e["start"],
                    "duration": e.get("dur"), "profile": e.get("profile", "step"),
                    "ramp": e.get("ramp", 2), "recover": e.get("recover", 0),
                    "tau": e.get("tau", 2), "label": e.get("name")})

    # 重复执行同一动作：收益按 decay^(n-1) 打折，代价照付。
    # 这条规则只在"同一动作被做第二次以上"时生效（decay^0 = 1），
    # 所以 06-动作库跨行业校验里所有单次动作的结论一字不变。
    rep = load("_actions").get("repeat_decay", {}) or {}

    notes, one_times, refused, weakened = [], {}, [], []
    done = {}
    for a in actions:
        if gating:
            ap = applicability(pack, a["id"])
            if ap["tier"] == "impossible":
                # 硬拒绝：不掷骰、不部分生效、不按失败结算、不占带宽
                notes.append(f"{a['id']}：已拒绝——{ap['reasons'][0]}")
                refused.append({"id": a["id"], "name": a.get("name", ""),
                                "at": a["at"], "reason": ap["reasons"][0]})
                continue
            if ap["tier"] == "weak":
                weakened.append({"id": a["id"], "name": a.get("name", ""),
                                 "at": a["at"], "reason": ap["reasons"][0]})
        more, once, note = compile_action(a, pack, a["at"])
        if a.get("failed"):
            # success_roll：失败付一半代价，effects 全部不生效
            one_times[a["at"]] = one_times.get(a["at"], 0) - once * 0.5
            notes.append(f"{a['id']}：动作失败（只承担一半成本，效果未生效）")
            continue
        n = done.get(a["id"], 0)
        rd = rep.get(a["id"])
        if n > 0 and rd and rd.get("benefit"):
            f = rd.get("decay", 1.0) ** n
            for e in more:
                if e["slot"] in rd["benefit"]:
                    e["delta"] *= f
            notes.append(f"{a['id']}：第 {n + 1} 次做，收益已衰减到 {f:.0%}")
        done[a["id"]] = n + 1
        eff.extend(more)
        # 一次性成本是"付出"，必须做减法；做成加法会让每个重动作凭空生钱
        one_times[a["at"]] = one_times.get(a["at"], 0) - once
        if note:
            notes.append(f"{a['id']}：{note}")
    if trace is not None:
        trace["refused"] = refused
        trace["weak"] = weakened

    mult, add, once_ev, liab_ev, labels = build_timeline(eff, months)
    for m, v in one_times.items():
        once_ev[m] = once_ev.get(m, 0) + v

    pre = pack.get("prepaid", {}) or {}
    amort_n = max(1, pre.get("amort_months", 8))
    inflow_base = pre.get("inflow_per_month", 0) if pre.get("enabled") else 0

    cash = o["cash"]
    liab = o.get("liability", 0)
    # 退出类事件的时点：关店之后不再有固定成本，资产清零，预收余额要退
    close_at = min((e["start"] for e in eff if e["slot"] == "close_at"), default=None)
    wipe_at = min((e["start"] for e in eff if e["slot"] == "assets_zero"), default=None)
    refund_at = {e["start"]: e.get("rate", 0.0) for e in eff if e["slot"] == "prepaid_refund"}
    rows = []
    cum_g = 1.0        # 增长累计因子（逐月复利；标量输入时等价于 (1+g)^(m-2)）

    for m in range(1, months + 1):
        if m >= 3:
            cum_g *= (1 + g_path[m - 1])
        price = o["price"] * mult["price"][m] + add["price"][m]
        volume = o["volume"] * mult["volume"][m] + add["volume"][m]
        if volume_noise is not None:
            volume *= volume_noise[m - 1]
        volume *= cum_g
        unit_cost = max(0.0, o["unit_cost"] * mult["unit_cost"][m] + add["unit_cost"][m])
        fixed = o["fixed_cost"] * mult["fixed_cost"][m] + add["fixed_cost"][m]
        var_rate = max(0.0, o["var_rate"] * mult["var_rate"][m] + add["var_rate"][m])
        cap = min(o["capacity"] * mult["capacity"][m], o["capacity"] * ceiling)

        v = max(0.0, min(volume, cap))
        # 扩产只有在"原本就装不下"时才转化为成交：以物理上限的 85% 作饱和判据
        if volume >= 0.85 * o["capacity"] * ceiling:
            v = max(0.0, min(volume * mult["volume_gated"][m], cap))
        revenue = v * price * o["days"]
        cogs = v * unit_cost * o["days"]
        var = revenue * var_rate
        profit = revenue - cogs - var - fixed

        # 关店：不再有固定成本、不再进量。剩下的只有退出那次变现。
        closed = bool(close_at) and m >= close_at
        if closed:
            v = revenue = cogs = var = profit = 0.0

        amort = liab / amort_n
        inflow = inflow_base
        if closed:
            amort = inflow = 0.0
        liab_new = max(0.0, liab + inflow - amort + liab_ev.get(m, 0))
        # 关店时退还预收余额（年卡未履约部分），退完负债清零
        refund = 0.0
        if m in refund_at:
            refund = liab_new * refund_at[m]
            liab_new = 0.0
        once = once_ev.get(m, 0)
        cash_delta = profit - amort + inflow + once - refund
        cash += cash_delta

        # 资产账户：开业投入换了个形态存在，按月折旧，退出时清零
        ab = 0.0 if (wipe_at and m >= wipe_at) else asset_book(pack, m)

        rows.append({
            "月": m, "量": round(v), "价": round(price, 1), "营收": round(revenue),
            "成本": round(cogs + var + fixed), "净利": round(profit),
            "现金": round(cash), "负债": round(liab_new), "净现金": round(cash - liab_new),
            "资产": round(ab), "净资产": round(cash + ab - liab_new),
            "事件": "、".join(labels.get(m, [])) or "—",
        })
        # 交互层要"解释这个月为什么变了"，聚合行不够用：必须能拆到
        # 量/价/货品/变动/固定。这里只做记录，不影响任何结算。
        if trace is not None:
            trace.setdefault("monthly", []).append({
                "月": m, "价": price, "需求": volume, "成交": v, "产能": cap,
                "利用率": v / max(1e-9, cap),
                "营收": revenue, "货品": cogs, "变动": var, "固定": fixed,
                "摊销": amort, "一次性": once, "净利": profit,
                "资产": ab, "净资产": cash + ab - liab_new,
                "增长率": g_path[m - 1] if m >= 3 else 0.0,
            })
        liab = liab_new

    if verbose:
        for n in notes:
            print("  ! " + n)
    return rows


def breakeven(pack):
    o, b = pack["operations"], pack["baseline"]
    per_unit = o["price"] * (1 - o["var_rate"]) - o["unit_cost"]
    vol = o["fixed_cost"] / max(1e-9, per_unit) / o["days"]
    return vol


def summary(rows, pack):
    be = breakeven(pack)
    o = pack["operations"]
    return {
        "期初净现金": o["cash"] - o.get("liability", 0),
        "期末现金": rows[-1]["现金"],
        "期末净现金": rows[-1]["净现金"],
        "累计净利": sum(r["净利"] for r in rows),
        "累计现金流": rows[-1]["现金"] - o["cash"],
        "亏损月数": sum(1 for r in rows if r["净利"] < 0),
        "最低净现金": min(r["净现金"] for r in rows),
        "现金归零月": next((r["月"] for r in rows if r["现金"] < 0), "未发生"),
        "负净资产月": next((r["月"] for r in rows if r["净现金"] < 0), "未发生"),
        "盈亏平衡量": round(be, 1),
        "安全边际": f"{(o['volume'] - be) / max(1e-9, o['volume']) * 100:+.1f}%",
    }


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "cafe"
    pack = load_pack(key)
    print(f"# {pack['meta']['name']}")
    rows = simulate(pack, verbose=True)
    print(f"\n| 月 | 量 | 价 | 营收 | 成本 | 净利 | 现金 | 负债 | 净现金 | 事件 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['月']} | {r['量']} | {r['价']} | {r['营收']:,} | {r['成本']:,} | "
              f"{r['净利']:+,} | {r['现金']:,} | {r['负债']:,} | {r['净现金']:,} | {r['事件']} |")
    s = summary(rows, pack)
    print()
    for k, v in s.items():
        print(f"{k}: {v}")
