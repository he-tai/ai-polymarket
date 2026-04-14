# ai-polymarket

Polymarket 量化交易脚手架，包含三层能力：

- research：拉取历史价格并查看市场统计
- backtest：运行基础均值回归回测（含滑点与风控）
- execution：提交实盘订单（可选）并执行带心跳的轮询循环
- multi-market：基于 WebSocket 的多市场并行实盘（含熔断）
- storage：将交易/事件/PnL 日志落地到 SQLite 或 Postgres

## 环境准备

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

若要运行实盘，请在 `.env` 中填写：

- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- `SIGNATURE_TYPE` (`0` EOA, `1` proxy, `2` safe)
- `DATABASE_URL` (`sqlite:///trading.db` or `postgresql+psycopg://...`)

Polymarket 认证文档：<https://docs.polymarket.com/cn/api-reference/authentication>

## 命令说明

### 1）快照（市场 + 订单簿）

```bash
.venv/bin/python run_bot.py snapshot --slug russia-ukraine-ceasefire-before-gta-vi-554
```

### 2）研究（历史数据摘要）

```bash
.venv/bin/python run_bot.py research --slug russia-ukraine-ceasefire-before-gta-vi-554 --interval 1h --fidelity 5
```

### 3）回测（基础基线）

```bash
.venv/bin/python run_bot.py backtest --slug russia-ukraine-ceasefire-before-gta-vi-554 --window 24 --z-entry 1.2 --size 5 --slippage-bps 5 --taker-fee-bps 7 --impact-bps-per-unit 0.2
```

### 4）实盘下单（真实资金）

```bash
.venv/bin/python run_bot.py limit-order --slug russia-ukraine-ceasefire-before-gta-vi-554 --side BUY --price 0.45 --size 5 --signature-type 2 --confirm-live YES
```

### 5）实盘循环（轮询 + 心跳）

```bash
.venv/bin/python run_bot.py live-loop --slug russia-ukraine-ceasefire-before-gta-vi-554 --size 5 --loop-seconds 15 --max-loops 60 --signature-type 2 --confirm-live YES
```

### 6）多市场实盘（WebSocket + 熔断 + 数据库）

```bash
# 按 24h 交易量取前 3 个市场，选择 outcome 索引 0
.venv/bin/python run_bot.py live-multi --top-markets 3 --outcome-index 0 --size 5 --max-events 1200 --signature-type 2 --confirm-live YES

# 或者显式指定多个 slug
.venv/bin/python run_bot.py live-multi --slugs "fed-decision-in-october,bitcoin-above-100k-on-dec-31" --outcome-index 0 --signature-type 2 --confirm-live YES
```

`live-multi` includes:

- 每 N 个事件执行一次订单对账（`get_order`，参数 `--reconcile-every-events`）
- 组合级熔断（`--max-portfolio-loss`、`--max-portfolio-notional`）
- 单市场熔断（价差异常/API 错误/行情超时/连续亏损）

### 7）AI 报告（DeepSeek 数据分析）

先在 `.env` 中配置：

- `DEEPSEEK_API_KEY`
- 可选：`DEEPSEEK_MODEL`（默认 `deepseek-chat`）
- 可选：`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`）

然后执行：

```bash
.venv/bin/python run_bot.py ai-report --slug russia-ukraine-ceasefire-before-gta-vi-554 --interval 1h --fidelity 5
```

### 8）归因报表（CSV + Markdown）

```bash
.venv/bin/python run_bot.py attribution-report --days 7 --out-dir reports
```

输出文件：

- `reports/attribution-*.md`
- `reports/trades-*.csv`
- `reports/pnl-*.csv`

### 9）实盘预检（不下单）

用于验证 `.env` 里的 `PRIVATE_KEY` / `FUNDER_ADDRESS` / `SIGNATURE_TYPE` 是否正确。

```bash
.venv/bin/python run_bot.py preflight-live

# 社交账号登录用户常用：在 1 / 2 之间切换验证
.venv/bin/python run_bot.py preflight-live --signature-type 1
.venv/bin/python run_bot.py preflight-live --signature-type 2
```

### 10）资金检查（不下单）

用于检查当前余额与授权是否足够支持目标订单。

```bash
.venv/bin/python run_bot.py funding-check --price 0.45 --size 5 --signature-type 0
```

### 11）AI 自动分析并下单

默认是 dry-run（只输出计划，不下单）：

```bash
.venv/bin/python run_bot.py auto-trade --top-markets 10 --max-orders 2 --min-confidence 0.75 --default-size 5 --analysis-timeout-s 45
```

真实下单模式（务必小额）：

```bash
.venv/bin/python run_bot.py auto-trade --top-markets 10 --max-orders 1 --min-confidence 0.8 --default-size 5 --live --signature-type 1 --confirm-live YES
```

## Docker 自动运行（无需手动反复执行）

### 1）准备 `.env`

确保 `.env` 至少包含：

- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- `SIGNATURE_TYPE`
- `DEEPSEEK_API_KEY`
- `DATABASE_URL=sqlite:///trading.db`

并可选配置自动循环参数（见 `.env.example`）：

- `AUTO_LIVE_MODE`：`false`（默认 dry-run）或 `true`（真实下单）
- `AUTO_TOP_MARKETS`
- `AUTO_MAX_ORDERS`
- `AUTO_MIN_CONFIDENCE`
- `AUTO_DEFAULT_SIZE`
- `AUTO_ANALYSIS_TIMEOUT_S`
- `AUTO_INTERVAL_SECONDS`
- `AUTO_SIGNATURE_TYPE`

### 2）一键启动

```bash
docker compose up -d --build
```

### 3）查看日志

```bash
docker compose logs -f auto-trader
```

查看仪表盘（持仓 + 日志）：

- 浏览器打开：`http://localhost:8080`
- API：
  - `/api/positions` 当前持仓
  - `/api/trades?days=7` 交易日志
  - `/api/events?days=7` 事件日志
  - `/api/logs` 文件日志（运行/分析/下单）
  - `/api/runtime-config` 运行参数读取/更新
  - `/api/manual-order` 手动下单

### 4）停止

```bash
docker compose down
```

说明：

- 容器会循环执行 `auto-trade`，按 `AUTO_INTERVAL_SECONDS` 定期运行。
- `AUTO_LIVE_MODE=false` 时仅分析不下单，建议先验证稳定后再切到 `true`。

## 日志文件

程序会在项目根目录 `logs/` 写入三类日志：

- `logs/runtime.log`：运行日志
- `logs/analysis.log`：DeepSeek 分析日志
- `logs/orders.log`：下单日志

## 页面新增能力

- 手动下单：在仪表盘输入 `slug / outcome_index / side / price / size`，并填写 `confirm_live=YES` 后可直接提交订单。
- 参数面板：可修改自动循环参数（是否实盘、市场数量、置信度阈值、间隔等），保存后写入 `config/runtime_config.json`，自动循环下一轮会读取生效。

## 风险提示

- 本项目是工程脚手架，不构成投资建议。
- 回测已包含滑点/手续费/冲击成本，但仍为简化模型，可能与真实成交有偏差。
- 请始终先进行模拟或小资金测试。
- 熔断条件包括连续亏损、API 错误、行情超时与异常价差。
- 组合级熔断会在总亏损或总名义敞口越界时停机。
