#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""open_panel.py —— 等网关真的起来了，再打开面板。

为什么单独一个脚本
------------------
`start-gateway.bat` 里如果直接 `start "" http://...` 会抢在网关监听之前执行，
浏览器打开看到「无法访问」。用 `timeout` 盲等也不可靠：机器忙的时候 3 秒不够，
机器快的时候白等。

这里轮询端口，通了才开浏览器；超过 30 秒就放弃（不弹窗、不报错，免得吵）。

用法：
    python open_panel.py [--port 8788] [--timeout 30] [--path /]
"""

import argparse
import socket
import sys
import time
import webbrowser


def wait_port(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--path", default="/")
    args = ap.parse_args(argv)

    if not wait_port(args.host, args.port, args.timeout):
        # 网关没起来就别开浏览器了，交给 bat 窗口里的报错
        print("[open_panel] %ds 内没等到 %s:%d，不打开浏览器"
              % (int(args.timeout), args.host, args.port), file=sys.stderr)
        return 1

    url = "http://%s:%d%s" % (args.host, args.port, args.path)
    try:
        webbrowser.open(url)
        print("[open_panel] 已打开 %s" % url)
    except Exception as exc:
        print("[open_panel] 打开浏览器失败：%s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
