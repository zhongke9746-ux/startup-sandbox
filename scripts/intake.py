#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intake.py —— 开局信息采集 → 定制行业包。

用户开局只回答事实（地点/资金/面积/客源/加盟…），不判断任何影响。
本模块把事实翻译成引擎认的三层：
  operations（这家店现在长什么样）—— 资金、房租、人力、产能
  baseline   （这个业态的结构参数）—— 从最近的行业原型继承 + 按事实修正
  archetype/capability_overrides —— 加盟与资源会改变"哪些动作存在"

基准表（租金/薪资）是兜底值：快照抓到当地数据就覆盖它（derived 参数）。
"""
import copy
import json
import math
import sys

ASSET_INPUT_KEYS = ("transfer_fee", "fitout", "equipment", "deposit", "other_invested")
ASSET_LABEL = {"transfer": "转让费", "fitout": "装修", "equipment": "设备",
               "deposit": "押金", "other": "其他"}


def assets_is_reported(intake):
    """用户有没有分项报过资产结构。没报就只能按原型等比拆，并且必须标注是估算。"""
    return any(intake.get(k) not in (None, "")
               and float(intake.get(k) or 0) > 0 for k in ASSET_INPUT_KEYS)


# ────────────────────────── 采集字段定义 ──────────────────────────
FIELDS = [
    ("city",            "地点",        "城市，可写到商圈"),
    ("asof",            "开局时间",    "默认今天；快照抓取会围栏到这个日期之前"),
    ("industry",        "行业",        "cafe / gym / cnrestaurant（其他行业暂无原型，按最近原型跑并标注）"),
    ("shop_type",       "具体类型",    "一句话，如：社区精品咖啡店 / 商场快取店 / 24h 自助健身房"),
    ("total_invested",  "总投入",      "你为这家店一共投入的钱（元）。填了分项（设备/其他/转让费…）后，"
                                       "剩余部分就是账上现金；没填分项则按原型比例拆（估算）"),
    ("capital",         "账上现金",    "开业后可动用的现金（元）。填了「总投入」则忽略本项"),
    ("transfer_fee",    "转让费",      "选填。接手别人的店付的转让费：不折旧，但退出只能按位置价值折价转手"),
    ("fitout",          "装修投入",    "选填。装修花了多少：按租期摊，退租时基本归零"),
    ("equipment",       "设备投入",    "选填。器械/厨具/家具：按五年摊，二手残值约三成"),
    ("deposit",         "押金",        "选填。押金不折旧，正常退租议退"),
    ("other_invested",  "其他投入",    "选填。家具杂项/开办杂费等混合桶：按租期摊，退出回收率低"),
    ("area_sqm",        "面积",        "㎡；决定房租与产能上限"),
    ("staff",           "员工数",      "含自己；决定人力成本与出杯/接待产能"),
    ("customer_source", "客源",        "natural 自然流 / mixed 各半 / self 主要靠自主获客"),
    ("franchise",       "加盟",        "品牌名（自动抓加盟政策）；不加盟填 null"),
    ("resources",       "自有资源",    "非加盟选填：如自有铺面/供应商关系/私域客群/房东资源"),
    ("months",          "模拟时长",    "6–36 个月"),
    ("notes",           "其他",        "选填，任何你觉得影响经营的背景"),
    ("rent_annual",     "年租金",      "选填。实报年租（元）会覆盖「城市×面积」推算"),
    ("staff_cost",      "月人力成本",  "选填。实报月人力总成本（元），含员工与保洁"),
    ("model_api",       "模型 API",    "可选。有 → 创建时联网抓取真实信息冻结；无 → 用内部模型事件跑"),
    ("already_open",    "是否已开业",  "默认 true（接手在跑的店）；false 会扣开办成本且客流爬坡"),
    ("actual_volume",   "实测客源",    "在营实测：活跃会员数 / 日客流。会覆盖原型推算"),
    ("price",           "月费/客单",    "实报定价（元）。会覆盖原型 ARPU 假设"),
]

# ────────────────────────── 兜底基准表（快照可覆盖）──────────────────────────
# 城市分档：影响租金单价与人力成本。被快照 derived 覆盖时以快照为准。
CITY_TIER = {
    "一线":   {"rent_sqm": 300, "wage": 5500, "cities": ["北京", "上海", "深圳", "广州"]},
    "新一线": {"rent_sqm": 180, "wage": 4500, "cities": ["成都", "杭州", "武汉", "重庆", "西安", "苏州", "南京", "长沙", "郑州", "天津"]},
    "二线":   {"rent_sqm": 120, "wage": 3800, "cities": []},
    "三四线": {"rent_sqm": 70,  "wage": 3000, "cities": []},
}
# 商圈修正：写到具体核心商圈时上浮；社区/园区下调
LOCATION_MULT = {"核心商圈": 1.8, "商场": 1.5, "临街": 1.0, "社区": 0.8, "园区": 0.75, "高校": 0.85, "写字楼": 0.9}

# 行业原型：用户行业 → 最近的已有行业包
ARCHETYPE = {"cafe": "cafe", "gym": "gym", "cnrestaurant": "cnrestaurant",
             "咖啡": "cafe", "健身房": "gym", "健身": "gym",
             "中餐": "cnrestaurant", "餐饮": "cnrestaurant"}

# 各原型的参照面积/人手：产能缩放必须以原型自己的体量为基准，
# 拿咖啡店 60㎡ 的尺子去量 300㎡ 的健身房，产能会放大好几倍
REF_SIZE = {"cafe": (60.0, 3.0), "gym": (350.0, 2.0), "cnrestaurant": (200.0, 6.0)}

# 客源修正：改变起点客流与增长形态
SOURCE_MOD = {
    "natural": {"vol_mult": 1.00, "growth_add": 0.000, "channel_conc": 0.75, "note": "自然流：起步稳，但天花板看商圈"},
    "mixed":   {"vol_mult": 0.85, "growth_add": 0.002, "channel_conc": 0.60, "note": "各半：起步略低，有第二条腿"},
    "self":    {"vol_mult": 0.65, "growth_add": 0.005, "channel_conc": 0.40, "note": "自主获客：起步低、爬坡快，把命握在自己手里"},
}

# 加盟的默认条款（快照抓到具体品牌政策就覆盖）
FRANCHISE_DEFAULT = {
    "fee_once": 100000,        # 加盟费+保证金（一次性）
    "royalty": 0.03,           # 营业额抽成（进 var_rate）
    "supply_markup": 0.08,     # 总部供货溢价（进 unit_cost）
    "brand_traffic": 1.15,     # 品牌自然流加成
    "growth_add": 0.002,       # 总部营销支持
    "note": "默认条款，抓到品牌实际政策后覆盖",
}


def city_tier(city):
    for tier, t in CITY_TIER.items():
        if any(c in city for c in t["cities"]):
            return tier, t
    if any(k in city for k in ("县", "镇")):
        return "三四线", CITY_TIER["三四线"]
    # 「X市Y市」= 地级市下辖的县级市，按三四线算
    if city.count("市") >= 2:
        return "三四线", CITY_TIER["三四线"]
    return "二线", CITY_TIER["二线"]


def location_mult(city):
    for k, m in LOCATION_MULT.items():
        if k in city:
            return m
    return 1.0


def derive_pack(intake, base_pack, derived=None):
    """intake 事实 + 原型包 → 定制包。derived = 快照抓回来的当地/品牌数据（可空）。

    原则：用户只给事实，所有"影响"都由这里显式计算并写进 notes，
    不许在引擎里藏隐式修正——引擎只认槽位，不知道什么叫加盟。
    """
    derived = derived or {}
    pack = copy.deepcopy(base_pack)
    notes = []

    city = intake.get("city", "成都")
    area = float(intake.get("area_sqm") or 60)
    staff = int(intake.get("staff") or 3)
    capital = float(intake.get("capital") or 300000)
    source = intake.get("customer_source", "mixed")
    src = SOURCE_MOD.get(source, SOURCE_MOD["mixed"])
    franchise = intake.get("franchise")
    months = int(intake.get("months") or 24)
    assert 6 <= months <= 36, "模拟时长必须在 6–36 个月之间"

    # ── 房租与人力：面积 × 当地单价（快照优先，用户实测最优先）──
    tier, t = city_tier(city)
    rent_sqm = derived.get("rent_per_sqm") or (t["rent_sqm"] * location_mult(city))
    wage = derived.get("wage") or t["wage"]
    rent = area * rent_sqm
    # 用户直接报了年租金 / 月人力总成本时，按实报走——这是签约过的数字，
    # 比任何"城市 × 面积"的推算都可靠。快照里的地区单价只用于没报的情况。
    if intake.get("rent_annual"):
        rent = float(intake["rent_annual"]) / 12.0
        rent_sqm = rent / max(1e-9, area)
        notes.append(f"房租 {rent:,.0f}/月（用户实报年租 {float(intake['rent_annual']):,.0f}，"
                     f"折 {rent_sqm:.1f} 元/㎡·月，覆盖地区推算）")
        labor = float(intake.get("staff_cost") or staff * wage)
        notes.append(f"人力 {labor:,.0f}/月（{'用户实报' if intake.get('staff_cost') else f'{staff} 人 × {wage:,.0f}'}）")
    else:
        labor = staff * wage
        notes.append(f"房租 {rent:,.0f}/月（{area:.0f}㎡ × {rent_sqm:.0f}元/㎡·月，{tier}"
                     f"{'·快照' if derived.get('rent_per_sqm') else '·兜底表'}）"
                     f"；人力 {labor:,.0f}/月（{staff}人 × {wage:,.0f}）")
    # 水电：到店制按租金+人力一成估没问题；订阅制（健身房）空调热水是硬项，
    # 用「面积 × 单价」更接近现实（300㎡ 满负荷约 3,000/月）。
    if intake.get("utilities"):
        utilities = float(intake["utilities"])
    elif base_pack["operations"].get("days", 30) == 1:
        utilities = max(0.10 * (rent + labor), area * 10.0)
    else:
        utilities = 0.10 * (rent + labor)
    fixed = rent + labor + utilities + 3000    # 其他固定（系统/杂费）

    # ── 量、价、产能：原型 × 客源修正 × 规模修正 ──
    bo = base_pack["operations"]
    price = float(intake.get("price") or bo["price"])
    base_vol = bo["volume"] * src["vol_mult"]
    # 产能按"原型自己的体量"缩放：面积与人手取小，幂次体现边际递减
    ref_area, ref_staff = REF_SIZE.get(
        ARCHETYPE.get(intake.get("industry", ""), "cafe"), (60.0, 3.0))
    cap_area = bo["capacity"] * (area / ref_area) ** 0.5
    cap_staff = bo["capacity"] * (staff / ref_staff) ** 0.7
    capacity = round(min(cap_area, cap_staff))
    volume = round(min(base_vol, capacity * 0.85))
    unit_cost = price * base_pack["baseline"]["material_cost_share"]
    vol_unit = "活跃会员" if bo.get("days") == 1 else "日客流"
    notes.append(f"{vol_unit}起点 {volume}（原型 {bo['volume']} × 客源系数 {src['vol_mult']}），"
                 f"产能上限 {capacity}（面积与人手取小）")
    # 实测值优先：用户报了真实在营数字（会员数/日客流），原型假设整条让位。
    # 这是校准，不是微调——420→100 会让盈亏平衡和安全边际整个翻转。
    if intake.get("actual_volume"):
        volume = int(intake["actual_volume"])
        notes.append(f"⚑ 用户实测{vol_unit} {volume}，覆盖原型推算（这是全模型最该校准的输入）")

    # ── 开业投入的资产形态：钱没有消失，只是换了形态 ──
    # 这四项的退出回收率差得很远（押金 95%、转让费 50%、设备 30%、装修 25%）。
    # 把「沉没投入」压成一个数字，等于假装它们都一样，退出价就永远是拍脑袋。
    proto_assets = dict(base_pack["operations"].get("assets") or {})
    assets = {k: float(intake[key]) for k, key in
              (("transfer", "transfer_fee"), ("fitout", "fitout"),
               ("equipment", "equipment"), ("deposit", "deposit"),
               ("other", "other_invested"))
              if intake.get(key) not in (None, "") and float(intake.get(key) or 0) > 0}
    if isinstance(intake.get("assets"), dict):
        assets = {k: float(v) for k, v in intake["assets"].items() if float(v or 0) > 0}
    if not assets:
        assets = proto_assets

    # 只报了「沉没投入」一个总数：按原型结构等比拆，并说明这是估算
    tot = intake.get("invested_assets")
    if tot and not assets_is_reported(intake):
        tot = float(tot)
        base_sum = sum(proto_assets.values())
        if base_sum > 0:
            assets = {k: round(v * tot / base_sum) for k, v in proto_assets.items()}
            notes.append(f"沉没投入 {tot:,.0f} 按原型资产结构等比拆分（估算；分项填报更准）")
        else:
            assets = {"transfer": tot}
            notes.append(f"沉没投入 {tot:,.0f} 记作转让费（不折旧，退出按位置价值折价）")

    # ── 总投入：顶层口径。分项资产从总投入里扣出去，剩下的才是账上现金 ──
    # 这笔数是回报率的分母（committed_capital = 现金 + 资产 = 总投入），不能有两个来源。
    tot_inv = intake.get("total_invested")
    if tot_inv not in (None, "", 0):
        tot_inv = float(tot_inv)
        if assets and assets_is_reported(intake):
            cash_left = tot_inv - sum(assets.values())
            notes.append(f"总投入 {tot_inv:,.0f}：资产分项合计 {sum(assets.values()):,.0f}，"
                         f"剩余 {cash_left:,.0f} 进账上现金")
            if cash_left < 0:
                notes.append(f"⚠ 分项资产合计超过总投入 {-cash_left:,.0f}："
                             "现金按 0 记，请核对填报（要么总投入填少了，要么分项填多了）")
            capital = max(0.0, cash_left)
        else:
            # 只报总数没分项：按原型「现金:资产」比例拆，两个口径都保住。
            # 现金用「总投入 − 资产合计」倒推，保证 总投入 = 现金 + 资产 精确成立（分项取整后不再差几块钱）。
            proto_cash = float(base_pack["operations"].get("cash", 0))
            proto_sum = sum(proto_assets.values())
            scale = tot_inv / max(1.0, proto_cash + proto_sum)
            assets = {k: round(v * scale) for k, v in proto_assets.items()}
            capital = round(tot_inv - sum(assets.values()))
            notes.append(f"总投入 {tot_inv:,.0f} 按原型「现金:资产」比例拆分"
                         f"（现金 {capital:,.0f} + 资产 {tot_inv - capital:,.0f}，估算；"
                         "分项填设备/装修/转让费/押金更准）")

    # ── 加盟：一次性费用 + 持续抽成 + 供货溢价 + 品牌流量 ──
    cash = capital
    liab = 0.0
    fr = None
    if franchise:
        fr = dict(FRANCHISE_DEFAULT)
        fr.update(derived.get("franchise_terms") or {})
        cash -= fr["fee_once"]
        unit_cost *= (1 + fr["supply_markup"])
        volume = round(volume * fr["brand_traffic"])
        notes.append(f"加盟「{franchise}」：一次性 {fr['fee_once']:,.0f}，抽成 {fr['royalty']:.0%}，"
                     f"供货溢价 {fr['supply_markup']:.0%}，品牌流量 ×{fr['brand_traffic']}"
                     f"（{'抓取的条款' if derived.get('franchise_terms') else '默认条款，待抓取覆盖'}）")
    if not intake.get("already_open", True):
        # 开办支出分两类：装修/设备/押金换成了资产，证照首批货是**真花掉**的。
        # 原先把整笔当费用从现金里扣掉，导致「开业当天净资产凭空少一半」。
        fit = area * 900                 # 装修 900 元/㎡
        equip = area * 600               # 设备 600 元/㎡
        dep = rent * 2                   # 押二
        misc = 30000                     # 证照/首批货/开业营销
        spend = fit + equip + dep + misc
        cash -= spend
        assets["fitout"] = assets.get("fitout", 0) + fit
        assets["equipment"] = assets.get("equipment", 0) + equip
        assets["deposit"] = assets.get("deposit", 0) + dep
        volume = round(volume * 0.5)     # 冷启动，客流爬坡交给增长
        notes.append(f"未开业：开办支出 {spend:,.0f}（装修 {fit:,.0f}/设备 {equip:,.0f}"
                     f"/押二 {dep:,.0f}/证照杂项 {misc:,.0f}），其中前两项进资产不是费用")
        if cash < 0:
            notes.append(f"⚠ 开办支出超过你报的资金，缺口 {-cash:,.0f}："
                         "这笔钱必须另有来源（借款/追加投入），否则第一个月就付不出账")

    # ── 写回 pack ──
    pack["meta"]["name"] = f"{intake.get('shop_type', '门店')}（{city}）"
    pack["meta"]["pack_id"] = f"intake-{intake.get('industry', 'custom')}"
    pack["meta"]["note"] = "由开局信息采集派生：" + "；".join(notes)

    b = pack["baseline"]
    rev = price * volume * bo.get("days", 30)   # 订阅制（健身房）days=1，到店制 days=30
    b["channel_concentration"] = src["channel_conc"]
    g = b.get("growth_assumption", [0.0, 0.01])
    g_add = src["growth_add"] + (fr["growth_add"] if fr else 0.0)
    b["growth_assumption"] = [g[0] + g_add, g[1] + g_add]
    b["rent_cost_share"] = round(rent / max(1.0, rev), 3)
    b["labor_cost_share"] = round(labor / max(1.0, rev), 3)
    if fr:
        b["supply_chain_depth"] = 0.85          # 总部统一供货：稳定但被绑定
        b["quality_visibility"] = min(1.0, b.get("quality_visibility", 0.5) + 0.15)

    o = pack["operations"]
    # 订阅制（健身房 days=1）与到店制（days=30）的结算节奏不同，沿用原型的
    days = bo.get("days", 30)
    o.update({"price": price, "volume": volume, "days": days,
              "fixed_cost": round(fixed), "unit_cost": round(unit_cost, 2),
              "capacity": capacity, "cash": round(cash), "liability": liab,
              "assets": {k: round(v) for k, v in assets.items() if v > 0}})
    if o["assets"]:
        notes.append("资产账面 " + "、".join(
            f"{ASSET_LABEL.get(k, k)} {v:,.0f}" for k, v in o["assets"].items())
            + "——这些不是费用，退出时按残值变现（押金 95%／转让费 50%／设备 30%／装修 25%／其他 20%）")
    # 预收行业（健身房）：负债与新售流入随会员规模等比缩放，不能拿原型的绝对值
    if pack.get("prepaid", {}).get("enabled"):
        ratio = volume / max(1.0, bo["volume"])
        o["liability"] = round(bo.get("liability", 0) * ratio)
        pack["prepaid"]["inflow_per_month"] = round(
            pack["prepaid"].get("inflow_per_month", 0) * ratio)
        notes.append(f"预收负债 {o['liability']:,.0f}、月均新售流入 "
                     f"{pack['prepaid']['inflow_per_month']:,.0f}（随会员规模 ×{ratio:.2f} 缩放）")
    # 沉没投入（转让费/装修/器械）已在上面的资产结构里，这里不再单独记一个总数：
    # 一个数没法参与退出清算，只有拆成四项才知道能收回多少。
    if intake.get("invested_assets") and not o.get("assets"):
        o["assets"] = {"transfer": float(intake["invested_assets"])}

    # 能力覆写：加盟店通常有品牌方的会员系统与外卖通道
    co = pack.setdefault("capability_overrides", {})
    co["headcount"] = staff
    if fr:
        co["crm_maturity"] = 0.7
        co["channel_count"] = 1                 # 外卖仍需自己开，但品牌方有现成流程

    pack["_intake_notes"] = notes
    pack["_intake_months"] = months
    return pack


def load_intake(path):
    with open(path, encoding="utf-8") as f:
        it = json.load(f)
    missing = [k for k in ("city", "industry") if k not in it]
    assert not missing, f"采集表缺少必填项：{missing}"
    assert it.get("capital") or it.get("total_invested"), \
        "采集表缺少资金口径：capital（账上现金）或 total_invested（总投入）至少填一个"
    return it


if __name__ == "__main__":
    # 自测：把 intake 示例派生成包，打印关键数字
    sys.path.insert(0, "scripts")
    import engine as E
    E.build()
    it = {"city": "示例市·示例商圈", "industry": "cafe", "shop_type": "社区精品咖啡店",
          "capital": 300000, "area_sqm": 60, "staff": 3, "customer_source": "mixed",
          "franchise": None, "months": 24}
    base = E.load_pack("cafe")
    p = derive_pack(it, base)
    print(p["meta"]["name"])
    print(p["meta"]["note"])
    o, b = p["operations"], p["baseline"]
    print(f"月营收 {o['price']*o['volume']*30:,.0f} | 固定 {o['fixed_cost']:,.0f} | "
          f"量 {o['volume']}/{o['capacity']} | 现金 {o['cash']:,.0f}")
    print(f"房租占比 {b['rent_cost_share']:.1%} | 增长 {b['growth_assumption']}")
