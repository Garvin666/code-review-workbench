# 守护工作台（Guardian Workbench）

> 把「代码审查工作台」的形态从演示界面升级为**守护中枢**——对外分发 API，让网站 / 小程序 / App 接入后被守护。
> 当前版本：**v0.3**（2026-09-05）· 能力取舍对标 uptime-kuma / gatus / healthchecks，落地见「v0.3 新增」。

## 一句话理解

你打开浏览器看到的工作台，不再是展示数据用的——它本身就是一个**守护服务**（Python 单文件，零第三方依赖）。其他需要被守护的网站、小程序、App 只需要把一段 ≤15 行的代码片段接到自己那边，定时往工作台的 `/api/v1/report` 上报心跳；工作台帮你记住所有目标的状态、判定异常、通知推送、对外公开状态。

## 目录结构

```
guardian/
├─ guardian_server.py      # 主程序：HTTP + 探测引擎 + 状态机 + 存储 + API（v0.3）
├─ guardian_ui.html        # 守护驾驶舱界面（含演示降级，file:// / Pages 直开可预览）
├─ public_status.html      # 公开只读状态页（服务路径 /public 或 /status）
├─ agent_collect.py        # 代理采集脚本（替不可改造目标上报）
├─ start.bat               # 一键启动（演示模式）
├─ snippets/
│  ├─ report_python.py     # 被守护方 Python 心跳片段
│  ├─ report_js.js         # 被守护方 JS 网页埋点片段
│  ├─ report_wx.js         # 被守护方微信小程序埋点片段（app.js）
│  └─ report_curl.txt      # curl 定时任务 / 脚本示例
└─ data/                   # 运行期自动创建
   ├─ targets.json         # 目标清单（token 仅存哈希）
   ├─ events.jsonl         # 事件流（追加写，兼作可用率重算数据源）
   ├─ notify.json          # v0.3 通知渠道配置
   └─ maintenance.json     # v0.3 维护窗口
```

## 30 秒上手

### 1) 启动守护中枢

```bash
cd guardian
python guardian_server.py --demo
```

打开 `http://127.0.0.1:8700/` 就是驾驶舱。`--demo` 在 `127.0.0.1:8800` 起演示站点并预置示例目标（正常 / 慢 / 故障 / App 心跳）。Windows 也可直接双击 `start.bat`。

> 无后端也能看界面：直接双击打开 `guardian_ui.html`（file://），或放到 GitHub Pages 上静态托管——页面会进入**演示模式**展示内置数据，顶部横幅可「重试连接」回到真实中枢。

### 2) 把被守护方接入中枢

驾驶舱右上角「+ 登记目标」→ 选通道 →（探测通道可先点 **「测试连通」** 单次验证，不入库）→ 登记并领取一次性 token。

| 通道 | 适用 | 接入成本 |
|---|---|---|
| **中枢探测** (probe) | 任何能给 URL 的网站 / API | 0 行：填 URL 即可 |
| **客户端上报** (report) | 小程序 / App / 网页埋点 | ≤15 行：snippets/ 任选 |
| **代理采集** (agent) | 老项目、不可改造目标 | 跑 `agent_collect.py` |

### 3) 配置通知渠道（v0.3）

顶栏「通知设置」→ 预设：Webhook / 企业微信 / 钉钉 / 飞书 / Server酱 → 粘贴机器人地址 → 勾选投递级别 → 保存 / 「发送测试」。状态翻转且命中级别时才投递；**维护窗口内自动静默**。也可直接编辑 `data/notify.json`。

### 4) 对外公开（v0.3）

- 公开只读状态页：`http://127.0.0.1:8700/public`
- 单目标可用率 SVG 徽章（README / 看板可嵌入）：
  `http://127.0.0.1:8700/api/v1/targets/<id>/badge.svg?window=7d`

## v0.3 新增（对标开源取舍）

| 能力 | 说明 | 入口 |
|---|---|---|
| 通知渠道 | webhook / 企微 / 钉钉 / 飞书 / Server酱；异步投递，失败回写事件流 | UI「通知设置」/ `POST /api/v1/notify` |
| 可用率 | 每目标 24h / 7d / 30d，events.jsonl 翻转事件精确重算，跨重启不丢 | `/api/v1/status` 返回 `target.uptime` |
| 维护窗口 | 计划内维护：UI 横幅公告、通知静默、可用率豁免 | `POST /api/v1/maintenance`（add/delete） |
| 登记自测 | 登记前连通性验证（不入库） | `POST /api/v1/validate` |
| Token 生命周期 | 轮换后旧 token 立即失效 | `POST /api/v1/targets/<id>/token/rotate` |
| 目标管理 | 启停 / 改参数 / 注销 | `POST|DELETE /api/v1/targets/<id>` |
| SVG 徽章 | 可用率徽章嵌入 README | `GET /api/v1/targets/<id>/badge.svg` |
| 公开状态页 | 无鉴权只读状态页 | `GET /public` |
| 演示降级 | UI 连不上服务自动切演示数据并可重连 | 自动 |

## API 一览（v1）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/report` | 心跳 / 指标上报 |
| POST | `/api/v1/event` | 一次性异常事件（crash / attack / error） |
| GET | `/api/v1/status` | 全量状态（含 uptime / maintenance / notify 摘要 / version） |
| GET | `/api/v1/history?target=&metric=` | 指标历史 |
| GET | `/api/v1/targets` | 目标清单（脱敏） |
| POST | `/api/v1/targets` | 登记目标，签发 token（一次性展示） |
| POST | `/api/v1/validate` | 登记前连通性自测 |
| POST | `/api/v1/targets/<id>/token/rotate` | 轮换 token |
| POST | `/api/v1/targets/<id>` | 更新目标（enabled/interval_s/name/url） |
| DELETE | `/api/v1/targets/<id>` | 注销目标 |
| GET | `/api/v1/maintenance` | 维护窗口（含生效中） |
| POST | `/api/v1/maintenance` | 维护窗口管理 `{action:add|delete}` |
| GET | `/api/v1/notify` | 通知配置摘要（URL 打码） |
| POST | `/api/v1/notify` | 通知配置 `{action:save|test}` |
| GET | `/api/v1/targets/<id>/badge.svg` | 可用率 SVG 徽章 |
| GET | `/public` `/status` | 公开只读状态页 |
| GET | `/api/v1/snippets` | 接入代码片段（python/js/wx/curl） |
| GET | `/api/v1/health` | 健康检查 |

鉴权：本地（127.0.0.1）默认信任；对外开放 `--auth` 后，目标维度上报需 `X-Guardian-Token`，管理操作需 `X-Guardian-Admin: <--admin-token>`。

## 判定引擎 / 状态机

```
             连续 ≥ fail_to_down 次失败
 ┌──────┐ ───────────────────────► ┌──────┐
 │  ok  │                          │ down │
 │  (绿)│ ◄─────────────────────── │  (红) │
 └──────┘   单次成功自动恢复        └──────┘
     ▲  │
     │  │ 连续 ≥ fail_to_warn 次失败 / 条件告警
     ▼  │
 ┌──────┐
 │ warn │ (黄) 慢 / 证书临期 / 内容变更 / 缺安全头
 └──────┘
```

- 默认阈值：warn=连续 1 次失败、down=连续 3 次；登记时可用 `thresholds` 覆盖。
- 事件类型：`target_down` / `target_recovered` / `probe_fail` / `probe_timeout` / `perf_slow` / `tls_expiring` / `content_changed` / `security_header_missing` / `client_crash` / `client_error` / `client_attack` / `target_removed` / `notify_failed`。
- **维护窗口**：状态照常记录，但通知静默、UI 横幅公告、可用率豁免该时段。
- **可用率口径**：down 时长按 events.jsonl 中 `target_down` → `target_recovered` 时间轴重算；全新且无任何故障历史的目标显示 100%（无故障即算在线），随时间自然丰满。

## 命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--host` | 绑定地址 | `127.0.0.1` |
| `--port` | 端口 | `8700` |
| `--demo` | 内置演示站点（`:8800`）+ 演示目标 | 关 |
| `--auth` | 强制 token 鉴权（对外开放时建议） | 关（本机信任） |
| `--admin-token` | 管理口令（`--auth` 下管理操作校验） | 空 |
| `--verbose` | 打印请求日志 | 关 |

## 已知边界（务必读完）

1. **真机上运行的小程序 / App 连不上 `127.0.0.1`**：客户端心跳要真实落地，需把中枢部署到目标可达的服务器；本地适合开发 / 演示 / 模拟器。这是部署边界，不是代码能绕过的。
2. **开放到公网**：`--auth --admin-token` + HTTPS 反代；token 只在登记 / 轮换时明文返回一次，服务端只存哈希。
3. **桌面通知**：依赖浏览器 Notification 授权；页面完全关闭后不弹。
4. **Windows 端口叠加**：多个 Python 进程可同时绑定同一端口（SO_REUSEADDR），curl 可能落到旧进程。重启前用 `Get-NetTCPConnection -LocalPort 8700,8800 -State Listen | Stop-Process` 清理。
5. **规模边界**：目标数十个、低频上报时单文件足够；更大规模再引入队列与独立存储。

## 相关

- 完整设计：`../守护工作台_设计方案.md`（v0.3 版含升级取舍章节）
- 端到端冒烟产物：`../.workbuddy/smoke-test/`
- 工作台视觉语言继承自 `../代码审查工作台_v2.html`（守护中枢为该工作台第 8 个导航页）
