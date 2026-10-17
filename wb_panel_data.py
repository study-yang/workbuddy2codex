#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_panel_data.py —— 给自建面板（wb-panel.html）供数据。

设计原则
--------
1. **全部走网关自己的模块级函数**，不发 HTTP、不做二次鉴权：
   `account_views()` / `credit_grants()` / `fetch_models()` /
   `SCHEDULER.status()` / `get_logs()` / `wb_activity.query()`。
   上游改内部实现时，这里跟着自动改，不会漂移。
2. **字段形状对齐面板的 DATA 契约**，面板的渲染函数一行都不用改。
3. **任何一块失败不影响其它块**：每段独立 try，失败就给空值 + 错误标记，
   面板那边显示「取不到」，而不是整页白屏。
4. 带 8 秒缓存：面板 60 秒自动刷新，没必要每次重算（模型目录、积分都快照）。
"""

import json
import os
import time

# ---------------------------------------------------------------- 内部依赖
# wb_proxy / wb_activity 由 run_gateway.py 先把 gateway/ 塞进 sys.path 后再导入本模块，
# 所以这里直接 import 就行。
import wb_proxy as WP


HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.join(HERE, "gateway")
USAGE_LOG = os.path.join(GW, "usage", "usage.jsonl")

_CACHE = {"at": 0.0, "data": None}
_CACHE_TTL = 8.0


# ---------------------------------------------------------------- 小工具
def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _today_str(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _fmt_at(iso):
    """'2026-10-17T20:20:51+08:00' → '今日 20:20' / '昨日 20:20' / '10-15 20:20'"""
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(iso)
    except Exception:
        return str(iso or "")[:16]
    now = _dt.datetime.now().astimezone()
    d = (now.date() - t.date()).days
    hm = t.strftime("%H:%M")
    if d == 0:
        return "今日 " + hm
    if d == 1:
        return "昨日 " + hm
    return t.strftime("%m-%d %H:%M")


def _fmt_day(iso):
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except Exception:
        return str(iso or "")[:10]


def _fmt_dt(epoch):
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


_TASK_CN = {
    "checkin": "国内版签到",
    "daily_chat": "国际版每日活跃",
    "daily_chat_web": "网页通道打卡",
    "buddy": "Buddy 加油站",
    "travel": "猫猫旅行",
    "token": "Token 保活",
    "night": "夜猫子任务",
}
_TRIGGER_CN = {
    "manual": "手动",
    "account_import": "导入时",
    "scheduler": "调度器",
    "": "调度器",
}


# ---------------------------------------------------------------- 用量流水
def _load_usage(limit_days=None):
    """读 usage.jsonl，返回按时间正序的 dict 列表。文件不存在就给空。"""
    rows = []
    if not os.path.isfile(USAGE_LOG):
        return rows
    try:
        with open(USAGE_LOG, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return rows
    rows.sort(key=lambda r: r.get("at") or 0)
    if limit_days:
        cutoff = time.time() - limit_days * 86400
        rows = [r for r in rows if (r.get("at") or 0) >= cutoff]
    return rows


# ---------------------------------------------------------------- 各数据块
def _block_credits(accts):
    """积分余额：取第一个有积分快照的账号。"""
    for a in accts:
        c = a.get("credits") or {}
        if c.get("remain") is not None:
            ck = c.get("checkin") or {}
            return {
                "remain": _f(c.get("remain")),
                "used": _f(c.get("used")),
                "size": _f(c.get("size")),
                "used_percent": c.get("used_percent") or "",
            }, {
                "streak": int(ck.get("streak_days") or 0),
                "daily": int(ck.get("daily_credit") or 0),
                "todayDone": bool(ck.get("today_checked_in")),
            }
    return {"remain": 0, "used": 0, "size": 0, "used_percent": ""}, {
        "streak": 0, "daily": 0, "todayDone": False}


def _block_expire(accts):
    for a in accts:
        ei = a.get("expiresIn") or ""
        if ei:
            import re
            m = re.search(r"(\d+)", ei)
            days = int(m.group(1)) if m else 0
            ep = a.get("expiresAt") or 0
            return {"days": days, "date": time.strftime("%Y-%m-%d", time.localtime(ep)) if ep else ""}
    return {"days": 0, "date": ""}


def _block_accounts(accts):
    """健康快照卡。"""
    out = []
    for a in accts:
        ei = a.get("expiresIn") or ""
        import re
        m = re.search(r"(\d+)", ei)
        days = int(m.group(1)) if m else 0
        src = a.get("source") or ""
        src_cn = {"desktop-app": "桌面端导入", "oauth": "浏览器登录",
                  "import": "文件导入", "cli": "CLI 添加"}.get(src, src or "未知")
        out.append({
            "name": a.get("nickname") or (a.get("uid") or "")[:8],
            "realm": "国内版" if a.get("realm") == "cn" else "国际版",
            "domain": a.get("domain") or "",
            "source": src_cn,
            "status": "online" if a.get("enabled") else "off",
            "daysLeft": days,
            "credits": _f((a.get("credits") or {}).get("remain")),
            "weight": a.get("weight") or 1,
            "enabled": bool(a.get("enabled")),
        })
    return out


def _block_recent(rows, limit=12):
    out = []
    for r in rows[-limit:][::-1]:
        out.append({
            "time": (r.get("iso") or "")[11:19],
            "model": r.get("model") or "",
            "prompt": int(r.get("prompt_tokens") or 0),
            "completion": int(r.get("completion_tokens") or 0),
            "total": int(r.get("total_tokens") or 0),
            "credit": _f(r.get("credit")),
            "cache": _f(r.get("cache_hit_pct")),
        })
    return out


def _block_hourly(rows):
    """今天 24 个小时的 tokens / 积分。"""
    today = _today_str()
    hs = [{"h": h, "t": 0, "c": 0.0} for h in range(24)]
    for r in rows:
        iso = r.get("iso") or ""
        if not iso.startswith(today):
            continue
        try:
            h = int(iso[11:13])
        except Exception:
            continue
        hs[h]["t"] += int(r.get("total_tokens") or 0)
        hs[h]["c"] += _f(r.get("credit"))
    for h in hs:
        h["c"] = round(h["c"], 2)
    return hs


def _block_today(rows, grants, activity):
    today = _today_str()
    credit = requests = tokens = 0
    cache_sum = 0.0
    for r in rows:
        if not (r.get("iso") or "").startswith(today):
            continue
        credit += _f(r.get("credit"))
        requests += 1
        tokens += int(r.get("total_tokens") or 0)
        cache_sum += _f(r.get("cache_hit_pct"))
    # 今日新增积分：今天发放的积分包面额之和
    gain = 0.0
    for g in grants:
        if (g.get("create_iso") or "")[:10] == today:
            gain += _f(g.get("size"))
    return {
        "credit": round(credit, 2),
        "requests": requests,
        "tokens": tokens,
        "gain": int(gain),
        "cachePct": round(cache_sum / requests, 1) if requests else 0.0,
    }


def _block_models(realm):
    """模型清单。

    注意 `fetch_models()` 返回的是 [(model_id, meta), ...] 元组列表，
    对外的条目要再过一道 `model_entry()`；目录来源看 `catalog_meta()`。
    """
    try:
        entries = WP.fetch_models(realm=realm) or []
        data = [WP.model_entry(mid, meta) for mid, meta in entries]
    except Exception as exc:
        return [], "remote", "取不到：%s" % exc
    try:
        src = (WP.catalog_meta(realm) or {}).get("source") or "remote"
    except Exception:
        src = "remote"
    out = []
    for m in data:
        cr = str(m.get("credits") or "")
        rate = 0.0
        if cr.startswith("x"):
            try:
                rate = float(cr[1:])
            except Exception:
                rate = 0.0
        out.append({
            "id": m.get("id") or "",
            "rate": rate,
            "ctx": int(m.get("context_length") or 0),
            "levels": list(m.get("reasoning_efforts") or []),
            "vision": 1 if m.get("supports_vision") else 0,
            "tools": 1 if m.get("supports_tool_calls") else 0,
            "reason": 1 if m.get("supports_reasoning") else 0,
            "vendor": m.get("vendor") or "",
            "temp": str(m.get("temperature") if m.get("temperature") is not None else ""),
            "desc": m.get("description") or "",
            "name": m.get("name") or "",
        })
    return out, src, ""


def _block_grants(gj):
    """积分获取历史（积分包维度）。"""
    out = []
    for r in gj.get("rows") or []:
        st = r.get("status") or ""
        st_cn = {"active": "使用中", "available": "可用",
                 "expired": "已过期", "exhausted": "已耗尽"}.get(st, st or "可用")
        exp = r.get("expire_iso") or ""
        out.append({
            "name": r.get("name") or "",
            "at": _fmt_day(r.get("create_iso")),
            "size": _f(r.get("size")),
            "left": _f(r.get("remain")),
            "used": _f(r.get("used")),
            "exp": _fmt_day(exp) if exp else ("长期" if r.get("no_expiry") else ""),
            "st": st_cn,
            "days": r.get("days_left"),
        })
    return out


def _block_deductions(gj):
    """积分扣减历史：每个包扣了多少、扣掉百分之几。"""
    out = []
    for r in gj.get("rows") or []:
        size = _f(r.get("size"))
        used = _f(r.get("used"))
        if size <= 0 and used <= 0:
            continue
        out.append({
            "name": r.get("name") or "",
            "size": size,
            "used": used,
            "remain": _f(r.get("remain")),
            "pct": round(used / size * 100, 1) if size else 0.0,
            "exp": _fmt_day(r.get("expire_iso")) if r.get("expire_iso") else ("长期" if r.get("no_expiry") else ""),
            "st": r.get("status") or "",
        })
    out.sort(key=lambda x: -x["used"])
    return out


def _block_activity(limit=40):
    try:
        d = WP.wb_activity.query(limit=str(limit)) or {}
    except Exception as exc:
        return [], "取不到：%s" % exc
    out = []
    for r in d.get("rows") or []:
        out.append({
            "at": _fmt_at(r.get("ts") or ""),
            "acct": r.get("nickname") or (r.get("uid") or "")[:8],
            "realm": "国内版" if r.get("realm") == "cn" else "国际版",
            "task": _TASK_CN.get(r.get("task") or "", r.get("task") or ""),
            "src": _TRIGGER_CN.get(r.get("trigger") or "", "调度器"),
            "ok": bool(r.get("ok")),
            "note": r.get("message") or "",
        })
    return out, ""


def _block_accounts_tbl(accts, rows, activity):
    """账号管理表。"""
    today = _today_str()
    agg = {}
    for r in rows:
        uid = r.get("account") or ""
        a = agg.setdefault(uid, {"req": 0, "tok": 0, "cache": 0, "credit": 0.0})
        a["req"] += 1
        a["tok"] += int(r.get("total_tokens") or 0)
        a["cache"] += int(r.get("cached_tokens") or 0)
        a["credit"] += _f(r.get("credit"))

    today_tasks = {}
    for a in activity:
        if a["at"].startswith("今日") and a["ok"]:
            # 同一个任务今天可能触发多次（导入时一次、手动一次），按任务名去重
            lst = today_tasks.setdefault(a["acct"], [])
            if a["task"] not in lst:
                lst.append(a["task"])

    out = []
    for a in accts:
        uid = a.get("uid") or ""
        st = agg.get(uid, {"req": 0, "tok": 0, "cache": 0, "credit": 0.0})
        ei = a.get("expiresIn") or ""
        import re
        m = re.search(r"(\d+)", ei)
        days = int(m.group(1)) if m else 0
        ep = a.get("expiresAt") or 0
        tok = st["tok"]
        out.append({
            "uid": uid,
            "nick": a.get("nickname") or uid[:8],
            "note": a.get("note") or "",
            "realm": a.get("realm") or "cn",
            "source": a.get("source") or "",
            "enabled": bool(a.get("enabled")),
            "today": today_tasks.get(a.get("nickname") or uid[:8], []),
            "exit": ("代理 " + a["proxySlot"]) if a.get("proxySlot") else "直连",
            "weight": a.get("weight") or 1,
            "remain": _f((a.get("credits") or {}).get("remain")),
            "size": _f((a.get("credits") or {}).get("size")),
            "expIn": days,
            "expEnd": time.strftime("%Y-%m-%d", time.localtime(ep)) if ep else "",
            "req": st["req"],
            "tok": ("%.2fM" % (tok / 1e6)) if tok >= 1e6 else ("%.1fK" % (tok / 1e3) if tok else "0"),
            "cache": ("%.2fM" % (st["cache"] / 1e6)) if st["cache"] >= 1e6 else ("%.1fK" % (st["cache"] / 1e3) if st["cache"] else "0"),
            "product": a.get("product") or "workbuddy",
        })
    return out


def _block_heatmap(rows, days=365):
    """近一年每日用量（热力图）。"""
    cutoff = time.time() - days * 86400
    byday = {}
    for r in rows:
        at = r.get("at") or 0
        if at < cutoff:
            continue
        d = time.strftime("%Y-%m-%d", time.localtime(at))
        b = byday.setdefault(d, {"t": 0, "c": 0.0, "n": 0})
        b["t"] += int(r.get("total_tokens") or 0)
        b["c"] += _f(r.get("credit"))
        b["n"] += 1
    out = []
    for d in sorted(byday):
        b = byday[d]
        out.append({"d": d, "t": b["t"], "c": round(b["c"], 2), "n": b["n"]})
    return out


def _block_estimate(credits, rows, days=7):
    """剩余用量估算：按最近 N 天的平均日消耗算还能撑多久。"""
    remain = credits.get("remain") or 0
    cutoff = time.time() - days * 86400
    recent = [r for r in rows if (r.get("at") or 0) >= cutoff]
    per_day = sum(_f(r.get("credit")) for r in recent) / float(days)
    if per_day <= 0:
        return {"days": None, "perDay": 0.0, "msg": "最近 %d 天没有消耗，估算不了" % days}
    d = remain / per_day
    return {
        "days": round(d, 1),
        "perDay": round(per_day, 2),
        "msg": "按最近 %d 天平均每天 %.2f 积分算，还能用约 %.0f 天" % (days, per_day, d),
    }


def _block_perf(rows):
    """模型性能：每个模型的请求数 / tokens / 速度 / 积分。"""
    agg = {}
    for r in rows:
        m = r.get("model") or ""
        a = agg.setdefault(m, {"n": 0, "t": 0, "c": 0.0, "ms": 0, "tps": 0.0})
        a["n"] += 1
        a["t"] += int(r.get("total_tokens") or 0)
        a["c"] += _f(r.get("credit"))
        a["ms"] += int(r.get("elapsed_ms") or 0)
        a["tps"] += _f(r.get("tokens_per_sec"))
    out = []
    for m, a in agg.items():
        out.append({
            "model": m, "requests": a["n"], "tokens": a["t"],
            "credit": round(a["c"], 2),
            "tps": round(a["tps"] / a["n"], 1) if a["n"] else 0,
            "avgMs": int(a["ms"] / a["n"]) if a["n"] else 0,
        })
    out.sort(key=lambda x: -x["credit"])
    return out


def _block_by_acct(rows, accts):
    """账号用量透视：每个账号的请求 / tokens / 积分 / 缓存命中。"""
    name = {}
    for a in accts:
        name[a.get("uid") or ""] = a.get("nickname") or (a.get("uid") or "")[:8]
    agg = {}
    for r in rows:
        uid = r.get("account") or ""
        a = agg.setdefault(uid, {"n": 0, "t": 0, "c": 0.0, "cache": 0})
        a["n"] += 1
        a["t"] += int(r.get("total_tokens") or 0)
        a["c"] += _f(r.get("credit"))
        a["cache"] += _f(r.get("cache_hit_pct"))
    out = []
    for uid, a in agg.items():
        out.append({
            "name": name.get(uid, uid[:8] or "未知"),
            "requests": a["n"], "tokens": a["t"],
            "credit": round(a["c"], 2),
            "cachePct": round(a["cache"] / a["n"], 1) if a["n"] else 0.0,
        })
    out.sort(key=lambda x: -x["credit"])
    return out


def _block_by_pair(rows, accts):
    """账号 × 模型 的用量明细。"""
    name = {}
    for a in accts:
        name[a.get("uid") or ""] = a.get("nickname") or (a.get("uid") or "")[:8]
    agg = {}
    for r in rows:
        uid = r.get("account") or ""
        m = r.get("model") or ""
        k = (uid, m)
        a = agg.setdefault(k, {"n": 0, "t": 0, "c": 0.0})
        a["n"] += 1
        a["t"] += int(r.get("total_tokens") or 0)
        a["c"] += _f(r.get("credit"))
    out = []
    for (uid, m), a in agg.items():
        out.append({
            "acct": name.get(uid, uid[:8] or "未知"),
            "model": m, "requests": a["n"], "tokens": a["t"],
            "credit": round(a["c"], 2),
        })
    out.sort(key=lambda x: -x["credit"])
    return out


def _block_tasks(checkin, today):
    """成长任务。前两项能从真实状态推出来，后两项网关看不到，保持未完成。"""
    return [
        {"n": "每日签到", "d": "每天一次，连签有额外奖励", "done": bool(checkin.get("todayDone"))},
        {"n": "每日活跃对话", "d": "与任意模型完成一轮对话", "done": (today.get("requests") or 0) > 0},
        {"n": "探索新模型", "d": "使用一个本周没用过的模型", "done": False},
        {"n": "分享邀请", "d": "邀请好友注册并激活", "done": False},
    ]


def _block_logs(limit=40):
    try:
        d = WP.get_logs(limit=limit) or {}
    except Exception as exc:
        return [], "取不到：%s" % exc
    out = []
    for l in d.get("logs") or []:
        lv = (l.get("level") or "INFO").upper()
        out.append({
            "t": l.get("time") or "",
            "l": {"INFO": "i", "WARN": "w", "WARNING": "w", "ERROR": "e"}.get(lv, "i"),
            "m": l.get("msg") or "",
        })
    return out, ""


def _block_scheduler():
    try:
        st = WP.SCHEDULER.status() if WP.SCHEDULER else {"enabled": False}
    except Exception as exc:
        return {"enabled": False, "next": "取不到", "last": str(exc)}
    return {
        "enabled": bool(st.get("enabled")),
        "next": (st.get("next_run_time") or "").replace("2026-", "") or "—",
        "last": (st.get("last_run_time") or "").replace("2026-", "") or "—",
        "mode": st.get("mode_cn") or st.get("mode") or "",
    }


def _block_conversations(limit=10, days=7):
    """对话级积分：复用 wb_sessions 的对账逻辑。"""
    try:
        import wb_sessions as ws
        gw_rows = ws.load_gateway(USAGE_LOG)
        cx_rows = ws.load_codex(os.path.expanduser("~/.codex/sessions"), days=days)
        pairs, unmatched = ws.pair(cx_rows, gw_rows)
        convs = ws.summarize(pairs, unmatched)
    except Exception as exc:
        return [], "取不到：%s" % exc
    out = []
    for c in convs[:limit]:
        out.append({
            "when": ws.fmt_when(c),
            "requests": c["requests"],
            "turns": c.get("turn_count", 0),
            "tokens": c["tokens"],
            "credit": round(c["credit"], 2),
            "models": c["models"],
        })
    return out, ""


# ---------------------------------------------------------------- 总装
def build_payload():
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["data"]

    # 区域：读一次持久化文件，避免调用早于 main() 时拿到默认的 intl
    realm = getattr(WP, "CURRENT_REALM", "cn")
    try:
        realm = WP.load_persisted_realm() or realm
    except Exception:
        pass
    notes = {}

    # 账号（其它很多块都依赖它）
    try:
        accts = WP.account_views(realm=realm) or []
    except Exception as exc:
        accts, notes["accounts"] = [], "账号读取失败：%s" % exc

    credits, checkin = _block_credits(accts)
    expire = _block_expire(accts)
    accounts = _block_accounts(accts)

    models, cat_src, m_err = _block_models(realm)
    if m_err:
        notes["models"] = m_err

    try:
        gj = WP.credit_grants() or {}
    except Exception as exc:
        gj, notes["grants"] = {}, "积分包读取失败：%s" % exc
    grants = _block_grants(gj)
    deductions = _block_deductions(gj)

    activity, a_err = _block_activity()
    if a_err:
        notes["activity"] = a_err

    rows = _load_usage()
    recent = _block_recent(rows)
    hourly = _block_hourly(rows)
    today = _block_today(rows, gj.get("rows") or [], activity)
    accounts_tbl = _block_accounts_tbl(accts, rows, activity)
    heatmap = _block_heatmap(rows)
    estimate = _block_estimate(credits, rows)
    perf = _block_perf(rows)
    by_acct = _block_by_acct(rows, accts)
    by_pair = _block_by_pair(rows, accts)

    logs, l_err = _block_logs()
    if l_err:
        notes["logs"] = l_err

    convs, c_err = _block_conversations()
    if c_err:
        notes["conversations"] = c_err

    payload = {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "realm": realm,
        "meta": {
            "realm": realm,
            "realmLabel": "国内版 cn" if realm == "cn" else "国际版 intl",
            "host": "127.0.0.1:8788",
            "catalogSource": cat_src,
        },
        "credits": credits,
        "checkin": checkin,
        "today": today,
        "expire": expire,
        "accounts": accounts,
        "hourly": hourly,
        "recent": recent,
        "models": models,
        "grants": grants,
        "deductions": deductions,
        "activity": activity,
        "accountsTbl": accounts_tbl,
        "heatmap": heatmap,
        "estimate": estimate,
        "perf": perf,
        "byAcct": by_acct,
        "byPair": by_pair,
        "tasks": _block_tasks(checkin, today),
        "scheduler": _block_scheduler(),
        "logs": logs,
        "conversations": convs,
        "notes": notes,
    }
    _CACHE["at"] = now
    _CACHE["data"] = payload
    return payload
