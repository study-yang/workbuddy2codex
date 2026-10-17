#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_sessions.py —— 按「一次对话」汇总积分消耗（对账法，零侵入）。

思路
----
两边各有一半信息：

    网关  usage/usage.jsonl   →  每次请求扣了多少积分（credit，上游给的原始数字）
    Codex ~/.codex/sessions/  →  每次请求属于哪个对话（thread_id / turn_id）

只要把两边对上，就能得出「这次对话总共花了多少积分」。

怎么对：Codex 的 token_usage_record 里有 response_id、input_tokens、output_tokens、
时间戳；网关那边有 prompt_tokens、completion_tokens、时间。同一笔请求两边数字完全
相等（实测：都是 122586 / 635）。所以按「token 数相等 + 时间最接近」配对。

不修改任何一方的代码，也不往任何地方写东西 —— 纯读。

用法
----
    python wb_sessions.py                # 最近 10 次对话
    python wb_sessions.py --days 3       # 只看最近 3 天的会话文件
    python wb_sessions.py --limit 30
    python wb_sessions.py --today
    python wb_sessions.py --unmatched    # 顺便列出没配上的请求
"""

import argparse
import glob
import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HERE, "gateway", "usage", "usage.jsonl")
DEFAULT_SESSIONS = os.path.expanduser("~/.codex/sessions")

# 配对时允许的最大时间偏差（秒）。两边时钟一致，正常误差在 1 秒内，
# 给 120 秒是为了容忍跨时区/夏令时配置错误时不至于全盘配不上。
PAIR_WINDOW = 120

# Codex 只为「主回复」写用量记录，工具调用的续写、子代理、后台请求它不写。
# 所以宁可少配也不能只靠一对一：一条对话的时间区间内，网关收到的全部请求
# 都算这次对话的。GRACE 是最后一条记录之后还允许延伸多久（秒）。
SPAN_GRACE = 300


# ---------------------------------------------------------------- 读网关
def load_gateway(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            iso = d.get("iso") or ""
            try:
                at = datetime.fromisoformat(iso).timestamp()
            except Exception:
                at = float(d.get("at") or 0)
            rows.append({
                "at": at,
                "iso": iso,
                "prompt": int(d.get("prompt_tokens") or 0),
                "completion": int(d.get("completion_tokens") or 0),
                "total": int(d.get("total_tokens") or 0),
                "credit": float(d.get("credit") or 0),
                "model": d.get("model") or "?",
                "cached": int(d.get("cached_tokens") or 0),
                "used": False,
            })
    rows.sort(key=lambda r: r["at"])
    return rows


# ---------------------------------------------------------------- 读 Codex
def load_codex(sessions_dir, days=None, username=None):
    if not os.path.isdir(sessions_dir):
        return []
    cutoff = time.time() - days * 86400 if days else 0
    out = []
    for path in glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"),
                          recursive=True):
        try:
            if cutoff and os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "token_usage_record" not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "token_usage_record":
                    continue
                pl = d.get("payload") or {}
                u = pl.get("usage") or {}
                ts = d.get("timestamp") or ""
                try:
                    at = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                out.append({
                    "at": at,
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
                    "thread": pl.get("thread_id") or pl.get("session_id") or "?",
                    "turn": pl.get("turn_id") or "?",
                    "response": pl.get("response_id") or "",
                    "prompt": int(u.get("input_tokens") or 0),
                    "completion": int(u.get("output_tokens") or 0),
                    "total": int(u.get("total_tokens") or 0),
                    "cached": int(u.get("cached_input_tokens") or 0),
                    "file": os.path.basename(path),
                })
    out.sort(key=lambda r: r["at"])
    return out


# ---------------------------------------------------------------- 配对
def pair(codex_rows, gw_rows):
    """两轮归属：

    ① token 数相等 + 时间最近 → 精确配对；
    ② 剩下的网关流水，落在某条对话 [首条记录, 末条记录 + GRACE] 区间内的，
       归给那条对话。Codex 不记录工具调用的续写，这些中间请求只能按区间算。

    区间会互相裁剪：后一条对话开始后，前一条不再往后延伸。
    返回 (已配对, 未配对网关行)。
    """
    index = {}
    for i, g in enumerate(gw_rows):
        index.setdefault((g["prompt"], g["completion"]), []).append(i)

    paired = []
    for c in codex_rows:
        best = None
        best_dt = None
        for i in index.get((c["prompt"], c["completion"]), []):
            g = gw_rows[i]
            if g["used"]:
                continue
            dt = abs(g["at"] - c["at"])
            if dt > PAIR_WINDOW:
                continue
            if best_dt is None or dt < best_dt:
                best, best_dt = i, dt
        if best is None:
            paired.append((c, None))
            continue
        gw_rows[best]["used"] = True
        paired.append((c, gw_rows[best]))

    # ---- 第二轮：按对话时间区间吸收剩下的流水 ----
    spans = {}
    for c in codex_rows:
        rec = spans.setdefault(c["thread"], [c["at"], c["at"]])
        rec[0] = min(rec[0], c["at"])
        rec[1] = max(rec[1], c["at"])
    ordered = sorted(spans.items(), key=lambda kv: kv[1][0])
    clipped = []
    for i, (thread, (start, last)) in enumerate(ordered):
        end = last + SPAN_GRACE
        if i + 1 < len(ordered):
            end = min(end, ordered[i + 1][1][0])   # 不侵占有记录的后一条对话
        clipped.append((thread, start, max(end, last)))

    absorbed = {}
    for g in gw_rows:
        if g["used"]:
            continue
        for thread, start, end in clipped:
            if start <= g["at"] <= end:
                g["used"] = True
                absorbed.setdefault(thread, []).append(g)
                break

    matched_threads = {c["thread"] for c, g in paired if g is not None}
    matched_threads |= set(absorbed)
    paired = [(c, g) for c, g in paired if c["thread"] in matched_threads]
    for thread, rows in absorbed.items():
        anchor = next(c for c in codex_rows if c["thread"] == thread)
        for g in rows:
            paired.append((anchor, g))

    unmatched = [g for g in gw_rows if not g["used"]]
    return paired, unmatched


# ---------------------------------------------------------------- 汇总
def summarize(paired, unmatched):
    convs = {}
    for c, g in paired:
        key = c["thread"]
        rec = convs.setdefault(key, {
            "thread": key, "start": c["at"], "end": c["at"],
            "start_iso": c["iso"], "end_iso": c["iso"],
            "requests": 0, "matched": 0, "tokens": 0, "credit": 0.0,
            "models": set(), "cached": 0, "turns": set(),
        })
        rec["requests"] += 1
        rec["turns"].add(c["turn"])
        # 有网关行时以网关的 token 为准（它记的是真实发出去的那一次）；
        # 没配上时才退回 Codex 记的数。
        if g is not None:
            rec["tokens"] += g["total"]
            rec["cached"] += g["cached"]
        else:
            rec["tokens"] += c["total"]
            rec["cached"] += c["cached"]
        rec["start"] = min(rec["start"], c["at"])
        if c["at"] >= rec["end"]:
            rec["end"] = c["at"]
            rec["end_iso"] = c["iso"]
        if c["at"] <= rec["start"]:
            rec["start_iso"] = c["iso"]
        if g is not None:
            rec["matched"] += 1
            rec["credit"] += g["credit"]
            rec["models"].add(g["model"])

    out = list(convs.values())
    for r in out:
        r["models"] = sorted(r["models"]) or ["?"]
        r["credit"] = round(r["credit"], 4)
        r["turn_count"] = len(r["turns"])
    out.sort(key=lambda r: r["end"], reverse=True)
    return out


def fmt_credit(v):
    if not v:
        return "0"
    return ("%.2f" % v).rstrip("0").rstrip(".")


def fmt_when(conv):
    today = time.strftime("%Y-%m-%d")
    end = conv["end_iso"] or ""
    return end[11:16] if end.startswith(today) else end[5:16].replace("T", " ")


def render(convs, unmatched, args):
    lines = [""]
    if convs:
        top = convs[0]
        lines.append("  最近一次对话")
        lines.append("  " + "─" * 48)
        lines.append("  积分 %-8s tokens %-10s 请求 %-3s 轮次 %s" % (
            fmt_credit(top["credit"]), f'{top["tokens"]:,}',
            top["requests"], top["turn_count"]))
        lines.append("  模型 %s" % "、".join(top["models"]))
        lines.append("  时间 %s → %s" % (top["start_iso"][11:19],
                                        top["end_iso"][11:19]))
        lines.append("")

    lines.append("  对话列表（最近 %d 次）" % min(len(convs), args.limit))
    lines.append("  %-3s %-8s %-5s %-6s %-10s %-8s %s" % (
        "#", "结束", "请求", "轮次", "tokens", "积分", "模型"))
    lines.append("  " + "-" * 84)
    for i, c in enumerate(convs[:args.limit], 1):
        lines.append("  %-3d %-8s %-5d %-6d %-10s %-8s %s" % (
            i, fmt_when(c), c["requests"], c["turn_count"],
            f'{c["tokens"]:,}', fmt_credit(c["credit"]),
            "、".join(c["models"])[:22]))

    total = sum(c["credit"] for c in convs)
    lines.append("  " + "-" * 84)
    lines.append("  合计 %d 次对话 / %d 次请求，积分 %s" % (
        len(convs), sum(c["requests"] for c in convs), fmt_credit(round(total, 4))))

    if args.unmatched and unmatched:
        lines.append("")
        lines.append("  未归属的请求（非 Codex 发出，或对不上账）：%d 条" % len(unmatched))
        for g in unmatched[-10:]:
            lines.append("    %s  %-22s %7d tokens  积分 %s" % (
                g["iso"][11:19], g["model"], g["total"], fmt_credit(g["credit"])))

    lines.append("")
    lines.append("  积分由上游返回，四舍五入到 0.01。")
    lines.append("  归属依据：Codex 记录的 token 精确配对；它没记的续写请求按对话时间区间吸收。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按一次对话汇总 WB 网关的积分消耗（读 Codex 会话 + 网关流水对账）")
    ap.add_argument("--log", default=DEFAULT_LOG, help="网关 usage.jsonl 路径")
    ap.add_argument("--sessions", default=DEFAULT_SESSIONS, help="Codex 会话目录")
    ap.add_argument("--days", type=float, default=14, help="只读最近 N 天的会话文件，默认 14")
    ap.add_argument("--limit", type=int, default=10, help="显示多少次对话")
    ap.add_argument("--today", action="store_true", help="只看今天")
    ap.add_argument("--unmatched", action="store_true", help="列出没配上的请求")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    gw = load_gateway(args.log)
    cx = load_codex(args.sessions, days=args.days)
    if args.today:
        day = time.strftime("%Y-%m-%d")
        cx = [c for c in cx if c["iso"].startswith(day)]
        gw = [g for g in gw if (g["iso"] or "").startswith(day)]

    pairs, unmatched = pair(cx, gw)
    convs = summarize(pairs, unmatched)

    if args.json:
        for c in convs:
            c.pop("turns", None)
        print(json.dumps(convs[:args.limit], ensure_ascii=False, indent=2))
        return 0

    if not cx and not gw:
        print("\n  还没有记录。先用 Codex 说一句话。\n")
        return 0
    if not cx:
        print("\n  网关有 %d 条流水，但在 %s 下没找到 Codex 的用量记录。\n"
              "  确认 --sessions 指向正确的目录。\n" % (len(gw), args.sessions))
        return 0

    print(render(convs, unmatched, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
