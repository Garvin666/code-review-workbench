/**
 * Guardian 上报片段 · 微信小程序（app.js）
 * =========================================
 * 把下面内容并入你的小程序 app.js：
 *   1) 修改 HUB 与 TOKEN
 *      HUB   = 守护中枢地址。注意：真机上 127.0.0.1 指手机自己，
 *              请填可被小程序访问的服务器地址（如 https://guard.example.com）
 *      TOKEN = 登记目标后从工作台领取的 token
 *   2) 小程序会：启动即上报一次心跳 → 每 60s 一次心跳 → 捕获全局 onError
 *   3) 可选：在需要的地方主动调用 guardianReport(false, "crash", 0, "OOM in page")
 */
const HUB = "http://127.0.0.1:8700";          // ← 守护中枢地址
const TOKEN = "<登记目标后领取的 token>";      // ← 工作台里复制

const GUARDIAN_HEARTBEAT_S = 60;

function guardianReport(ok = true, metric = "online", value, detail) {
  wx.request({
    url: HUB + "/api/v1/report",
    method: "POST",
    header: { "Content-Type": "application/json", "X-Guardian-Token": TOKEN },
    data: { metric, ok, value, detail },
    fail: () => {},                // 守护上报失败绝不影响业务
    timeout: 5000,
  });
}

function guardianEvent(type, message) {
  wx.request({
    url: HUB + "/api/v1/event",
    method: "POST",
    header: { "Content-Type": "application/json", "X-Guardian-Token": TOKEN },
    data: { type, message },
    fail: () => {},
    timeout: 5000,
  });
}

App({
  onLaunch() {
    guardianReport();                              // 启动即心跳
    this._guardianTimer = setInterval(() => guardianReport(), GUARDIAN_HEARTBEAT_S * 1000);
  },
  onHide() {
    if (this._guardianTimer) { clearInterval(this._guardianTimer); this._guardianTimer = null; }
  },
  onShow() {
    if (!this._guardianTimer) {
      guardianReport();
      this._guardianTimer = setInterval(() => guardianReport(), GUARDIAN_HEARTBEAT_S * 1000);
    }
  },
  onError(err) {                                   // 全局脚本错误 → runtime_error
    guardianReport(false, "runtime_error", 0, String(err && err.message ? err.message : err));
  },
  // 供业务页调用：
  //   const app = getApp();
  //   app.guardianReport(false, "crash", 0, "webview 崩溃");
  guardianReport,
  guardianEvent,
});
