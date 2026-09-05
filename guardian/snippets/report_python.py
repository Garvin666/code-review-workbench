#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Guardian 上报片段 · Python 心跳
================================
把这段代码放进被守护方的主循环 / 定时器 / 守护线程里。
它每 60 秒向守护中枢上报一次"我还活着"。异常时同时上报 detail。

使用方法：
    1) 修改下面两个常量：HUB 与 TOKEN
       HUB   = 守护中枢地址，例如 http://127.0.0.1:8700
       TOKEN = 登记目标后从工作台（或 POST /api/v1/targets 响应）拿到的 token
    2) 在被守护方的进程里调用 report() 即可：
         report()                     # 心跳，metric=online, ok=True
         report(False, "crash")      # 异常上报
         report(False, "perf_slow", 3200)  # 性能上报（value=ms）
       单独调用 send_event("crash", "OOM in worker") 上报崩溃
"""

import json
import time
import urllib.request
import urllib.error

HUB = "http://127.0.0.1:8700"          # ← 守护中枢地址
TOKEN = "<登记目标后领取的 token>"      # ← 工作台里复制粘贴进来

REPORT_PATH = HUB + "/api/v1/report"
EVENT_PATH = HUB + "/api/v1/event"
TIMEOUT_S = 5


def _post(path, body):
    req = urllib.request.Request(
        path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Guardian-Token": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.status, r.read()


def report(ok=True, metric="online", value=None, detail=""):
    """上报一次心跳或指标。失败时只打印，不抛异常——守护自身不能因上报而崩。"""
    body = {"metric": metric, "ok": bool(ok), "value": value, "detail": detail}
    try:
        _post(REPORT_PATH, body)
    except Exception as e:
        print(f"[guardian] report failed: {type(e).__name__}: {e}")


def send_event(type_name, message):
    """上报一次性异常事件（崩溃/攻击/错误）。"""
    body = {"type": type_name, "message": message}
    try:
        _post(EVENT_PATH, body)
    except Exception as e:
        print(f"[guardian] event failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"[guardian] start heartbeat to {REPORT_PATH} (ctrl+C to stop)")
    while True:
        report()
        time.sleep(60)