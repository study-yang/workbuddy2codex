#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb2cc.py —— 把本机 WB 网关写成 cc-switch 里的一个 Codex 供应商。

设计原则（都是硬约束，不是建议）：

1. **默认不写**。不加 --write 只打印将要写入的内容，一个字节都不落库。
2. **cc-switch 在跑就拒绝写入**。它在内存里缓存着供应商状态，外部写进去
   会被它下次保存时盖掉。
3. **写前必备份**。用 SQLite 自己的 backup API 拷一份，不是 file copy。
4. **只碰自己那一行**。不动 is_current，不动 settings.json，不动别人的供应商。
5. **幂等**。id 固定，重复运行是 UPDATE，不会长出第二条。

用法：

    python wb2cc.py                      # 预览（默认，安全）
    python wb2cc.py --write              # 真正写入
    python wb2cc.py --model glm-5.3      # 指定默认模型
    python wb2cc.py --gateway http://127.0.0.1:8788
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---- 固定标识：改这里会导致重复供应商 -------------------------------------
PROVIDER_ID = "wb-proxy-local"      # 主键 (id, app_type)，固定 = 幂等
PROVIDER_NAME = "WB Proxy"          # 在 cc-switch 列表里显示的名字

# cc-switch 的「余额查询脚本」：它按 provider 各自的脚本去问余额，
# 没写脚本的 provider 会一直显示 0%。网关为这类客户端准备了
# GET {base}/usage 的余额摘要（remaining / unit / is_active）。
USAGE_SCRIPT = """({
  request: {
    url: "{{baseUrl}}/usage",
    method: "GET",
    headers: { "Authorization": "Bearer {{apiKey}}" }
  },
  extractor: function(response) {
    return {
      isValid: response && (response.is_active !== false),
      remaining: (response && response.remaining) || 0,
      unit: (response && response.unit) || "credits"
    };
  }
})"""
APP_TYPE = "codex"
DEFAULT_GATEWAY = "http://127.0.0.1:8788"
DEFAULT_MODEL = "deepseek-v4.1-flash"


def die(msg, code=1):
    print("错误：%s" % msg, file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# 网关侧
# --------------------------------------------------------------------------

def http_json(url, payload=None, timeout=30):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    # 本机地址一律绕过系统代理：加速器开着时也不该被劫持。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        die("连不上网关 %s（%s）。网关起了吗？" % (url, exc))
    except Exception as exc:
        die("请求网关失败 %s（%s）" % (url, exc))


def fetch_models(gateway):
    payload = http_json(gateway.rstrip("/") + "/v1/models", timeout=90)
    models = payload.get("data") or []
    if not models:
        die("网关返回了空模型清单。账号导入了吗？目录刷新过吗？")
    return models, payload.get("realm"), (payload.get("catalog") or {}).get("source")


# --------------------------------------------------------------------------
# 内容构造
# --------------------------------------------------------------------------

def build_config_toml(base_url_v1, default_model):
    """生成 cc-switch 切换时写进 ~/.codex/config.toml 的那段字符串。

    结构与本机已在用的 TinySnow 供应商保持一致：
      - model_provider 用 "custom"（cc-switch 现有 codex 供应商全是这个）
      - wire_api 必须 responses（0.162 起 chat 已废弃）
      - requires_openai_auth，让 Codex 从 auth.json 取 key
    """
    return (
        'model = "%s"\n'
        'model_provider = "custom"\n'
        '\n'
        '[model_providers.custom]\n'
        'name = "%s"\n'
        'base_url = "%s"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    ) % (default_model, PROVIDER_NAME, base_url_v1)


def build_model_catalog(models):
    """网关 /v1/models → cc-switch modelCatalog.models。"""
    out = []
    for m in models:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        inputs = m.get("input_modalities")
        if not isinstance(inputs, list) or not inputs:
            inputs = ["text", "image"] if m.get("supports_vision") else ["text"]
        levels = m.get("reasoning_efforts")
        if not isinstance(levels, list):
            levels = []
        item = {
            "model": mid,
            "displayName": m.get("name") or mid,
            "contextWindow": int(m.get("context_length") or m.get("max_input_tokens") or 0) or 128000,
            "inputModalities": inputs,
            "reasoningLevels": levels,
        }
        out.append(item)
    return out


def build_settings_config(models, api_key, base_url_v1, default_model):
    return {
        "auth": {"OPENAI_API_KEY": api_key},
        "config": build_config_toml(base_url_v1, default_model),
        "modelCatalog": {"models": build_model_catalog(models)},
    }


# --------------------------------------------------------------------------
# cc-switch 侧
# --------------------------------------------------------------------------

def find_db(explicit=None):
    if explicit:
        return explicit
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".cc-switch", "cc-switch.db"),
        os.path.join(os.environ.get("APPDATA", ""), "cc-switch", "cc-switch.db"),
        os.path.join(os.environ.get("APPDATA", ""), "CC-Switch", "cc-switch.db"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    die("找不到 cc-switch 数据库。用 --db 指定路径。试过：%s"
        % "、".join(p for p in candidates if p))


def ccswitch_running():
    """cc-switch 是否在运行。宁可误判为"在跑"也不要盲写。"""
    if os.name != "nt":
        # 非 Windows 没有 tasklist，退而求其次：看有没有进程名匹配。
        try:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True,
                                 text=True, timeout=5).stdout
            return any("cc-switch" in line or "cc_switch" in line
                       for line in out.splitlines())
        except Exception:
            return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    for line in out.splitlines():
        name = line.split(",")[0].strip('"').lower()
        if "cc-switch" in name or "cc_switch" in name:
            return True
    return False


def backup_db(db_path):
    """用 SQLite backup API 备份（比复制文件安全）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(os.path.dirname(db_path), "backups", "wb2cc-" + stamp)
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, os.path.basename(db_path))
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def read_existing(db_path):
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        row = con.execute(
            "select name, sort_index, settings_config from providers "
            "where id=? and app_type=?", (PROVIDER_ID, APP_TYPE)).fetchone()
        max_sort = con.execute(
            "select coalesce(max(sort_index),0) from providers where app_type=?",
            (APP_TYPE,)).fetchone()[0]
        total = con.execute(
            "select count(*) from providers where app_type=?", (APP_TYPE,)).fetchone()[0]
    finally:
        con.close()
    return row, int(max_sort or 0), int(total or 0)


def upsert(db_path, settings_config, sort_index, existed):
    payload = json.dumps(settings_config, ensure_ascii=False)
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(db_path, timeout=15)
    try:
        with con:                                    # 事务：失败整体回滚
            if existed:
                con.execute(
                    "update providers set name=?, settings_config=?, meta=? "
                    "where id=? and app_type=?",
                    (PROVIDER_NAME, payload,
                     json.dumps({"apiFormat": "openai_responses",
                                 "commonConfigEnabled": False,
                                 "usage_script": {"enabled": True,
                                                  "language": "javascript",
                                                  "code": USAGE_SCRIPT,
                                                  "timeout": 10,
                                                  "autoQueryInterval": 60}}, ensure_ascii=False),
                     PROVIDER_ID, APP_TYPE))
                return "updated"
            con.execute(
                "insert into providers ("
                " id, app_type, name, settings_config, website_url, category,"
                " created_at, sort_index, notes, icon, icon_color, meta,"
                " is_current, in_failover_queue, cost_multiplier"
                ") values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PROVIDER_ID, APP_TYPE, PROVIDER_NAME, payload,
                 DEFAULT_GATEWAY, None, now_ms, sort_index, None, None, None,
                 json.dumps({"apiFormat": "openai_responses",
                             "commonConfigEnabled": False,
                             "usage_script": {"enabled": True,
                                              "language": "javascript",
                                              "code": USAGE_SCRIPT,
                                              "timeout": 10,
                                              "autoQueryInterval": 60}}, ensure_ascii=False),
                 0, 0, "1.0"))
            return "inserted"
    finally:
        con.close()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _fix_balance(args):
    """只改 meta 里的 usage_script，其它列一个字节都不动。"""
    db = find_db(args.db)
    if not db:
        print("没找到 cc-switch.db")
        return 2
    if ccswitch_running():
        print("cc-switch 正在运行。它在内存里缓存着状态，现在写会被盖回去。")
        print("请先完全退出（托盘图标也要退），再跑一次。")
        return 3
    con = sqlite3.connect(db, timeout=15)
    try:
        row = con.execute(
            "select id, name, meta from providers where app_type=? and name=?",
            (APP_TYPE, PROVIDER_NAME)).fetchone()
        if not row:
            print("cc-switch 里没有叫「%s」的供应商，先跑 --write 建一个。" % PROVIDER_NAME)
            return 4
        pid, name, raw_meta = row
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            meta = {}
        meta["usage_script"] = {
            "enabled": True,
            "language": "javascript",
            "code": USAGE_SCRIPT,
            "timeout": 10,
            "autoQueryInterval": 60,
        }
        con.execute("update providers set meta=? where id=? and app_type=?",
                    (json.dumps(meta, ensure_ascii=False), pid, APP_TYPE))
        con.commit()
        print("已给「%s」补上余额查询脚本。" % name)
        print("重启 cc-switch 后，那个 0% 会变成真实余额（单位：积分）。")
        return 0
    finally:
        con.close()


def main(argv=None):
    global PROVIDER_NAME
    ap = argparse.ArgumentParser(
        description="把本机 WB 网关写成 cc-switch 的 Codex 供应商（默认只预览）")
    ap.add_argument("--gateway", default=os.environ.get("WB_GATEWAY") or DEFAULT_GATEWAY,
                    help="网关地址，默认 %s" % DEFAULT_GATEWAY)
    ap.add_argument("--api-key", default=os.environ.get("WB_API_KEY") or "sk-wb-local",
                    help="网关要求的 key（网关未开鉴权时随便填）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="默认模型，默认 %s" % DEFAULT_MODEL)
    ap.add_argument("--name", default=PROVIDER_NAME, help="供应商显示名")
    ap.add_argument("--db", default=None, help="cc-switch.db 路径，默认自动查找")
    ap.add_argument("--write", action="store_true",
                    help="真正写入。不加这个参数 = 只预览")
    ap.add_argument("--fix-balance", action="store_true",
                    help="只给已有供应商补上「余额查询脚本」（不动配置与模型表），"
                         "修 cc-switch 里一直显示 0% 的问题")
    args = ap.parse_args(argv)

    if args.fix_balance:
        return _fix_balance(args)

    PROVIDER_NAME = args.name
    base_url_v1 = args.gateway.rstrip("/")
    if not base_url_v1.endswith("/v1"):
        base_url_v1 += "/v1"

    print("网关      : %s" % args.gateway)
    models, realm, source = fetch_models(args.gateway)
    print("区域      : %s（目录来源：%s）" % (realm, source))
    print("模型      : %d 个" % len(models))
    print()

    # 模型表（含倍率，方便肉眼核对）
    print("%-26s %-10s %-12s %s" % ("模型", "倍率", "上下文", "推理档位"))
    print("-" * 78)
    for m in models:
        print("%-26s %-10s %-12s %s" % (
            m.get("id"), m.get("credits") or "-",
            m.get("context_length") or "-",
            "/".join(m.get("reasoning_efforts") or []) or "-"))
    print()

    if args.model not in {m.get("id") for m in models}:
        die("默认模型 %r 不在网关清单里" % args.model)

    settings_config = build_settings_config(models, args.api_key, base_url_v1, args.model)
    db_path = find_db(args.db)
    row, max_sort, total = read_existing(db_path)
    existed = row is not None

    print("cc-switch : %s" % db_path)
    print("现有 codex 供应商 : %d 个" % total)
    print("本次动作  : %s（id=%s）" % ("更新已有条目" if existed else "新增一条", PROVIDER_ID))
    print()

    print("=== 将要写入的 config（切换时原样写进 ~/.codex/config.toml）===")
    print(settings_config["config"])
    print("=== settings_config 其余部分 ===")
    preview = dict(settings_config)
    preview["config"] = "<上面那段>"
    preview["auth"] = {"OPENAI_API_KEY": args.api_key[:6] + "***"}
    preview["modelCatalog"] = {"models": "<%d 条，见上表>" % len(models)}
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    print()

    if not args.write:
        print("[预览模式] 什么都没写。确认无误后加 --write 执行。")
        return 0

    if ccswitch_running():
        die("cc-switch 正在运行。请先完全退出 cc-switch（托盘也要退），再重跑。\n"
            "原因：它在内存里缓存供应商状态，写入会被它盖回去。")

    backup = backup_db(db_path)
    print("已备份    : %s" % backup)

    action = upsert(db_path, settings_config, max_sort + 1, existed)
    print("完成      : %s" % ("已更新" if action == "updated" else "已新增"))
    print()
    print("下一步：打开 cc-switch → Codex → 选中「%s」→ 切换。" % PROVIDER_NAME)
    print("注意  ：切换动作由 cc-switch 自己做，本脚本不会改「当前供应商」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
