#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_gateway.py —— 网关启动器：给看板加一个「对话积分」浮窗。

为什么要有这个文件
------------------
上游 gateway/ 是第三方项目，直接改它会和 `git pull` 打架（用户明确不接受
这个代价）。所以这里采用**运行时注入**：

  1. 把 wb_proxy 当模块导入（不触发它自己的 main），
  2. 注册两个只读接口：/sessions（对话积分数据）、/__wbsc.js（浮窗脚本），
  3. 生成一份「原面板 + 一段 <script>」的副本，把 DASHBOARD_HTML 指向副本，
  4. 再调 wb_proxy.main() 正常启动。

上游文件一个字节都不动。升级后重新启动，副本会自动按新的 dashboard.html
重新生成。

两个接口都只在**本机回环地址**上放行：网关本来就绑 127.0.0.1，但如果哪天用
--lan 开放到局域网，这两个接口会直接拒绝非本机来源。

关掉这套注入：启动前设 WBSC_DISABLE=1。

用法与 wb_proxy.py 完全一致：
    python run_gateway.py --host 127.0.0.1 --port 8788
"""

import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.join(HERE, "gateway")
sys.path.insert(0, GW)
sys.path.insert(0, HERE)

import wb_sessions as ws          # noqa: E402  本项目自己的归集逻辑
import wb_panel_data as wpd       # noqa: E402  自建面板的数据层
import wb_proxy                   # noqa: E402  上游网关（作为模块导入，不自动启动）

USAGE_LOG = os.path.join(GW, "usage", "usage.jsonl")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
SRC_HTML = os.path.join(GW, "dashboard.html")
JS_FILE = os.path.join(HERE, "panel_sessions.js")
PANEL_HTML = os.path.join(HERE, "wb-panel.html")
CACHE_DIR = os.path.join(HERE, ".cache")
LOCAL_HTML = os.path.join(CACHE_DIR, "dashboard.wbsc.html")
STAMP_FILE = os.path.join(CACHE_DIR, "dashboard.wbsc.stamp")

API_PATH = "/sessions"
JS_PATH = "/__wbsc.js"
PANEL_PATH = "/wb-panel"
PANEL_DATA_PATH = "/wb-panel/data"

# 数据缓存：每次刷新都要扫一遍 Codex 会话文件，几秒钟的量级，
# 面板 60 秒自动拉一次，没必要每次都重算。
_CACHE = {"at": 0.0, "data": None}
_CACHE_TTL = 8.0


# ------------------------------------------------------------------ 数据
def _payload(limit=10, days=7):
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["data"]

    gw_rows = ws.load_gateway(USAGE_LOG)
    cx_rows = ws.load_codex(CODEX_SESSIONS, days=days)
    pairs, unmatched = ws.pair(cx_rows, gw_rows)
    convs = ws.summarize(pairs, unmatched)

    items = []
    for c in convs[:limit]:
        items.append({
            "when": ws.fmt_when(c),
            "requests": c["requests"],
            "turns": c.get("turn_count", 0),
            "tokens": c["tokens"],
            "credit": round(c["credit"], 2),
            "models": c["models"],
        })

    data = {
        "conversations": items,
        "total_credit": round(sum(c["credit"] for c in convs), 2),
        "total_count": len(convs),
        "unmatched": len(unmatched),
        "generated_at": time.strftime("%H:%M:%S"),
    }
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data


# ------------------------------------------------------------------ 面板副本
def _build_local_html():
    """按当前 dashboard.html 重新生成带注入的副本。返回副本路径或 None。"""
    try:
        with open(SRC_HTML, "rb") as fh:
            src = fh.read()
    except Exception as exc:
        print("[wbsc] 读不到 dashboard.html：%s" % exc, file=sys.stderr)
        return None

    stamp = hashlib.sha256(src).hexdigest()
    try:
        with open(STAMP_FILE, "r", encoding="utf-8") as fh:
            if fh.read().strip() == stamp and os.path.isfile(LOCAL_HTML):
                return LOCAL_HTML
    except Exception:
        pass

    if b"</body>" not in src:
        print("[wbsc] dashboard.html 里没有 </body>，放弃注入", file=sys.stderr)
        return None

    snippet = ('\n<!-- wbsc: 对话积分浮窗（本地注入，未改动原文件） -->\n'
               '<script src="%s" defer></script>\n' % JS_PATH).encode("utf-8")
    out = src.replace(b"</body>", snippet + b"</body>", 1)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LOCAL_HTML, "wb") as fh:
        fh.write(out)
    with open(STAMP_FILE, "w", encoding="utf-8") as fh:
        fh.write(stamp)
    print("[wbsc] 已生成面板副本（%d 字节，原 %d 字节）" % (len(out), len(src)))
    return LOCAL_HTML


# ------------------------------------------------------------------ 注入
def _install():
    Handler = wb_proxy.Handler
    orig_do_GET = Handler.do_GET

    def _local_ok(self):
        try:
            return self.client_address[0] in ("127.0.0.1", "::1")
        except Exception:
            return False

    def _send_js(self):
        try:
            with open(JS_FILE, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self.send_error(404, "panel_sessions.js missing: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _send_panel(self):
        """自建面板页面。每次请求都重新读文件，改完刷新即可见，不用重启网关。"""
        try:
            with open(PANEL_HTML, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self.send_error(404, "wb-panel.html missing: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def do_GET(self):
        path = (self.path or "").split("?", 1)[0]
        if path == API_PATH or path == "/v1" + API_PATH:
            if not _local_ok(self):
                return self._error(403, "local access only", "invalid_request_error")
            try:
                return self._json(200, _payload())
            except Exception as exc:
                return self._error(500, "sessions unavailable: %s" % exc)
        if path == JS_PATH:
            if not _local_ok(self):
                return self._error(403, "local access only", "invalid_request_error")
            return _send_js(self)
        if path in (PANEL_PATH, PANEL_PATH + "/", PANEL_PATH + "/index.html"):
            if not _local_ok(self):
                return self._error(403, "local access only", "invalid_request_error")
            return _send_panel(self)
        if path == PANEL_DATA_PATH:
            if not _local_ok(self):
                return self._error(403, "local access only", "invalid_request_error")
            try:
                return self._json(200, wpd.build_payload())
            except Exception as exc:
                return self._error(500, "panel data unavailable: %s" % exc)
        return orig_do_GET(self)

    Handler.do_GET = do_GET
    print("[wbsc] 已注册 %s 与 %s" % (API_PATH, JS_PATH))
    print("[wbsc] 自建面板：http://127.0.0.1:8788%s （数据 %s）" % (PANEL_PATH, PANEL_DATA_PATH))


def main():
    if os.environ.get("WBSC_DISABLE") == "1":
        print("[wbsc] WBSC_DISABLE=1，按原样启动")
    else:
        local = _build_local_html()
        if local:
            wb_proxy.DASHBOARD_HTML = local
            _install()
        else:
            print("[wbsc] 面板浮窗未启用，网关照常启动", file=sys.stderr)
    return wb_proxy.main()


if __name__ == "__main__":
    raise SystemExit(main())
