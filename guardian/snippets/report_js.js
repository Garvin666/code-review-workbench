/*
 * Guardian 上报片段 · JS 网页埋点
 * =================================
 * 把这段脚本放到被守护的网页里（HTML 头部 / 静态资源）。它会在页面加载时
 * 立刻上报一次心跳，并监听 window error / unhandledrejection 自动上报，
 * 同时按 setInterval 周期心跳。
 *
 * 使用方法：
 *   1) 修改下方 HUB、TOKEN 两个常量
 *   2) 在 HTML <head> 里加入：
 *        <script src="/path/to/report_js.js"></script>
 *      或复制下方整段贴入页面（注意替换 token）。
 */

(function () {
  const HUB   = "http://127.0.0.1:8700";        // ← 守护中枢地址
  const TOKEN = "<token>";                       // ← 粘贴 token
  const INTERVAL_MS = 60000;                     // 心跳周期
  const REPORT = HUB + "/api/v1/report";
  const EVENT  = HUB + "/api/v1/event";

  function post(path, body) {
    const json = JSON.stringify(body);
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(path, new Blob([json], { type: "application/json" }));
        return;
      }
    } catch (_) { /* 降级到 fetch */ }
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Guardian-Token": TOKEN },
      body: json,
      keepalive: true,
    }).catch(function (e) { console.warn("[guardian] post fail", e); });
  }

  function report(ok, metric, value, detail) {
    post(REPORT, { ok: ok !== false, metric: metric || "online", value: value, detail: detail || "" });
  }
  function sendEvent(type, message) { post(EVENT, { type: type, message: message }); }

  // 页面加载即一次心跳
  report(true, "online");

  // 错误自动上报
  window.addEventListener("error", function (e) {
    report(false, "runtime_error", 0, (e.message || "error") + " @ " + (e.filename || "") + ":" + (e.lineno || 0));
  });
  window.addEventListener("unhandledrejection", function (e) {
    report(false, "runtime_error", 0, String(e.reason || "unhandled rejection"));
  });

  // 周期心跳
  setInterval(function () { report(true, "online"); }, INTERVAL_MS);

  // 暴露给业务调用
  window.guardianReport = report;
  window.guardianEvent = sendEvent;
})();