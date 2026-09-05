#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
守护工作台（Guardian Workbench）· 守护中枢服务端
================================================
把「代码审查工作台」的工作台形态升级为守护中枢：对外分发 API，
让网站 / 小程序 / App 接入后被守护；同时内置主动探测通道与代理采集通道。

零第三方依赖（Python 标准库），单文件即可运行。

用法:
    python guardian_server.py                  # 默认 127.0.0.1:8700
    python guardian_server.py --demo           # 附带本地演示目标（内置演示站点 :8800）
    python guardian_server.py --host 0.0.0.0   # 开放局域网（需防火墙放行）
    python guardian_server.py --auth           # 强制 token 鉴权（对外开放时建议）
    python guardian_server.py --auth --admin-token <口令>   # 管理端 API 另设口令

v0.3（2026-09-05）新增（对标 uptime-kuma / gatus / healthchecks）:
    · 通知渠道：Webhook / 企业微信 / 钉钉 / 飞书 / Server酱（状态翻转时投递，可配级别，含测试通知）
    · 可用率统计：24h / 7d / 30d（由事件时间线精确重算，维护窗口期豁免）
    · 维护窗口：计划内维护期间通知静默、可用率豁免、UI 横幅
    · token 轮换 / 目标启停与注销（管理端，--auth 下需 --admin-token）
    · 登记自测：POST /api/v1/validate 一次性连通性验证（不入库）
    · SVG 可用率徽章 与 公开只读状态页 /public（对外可分享）

设计文档：守护工作台_设计方案.md（同目录上级）
"""

VERSION = "v0.3.0"

import argparse
import datetime
import hashlib
import json
import os
import random
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# 常量与路径
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TARGETS_FILE = os.path.join(DATA_DIR, "targets.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
NOTIFY_FILE = os.path.join(DATA_DIR, "notify.json")         # v0.3 通知渠道配置
MAINT_FILE = os.path.join(DATA_DIR, "maintenance.json")     # v0.3 维护窗口
UI_FILE = os.path.join(BASE_DIR, "guardian_ui.html")
PUBLIC_FILE = os.path.join(BASE_DIR, "public_status.html")  # v0.3 公开状态页

DEFAULT_PORT = 8700
EVENT_RING_MAX = 400          # 内存事件环
HISTORY_MAX = 720             # 每 (target, metric) 保留条数
EVENTS_SCAN_MAX = 30000       # 可用率重算时向后扫描事件文件的最大行数
UPTIME_CACHE_TTL = 10         # 可用率缓存秒数
UPTIME_WINDOWS = ("1d", "7d", "30d")
WINDOW_MS = {"1d": 24 * 3600 * 1000, "7d": 7 * 24 * 3600 * 1000,
             "30d": 30 * 24 * 3600 * 1000}
NOTIFY_PRESETS = ("webhook", "qyweixin", "dingtalk", "feishu", "serverchan")

DEFAULT_THRESHOLDS = {
    "fail_to_warn": 1,
    "fail_to_down": 3,
    "slow_ms": 2500,
    "tls_days": 30,
    "content_strict": False,
}
TYPES = ("website", "api", "app", "miniprogram", "agent")
SOURCES = ("probe", "report", "agent")
PROBES = ("online", "latency", "tls", "content", "security")
EVENT_LEVELS = ("info", "warn", "critical")

NOTIFY_DEFAULTS = {
    "enabled": False,
    "preset": "webhook",        # webhook | qyweixin | dingtalk | feishu | serverchan
    "url": "",                  # webhook 地址（Server酱为完整 send 地址；其余为机器人地址）
    "levels": ["warn", "critical"],   # 只投递这些级别的事件
    "template": "【守护中枢】{level} · {name}\n{message}\n时间：{time}",
}

FLIP_DOWN = ("target_down",)                     # 判 down 事件（可用率断点）
FLIP_RECOVER = ("target_recovered",)             # 恢复事件

# 尝试让 Windows 控制台正确输出中文
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_io_lock = threading.RLock()       # 落盘 / 事件环共用锁（可重入）
_state_lock = threading.RLock()    # 内存运行时状态（可重入）


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def ts_ms():
    return int(time.time() * 1000)


def ts_iso(ms=None):
    if ms is None:
        ms = ts_ms()
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def token_hash(token):
    return "sha256:" + sha256_hex(token)


def gen_token():
    return secrets.token_hex(16)


def slugify(name):
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", name).strip("-").lower()
    return s or "target-" + secrets.token_hex(3)


def jload(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path, record):
    with _io_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------
# 运行时状态存储（单例）
# ----------------------------------------------------------------------------
class Store:
    """中枢内存态：目标清单 + 运行时快照 + 事件环 + 历史环。"""

    def __init__(self):
        self.targets = []            # list[dict]（含 token_hash，不含明文 token）
        self.by_id = {}              # id -> target dict
        self.rt = {}                 # id -> runtime dict
        self.events = deque(maxlen=EVENT_RING_MAX)   # 新事件在前
        self.history = {}            # (tid, metric) -> deque[{ts,value,ok}]
        self.notify = {}             # v0.3 通知渠道配置（见 NOTIFY_DEFAULTS）
        self.maintenance = []        # v0.3 维护窗口 list[dict]
        self._uptime_cache = {}      # (tid,) -> (cached_at, {win: pct})
        self._events_mtime = None
        self._events_snapshot = []   # 事件文件尾部快照（供可用率重算）
        self._events_index = {}      # tid -> {"down": [...], "ok": [...]}

    # ---- 配置读写（v0.3：通知渠道 / 维护窗口） ----
    def load_config(self):
        self.maintenance = jload(MAINT_FILE, [])
        nc = jload(NOTIFY_FILE, {})
        if isinstance(nc, dict):
            self.notify = nc
        for k, v in NOTIFY_DEFAULTS.items():
            self.notify.setdefault(k, v)

    def save_notify(self):
        with _io_lock:
            jsave(NOTIFY_FILE, self.notify)

    def save_maintenance(self):
        with _io_lock:
            jsave(MAINT_FILE, self.maintenance)

    # ---- 目标读写 ----
    def load_targets(self):
        self.targets = jload(TARGETS_FILE, [])
        self._reindex()

    def _reindex(self):
        self.by_id = {t["id"]: t for t in self.targets}
        # 为每个目标补 runtime
        for t in self.targets:
            if t["id"] not in self.rt:
                self.rt[t["id"]] = new_runtime(t)

    def persist(self):
        with _io_lock:
            jsave(TARGETS_FILE, self.targets)

    def get(self, tid):
        return self.by_id.get(tid)

    def add_target(self, data, raw_token=None):
        """登记目标。返回 (target, raw_token)。token 只在此处明文出现一次。"""
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("目标名称不能为空")
        tid = str(data.get("id", "")).strip() or slugify(name)
        if not tid:
            raise ValueError("目标 id 非法")
        existed = self.by_id.get(tid)
        t = existed or {"id": tid, "created_at": now_iso()}
        t["name"] = name
        t["type"] = data.get("type") if data.get("type") in TYPES else "website"
        t["url"] = str(data.get("url", "")).strip()
        src = data.get("source")
        if src not in SOURCES:
            src = "probe" if t["url"] else "report"
        t["source"] = src
        if src == "probe" and t["url"]:
            probes = data.get("probes") or ["online", "latency", "content", "security"]
            t["probes"] = [p for p in probes if p in PROBES] or ["online", "latency"]
        else:
            t["probes"] = []
        th = dict(DEFAULT_THRESHOLDS)
        if isinstance(data.get("thresholds"), dict):
            th.update({k: v for k, v in data["thresholds"].items() if k in th})
        t["thresholds"] = th
        t.setdefault("timeout_ms", int(data.get("timeout_ms", 3000)))
        t["timeout_ms"] = min(max(int(t["timeout_ms"]), 500), 30000)
        t.setdefault("interval_s", int(data.get("interval_s", 30)))
        t["interval_s"] = min(max(int(t["interval_s"]), 3), 600)
        exp = data.get("expected_headers") or ["Strict-Transport-Security",
                                                "Content-Security-Policy",
                                                "X-Content-Type-Options"]
        t["expected_headers"] = exp
        t["content_baseline_sha256"] = data.get("content_baseline_sha256") or \
            t.get("content_baseline_sha256")
        t["enabled"] = bool(data.get("enabled", t.get("enabled", True)))
        if existed is None:
            # 新目标才签发 token（已有目标保留原 token 哈希）
            raw_token = raw_token or gen_token()
            t["token_hash"] = token_hash(raw_token)
            self.targets.append(t)
        self._reindex()
        self.persist()
        return t, raw_token

    # ---- 事件 ----
    def push_event(self, target_id, level, etype, message, detail=None):
        ev = {"ts": ts_ms(), "time": ts_iso(), "target_id": target_id,
              "level": level, "type": etype, "message": message,
              "detail": detail or {}}
        with _io_lock:
            self.events.appendleft(ev)
            append_jsonl(EVENTS_FILE, ev)
        schedule_notify(ev, self.by_id.get(target_id))   # v0.3 通知渠道
        return ev

    def events_snapshot(self, limit=60):
        return list(self.events)[:limit]

    # ---- 历史 ----
    def push_history(self, target_id, metric, ok, value=None):
        key = (target_id, metric)
        with _io_lock:
            dq = self.history.setdefault(key, deque(maxlen=HISTORY_MAX))
            dq.appendleft({"ts": ts_ms(), "time": ts_iso(), "ok": bool(ok),
                           "value": value})

    def history_get(self, target_id, metric, limit=200):
        key = (target_id, metric)
        dq = self.history.get(key, deque())
        return list(dq)[:limit]

    # ---- 状态快照（供 /status 与页面） ----
    def status(self):
        today = datetime.date.today().isoformat()
        counts = {"total": 0, "ok": 0, "warn": 0, "down": 0,
                  "probe": 0, "report": 0, "agent": 0}
        targets_view = []
        with _state_lock:
            for t in self.targets:
                if not t.get("enabled"):
                    continue
                r = self.rt.get(t["id"]) or new_runtime(t)
                counts["total"] += 1
                counts[r["state"]] += 1
                src = t.get("source", "report")
                if src in counts:
                    counts[src] += 1
                last_ev = next((e for e in self.events if e["target_id"] == t["id"]), None)
                targets_view.append({
                    "id": t["id"], "name": t["name"], "type": t.get("type"),
                    "source": src, "url": t.get("url"), "enabled": t.get("enabled", True),
                    "state": r["state"], "fail_count": r["fail_count"],
                    "last_check": r["last_check"],
                    "probe_status": r["probes"], "conditions": r["conds"],
                    "metrics": r["metrics"],
                    "in_maintenance": in_maintenance(t["id"]) is not None,
                    "uptime": uptime_for(t["id"]),          # v0.3 {1d,7d,30d}
                    "last_event": {"level": last_ev["level"], "type": last_ev["type"],
                                   "message": last_ev["message"], "time": last_ev["time"]}
                    if last_ev else None,
                })
        ev_today = sum(1 for e in self.events if e["time"][:10] == today)
        m = maintenance_status_public()
        return {
            "generated_at": now_iso(),
            "version": VERSION,
            "global": {**counts, "events_today": ev_today,
                       "maintenance_active": len(m["active"])},
            "maintenance": m,               # v0.3
            "notify": notify_config_summary(),   # v0.3（URL 已打码）
            "targets": targets_view,
            "events": self.events_snapshot(60),
        }


def new_runtime(t):
    return {
        "state": "ok",              # ok | warn | down | (pending 前视为 ok)
        "fail_count": 0,
        "last_fail_reason": "",
        "last_check": None,
        "last_report": None,
        "probes": {p: {"status": "pending", "detail": "尚未探测", "at": None}
                   for p in t.get("probes") or []},
        "conds": {"slow": False, "tls_expiring": False, "content_changed": False,
                  "sec_missing": False},
        "metrics": {"latency_ms": None},
        "next_probe_at": 0,
    }


STORE = Store()


# ----------------------------------------------------------------------------
# v0.3 维护窗口 / 可用率 / 通知渠道
# ----------------------------------------------------------------------------
def _parse_ms(v):
    """把 ts 兼容 int(ms)/ISO 字符串解析成 ms；失败返回 None。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def maintenance_windows_for(target_id):
    """返回命中指定目标（或全部目标）的维护窗口列表。"""
    out = []
    for w in STORE.maintenance:
        if not isinstance(w, dict):
            continue
        tids = w.get("targets") or []
        if tids and target_id not in tids:
            continue
        out.append(w)
    return out


def in_maintenance(target_id, at_ms=None):
    """目标当前是否处于某个维护窗口；返回命中的窗口 dict 或 None。"""
    at_ms = at_ms if at_ms is not None else ts_ms()
    for w in maintenance_windows_for(target_id):
        start = _parse_ms(w.get("start"))
        end = _parse_ms(w.get("end"))
        if start is None or end is None or end < start:
            continue
        if start <= at_ms <= end:
            return w
    return None


def maintenance_status_public():
    """给 UI/状态页的全局维护摘要。"""
    active = []
    now = ts_ms()
    for w in STORE.maintenance:
        start = _parse_ms(w.get("start"))
        end = _parse_ms(w.get("end"))
        if start is None or end is None:
            continue
        if start <= now <= end:
            active.append({"id": w.get("id"), "title": w.get("title", "维护中"),
                           "start": ts_iso(start), "end": ts_iso(end),
                           "targets": w.get("targets") or []})
    return {"active": active,
            "all": [{"id": w.get("id"), "title": w.get("title", "维护中"),
                     "start": w.get("start"), "end": w.get("end"),
                     "targets": w.get("targets") or []} for w in STORE.maintenance]}


def _events_file_tail():
    """读事件文件尾部（带缓存，按 mtime+行数变化失效），并维护按目标分组的翻转索引。

    返回 (events_list, {tid: {"down":[...], "ok":[...]}})。索引供可用率 O(1/目标) 重算。
    """
    try:
        st = os.stat(EVENTS_FILE)
        key = (st.st_mtime, st.st_size)
        if key == STORE._events_mtime and STORE._events_snapshot:
            return STORE._events_snapshot, STORE._events_index
        tail = []
        with open(EVENTS_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            if f.tell() > 0:
                f.readline()          # 丢弃截断首行
            for line in f:
                try:
                    tail.append(json.loads(line.decode("utf-8")))
                except Exception:
                    continue
        tail = tail[-EVENTS_SCAN_MAX:]
        idx = {}
        for e in tail:
            et = e.get("type")
            if et in FLIP_DOWN or et in FLIP_RECOVER:
                d = idx.setdefault(e.get("target_id"), {"down": [], "ok": []})
                d["down" if et in FLIP_DOWN else "ok"].append(e["ts"])
        for d in idx.values():
            d["down"].sort()
            d["ok"].sort()
        STORE._events_mtime = key
        STORE._events_snapshot = tail
        STORE._events_index = idx
        return tail, idx
    except Exception:
        return [], {}


def _intervals_from_lists(downs, recovers, now_ms):
    """把 down/recover 两个有序时间数组合并成 down 区间 [(s,e),...]。"""
    intervals = []
    j = 0
    for ds in downs:
        while j < len(recovers) and recovers[j] <= ds:
            j += 1
        end = recovers[j] if j < len(recovers) else now_ms
        intervals.append((ds, end))
    # 合并重叠区间（异常时序保护）
    intervals.sort()
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _target_down_intervals(target_id, idx, now_ms=None):
    """由翻转索引重建该目标 down 区间列表。"""
    now_ms = now_ms if now_ms is not None else ts_ms()
    d = idx.get(target_id, {"down": [], "ok": []})
    return _intervals_from_lists(d["down"], d["ok"], now_ms)


def _sub_maintenance(interval, maint_windows):
    """从 down 区间中扣除与维护窗口重叠的时段。返回扣除后的区间列表。"""
    if not maint_windows:
        return [interval]
    s0, e0 = interval
    spans = []
    for w in maint_windows:
        s = _parse_ms(w.get("start"))
        e = _parse_ms(w.get("end"))
        if s is None or e is None:
            continue
        ov_s, ov_e = max(s0, s), min(e0, e)
        if ov_s < ov_e:
            spans.append((ov_s, ov_e))
    if not spans:
        return [interval]
    spans.sort()
    kept, cur = [], s0
    for s, e in spans:
        if cur < s:
            kept.append((cur, s))
        cur = max(cur, e)
    if cur < e0:
        kept.append((cur, e0))
    return kept


def uptime_for(target_id):
    """返回 {win: pct|None}。None 表示窗口内无足够历史（界面显示 —）。"""
    now = ts_ms()
    cached = STORE._uptime_cache.get(target_id)
    if cached and now - cached[0] < UPTIME_CACHE_TTL * 1000:
        return cached[1]
    _events, idx = _events_file_tail()
    intervals = _target_down_intervals(target_id, idx, now)
    mw = maintenance_windows_for(target_id)
    res = {}
    for win in UPTIME_WINDOWS:
        ws = now - WINDOW_MS[win]
        down_ms = 0
        for s, e in intervals:
            if e <= ws or s >= now:
                continue
            for k_s, k_e in _sub_maintenance((max(s, ws), min(e, now)), mw):
                down_ms += max(0, k_e - k_s)
        total = max(1, now - ws)
        up = max(0.0, min(100.0, (1 - down_ms / total) * 100))
        res[win] = round(up, 2)
    STORE._uptime_cache[target_id] = (now, res)
    return res


def mask_url(url):
    """把 webhook URL 里的凭证部分打码，避免在 UI/日志暴露。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc,
                           re.sub(r"/[^/]+$", "/***", parts.path or "/"),
                           "***" if parts.query else "", ""))
    except Exception:
        return "***"


def notify_config_summary():
    n = STORE.notify
    return {"enabled": bool(n.get("enabled")),
            "preset": n.get("preset", "webhook"),
            "url_masked": mask_url(n.get("url", "")),
            "levels": n.get("levels", ["warn", "critical"]),
            "has_url": bool(n.get("url"))}


def _notify_payload(preset, url, text):
    """按渠道预设组装 outbound 请求 (method, headers, body)。"""
    if preset == "dingtalk":
        body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        return "POST", {"Content-Type": "application/json"}, body
    if preset == "qyweixin":
        body = json.dumps({"msgtype": "markdown",
                           "markdown": {"content": text[:4000]}}).encode("utf-8")
        return "POST", {"Content-Type": "application/json"}, body
    if preset == "feishu":
        body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
        return "POST", {"Content-Type": "application/json"}, body
    if preset == "serverchan":
        # url 形如 https://sctapi.ftqq.com/<KEY>.send
        data = urllib.parse.urlencode({"text": "守护中枢通知",
                                       "desp": text}).encode("utf-8")
        return "POST", {"Content-Type": "application/x-www-form-urlencoded"}, data
    # 默认 webhook：通用 JSON
    payload = {"text": text, "ts": ts_iso()}
    return "POST", {"Content-Type": "application/json"}, \
        json.dumps(payload, ensure_ascii=False).encode("utf-8")


def deliver_notify(ev, target=None):
    """按通知配置把单条事件投递出去；在调用线程内执行（调用方应放后台线程）。"""
    n = STORE.notify
    if not n.get("enabled") or not n.get("url"):
        return None
    if in_maintenance(ev.get("target_id", "")):
        return "skipped: maintenance"
    if ev.get("level") not in n.get("levels", ["warn", "critical"]):
        return None
    name = (target or {}).get("name", ev.get("target_id", ""))
    try:
        text = (n.get("template") or NOTIFY_DEFAULTS["template"]).format(
            level=ev.get("level", ""), name=name,
            message=ev.get("message", ""), time=ev.get("time", ""),
            type=ev.get("type", ""))
    except Exception:
        text = f"[Guardian] {ev.get('level')} {name} {ev.get('message', '')}"
    method, headers, body = _notify_payload(n.get("preset", "webhook"),
                                            n.get("url", ""), text)
    req = urllib.request.Request(n["url"], data=body, method=method,
                                 headers={"User-Agent": "Guardian-Workbench/1.3", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return f"ok:{resp.status}"
    except Exception as e:
        return f"fail:{type(e).__name__}:{e}"


def schedule_notify(ev, target=None):
    """后台异步投递通知，避免阻塞事件写入；失败事件回写 info 流。"""
    n = STORE.notify
    if not (n.get("enabled") and n.get("url")):
        return
    if ev.get("level") not in n.get("levels", ["warn", "critical"]):
        return
    if str(ev.get("type", "")).startswith("notify"):
        return   # 防止通知结果事件再次触发通知（死循环）

    def _worker():
        try:
            res = deliver_notify(ev, target)
            if res and res.startswith("fail"):
                STORE.push_event(ev.get("target_id", "system"), "info", "notify_failed",
                                 f"通知投递失败: {res[:120]}", {})
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


# ----------------------------------------------------------------------------
# 判定引擎（状态机）
# ----------------------------------------------------------------------------
def set_cond(target, r, key, value, etype, message, level="warn", detail=None):
    """条件成立与否只在翻转时产生一次事件，避免刷屏。"""
    if bool(r["conds"][key]) == bool(value):
        r["conds"][key] = bool(value)
        return
    r["conds"][key] = bool(value)
    if value:
        STORE.push_event(target["id"], level, etype, message, detail)


def apply_online_result(target, r, ok, reason="", detail=""):
    """online 探测 / 心跳 ok 与否 → 更新失败计数。"""
    r["last_check"] = now_iso()
    if ok:
        r["fail_count"] = 0
        r["last_fail_reason"] = ""
    else:
        r["fail_count"] = r.get("fail_count", 0) + 1
        r["last_fail_reason"] = reason


def recompute_state(target, r, extra_warn=False):
    """据失败计数与告警条件重算目标状态，并在状态翻转时写事件。"""
    th = target.get("thresholds", DEFAULT_THRESHOLDS)
    prev = r["state"]
    cond_warn = any(r["conds"].values()) or extra_warn
    if r["fail_count"] >= th["fail_to_down"]:
        new_state = "down"
    elif r["fail_count"] >= th["fail_to_warn"] or cond_warn:
        new_state = "warn"
    else:
        new_state = "ok"
    r["state"] = new_state

    if new_state == prev:
        return
    if new_state == "down":
        if prev in ("ok", "warn"):
            reason = r["last_fail_reason"]
            if reason == "timeout":
                STORE.push_event(target["id"], "warn", "probe_timeout",
                                 f"{target['name']} 探测超时", {"count": r["fail_count"]})
            STORE.push_event(target["id"], "critical", "target_down",
                             f"{target['name']} 已判定 DOWN",
                             {"fail_count": r["fail_count"], "reason": reason})
    elif new_state == "warn":
        STORE.push_event(target["id"], "warn", "probe_fail",
                         f"{target['name']} 出现异常（进入预警）",
                         {"fail_count": r["fail_count"],
                          "reason": r["last_fail_reason"] or "条件告警"})
    elif new_state == "ok" and prev in ("warn", "down"):
        STORE.push_event(target["id"], "info", "target_recovered",
                         f"{target['name']} 已恢复")
    STORE.push_history(target["id"], "state", new_state == "ok",
                       {"ok": 0, "warn": 1, "down": 2}[new_state])


# ----------------------------------------------------------------------------
# 探测通道（Channel B）· 全部标准库实现
# ----------------------------------------------------------------------------
def _http_fetch(url, timeout_s, method="GET"):
    """执行一次请求，返回统一结果字典。成功也会带上 headers/body。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "Guardian-Workbench/1.0 (+ping)")
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        ms = (time.time() - start) * 1000
        body = resp.read()
        try:
            text = body.decode("utf-8")
        except Exception:
            text = body.decode("latin-1", errors="replace")
        return {"ok": True, "status": resp.status, "ms": ms,
                "headers": resp.headers, "body": text, "err": ""}
    except urllib.error.HTTPError as e:
        ms = (time.time() - start) * 1000
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return {"ok": 200 <= e.code < 400, "status": e.code, "ms": ms,
                "headers": e.headers, "body": text, "err": f"HTTP {e.code}"}
    except Exception as e:
        ms = (time.time() - start) * 1000
        name = type(e).__name__
        reason = "timeout" if name == "socket.timeout" or "timed out" in str(e) else name
        return {"ok": False, "status": None, "ms": ms, "headers": None,
                "body": "", "err": str(e) or name, "reason": reason}


def probe_online_latency_content_security(target, r):
    """一次 HTTP 请求完成 online/latency/content/security 四探针。"""
    url = target.get("url", "")
    timeout_s = target["timeout_ms"] / 1000.0
    slow_ms = target["thresholds"]["slow_ms"]
    res = _http_fetch(url, timeout_s)

    # --- online ---
    ok = res["ok"] and res["status"] is not None
    reason = res.get("reason") or ("" if ok else f"HTTP {res['status']}")
    detail = ""
    if res["status"]:
        detail = f"{res['status']} · {res['ms']:.0f}ms"
    if not ok and res["err"]:
        detail = (res.get("reason") or "错误") + f" · {res['ms']:.0f}ms"
    p = r["probes"].setdefault("online", {})
    p.update({"status": "ok" if ok else "fail", "detail": detail or "n/a",
              "at": now_iso()})
    apply_online_result(target, r, ok, reason=reason, detail=detail)
    if ok:
        r["probes"].setdefault("latency", {})
        r["probes"]["latency"].update(
            {"status": "warn" if res["ms"] > slow_ms else "ok",
             "detail": f"{res['ms']:.0f}ms" + (" · 超阈值" if res["ms"] > slow_ms else ""),
             "at": now_iso()})
        r["metrics"]["latency_ms"] = round(res["ms"], 1)
        STORE.push_history(target["id"], "latency_ms", True, round(res["ms"], 1))
        # --- latency 慢判定 ---
        if res["ms"] > slow_ms:
            set_cond(target, r, "slow", True, "perf_slow",
                     f"{target['name']} 响应偏慢 {res['ms']:.0f}ms（阈值 {slow_ms}ms）",
                     "warn", {"latency_ms": round(res["ms"], 1)})
        else:
            set_cond(target, r, "slow", False, "perf_slow", "")
        # --- content 内容指纹 ---
        if "content" in (target.get("probes") or []):
            digest = sha256_hex(res["body"][:200000])
            base = target.get("content_baseline_sha256")
            if not base:
                target["content_baseline_sha256"] = digest
                STORE.persist()
                r["probes"].setdefault("content", {}).update(
                    {"status": "ok", "detail": "基线已建立", "at": now_iso()})
            elif base == digest:
                set_cond(target, r, "content_changed", False, "content_changed", "")
                r["probes"].setdefault("content", {}).update(
                    {"status": "ok", "detail": "内容一致", "at": now_iso()})
            else:
                lv = "critical" if target["thresholds"]["content_strict"] else "warn"
                set_cond(target, r, "content_changed", True, "content_changed",
                         f"{target['name']} 内容已变更", lv, {"digest": digest[:12]})
                r["probes"].setdefault("content", {}).update(
                    {"status": "warn", "detail": "内容变更", "at": now_iso()})
        # --- security 安全响应头 ---
        if "security" in (target.get("probes") or []) and res["headers"]:
            missing = [h for h in target.get("expected_headers", [])
                       if not res["headers"].get(h)]
            r["probes"].setdefault("security", {})
            if missing:
                set_cond(target, r, "sec_missing", True, "security_header_missing",
                         f"{target['name']} 缺少安全响应头: {', '.join(missing)}",
                         "warn", {"missing": missing})
                r["probes"]["security"].update(
                    {"status": "warn", "detail": "缺: " + ",".join(missing), "at": now_iso()})
            else:
                set_cond(target, r, "sec_missing", False, "security_header_missing", "")
                r["probes"]["security"].update(
                    {"status": "ok", "detail": "安全头齐备", "at": now_iso()})
    else:
        # 失败时 latency 也标 fail
        r["probes"].setdefault("latency", {}).update(
            {"status": "fail", "detail": "无响应", "at": now_iso()})
    STORE.push_history(target["id"], "online", ok)
    return ok


def probe_tls(target, r):
    """TLS 证书到期检查（仅 https）。"""
    url = target.get("url", "")
    if not url.lower().startswith("https://"):
        r["probes"].setdefault("tls", {}).update({"status": "n/a", "detail": "非 HTTPS",
                                                   "at": now_iso()})
        return
    m = re.match(r"https://([^/:]+)(?::(\d+))?", url)
    if not m:
        r["probes"].setdefault("tls", {}).update({"status": "fail", "detail": "URL 非法",
                                                   "at": now_iso()})
        return
    host, port = m.group(1), int(m.group(2) or 443)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=target["timeout_ms"] / 1000) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                cert = s.getpeercert()
        if not cert:
            raise ValueError("无证书")
        na = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        days = (na - datetime.datetime.utcnow()).days
        th_days = target["thresholds"]["tls_days"]
        p = r["probes"].setdefault("tls", {})
        if days < th_days:
            set_cond(target, r, "tls_expiring", True, "tls_expiring",
                     f"{target['name']} 证书 {days} 天后到期（预警 {th_days} 天）",
                     "warn", {"days_left": days, "not_after": cert["notAfter"]})
            p.update({"status": "warn", "detail": f"{days} 天后到期", "at": now_iso()})
        else:
            set_cond(target, r, "tls_expiring", False, "tls_expiring", "")
            p.update({"status": "ok", "detail": f"{days} 天后到期", "at": now_iso()})
    except Exception as e:
        set_cond(target, r, "tls_expiring", True, "tls_expiring",
                 f"{target['name']} TLS 握手失败: {type(e).__name__}", "warn", {})
        r["probes"].setdefault("tls", {}).update(
            {"status": "warn", "detail": f"握手失败 {type(e).__name__}", "at": now_iso()})


def run_probe_round(target):
    """对单个 source=probe 目标执行一轮探测。"""
    r = STORE.rt.get(target["id"])
    if r is None or not target.get("enabled") or target.get("source") != "probe":
        return
    if not target.get("url"):
        return
    probes = target.get("probes") or ["online", "latency"]
    try:
        if any(p in probes for p in ("online", "latency", "content", "security")):
            probe_online_latency_content_security(target, r)
        if "tls" in probes:
            probe_tls(target, r)
    except Exception as e:
        STORE.push_event(target["id"], "warn", "probe_fail",
                         f"{target['name']} 探针异常 {type(e).__name__}: {e}")
    recompute_state(target, r)
    r["next_probe_at"] = time.time() + target.get("interval_s", 30)


def probe_scheduler(interval_s=1.0):
    """守护线程：按各目标 interval 调度探测。

    快照待探测目标后立刻释放锁；真正探测不持全局锁（网络 I/O 可能耗时数秒，
    若持锁会阻塞 /api/v1/status 轮询）。各目标 runtime 相互独立，单 dict 写入
    在 GIL 下原子，满足当前规模。
    """
    while True:
        due = []
        with _state_lock:
            for t in STORE.targets:
                if t.get("source") != "probe" or not t.get("enabled"):
                    continue
                r = STORE.rt.get(t["id"])
                if r and time.time() >= r["next_probe_at"]:
                    due.append(t)
        for t in due:
            try:
                run_probe_round(t)
            except Exception:
                pass
        time.sleep(interval_s)


# ----------------------------------------------------------------------------
# 上报通道处理（Channel A）
# ----------------------------------------------------------------------------
def locate_target(handler, body):
    """按 token 头定位目标；宽松模式（非 --auth）下允许 body.target 直指。"""
    th = (handler.headers.get("X-Guardian-Token") or "").strip()
    if th:
        h = token_hash(th)
        for t in STORE.targets:
            if t.get("token_hash") == h:
                return t
        if handler.server.auth_enforced:
            return None
    tid = body.get("target") or body.get("target_id")
    if not handler.server.auth_enforced and tid:
        return STORE.get(str(tid))
    return None if handler.server.auth_enforced else None


def handle_report(body):
    """处理一次心跳/指标上报，返回 {state,...} 供响应。"""
    target = None
    # 已由 handler 定位好 target
    tid = body.get("_target_id")
    target = STORE.get(tid) if tid else None
    if target is None:
        return {"error": "unknown target"}

    r = STORE.rt.setdefault(target["id"], new_runtime(target))
    metric = str(body.get("metric") or "online").lower()
    ok = bool(body.get("ok", True))
    value = body.get("value")
    detail = body.get("detail")
    r["last_report"] = now_iso()

    if metric in ("online", "heartbeat", "status", "ping"):
        apply_online_result(target, r, ok,
                            reason="" if ok else "客户端上报异常",
                            detail=str(detail or "")[:300])
        recompute_state(target, r)
    elif metric in ("latency_ms", "latency", "perf"):
        ms = float(value or 0)
        r["metrics"]["latency_ms"] = round(ms, 1)
        STORE.push_history(target["id"], "latency_ms", ok, round(ms, 1))
        slow_ms = target["thresholds"]["slow_ms"]
        if ms > slow_ms:
            set_cond(target, r, "slow", True, "perf_slow",
                     f"{target['name']} 客户端上报延迟 {ms:.0f}ms（阈值 {slow_ms}ms）",
                     "warn", {"latency_ms": round(ms, 1)})
        else:
            set_cond(target, r, "slow", False, "perf_slow", "")
        if not ok:
            apply_online_result(target, r, False, "客户端性能异常")
        else:
            apply_online_result(target, r, True)
        recompute_state(target, r)
    elif metric in ("crash",):
        STORE.push_event(target["id"], "critical", "client_crash",
                         f"{target['name']} 上报崩溃", {"detail": detail})
        apply_online_result(target, r, False, "客户端崩溃")
        recompute_state(target, r)
    elif metric in ("attack", "security", "abuse"):
        STORE.push_event(target["id"], "critical", "client_attack",
                         f"{target['name']} 上报安全攻击", {"detail": detail})
        apply_online_result(target, r, False, "安全事件")
        recompute_state(target, r)
    elif metric in ("error", "runtime_error", "exception"):
        STORE.push_event(target["id"], "warn", "client_error",
                         f"{target['name']} 上报运行时错误: {detail or ''}"[:200])
        if not ok:
            apply_online_result(target, r, False, "客户端错误")
            recompute_state(target, r)
    elif metric == "content_hash":
        digest = str(value or detail or "")
        base = target.get("content_baseline_sha256")
        if base and digest and digest != base:
            set_cond(target, r, "content_changed", True, "content_changed",
                     f"{target['name']} 内容指纹变化（客户端上报）", "warn", {})
        elif digest:
            set_cond(target, r, "content_changed", False, "content_changed", "")
        recompute_state(target, r)
    else:
        # 未知 metric：仅记录历史，不改变状态
        STORE.push_history(target["id"], metric, ok, value)
    return {"state": r["state"], "fail_count": r["fail_count"],
            "last_report": r["last_report"], "metric": metric}


def handle_event(body, target):
    """处理一次性异常事件上报。"""
    etype = str(body.get("type") or "client_error")
    level = str(body.get("level") or "warn")
    msg = str(body.get("message") or body.get("detail") or "")[:300]
    if etype == "crash":
        etype, level = "client_crash", "critical"
    elif etype in ("attack", "security"):
        etype, level = "client_attack", "critical"
    elif etype in ("error", "exception", "runtime_error"):
        etype, level = "client_error", "warn"
    level = level if level in EVENT_LEVELS else "warn"
    STORE.push_event(target["id"], level, etype,
                     f"{target['name']} · {msg}"[:200], {"report": True})
    return True


# ----------------------------------------------------------------------------
# HTTP 处理
# ----------------------------------------------------------------------------
def send_json(handler, obj, code=200):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers",
                        "Content-Type, X-Guardian-Token, X-Guardian-Admin")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_body(handler):
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        if n <= 0 or n > 2 * 1024 * 1024:
            return {}
        raw = handler.rfile.read(n)
        return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {}


class GuardianHandler(BaseHTTPRequestHandler):
    server_version = "GuardianWorkbench/1.3"

    # ---- 路由 ----
    def _route(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        return path

    def do_OPTIONS(self):
        """跨域预检：file:// 打开的 v2 工作台经 http://127.0.0.1 访问时浏览器会先发 OPTIONS。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Guardian-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            parts = self._path_parts()
            path = self._route()
            if path in ("/", "/guardian_ui.html", "/index.html", "/ui"):
                self._serve_ui()
            elif path in ("/public", "/public/", "/status", "/status/"):
                self._serve_public()
            elif parts[:3] == ["api", "v1", "status"]:
                send_json(self, {"ok": True, "data": STORE.status()})
            elif path == "/api/v1/targets":
                send_json(self, {"ok": True, "data": self._targets_public()})
            elif len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] \
                    and parts[4] == "badge.svg":
                self._api_badge(parts[3])
            elif path == "/api/v1/maintenance":
                send_json(self, {"ok": True, "data": maintenance_status_public()})
            elif path == "/api/v1/notify":
                send_json(self, {"ok": True, "data": notify_config_summary()})
            elif path == "/api/v1/snippets":
                send_json(self, {"ok": True, "data": self._snippets()})
            elif path == "/api/v1/history":
                self._api_history()
            elif path == "/api/v1/health":
                send_json(self, {"ok": True, "data": {"status": "alive",
                                                      "version": VERSION}})
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                send_json(self, {"ok": False, "error": "not found"}, 404)
        except Exception as e:
            send_json(self, {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        try:
            parts = self._path_parts()
            body = read_body(self)
            path = "/" + "/".join(parts)
            if path == "/api/v1/report":
                self._api_report(body)
            elif path == "/api/v1/event":
                self._api_event(body)
            elif path == "/api/v1/targets":
                self._api_add_target(body)
            elif path == "/api/v1/validate":
                self._api_validate(body)
            elif path == "/api/v1/maintenance":
                self._api_maintenance(body)
            elif path == "/api/v1/notify":
                self._api_notify(body)
            elif parts[:3] == ["api", "v1", "targets"]:
                if len(parts) == 6 and parts[4] == "token" and parts[5] == "rotate":
                    self._api_rotate_token(parts[3], body)
                elif len(parts) == 4:
                    self._api_patch_target(parts[3], body)
                else:
                    send_json(self, {"ok": False, "error": "not found"}, 404)
            else:
                send_json(self, {"ok": False, "error": "not found"}, 404)
        except Exception as e:
            send_json(self, {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_DELETE(self):
        try:
            parts = self._path_parts()
            if parts[:3] == ["api", "v1", "targets"] and len(parts) == 4:
                self._api_delete_target(parts[3])
            else:
                send_json(self, {"ok": False, "error": "not found"}, 404)
        except Exception as e:
            send_json(self, {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _path_parts(self):
        import urllib.parse
        p = self.path.split("?", 1)[0].strip("/")
        return [urllib.parse.unquote(x) for x in p.split("/") if x] or []

    def _admin_ok(self):
        """管理操作放行：开放模式（127.0.0.1 信任）直接放行；
        --auth 模式需 X-Guardian-Admin 与 --admin-token 一致。"""
        if not self.server.auth_enforced:
            return True
        tok = (self.headers.get("X-Guardian-Admin") or "").strip()
        admin = getattr(self.server, "admin_token", "") or ""
        return bool(admin) and tok == admin

    # ---- 页面 ----
    def _serve_public(self):
        """公开只读状态页（无鉴权，适合对外分享）。"""
        if os.path.exists(PUBLIC_FILE):
            with open(PUBLIC_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            send_json(self, {"ok": False,
                             "error": "public_status.html 未生成；可用 GET /api/v1/status 取 JSON"},
                      404)

    # ---- 新增 v0.3 API 实现 ----
    def _deny_admin(self):
        send_json(self, {"ok": False,
                         "error": "管理操作需 X-Guardian-Admin 头（--auth 模式下）"}, 401)

    def _api_validate(self, body):
        """登记前连通性自测：只测一次，不写库不改状态。"""
        if not self._admin_ok():
            self._deny_admin()
            return
        url = str(body.get("url") or "").strip()
        if not url:
            send_json(self, {"ok": False, "error": "url 不能为空"}, 400)
            return
        timeout_ms = min(max(int(body.get("timeout_ms", 3000)), 500), 30000)
        probes = [p for p in (body.get("probes") or ["online", "latency"])
                  if p in PROBES] or ["online", "latency"]
        slow_ms = int(body.get("slow_ms", 2500))
        res = _http_fetch(url, timeout_ms / 1000.0)
        out = {}
        # online / latency
        ok = res["ok"] and res["status"] is not None
        out["online"] = {"status": "ok" if ok else "fail",
                         "detail": (f"{res['status']} · {res['ms']:.0f}ms" if ok
                                    else f"{(res.get('reason') or res['err'])} · {res['ms']:.0f}ms")}
        out["latency"] = {"status": ("warn" if (ok and res["ms"] > slow_ms) else
                                     ("ok" if ok else "fail")),
                          "detail": f"{res['ms']:.0f}ms"}
        if "content" in probes and ok:
            digest = sha256_hex(res["body"][:200000])
            out["content"] = {"status": "ok",
                              "detail": f"sha256 {digest[:16]}…（登记后以此为基线）"}
        if "security" in probes and ok and res["headers"]:
            missing = [h for h in body.get("expected_headers") or
                       ["Strict-Transport-Security", "Content-Security-Policy",
                        "X-Content-Type-Options"] if not res["headers"].get(h)]
            out["security"] = {"status": "warn" if missing else "ok",
                               "detail": ("缺: " + ",".join(missing)) if missing
                                         else "安全头齐备"}
        if "tls" in probes:
            out["tls"] = self._tls_probe_once(url, timeout_ms)
        send_json(self, {"ok": True, "data": {"url": url,
                                              "probes": out,
                                              "summary": "ok" if out.get("online", {}).get("status") == "ok"
                                              else "fail"}})

    @staticmethod
    def _tls_probe_once(url, timeout_ms):
        if not url.lower().startswith("https://"):
            return {"status": "n/a", "detail": "非 HTTPS"}
        m = re.match(r"https://([^/:]+)(?::(\d+))?", url)
        if not m:
            return {"status": "fail", "detail": "URL 非法"}
        host, port = m.group(1), int(m.group(2) or 443)
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout_ms / 1000) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as s:
                    cert = s.getpeercert()
            na = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
            days = (na - datetime.datetime.utcnow()).days
            return {"status": "warn" if days < 30 else "ok",
                    "detail": f"{days} 天后到期"}
        except Exception as e:
            return {"status": "warn", "detail": f"握手失败 {type(e).__name__}"}

    def _api_rotate_token(self, tid, body):
        if not self._admin_ok():
            self._deny_admin()
            return
        t = STORE.get(tid)
        if t is None:
            send_json(self, {"ok": False, "error": "target not found"}, 404)
            return
        raw = gen_token()
        t["token_hash"] = token_hash(raw)
        STORE.persist()
        send_json(self, {"ok": True,
                         "data": {"id": tid, "token": raw,
                                  "token_hint": raw[:6] + "..."}})

    def _api_patch_target(self, tid, body):
        if not self._admin_ok():
            self._deny_admin()
            return
        t = STORE.get(tid)
        if t is None:
            send_json(self, {"ok": False, "error": "target not found"}, 404)
            return
        if "enabled" in body and isinstance(body["enabled"], bool):
            t["enabled"] = body["enabled"]
        if "interval_s" in body:
            t["interval_s"] = min(max(int(body["interval_s"]), 3), 600)
        if "name" in body and str(body["name"]).strip():
            t["name"] = str(body["name"]).strip()
        if "url" in body:
            t["url"] = str(body.get("url", "")).strip()
        STORE.persist()
        r = STORE.rt.setdefault(tid, new_runtime(t))
        send_json(self, {"ok": True,
                         "data": {"id": tid, "name": t["name"],
                                  "enabled": t["enabled"],
                                  "interval_s": t["interval_s"],
                                  "url": t.get("url"),
                                  "state": r["state"]}})

    def _api_delete_target(self, tid):
        if not self._admin_ok():
            self._deny_admin()
            return
        if STORE.get(tid) is None:
            send_json(self, {"ok": False, "error": "target not found"}, 404)
            return
        with _state_lock:
            STORE.targets = [t for t in STORE.targets if t["id"] != tid]
            STORE.by_id.pop(tid, None)
            STORE.rt.pop(tid, None)
        STORE.persist()
        STORE.push_event("system", "info", "target_removed",
                         f"目标 {tid} 已注销")
        send_json(self, {"ok": True, "data": {"id": tid, "removed": True}})

    def _api_maintenance(self, body):
        if not self._admin_ok():
            self._deny_admin()
            return
        action = body.get("action")
        if action == "add":
            start = _parse_ms(body.get("start"))
            end = _parse_ms(body.get("end"))
            if start is None or end is None or end <= start:
                send_json(self, {"ok": False,
                                 "error": "start/end 需为 ISO 时间或毫秒时间戳且 end>start"},
                          400)
                return
            title = str(body.get("title") or "计划维护")[:80]
            mids = [w.get("id") for w in STORE.maintenance]
            wid = "mt-" + slugify(title) if slugify(title).startswith("mt-") else \
                "mt-" + (slugify(title) if slugify(title) else secrets.token_hex(3))
            wid = wid[:40]
            n = 1
            while wid in mids:
                n += 1
                wid = f"{wid}-{n}"[:44]
            tids = body.get("targets")
            if not isinstance(tids, list):
                tids = []
            w = {"id": wid, "title": title, "start": ts_iso(start),
                 "end": ts_iso(end), "targets": [str(x) for x in tids],
                 "created_at": now_iso()}
            STORE.maintenance.append(w)
            STORE.save_maintenance()
            send_json(self, {"ok": True, "data": w}, 201)
        elif action == "delete":
            wid = str(body.get("id") or "")
            before = len(STORE.maintenance)
            STORE.maintenance = [w for w in STORE.maintenance
                                 if w.get("id") != wid]
            if len(STORE.maintenance) == before:
                send_json(self, {"ok": False, "error": "maintenance not found"}, 404)
                return
            STORE.save_maintenance()
            send_json(self, {"ok": True, "data": {"id": wid, "removed": True}})
        else:
            send_json(self, {"ok": False, "error": "action 需为 add|delete"}, 400)

    def _api_notify(self, body):
        if not self._admin_ok():
            self._deny_admin()
            return
        action = body.get("action")
        if action == "save":
            url = str(body.get("url") or "").strip()
            preset = body.get("preset", STORE.notify.get("preset", "webhook"))
            if preset not in NOTIFY_PRESETS:
                send_json(self, {"ok": False, "error": f"preset 非法: {preset}"}, 400)
                return
            if url and not (url.startswith("http://") or url.startswith("https://")):
                send_json(self, {"ok": False, "error": "url 需以 http(s):// 开头"}, 400)
                return
            levels = body.get("levels")
            if isinstance(levels, list):
                levels = [x for x in levels if x in ("info", "warn", "critical")]
                if not levels:
                    levels = ["warn", "critical"]
            else:
                levels = STORE.notify.get("levels", ["warn", "critical"])
            tmpl = str(body.get("template") or STORE.notify.get("template") or
                       NOTIFY_DEFAULTS["template"])[:500]
            STORE.notify.update({
                "enabled": bool(body.get("enabled", STORE.notify.get("enabled", False))),
                "preset": preset,
                "url": url,
                "levels": levels,
                "template": tmpl,
            })
            STORE.save_notify()
            send_json(self, {"ok": True, "data": notify_config_summary()})
        elif action == "test":
            ev = {"ts": ts_ms(), "time": ts_iso(), "target_id": "system",
                  "level": "warn", "type": "notify_test",
                  "message": "这是一条来自守护中枢的测试通知", "detail": {}}
            res = deliver_notify(ev)
            send_json(self, {"ok": res and not str(res).startswith("fail"),
                             "data": {"result": res or "未启用/无 URL",
                                      "config": notify_config_summary()}})
        else:
            send_json(self, {"ok": False, "error": "action 需为 save|test"}, 400)

    def _api_badge(self, tid):
        """SVG 可用率徽章（嵌入 README/看板，无需鉴权）。"""
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        win = (q.get("window") or ["7d"])[0]
        win = win if win in UPTIME_WINDOWS else "7d"
        t = STORE.get(tid)
        up = uptime_for(tid).get(win)
        name = (t or {}).get("name", tid)
        if up is None:
            pct = "100.0%"
        else:
            pct = f"{up:.2f}%".rstrip("0").rstrip(".") + "%"
        color = "#1a7f37"
        if up is not None:
            if up < 95:
                color = "#cf222e"
            elif up < 99.5:
                color = "#9a6700"
        label = f"{win} 可用率"
        # 简易两段式徽章
        lw = max(60, len(label) * 8 + 16)
        pw = max(70, len(pct) * 9 + 18)
        wdt = lw + pw
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="20" '
               'role="img" aria-label="uptime">'
               '<linearGradient id="s" x2="0" y2="100%%"><stop offset="0" '
               'stop-color="#bbb" stop-opacity=".1"/><stop offset="1" '
               'stop-opacity=".1"/></linearGradient><clipPath id="r">'
               '<rect width="%d" height="20" rx="3" fill="#fff"/></clipPath>'
               '<g clip-path="url(#r)"><rect width="%d" height="20" fill="#555"/>'
               '<rect x="%d" width="%d" height="20" fill="%s"/>'
               '<rect width="%d" height="20" fill="url(#s)"/></g>'
               '<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,'
               'sans-serif" font-size="11">'
               '<text x="%d" y="14">%s</text><text x="%d" y="14">%s</text></g></svg>'
               ) % (wdt, wdt, lw, lw, pw, color, wdt, lw // 2, label[:22],
                    lw + pw // 2, pct)
        data = svg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 页面 ----
    def _serve_ui(self):
        if os.path.exists(UI_FILE):
            with open(UI_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            html = ("<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
                    "<h2>Guardian Workbench</h2><p>驾驶舱界面 guardian_ui.html 尚未生成。"
                    "API 正常可用。</p></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    # ---- API 实现 ----
    def _targets_public(self):
        out = []
        for t in STORE.targets:
            r = STORE.rt.get(t["id"]) or new_runtime(t)
            out.append({
                "id": t["id"], "name": t["name"], "type": t.get("type"),
                "source": t.get("source"), "url": t.get("url"),
                "probes": t.get("probes", []),
                "enabled": t.get("enabled", True),
                "interval_s": t.get("interval_s"),
                "state": r["state"],
                "created_at": t.get("created_at"),
                "has_token": bool(t.get("token_hash")),
            })
        return out

    def _api_report(self, body):
        target = self._locate(body)
        if target is None:
            send_json(self, {"ok": False, "error": "unauthorized or unknown target"}, 401)
            return
        body["_target_id"] = target["id"]
        res = handle_report(body)
        if res.get("error"):
            send_json(self, {"ok": False, "error": res["error"]}, 400)
            return
        send_json(self, {"ok": True, "data": res})

    def _api_event(self, body):
        target = self._locate(body)
        if target is None:
            send_json(self, {"ok": False, "error": "unauthorized or unknown target"}, 401)
            return
        handle_event(body, target)
        send_json(self, {"ok": True})

    def _locate(self, body):
        th = (self.headers.get("X-Guardian-Token") or "").strip()
        if th:
            h = token_hash(th)
            for t in STORE.targets:
                if t.get("token_hash") == h:
                    return t
        if not self.server.auth_enforced:
            tid = body.get("target") or body.get("target_id")
            if tid:
                return STORE.get(str(tid))
        return None

    def _api_add_target(self, body):
        if not self._admin_ok():
            self._deny_admin()
            return
        try:
            t, raw_token = STORE.add_target(body)
            send_json(self, {"ok": True,
                             "data": {"id": t["id"], "token": raw_token,
                                      "token_hint": raw_token[:6] + "...",
                                      "target": t}}, 201)
        except ValueError as e:
            send_json(self, {"ok": False, "error": str(e)}, 400)

    def _api_history(self):
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        tid = (q.get("target") or [""])[0]
        metric = (q.get("metric") or ["latency_ms"])[0]
        limit = int((q.get("limit") or ["240"])[0])
        data = STORE.history_get(tid, metric, min(max(limit, 1), 720)) if tid else []
        send_json(self, {"ok": True, "data": data})

    def _snippets(self):
        base = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
        hub = f"http://{base}"
        py = f'''import json, time, urllib.request
HUB = "{hub}"
TOKEN = "<登记目标后领取的 token>"
def report(metric="online", ok=True, value=None, detail=""):
    body = json.dumps({{"metric": metric, "ok": ok, "value": value,
                       "detail": detail}}).encode()
    req = urllib.request.Request(HUB + "/api/v1/report", body,
                                 {{"Content-Type": "application/json",
                                   "X-Guardian-Token": TOKEN}})
    urllib.request.urlopen(req, timeout=5)
while True:            # 业务代码里放进主循环 / 定时器
    report()           # 心跳，默认在线
    time.sleep(60)     # 建议 30~120s'''
        js = f'''const HUB = "{hub}/api/v1/report";
const TOKEN = "<token>";
function report(ok = true, metric = "online", value, detail) {{
  const body = JSON.stringify({{metric, ok, value, detail}});
  if (navigator.sendBeacon)
    navigator.sendBeacon(HUB, new Blob([body], {{type: "application/json"}}));
  else
    fetch(HUB, {{method: "POST",
      headers: {{"Content-Type": "application/json", "X-Guardian-Token": TOKEN}},
      body}});
}}
report();                                     // 页面加载即心跳
window.addEventListener("error",
  e => report(false, "runtime_error", 0, e.message));
window.addEventListener("unhandledrejection",
  e => report(false, "runtime_error", 0, String(e.reason)));
setInterval(() => report(), 60000);           // 每 60s 一次心跳'''
        curl = f'''curl -s -X POST {hub}/api/v1/report \
  -H "Content-Type: application/json" -H "X-Guardian-Token: <token>" \
  -d '{{"metric":"online","ok":true}}'
# 崩溃上报
curl -s -X POST {hub}/api/v1/event \
  -H "Content-Type: application/json" -H "X-Guardian-Token: <token>" \
  -d '{{"type":"crash","message":"oom in webview"}}'
# 一键登记目标（网页/脚本管理用，返回明文 token）
curl -s -X POST {hub}/api/v1/targets -H "Content-Type: application/json" \
  -d '{{"name":"我的网站","url":"https://example.com"}}'
# 登记前连通性自测（不写库）
curl -s -X POST {hub}/api/v1/validate -H "Content-Type: application/json" \
  -d '{{"url":"https://example.com","probes":["online","latency","tls"]}}'
# 轮换 token（旧 token 立即失效）
curl -s -X POST {hub}/api/v1/targets/<id>/token/rotate \
  -H "Content-Type: application/json" -d '{{}}'
'''
        wx = f'''// app.js（微信小程序）· 启动即心跳 + 全局错误上报
const HUB = "{hub}";
const TOKEN = "<token>";
function guardianReport(ok = true, metric = "online", value, detail) {{
  wx.request({{
    url: HUB + "/api/v1/report",
    method: "POST",
    header: {{"Content-Type": "application/json",
             "X-Guardian-Token": TOKEN}},
    data: {{ metric, ok, value, detail }},
    fail: () => {{}}            // 守护上报失败不影响业务
  }});
}}
App({{
  onLaunch() {{
    guardianReport();
    setInterval(() => guardianReport(), 60000);   // 每 60s 心跳
  }},
  onError(err) {{                                  // 页面/逻辑错误
    guardianReport(false, "runtime_error", 0, String(err));
  }}
}});
'''
        return {"python": py, "js": js, "wx": wx, "curl": curl}

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


# ----------------------------------------------------------------------------
# 演示模式：内置演示站点 + 演示目标
# ----------------------------------------------------------------------------
class _DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/slow":
            time.sleep(2.7)   # 刻意超过默认 2500ms 慢阈值
            self._send(200, b"guardian demo: slow page - ok after delay")
        elif path == "/err":
            self._send(500, b"guardian demo: internal error page")
        elif path == "/json":
            self._send(200, json.dumps({"app": "guardian-demo", "ts": time.time()},
                                       ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
        else:
            body = ("<html><meta charset='utf-8'><h1>Guardian Demo Site</h1>"
                    "<p>守护工作台演示站点 · 路径: /slow /err /json</p>"
                    "<p id='stamp'>stable-content-v1</p></html>").encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")

    def do_POST(self):
        self._send(200, b"demo site: ok")


def demo_seed():
    """首次运行 --demo 时写入演示目标（不覆盖已有配置）。"""
    if os.path.exists(TARGETS_FILE) and STORE.targets:
        return False
    demo_token = "demo-heartbeat-2026"
    seeds = [
        {"id": "demo-web-ok", "name": "演示站点 · 正常", "type": "website",
         "source": "probe", "url": "http://127.0.0.1:8800/",
         "probes": ["online", "latency", "content", "security"],
         "interval_s": 8, "timeout_ms": 3000},
        {"id": "demo-web-slow", "name": "演示站点 · 慢响应", "type": "website",
         "source": "probe", "url": "http://127.0.0.1:8800/slow",
         "probes": ["online", "latency"], "interval_s": 8, "timeout_ms": 5000},
        {"id": "demo-web-err", "name": "演示站点 · 故障", "type": "website",
         "source": "probe", "url": "http://127.0.0.1:8800/err",
         "probes": ["online", "latency"], "interval_s": 8, "timeout_ms": 3000},
        {"id": "demo-app-heartbeat", "name": "演示 App · 心跳上报", "type": "app",
         "source": "report", "interval_s": 30},
    ]
    for s in seeds:
        STORE.add_target(s, raw_token=(demo_token if s["id"] == "demo-app-heartbeat" else None))
    print("  演示目标已写入:", TARGETS_FILE)
    print("  演示 App 心跳 token: demo-heartbeat-2026")
    return True


def start_demo_site():
    """内置演示站点 :8800（让探测通道有真实对象可打）。"""
    server = ThreadingHTTPServer(("127.0.0.1", 8800), _DemoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="守护工作台 · 守护中枢")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8700）")
    ap.add_argument("--demo", action="store_true", help="写入并启用本地演示目标")
    ap.add_argument("--auth", action="store_true",
                    help="强制 token 鉴权（对外开放时建议开启）")
    ap.add_argument("--admin-token", default="",
                    help="管理端口令（--auth 下管理操作需 X-Guardian-Admin 头）")
    ap.add_argument("--verbose", action="store_true", help="打印请求日志")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    STORE.load_targets()
    STORE.load_config()

    if args.demo:
        demo_seed()
        start_demo_site()

    # 探测调度线程
    threading.Thread(target=probe_scheduler, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), GuardianHandler)
    httpd.auth_enforced = args.auth
    httpd.admin_token = args.admin_token
    httpd.verbose = args.verbose

    print("=" * 56)
    print("  守护工作台 Guardian Workbench  ·  守护中枢运行中")
    print("=" * 56)
    print(f"  驾驶舱    http://{args.host}:{args.port}/")
    print(f"  API       http://{args.host}:{args.port}/api/v1/*")
    print(f"  守护目标  {len(STORE.targets)} 个  ·  {'token 鉴权: 开启' if args.auth else 'token 鉴权: 本机信任'}")
    if args.demo:
        print(f"  演示站点  http://127.0.0.1:8800/ (内部)")
    print("  Ctrl+C 停止")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n守护中枢已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
