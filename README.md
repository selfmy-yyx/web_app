# AI Panel Studio

本地 AI 圆桌演播厅。浏览器通过 FastAPI 创建场次、确认阵容和读取详情，SQLite 保存讨论数据与可回放的 SSE 事件。

## 技术选型

| 层 | 选型与职责 |
| --- | --- |
| 前端 | React 19、TypeScript、Vite，提供中文首页、阵容页和响应式演播厅 |
| 后端 | Python 3.11+、FastAPI、Uvicorn，负责阵容生成、场次调度和 API |
| 存储 | SQLite 3.38+，使用事务和外键保证业务数据、事件及场次边界 |
| 实时更新 | SSE + EventSource，客户端先取快照，再通过场次游标续接 |
| 模型 | 后端 OpenAI 兼容 Chat Completions，测试可注入固定模型 |

## 本地运行

在仓库根目录启动后端。先创建并填写 `backend/.env`，设置 `MODEL_BASE_URL`、`MODEL_NAME` 和 `MODEL_API_KEY`。模型服务需支持 Chat Completions 和 JSON 对象响应。

```dotenv
MODEL_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
MODEL_API_KEY=
```

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --env-file backend/.env
```

另开终端启动前端。

```powershell
cd frontend
npm install
npm run dev
```

打开 [http://localhost:5173/](http://localhost:5173/)。真实 API 为默认模式，Vite 将 `/api` 转发到本地 8000 端口。需要单独查看原前端模拟时，设置 `$env:VITE_DATA_MODE = 'mock'` 后启动 Vite。

前端建议使用 Node.js 22.12+。已有虚拟环境或在其他操作系统上运行时，可用该环境中的 Python 替代 PowerShell 示例中的 `\.venv\Scripts\python.exe`。

默认数据库为 `backend/panel.sqlite3`，可通过后端环境变量 `PANEL_DB_PATH` 指定。服务以单个 Uvicorn worker 运行，启动时恢复数据库中仍在生成或进行中的任务。浏览器页面可随时关闭或重新进入。

直播场次发布一轮发言后默认停留 30 秒，再开始生成下一轮或收尾。停顿期间，访客可从首页进入并查看已发布的内容。需要更长的入场窗口时，可在启动后端前设置 `$env:PANEL_TURN_PAUSE_SECONDS = '45'`。该值以整秒为单位，设为 `0` 可用于快速演示。修改后需重启后端。

## SQLite 和样例

后端首次启动会执行 [初始化脚本](db/init.sql)。需要先导入数据时，在仓库根目录执行以下命令。导入器也会为新路径自动建表。

```powershell
$env:PANEL_DB_PATH = 'backend/panel.sqlite3'
.\.venv\Scripts\python.exe -m db.seed --db $env:PANEL_DB_PATH
```

[五个样例](db/samples.json)各有 1 位主持人和 4 位专业背景、立场各异的专家。导入不请求模型，重复运行会保留已有样例。启动前后端后，打开导入命令打印的 URL 查看待确认阵容。首页只列进行中的讨论。点击确认后，真实 API 模式需要已配置模型。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `MODEL_BASE_URL` | 后端模型 API 前缀，含供应商需要的版本路径 |
| `MODEL_NAME` | 后端请求的模型名称 |
| `MODEL_API_KEY` | 后端环境中的密钥 |
| `PANEL_DB_PATH` | SQLite 文件路径，默认 `backend/panel.sqlite3` |
| `PANEL_TURN_PAUSE_SECONDS` | 每轮发布后停顿的秒数，默认 30，设为 0 可关闭 |
| `VITE_DATA_MODE` | 可设为 `mock` 启用前端演示，默认真实 API |
| `VITE_API_BASE` | 前端 API 基路径，默认 `/api/v1` |

模型请求每次最多等待 60 秒。共识提取重试一次后仍失败时，讨论继续，保留最近一次有效结果。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/discussions` | 列出进行中的讨论 |
| POST | `/api/v1/discussions` | 创建草稿并异步生成阵容 |
| POST | `/api/v1/discussions/{id}/roster/regenerate` | 按阵容版本重新生成或重试 |
| POST | `/api/v1/discussions/{id}/confirm` | 幂等确认并开场 |
| GET | `/api/v1/discussions/{id}` | 获取快照及事件游标 |
| GET | `/api/v1/discussions/{id}/events?after={cursor}` | SSE 回放和续接 |

完整请求、错误响应与事件类型见 [API 契约](开发文档/api.md)。需求和数据关系见 [PRD](开发文档/prd.md) 与 [Mermaid ER 图](开发文档/schema.md)。

## 验证

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -m pytest backend/tests -q
npm --prefix frontend run build
```

集成测试把可重复的假模型注入真实 FastAPI、SQLite 和 SSE 路径，验证并行场次、重复确认、事件续接与模型失败隔离。

### Playwright

安装上述后端依赖、前端依赖及 Playwright 的 Chromium，然后在仓库根目录运行端到端测试。

```powershell
npm --prefix frontend exec -- playwright-core install chromium
$env:PANEL_E2E_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:VITE_DATA_MODE = 'api'
npm --prefix frontend run test:e2e
```

可以通过 `PANEL_E2E_CHROMIUM` 使用现有 Chromium 或 Chrome 的可执行文件，通过 `PANEL_E2E_PYTHON` 指定 Python。测试使用生产构建和每例独立的临时 SQLite 数据库。Playwright 将请求直接转发给 FastAPI ASGI，SSE 帧通过短连接回放，浏览器负责续接。测试无需模型密钥或监听端口。

Windows 环境若缺少 Playwright 下载的 Chromium，可将 `PANEL_E2E_CHROMIUM` 设为本机 Chrome 的可执行文件路径。如果环境还限制 Python asyncio 的内部回环连接，可在运行测试前设置 `$env:PANEL_E2E_NO_LOOPBACK = '1'`。

## 已实现与后续改进

- 已实现阵容确认和重试、按讨论上下文调度、公开专家状态、正式实录、讨论中观点更新、自然语言总结、并行场次隔离和断线续接。
- 后续可增加真实模型的质量评测、持续 TCP 长连接验收，以及支持多 worker 的持久化任务协调。
- 开发材料见 [原始 Prompt](开发文档/prompts.md) 与 [工作流说明](开发文档/workflow.md)。
- 提交附件、仓库邀请和验收结果见 [提交核对表](docs/delivery.md)。
