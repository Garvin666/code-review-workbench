#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_collect.py · 代理采集脚本
================================
对「完全无法改造的目标」（老旧服务、没有源码接入的项目），把采集动作
放到这台机器上跑，采集完成后以「该目标的身份」向守护中枢上报。

典型场景：
    - 老 Erlang/Go 服务，没有 HTTP 探针入口，但要监控进程数
    - 没源码的第三方组件，要监控某个日志文件是否增长
    - 内网接口，要在能访问的网段机器上代理探测
    - 任何「跑一条命令 / 调一次接口 / 读一段日志」能完成的健康判定

设计要点：
    * 单进程多目标可加 &
    * 退出码 0 = 正常，非 0 = 异常（默认）
    * stdout 末行尝试解析为 JSON：取 {ok, value, detail}；否则视为 detail 文本
    * 内置 measure 包装：超时返回失败

示例用法：
    python agent_collect.py \\
        --hub http://127.0.0.1:8700 \\
        --token abc...         \\   # 老项目源 ID 的 token
        --target web-shop      \\   # 中枢上登记的 target.id
        --cmd "curl -s -o /dev/null -w '%{http_code}' https://shop.example.com/health" \\
        --every 30             \\   # 周期秒
        --timeout 5            \\   # 单次超时
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 5
USER_AGENT = "GuardianAgentCollect/1.0"


def _post(hub, token, path, body):
    req = urllib.request.Request(
        hub + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Guardian-Token": token,
                 "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.load(r)


def collect(cmd, timeout):
    """执行 cmd，返回 (ok, value, detail)。"""
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, f"timeout after {timeout}s"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"
    text = (out.stdout or "").strip()
    ok = (out.returncode == 0)
    value = None
    detail = (out.stderr or "").strip() or text[:200]
    # 尝试解析末行 JSON
    last = text.splitlines()[-1] if text else ""
    if last.startswith("{"):
        try:
            j = json.loads(last)
            ok = bool(j.get("ok", ok))
            value = j.get("value", value)
            if isinstance(j.get("detail"), str):
                detail = j["detail"]
        except Exception:
            pass
    return ok, value, detail[:300]


def main():
    ap = argparse.ArgumentParser(description="代理采集脚本")
    ap.add_argument("--hub", required=True, help="守护中枢地址")
    ap.add_argument("--token", required=True, help="目标 token（中枢签发）")
    ap.add_argument("--target", required=True, help="目标 id")
    ap.add_argument("--cmd", required=True, help="采集命令字符串")
    ap.add_argument("--every", type=int, default=30, help="采集周期秒")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单次超时秒")
    ap.add_argument("--metric", default="online", help="上报 metric 名称")
    ap.add_argument("--once", action="store_true", help="只跑一次就退出")
    args = ap.parse_args()

    print(f"[agent] start collect for {args.target} every {args.every}s", file=sys.stderr)
    while True:
        ok, value, detail = collect(args.cmd, args.timeout)
        body = {"metric": args.metric, "ok": ok, "value": value, "detail": detail}
        try:
            code, resp = _post(args.hub, args.token, "/api/v1/report", body)
            state = resp.get("data", {}).get("state", "?")
            print(f"[agent] {args.target} ok={ok} state={state} ({code})", file=sys.stderr)
        except urllib.error.HTTPError as e:
            print(f"[agent] HTTP {e.code} {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"[agent] post fail: {type(e).__name__}: {e}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()