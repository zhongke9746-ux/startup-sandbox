#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用层：把会话运行时包成 JSON 友好的结构。

设计原则：
  · 这里不重写任何结算逻辑——rerun/apply_choices/forecast/levers 全部原样复用；
  · 输出只有两种：结构化数据（给 UI 画）和 markdown（给 UI 读）；
  · 每个函数都是纯读或「读-改-存」，不做隐式副作用。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E          # noqa: E402
import session as S         # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(HERE, "snapshots")


# ────────────────────────── 元信息 ──────────────────────────
def meta():
    packs = []
    for key, fname in E.PACKS.items():
        try:
            p = E.load_pack(key)
            packs.append({"key": key, "name": p["meta"]["name"],
                          "note": p["meta"].get("note", "")})
        except Exception:
            continue
    snaps = []
    if os.path.isdir(SNAP_DIR):
        for f in sorted(os.listdir(SNAP_DIR)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(SNAP_DIR, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                m = d.get("meta", {})
                snaps.append({"file": f,
                              "label": m.get("label") or f"{m.get('industry', '')} · "
                                       f"{m.get('region', '')}",
                              "asof": m.get("asof"),
                              "sources": len(m.get("sources", []) or [])})
            except Exception:
                continue
    return {
        "packs": packs, "snapshots": snaps,
        "archetypes": ["cafe", "gym", "cnrestaurant"],
        "fields": [{"key": k, "label": lab, "hint": h} for k, lab, h in _intake_fields()],
    }


def _intake_fields():
    import intake as I
    return I.FIELDS


# ────────────────────────── 开局 ──────────────────────────
def create(pack_key, seed, name, months, intake=None, snapshot_file=None):
    """等价于 `session.py new`，但不打印；返回 st。"""
    E.build()
    pack = E.load_pack(pack_key)
    cfg = E.load("_randomness")
    months = int(months or 24)

    snap = None
    if snapshot_file:
        path = snapshot_file if os.path.isabs(snapshot_file) else os.path.join(SNAP_DIR, snapshot_file)
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        if not snap.get("meta", {}).get("asof"):
            raise ValueError("快照缺少 meta.asof")

    if intake:
        import intake as I
        arch = I.ARCHETYPE.get(intake.get("industry", ""), pack_key)
        base = E.load_pack(arch)
        derived = (snap or {}).get("derived") or {}
        pack = I.derive_pack(intake, base, derived)
        pack_key = E.PACK_ALIAS.get(arch, arch)
        months = pack.pop("_intake_months", months)

    sid = name or f"{pack_key}-{seed}"
    base_sid, n = sid, 2
    while os.path.exists(S._path(sid)):
        sid = f"{base_sid}-{n}"
        n += 1

    macro = S._macro(cfg, months)
    env = S.sample_env(pack, cfg, months, f"{pack_key}|{seed}", macro, snap=snap)
    st = {
        "sid": sid, "pack": pack_key, "pack_name": pack["meta"]["name"],
        "seed": seed, "months_total": months, "month": 0,
        "cadence": "event", "env": env, "actions": [], "decisions": [], "locks": [],
        "over": False, "created_at": None,
        **({"snapshot": snap} if snap else {}),
        **({"intake": intake, "pack_data": pack} if intake else {}),
    }
    rows, mon, _ = S.rerun(st)
    m, trig = S.next_point(st, rows, mon)
    st["month"] = (m or 1) - 1
    st["pending"] = m
    st["pending_triggers"] = trig
    S.save_session(st)
    return st


# ────────────────────────── 决策点 ──────────────────────────
def _changes_since_decision(st):
    """上次决策以来，用户自己能看见的三个结构变化（客单价、单位成本、固定成本）。"""
    if not st["decisions"] or st["month"] < 1:
        return []
    pack = S._pack(st)
    lx = S._lex(pack)
    last = max(d["at"] for d in st["decisions"])
    if st["month"] <= last:
        return []
    _, mon, _ = S.rerun(st)
    a, b = mon[last - 1], mon[st["month"] - 1]
    out = []
    if abs(b["价"] - a["价"]) >= 0.3:
        out.append(f"{lx['price']} ¥{a['价']:.0f} → ¥{b['价']:.0f}")
    dys = pack["operations"]["days"]
    ua = a["货品"] / max(1e-9, a["成交"] * dys)
    ub = b["货品"] / max(1e-9, b["成交"] * dys)
    if abs(ub - ua) / max(1e-9, ua) >= 0.03:
        out.append(f"单{lx['vol_unit']}{lx['material']}成本 ¥{ua:.2f} → ¥{ub:.2f}")
    if abs(b["固定"] - a["固定"]) / max(1e-9, a["固定"]) >= 0.03:
        out.append(f"月固定成本 ¥{a['固定']:,.0f} → ¥{b['固定']:,.0f}")
    return out


def _finished(st):
    """是不是已经跑完。

    老会话可能停在最后一个月却没打 over 标记（当年收尾逻辑没写），
    页面上点进去会给出一个不存在的「第 25 个月」决策点。
    这里统一按「月份到位且没有待决月份」判定为结束。
    """
    if st.get("over"):
        return True
    total, month = st.get("months_total") or 0, st.get("month") or 0
    return bool(total) and month >= total and (st.get("pending") or 0) <= month


def point(st):
    """当前决策点：状态 + 触发理由 + 动作菜单（含被剔除的及原因）。"""
    pack = S._pack(st)
    lx = S._lex(pack)
    snap = S.snapshot(st)

    if _finished(st):
        left = max(0, (st["months_total"] or 0) - (st["month"] or 0))
        return {"over": True,
                "end_reason": st.get("end_reason") or "跑完赛季，正常收尾",
                "end_kind": st.get("end_kind") or "natural",
                # 只有「我自己按的停」能撤销；死亡和跑满赛季不行
                "can_resume": st.get("end_kind") == "user",
                "left": left,
                "death_month": st.get("end_month") or st.get("death_month") or st.get("month"),
                "month": st["month"], "month_now": st["month"],
                "months_total": st["months_total"], "state": _state(st, snap, lx)}

    month = st.get("pending") or (st["month"] + 1)
    opts, blocked, slots = S.build_options(st, month, snap)
    intro = []
    if st["month"] == 0 and not st["decisions"]:
        intro = [ln[2:] for ln in S.render_intro(pack).split("\n") if ln.startswith("- ")]

    return {
        "over": False,
        "sid": st["sid"], "pack_name": st["pack_name"],
        "month": month, "month_now": st["month"], "months_total": st["months_total"],
        "lex": lx, "slots": slots,
        "state": _state(st, snap, lx),
        "triggers": [{"code": c, "text": t} for c, t in (st.get("pending_triggers") or [])],
        "changes": _changes_since_decision(st),
        "intro": intro,
        "options": [{
            "id": o["id"], "name": o["name"], "family": o["family"],
            "family_label": o["family_label"], "blurb": o["blurb"],
            "tier": o["tier"], "confidence": o["confidence"],
            "reversibility": o["reversibility"],
            "one_time": o.get("one_time", 0), "affordable": o.get("affordable", True),
            "warnings": o["warnings"], "intel": o.get("intel"),
            "params_hint": o.get("params_hint", {}),
        } for o in opts],
        "blocked": [{"id": b["id"], "name": b["name"], "why": b["why"]} for b in blocked],
        "history": [{"at": d["at"],
                     "chosen": [c["id"] if isinstance(c, dict) else str(c)
                                for c in d["chosen"]],
                     "cash": d["cash"], "profit": d["profit"]} for d in st["decisions"][-6:]],
    }


def _state(st, snap, lx):
    return {
        "month": snap["month"], "cash": snap["cash"], "net": snap["net"],
        "runway": snap["runway"], "vol": snap["vol"], "price": snap["price"],
        "util": snap["util"], "profit": snap["profit"],
        # 投资口径：net 是"还会不会断粮"，net_worth 是"投的钱现在值多少"
        "assets": snap.get("assets", 0), "net_worth": snap.get("net_worth", snap["net"]),
        "capital": snap.get("capital", 0), "salvage": snap.get("salvage", 0),
        "vol_label": lx["vol"], "price_label": lx["price"],
    }


# ────────────────────────── 推进一月 ──────────────────────────
def choose(sid, choices):
    """等价于 `session.py choose`：结算 → 找下一个决策点 → 存。返回本月结果 + 新决策点。"""
    st = S.load_session(sid)
    if _finished(st):
        raise ValueError(f"这一局已经结束了：{st.get('end_reason') or '跑完赛季'}")

    target = st.get("pending") or (st["month"] + 1)
    if st["month"] + 1 != target:
        st["month"] = target - 1

    choices = [{"id": c["id"], "params": c.get("params") or {}} for c in (choices or [])]
    res = S.apply_choices(st, choices)
    st["decisions"].append(S._decision(st, res, choices))

    out = {
        "month": res["month"], "accepted": res["accepted"], "rejected": res["rejected"],
        "cash_before": res["cash_before"], "cash_after": res["cash_after"],
        "profit": res["profit"], "net": res["net"],
        "util": res["util"], "vol": res["vol"],
        "story": res["story"],
    }

    if st.get("over"):
        S.save_session(st)
        return {"result": out, "point": point(st)}

    rows, mon, _ = S.rerun(st)
    m, trig = S.next_point(st, rows, mon)
    if m is None:
        st["month"] = st["months_total"]
        st["over"] = True
        st["end_reason"] = "跑完赛季，正常收尾"
        st["death_month"] = st["months_total"]
        S.save_session(st)
        return {"result": out, "point": point(st)}

    if rows[m - 1]["现金"] < 0:
        st["month"], st["over"], st["death_month"] = m, True, m
        st["end_reason"] = f"流动性死亡：第 {m} 个月现金归零"
        S.save_session(st)
        return {"result": out, "point": point(st)}

    skipped_from = st["month"] + 1
    st["month"] = m - 1
    st["pending"], st["pending_triggers"] = m, trig
    S.save_session(st)
    if m > skipped_from:
        evs = [e.get("name", e["id"]) for e in st["env"]["events"]
               if skipped_from <= e["start"] < m]
        span = f"第 {skipped_from} 个月" if m - 1 == skipped_from else f"第 {skipped_from}–{m - 1} 个月"
        out["skip"] = {"from": skipped_from, "to": m - 1, "events": evs, "span": span}
    return {"result": out, "point": point(st)}


# ────────────────────────── 主动结束 / 继续 ──────────────────────────
def end(sid, reason=None):
    """随时按停。停在当前已结算的月份，不解散资产（那是 A-STR-02 的事）。"""
    st = S.load_session(sid)
    if _finished(st):
        return {"point": point(st), "already": True}
    S.end_session(st, reason)
    S.save_session(st)
    return {"point": point(st), "ended": True}


def resume(sid):
    """撤销「主动结束」，回到同一个决策点。"""
    st = S.load_session(sid)
    if st.get("end_kind") != "user":
        # 老会话可能只是「月份到位」而没打 over 标记，也要按自然结束处理
        if _finished(st):
            raise ValueError(f"这一局是自然结束的（{st.get('end_reason') or '跑满赛季'}），不能继续")
        return {"point": point(st), "already": True}
    S.resume_session(st)
    S.save_session(st)
    return {"point": point(st)}


# ────────────────────────── 账本 ──────────────────────────
def ledger(st):
    rows, mon, _ = S.rerun(st)
    out = []
    for r, m in zip(rows, mon):
        out.append({"month": r["月"], "cash": round(r["现金"]), "net": round(r["净现金"]),
                    "profit": round(r["净利"]), "revenue": round(r["营收"]),
                    "util": round(m["利用率"], 3), "vol": round(m["成交"]),
                    "price": round(r["价"], 1)})
    return {"ledger": out, "months_total": st["months_total"],
            "events": [{"at": e["start"], "name": e.get("name", e["id"]),
                        "delta": round(e["delta"], 3), "slot": e["slot"]}
                       for e in sorted(st["env"]["events"], key=lambda x: x["start"])]}


# ────────────────────────── 预测 / 报告 / 审计 ──────────────────────────
def forecast(sid, n=400):
    st = S.load_session(sid)
    fc = S.forecast(st, n)
    if fc is None:
        return {"over": True}
    return {"n": fc["n"], "survival": fc["survival"], "p50": fc["p50"],
            "p5": fc["p5"], "median_monthly": fc.get("median_monthly"),
            "table": fc.get("table") or []}


def report(sid):
    st = S.load_session(sid)
    return {"markdown": S.render_report(st)}


def audit(sid):
    st = S.load_session(sid)
    base, full, rows = S.audit(st)
    return {"start_net": round(S.snapshot(st)["net"]), "base": round(base[-1]["净现金"]),
            "full": round(full[-1]["净现金"]),
            "delta": round(full[-1]["净现金"] - base[-1]["净现金"]),
            "rows": [{"at": r["at"], "name": r["name"], "failed": r["failed"],
                      "delta": round(r["delta"])} for r in rows]}


def collect(intake, key):
    """L0 采集：抓真实信息 → 冻结快照。返回给 UI 的摘要，不给完整快照。

    Key 只在这一次调用里用，落盘的东西里没有它。
    """
    import fetch as F
    snap, path = F.collect(intake, key, log=lambda *a, **k: None)
    return {"snapshot": os.path.basename(path),
            "asof": snap["meta"]["asof"],
            "brief": snap.get("brief") or [],
            "sources": len(snap["meta"]["sources"]),
            "events": len(snap["env"]["event_catalog"]),
            "adjusted": len(snap["action_adjustments"]["success_adjust"])
                        + len(snap["action_adjustments"]["param_defaults"]),
            "clamped": len(snap["clamped"]), "dropped": len(snap["dropped"])}


def sessions():
    out = []
    d = os.path.join(HERE, "sessions")
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                s = json.load(fh)
            out.append({"sid": s["sid"], "pack_name": s.get("pack_name"),
                        "month": s.get("month"), "months_total": s.get("months_total"),
                        "over": _finished(s), "seed": s.get("seed")})
        except Exception:
            continue
    return out
