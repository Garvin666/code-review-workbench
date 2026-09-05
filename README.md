# 代码审查工作台（Code Review Workbench）

一套「设计稿还原 → 交互应用 → 守护服务」完整落地的演示/工具型项目：Notion 风格代码审查工作台界面、单文件交互应用、配套数据管道，以及一个真正可运行的**守护中枢（Guardian Workbench）**——把网站 / 小程序 / App 接入后被统一守护、探测与告警。

> 纯前端页面可直开演示；守护中枢为 Python 标准库零依赖服务，一条命令启动。

---

## 仓库内容

| 路径 | 说明 |
|---|---|
| `index.html` | 仓库门户入口（GitHub Pages 首页） |
| `代码审查工作台_v2.html` | **主推** · 6 页面 SPA + 内嵌「守护中枢」第 8 导航页，单文件自包含，1440×960 设计稿还原 |
| `代码审查工作台.html` | 首版 · 仅实时审查页（深海军蓝代码面板 + 实时审查流） |
| `guardian/` | **守护中枢 v0.3**：`guardian_server.py`（单文件 API 服务）+ `guardian_ui.html`（独立驾驶舱）+ `public_status.html`（公开只读状态页）+ `agent_collect.py`（代理采集）+ `snippets/`（多语言接入片段）+ `start.bat` |
| `守护工作台_设计方案.md` | 守护中枢设计方案（v0.3，含升级实录） |
| `overview.md` | 工作台设计稿与功能概述 |
| `review_pipeline/` | 代码审查数据管道：采样 → 后端推理 → 结果抽取 → 试点报告 |

## 页面入口（GitHub Pages）

- 门户：<https://garvin666.github.io/code-review-workbench/>
- 工作台 v2（含守护中枢页）：[代码审查工作台_v2.html](代码审查工作台_v2.html)
- 守护驾驶舱独立版（连不上中枢时自动演示降级）：[guardian/guardian_ui.html](guardian/guardian_ui.html)

## 守护中枢（Guardian Workbench）快速开始

```bash
cd guardian
python guardian_server.py --demo      # 零依赖启动，默认 http://127.0.0.1:8700（Windows 可直接 start.bat）
```

浏览器打开 <http://127.0.0.1:8700/> 即驾驶舱；`--demo` 附带本地演示站点与示例目标，无网可完整体验。

### 双通道守护

- **中枢探测**：对登记 URL 定时巡检 online / latency / tls / content / security 五项；
- **客户端上报**：被守护方调用 `/api/v1/report` 心跳 / 异常（网站 / 小程序 / App 皆可），见 `guardian/snippets/`（Python / JS 埋点 / 微信小程序 / curl）；
- **代理采集**：不可改造的存量目标用 `agent_collect.py` 代为上报。

### v0.3 核心能力（对标 uptime-kuma / gatus / healthchecks）

- 通知渠道：Webhook / 企业微信 / 钉钉 / 飞书 / Server酱（状态翻转异步投递 + 测试通知）
- 可用率统计 24h / 7d / 30d（事件时间线重算，跨重启不丢，扣除维护期）
- 维护窗口：UI 横幅公告 + 通知静默 + 可用率豁免
- 登记自测 `/api/v1/validate` · Token 轮换 / 启停 / 注销 · 目标公开详情带连接口令
- SVG 可用率徽章 + 公开只读状态页 `/public`（对外分发 `public_status.html` 由驾驶舱托管）

### 常用 API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/report` | 客户端心跳 / 异常上报（带 `X-Guardian-Token`） |
| POST | `/api/v1/event` | 客户端业务事件上报 |
| GET | `/api/v1/status` | 驾驶舱全量状态（目标 / 事件 / 可用率 / 维护） |
| GET | `/api/v1/targets` | 目标与接入片段 |
| POST | `/api/v1/targets` | 登记新目标（返回一次性明文 Token） |
| POST | `/api/v1/validate` | 登记前连通性自测 |
| GET | `/public` | 公开只读状态页 |

> 服务端默认只监听 `127.0.0.1`；对外开放加 `--host 0.0.0.0 --auth` 并保留管理令牌。

## 工作台功能速览

- **实时审查**：六阶段流水线、LIVE 事件流、语义着色代码、行内 P0/P1 发现卡、暂停 / 继续、标记误报 / 加入整改单、导出报告
- **检查清单**：OWASP 项严重度 / 目标过滤、38 项已通过进度动画
- **代码扫描**：21 条规则、实时进度、LIVE 命中流
- **漏洞跟踪**：P0–P3 KPI、状态分布、责任人负载、逾期提醒
- **工具矩阵**：SAST / DAST / SCA 8 工具 2×4 网格
- **报告导出**：3 种模板切换 + 文档预览 + 导出历史
- **项目联动**：侧栏 mall-ecommerce / user-center / pay-service 三项目全局切换
- **守护中枢**：见上节

## 视觉系统

Notion 式浅色编辑外壳 + 主色紫 `#5645D4` + 深海军蓝 `#0A1530` 代码面板；P0–P3 语义色统一复用；Inter + JetBrains Mono。页面均为 1440×960 设计稿的单文件还原（Ardot 画布 722497116688493 / 722522304544442）。

## 设计与说明

- 界面为设计稿还原产物，面向演示 / 原型 / 对外讲解场景；守护中枢 API 与状态判定逻辑为可运行的真实实现（状态机 ok → warn → down，5s 去抖）。
- 冒烟测试记录见各版本说明与 `.workbuddy/smoke-test/`（本地工作区，不入库）。
