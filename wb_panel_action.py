#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_panel_action.py —— 自建面板的写操作执行器。

为什么走内部 HTTP
-----------------
面板上的按钮（刷新积分、测试账号、启停、删除…）在网关里都已经有现成的
POST 接口，且带着完整的校验、日志与错误处理。这里**不重写**这些逻辑，
而是自己签一个面板令牌，再以本机身份回调网关自己的接口：

    面板按钮 → POST /wb-panel/action
             → 本模块签发令牌 → POST 127.0.0.1:8788/accounts/xxx

这样做的前提是网关用的是 ThreadingHTTPServer（确认过，不是单线程），
否则请求处理中再发请求会把自己堵死。

好处：上游改内部实现时这边不用改；坏处：多一次本机回环，可忽略。

安全
----
- 令牌用 PanelSessions.create() 现签，只在这一次调用里用，不落盘、不回传给浏览器。
- 每个 action 白名单固定，不接受任意路径透传。
"""

import json
import urllib.error
import urllib.request

import wb_proxy as WP


# ---------------------------------------------------------------- 白名单
# action → (HTTP 方法, 路径, 允许从 params 透传的字段)
ACTIONS = {
    # 批量刷新类
    "refresh_credits":   ("POST", "/accounts/credits", ("realm",)),
    "refresh_tokens":    ("POST", "/accounts/refresh", ("uid",)),
    "test_all":          ("POST", "/accounts/test", ("uid", "model")),
    "sync_profile":      ("POST", "/accounts/sync-profile", ("uid", "realm")),
    "checkin":           ("POST", "/accounts/checkin", ("uid",)),
    "import_desktop":    ("POST", "/accounts/import/desktop", ("path", "realm", "recoverKey")),
    "scan_desktop":      ("POST", "/accounts/import/desktop", ("realm",)),
    # 单个账号
    "set_enabled":       ("POST", "/accounts/set", ("uid", "enabled")),
    "set_weight":        ("POST", "/accounts/set", ("uid", "weight")),
    "set_note":          ("POST", "/accounts/set", ("uid", "note")),
    "set_disabled_models": ("POST", "/accounts/set", ("uid", "disabledModels")),
    "set_proxy_slot":    ("POST", "/accounts/set", ("uid", "proxySlot")),
    "set_product":       ("POST", "/accounts/product", ("uid", "product", "realm")),
    "delete":            ("POST", "/accounts/delete", ("uid",)),
    # 全量
    "set_all":           ("POST", "/accounts/set-all", ("enabled",)),
    "product_all":       ("POST", "/accounts/product", ("product", "realm")),
    "import":            ("POST", "/accounts/import", ("data", "dryRun", "overwrite", "realm")),
    # 只读导出
    "export":            ("GET", "/accounts/export", ("uid", "realm")),
    # 调度器
    "scheduler_trigger": ("POST", "/scheduler/trigger", ()),
    "scheduler_toggle":  ("POST", "/scheduler/toggle", ("enabled",)),
    # 目录
    "catalog_refresh":   ("POST", "/catalog/refresh", ("realm",)),
    "pricing_refresh":   ("POST", "/pricing/refresh", ()),
    # 客户端配置（Codex / Claude Code / DSH / Crush / OpenCode）
    "agents_apply":      ("POST", "/agents/apply", ("client", "base_url", "model")),
    "agents_restore":    ("POST", "/agents/restore", ("client",)),
    # 日志
    "logs_clear":        ("POST", "/logs/clear", ()),
}

GET_QUERY_EXTRA = {
    "export": {"download": "0", "secrets": "1"},
}


class ActionError(Exception):
    pass


def _token():
    try:
        return WP.PANEL.create()
    except Exception as exc:
        raise ActionError("签不出面板令牌：%s" % exc)


def _call(method, path, payload, base_url, timeout=180):
    """回调网关自己的接口，返回解析后的 JSON。"""
    url = base_url.rstrip("/") + path
    data = None
    headers = {"X-Panel-Token": _token()}
    if method == "POST":
        data = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ActionError("网关返回 %s：%s" % (exc.code, body))
    except urllib.error.URLError as exc:
        raise ActionError("连不上网关：%s" % exc.reason)
    except Exception as exc:
        raise ActionError("%s: %s" % (type(exc).__name__, exc))


def run(action, params, base_url):
    """执行一个面板动作。返回 (ok, data_or_error)。"""
    spec = ACTIONS.get(action)
    if not spec:
        return False, "未知动作：%s" % action
    method, path, allowed = spec

    params = params or {}
    payload = {}
    for k in allowed:
        if k in params and params[k] is not None:
            payload[k] = params[k]

    # 导出：把过滤条件拼进查询串
    if method == "GET":
        q = dict(GET_QUERY_EXTRA.get(action) or {})
        for k, v in payload.items():
            q[k] = v
        if q:
            from urllib.parse import urlencode
            path = path + "?" + urlencode(q)
        payload = None

    # 「一键分配代理」不是单个接口，按上游看板的做法逐个绑定未分配的账号
    if action == "assign_proxy":
        return _assign_proxy(base_url)

    # 「一键配置客户端」要自己补 base_url；还原只要 client
    if action == "agents_apply" and not payload.get("base_url"):
        payload["base_url"] = base_url.rstrip("/") + "/v1"

    try:
        data = _call(method, path, payload, base_url)
    except ActionError as exc:
        return False, str(exc)

    # 统一把「成功/失败」判据抽出来：上游接口有的回 ok，有的回 error
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, msg or "接口返回错误"
    return True, data


def _assign_proxy(base_url):
    """把还没绑定出口的已启用账号，依次分到已启用的代理槽。"""
    try:
        slots = _call("GET", "/proxy/slots", None, base_url)
        accts = _call("GET", "/accounts?realm=all", None, base_url)
    except ActionError as exc:
        return False, str(exc)
    slot_list = [s for s in (slots.get("slots") or []) if s.get("enabled", True)]
    if not slot_list:
        return False, "没有启用的代理槽，先去上游看板的「设置 → 账号代理槽」加一个"
    unbound = [a for a in (accts.get("accounts") or [])
               if a.get("enabled") and not a.get("proxySlot")]
    if not unbound:
        return True, {"assigned": 0, "message": "所有已启用账号都已绑定出口"}
    done = 0
    for i, a in enumerate(unbound):
        slot = slot_list[i % len(slot_list)]
        try:
            _call("POST", "/accounts/set", {"uid": a["uid"], "proxySlot": slot["id"]}, base_url)
            done += 1
        except ActionError:
            pass
    return True, {"assigned": done, "total": len(unbound)}
