#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 采集层：用模型 API 抓真实世界，冻成快照。

     python3 scripts/fetch.py --intake intake.json --key sk-... 
     python3 scripts/fetch.py --intake intake.json --key sk-... --dry-run
     python3 scripts/fetch.py --replay fixtures/gym.json --out /tmp/x.json   # 不联网自测

四步，和 12 号文档写的一致：

  1. 检索  DeepSeek Search API（POST /v1/tools/search，¥5/千次）
           query 由模板生成，全部带 `before:<asof>` 围栏——不可能抓到创建日之后的新闻。
  2. 归一  模型把每条来源压成 {事实, 槽位, 方向, 力度区间, 置信度}。
  3. 审校  **代码** 卡边界，不信任模型给的数：力度按机理库 clamp、p 按证据强弱压、
           动作 id / 参数名必须在库里存在。被压过的值写进 snapshot.clamped，可追溯。
  4. 冻结  写 snapshots/<industry>-<地区>-<asof>.json，之后整局只读。

为什么归一和审校要分开：模型擅长"这段话说了什么"，不擅长"这件事对你的房租意味着几个百分点"。
让它只做前者，后者交给带边界的代码——这是这套系统能复现的前提。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import engine as E  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "snapshots")

PROVIDER = {
    "name": "deepseek",
    "search": "https://api.deepseek.com/v1/tools/search",
    "fetch": "https://api.deepseek.com/v1/tools/fetch",
    "chat": "https://api.deepseek.com/v1/chat/completions",
    "model": "deepseek-flash",
}

# 每个槽位允许的单次事件力度上限（绝对值）。
# 取自 _mechanisms.yaml 各机理 k_range 的上沿，再取该槽位的最大值：
# 一次事件把房租打掉 40% 不是"发现了机会"，是模型算错了。
_RAW_MAX = {
    "fixed_cost": 0.30, "unit_cost": 0.35, "variable_cost": 0.50,
    "volume": 0.45, "price": 0.25, "churn": 0.50,
    "capacity": 0.30, "revenue": 0.45,
}

QUERY_DIMS = [
    ("trend", "{industry} 行业 门店数 闭店率 趋势 现状"),
    ("price", "{industry} 客单价 价格 价格战 会员费"),
    ("rent", "{region} 商铺 租金 每平米 转让费 空置"),
    ("labor", "{region} {industry} 招聘 工资 底薪 人工成本"),
    ("policy", "{industry} 监管 政策 预付卡 资质 2026 新规"),
    ("subsidy", "{region} 创业 补贴 消费券 扶持 政策"),
]


# ────────────────────────── HTTP ──────────────────────────
def _post(url, body, key, timeout=90, retries=2):
    """只依赖标准库。429/5xx 按文档重试，4xx 直接报错不重试。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for i in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer %s" % key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last = "HTTP %d %s" % (e.code, detail)
            if e.code in (408, 429) or e.code >= 500:
                time.sleep(1.5 * (i + 1))
                continue
            if e.code == 401:
                raise RuntimeError("API Key 无效或已过期（401）。检查 --key 或 DEEPSEEK_API_KEY。")
            if e.code == 402:
                raise RuntimeError("账号余额不足（402）。")
            raise RuntimeError(last)
        except urllib.error.URLError as e:
            last = "网络错误：%s" % e.reason
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(last or "请求失败")


# ────────────────────────── 1. 检索 ──────────────────────────
def build_queries(intake, asof):
    """模板生成，不问模型。可复现、不花钱、能一眼看出查了什么。"""
    industry = intake.get("subtype") or intake.get("industry") or ""
    city = intake.get("city") or ""
    region = city.split("·")[0]
    # 只保留到市一级，区县词会让结果太少
    m = re.match(r"^(.{2,8}?[市州])", region)
    region = m.group(1) if m else region

    out = []
    for dim, tpl in QUERY_DIMS:
        q = tpl.format(industry=industry, region=region)
        out.append({"dim": dim, "q": "%s before:%s" % (q, asof)})
    return out


def search_all(queries, key, count=5, log=print):
    seen, sources, cost = {}, [], 0
    for item in queries:
        r = _post(PROVIDER["search"],
                  {"query": item["q"], "count": count, "need_content": False}, key)
        hits = r.get("results") or []
        if hits:
            cost += 1
        kept = 0
        for h in hits:
            url = h.get("url") or ""
            score = h.get("rerank_score") or 0
            auth = h.get("authority")
            # 优质线 1.5；权威 3+ 可以放宽到 1.2——政府/协会页面摘要短但可信
            if url in seen or (score < 1.5 and (auth is None or auth < 3)):
                continue
            seen[url] = True
            sid = "S%d" % (len(sources) + 1)
            sources.append({
                "id": sid, "query_dim": item["dim"],
                "title": h.get("title") or "",
                "url": url,
                "fact": (h.get("summary") or h.get("snippet") or "")[:1200],
                "date": (h.get("date") or "")[:10] or None,
                "authority": auth,
                "score": score,
            })
            kept += 1
        log("    %-8s %2d 条 · %s" % (item["dim"], kept, item["q"][:52]))
    return sources, cost


# ────────────────────────── 2. 归一 ──────────────────────────
SYS = """你是创业模拟器的资料员。任务只有一件：把给定网页摘要里**确实写着**的事实，转成结构化条目。

硬规矩：
1. 只能使用 sources 里出现的数字与表述。摘要里没有的，一律不许补。
2. 每条 fact/event 必须给 evidence_ids，指向来源 id。给不出证据就不要输出这一条。
3. magnitude 是"这件事对这个经营科目造成多大比例的变化"，用小数（0.06 = 6%），
   取区间。宁可写窄一点、小一点，也不要为了显得有洞察而放大。
4. 判断不了就留空数组。空数组是正确答案，不是失败。
5. 只输出 JSON，不要解释。"""

SCHEMA_HINT = """{
  "brief": ["给老板看的三五句情报，人话，每句带来源编号，如（S3）"],
  "facts": [{
    "claim": "一句话事实",
    "slot": "fixed_cost|unit_cost|variable_cost|volume|price|churn|capacity|revenue",
    "direction": "up|down",
    "magnitude": [0.03, 0.08],
    "confidence": 0.0,
    "evidence_ids": ["S1"]
  }],
  "events": [{
    "id": "E-SNAP-01", "name": "六个字以内的事件名",
    "window": [3, 8], "p": 0.0,
    "slot": "volume", "delta": [-0.15, -0.08],
    "dur": 2, "profile": "step|ramp", "recover": true,
    "group": null, "why": "为什么会发生", "evidence_ids": ["S2"]
  }],
  "action_success": [{"id": "A-COST-02", "delta": 0.15, "why": "依据", "evidence_ids": ["S4"]}],
  "params": [{"id": "A-GROW-02", "key": "channel_fee", "value": 0.14, "why": "依据", "evidence_ids": ["S6"]}],
  "derived": {"rent_per_sqm": null, "rent_note": "", "wage": null, "wage_note": ""},
  "not_changed": ["抓到了但不敢用的东西，及理由"]
}"""


def normalize(intake, pack, sources, key, log=print, replay=None):
    if replay is not None:
        return replay

    lib = E.load("_actions")
    action_ids = [a["id"] for a in lib["actions"]]
    slot_names = list(E.load("_mechanisms")["slots"].keys())

    src_txt = "\n".join(
        "[%s] %s | %s | 权威%d\n    %s" %
        (s["id"], s["title"], (s["date"] or "日期未知"), s["authority"] or -1,
         s["fact"].replace("\n", " "))
        for s in sources)

    user = """项目背景：
- 行业：%(industry)s / %(subtype)s
- 地点：%(city)s
- 面积：%(area)s ㎡，员工 %(staff)s 人
- 自有资金：%(capital)s 元
- 已开业：%(opened)s
- 快照截止日：%(asof)s（这天之后的信息一律不许用）

可选槽位：%(slots)s
可选动作 id：%(actions)s

来源：
%(sources)s

按要求输出 JSON，结构如下：
%(schema)s""" % {
        "industry": intake.get("industry", ""), "subtype": intake.get("subtype", ""),
        "city": intake.get("city", ""), "area": intake.get("area_sqm", ""),
        "staff": intake.get("staff", ""), "capital": intake.get("capital", ""),
        "opened": "是" if intake.get("already_open") else "否",
        "asof": intake.get("asof") or "",
        "slots": "、".join(slot_names), "actions": "、".join(action_ids),
        "sources": src_txt, "schema": SCHEMA_HINT,
    }

    r = _post(PROVIDER["chat"], {
        "model": PROVIDER["model"],
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 4000,
    }, key, timeout=180)
    txt = r["choices"][0]["message"]["content"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            raise RuntimeError("模型没有返回 JSON：%s" % txt[:200])
        return json.loads(m.group(0))


# ────────────────────────── 3. 审校 ──────────────────────────
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def review(intake, extracted, sources, asof):
    """模型说的每句话都要过一遍。压过的值全部留痕。"""
    src_by_id = {s["id"]: s for s in sources}
    used, clamped, dropped = set(), [], []

    def ev(ids):
        ids = [i for i in (ids or []) if i in src_by_id]
        used.update(ids)
        return ids

    def ev_text(ids):
        if not ids:
            return "无来源（内部先验，非本次抓取）"
        return "；".join("%s %s" % (i, src_by_id[i]["title"][:28]) for i in ids)

    # events
    catalog = []
    for i, e in enumerate(extracted.get("events") or []):
        slot = e.get("slot")
        ids = ev(e.get("evidence_ids"))
        if slot not in _RAW_MAX:
            dropped.append({"item": e.get("name"), "why": "槽位 %s 不在引擎语义槽内" % slot})
            continue
        if not ids:
            dropped.append({"item": e.get("name"), "why": "没有可指认的来源"})
            continue
        cap = _RAW_MAX[slot]
        d = e.get("delta") or [0, 0]
        lo, hi = float(d[0]), float(d[-1])
        if lo > hi:
            lo, hi = hi, lo
        lo2, hi2 = _clamp(lo, -cap, cap), _clamp(hi, -cap, cap)
        if (lo, hi) != (lo2, hi2):
            clamped.append({"field": "events.%s.delta" % (e.get("name") or i),
                            "from": [lo, hi], "to": [lo2, hi2],
                            "why": "超出该槽位单次冲击上限 %.0f%%" % (cap * 100)})

        # 证据越弱，发生概率越低；权威 3+ 才允许 p>0.6
        best = max((src_by_id[x]["authority"] or 0) for x in ids)
        p = _clamp(float(e.get("p") or 0.3), 0.05, 0.9)
        if best < 3 and p > 0.6:
            clamped.append({"field": "events.%s.p" % (e.get("name") or i),
                            "from": p, "to": 0.6, "why": "来源权威度不足以支撑高概率"})
            p = 0.6

        w = e.get("window") or [3, 9]
        catalog.append({
            "id": e.get("id") or "E-SNAP-%02d" % (len(catalog) + 1),
            "name": (e.get("name") or "未命名事件")[:12],
            "window": [int(_clamp(int(w[0]), 1, 33)), int(_clamp(int(w[-1]), 1, 36))],
            "p": round(p, 2),
            "slot": slot,
            "delta": [round(lo2, 3), round(hi2, 3)],
            "dur": int(_clamp(int(e.get("dur") or 2), 1, 12)),
            "profile": "ramp" if e.get("profile") == "ramp" else "step",
            "recover": bool(e.get("recover", True)),
            "group": e.get("group"),
            "why": e.get("why") or "",
            "evidence": ev_text(ids),
        })

    # 动作成功率修正
    lib = {a["id"]: a for a in E.load("_actions")["actions"]}
    success = {}
    for a in extracted.get("action_success") or []:
        if a.get("id") not in lib:
            dropped.append({"item": a.get("id"), "why": "动作 id 不存在"})
            continue
        ids = ev(a.get("evidence_ids"))
        if not ids:
            dropped.append({"item": a.get("id"), "why": "成功率修正没有来源"})
            continue
        d = float(a.get("delta") or 0)
        d2 = _clamp(d, -0.25, 0.25)
        if d != d2:
            clamped.append({"field": "action_success.%s" % a["id"], "from": d, "to": d2,
                            "why": "成功率修正幅度上限 ±0.25"})
        success[a["id"]] = {"delta": round(d2, 2), "evidence": ev_text(ids)}

    # 参数默认值（白名单：只认动作 params_hint 里出现过的 key）
    params = {}
    for p in extracted.get("params") or []:
        aid, k = p.get("id"), p.get("key")
        if aid not in lib:
            dropped.append({"item": aid, "why": "动作 id 不存在"})
            continue
        allowed = set((lib[aid].get("params") or {}).keys())
        if k not in allowed:
            dropped.append({"item": "%s.%s" % (aid, k),
                            "why": "参数名不存在（可选：%s）" % ("、".join(sorted(allowed)) or "无")})
            continue
        ids = ev(p.get("evidence_ids"))
        if not ids:
            dropped.append({"item": "%s.%s" % (aid, k), "why": "参数默认值没有来源"})
            continue
        params.setdefault(aid, {})[k] = {"value": p.get("value"), "evidence": ev_text(ids)}

    # derived：只接受三个已知字段，且必须是数字或 null
    d_in = extracted.get("derived") or {}
    derived = {}
    for k in ("rent_per_sqm", "wage"):
        v = d_in.get(k)
        derived[k] = float(v) if isinstance(v, (int, float)) else None
        derived[k + "_note"] = d_in.get(k + "_note") or ""
    derived["franchise_terms"] = d_in.get("franchise_terms")

    snap = {
        "meta": {
            "asof": asof,
            "industry": intake.get("industry"),
            "region": intake.get("city"),
            "collector": "L0 采集层：DeepSeek Search API + %s（自动）" % PROVIDER["model"],
            "queries": [q["q"] for q in intake.get("_queries") or []],
            "sources": [{"id": s["id"], "title": s["title"], "url": s["url"],
                         "fact": s["fact"][:600], "date": s["date"],
                         "authority": s["authority"]} for s in sources],
        },
        "brief": extracted.get("brief") or [],
        "derived": derived,
        "env": {"growth_assumption": None, "event_catalog": catalog},
        "action_adjustments": {"success_adjust": success, "param_defaults": params},
        "not_changed": extracted.get("not_changed") or [],
        "clamped": clamped,
        "dropped": dropped,
        "unused_sources": [s["id"] for s in sources if s["id"] not in used],
    }
    return snap


def validate(snap):
    """快照要能被会话层直接吃下去，这里先把明显不对的挡掉。"""
    errs = []
    if not snap["meta"].get("asof"):
        errs.append("缺少 meta.asof")
    if not snap["meta"].get("sources"):
        errs.append("没有任何来源")
    ids = set()
    for e in snap["env"]["event_catalog"]:
        if e["id"] in ids:
            errs.append("事件 id 重复：%s" % e["id"])
        ids.add(e["id"])
        if not (-1 < e["delta"][0] <= e["delta"][1] < 1):
            errs.append("事件 %s 的 delta 不合法：%s" % (e["id"], e["delta"]))
        if not (0 < e["p"] < 1):
            errs.append("事件 %s 的 p 不合法" % e["id"])
    return errs


# ────────────────────────── 4. 冻结 ──────────────────────────
def _slug(s):
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", "-", s or "")
    return s.strip("-")[:24] or "region"


def collect(intake, key, asof=None, log=print):
    asof = asof or intake.get("asof") or time.strftime("%Y-%m-%d")
    intake = dict(intake, asof=asof)

    arch = _archetype(intake)
    pack = E.load_pack(arch)

    qs = build_queries(intake, asof)
    intake["_queries"] = qs
    log("  1/4 检索 %d 个查询（围栏 before:%s）" % (len(qs), asof))
    sources, billed = search_all(qs, key, log=log)
    if not sources:
        raise RuntimeError("一条来源都没抓到。换个更常见的城市/行业写法再试，或用 --min-score 放松筛选。")
    log("  2/4 归一 %d 条来源 → 模型抽取" % len(sources))
    ext = normalize(intake, pack, sources, key, log=log)
    log("  3/4 审校 %d 事件 / %d 动作修正" %
        (len(ext.get("events") or []), len(ext.get("action_success") or [])))
    snap = review(intake, ext, sources, asof)
    errs = validate(snap)
    if errs:
        raise RuntimeError("快照自检失败：\n  " + "\n  ".join(errs))

    name = "%s-%s-%s.json" % (arch, _slug(intake.get("city")), asof)
    path = os.path.join(SNAP_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    log("  4/4 冻结 → snapshots/%s" % name)
    log("      %d 个事件、%d 条动作修正、%d 处被压回边界、%d 条未采用；"
        "本次 Search 计费请求 %d 次 ≈ ¥%.3f"
        % (len(snap["env"]["event_catalog"]),
           len(snap["action_adjustments"]["success_adjust"]),
           len(snap["clamped"]), len(snap["dropped"]), billed, billed * 0.005))
    return snap, path


def _archetype(intake):
    import intake as I
    arch = I.ARCHETYPE.get(intake.get("industry", ""), "cafe")
    return E.PACK_ALIAS.get(arch, arch)


# ────────────────────────── CLI ──────────────────────────
def main():
    ap = argparse.ArgumentParser(description="L0 采集：抓真实信息，冻成环境快照")
    ap.add_argument("--intake", help="开局采集 JSON 文件")
    ap.add_argument("--key", help="API Key（也可用环境变量 DEEPSEEK_API_KEY）")
    ap.add_argument("--asof", help="快照截止日 YYYY-MM-DD，默认今天")
    ap.add_argument("--replay", help="离线夹具：跳过联网，用给定 JSON 跑通审校/冻结")
    ap.add_argument("--out", help="输出路径（默认 snapshots/ 下自动命名）")
    a = ap.parse_args()

    if a.replay:
        fx = json.load(open(a.replay, encoding="utf-8"))
        intake = fx["intake"]
        asof = a.asof or intake.get("asof") or "2026-11-17"
        intake = dict(intake, asof=asof, _queries=[
            {"dim": d, "q": q} for d, q in fx.get("queries", [])])
        snap = review(intake, fx["llm"], fx["search"], asof)
        errs = validate(snap)
        print("审校结果：%d 事件 / %d 动作修正 / %d 压回 / %d 丢弃" % (
            len(snap["env"]["event_catalog"]),
            len(snap["action_adjustments"]["success_adjust"]),
            len(snap["clamped"]), len(snap["dropped"])))
        for c in snap["clamped"]:
            print("  压回 %-28s %s → %s  (%s)" % (c["field"], c["from"], c["to"], c["why"]))
        for d in snap["dropped"]:
            print("  丢弃 %-28s %s" % (str(d["item"]), d["why"]))
        if errs:
            print("自检失败：", errs)
            return 1
        out = a.out or "/tmp/snapshot-replay.json"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(snap, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已写入", out)
        return 0

    if not a.intake:
        ap.error("需要 --intake（或用 --replay 离线自测）")
    key = a.key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("没有 API Key。设环境变量 DEEPSEEK_API_KEY 或传 --key。")
        return 2

    intake = json.load(open(a.intake, encoding="utf-8"))
    try:
        snap, path = collect(intake, key, a.asof)
    except RuntimeError as e:
        print("\n采集失败：%s\n" % e)
        return 1
    print("\n情报摘要：")
    for b in snap["brief"]:
        print("  ·", b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
