# Longport MCP Server

长桥 / Longbridge 证券 MCP (Model Context Protocol) 服务器，让 AI Agent 能够进行股票交易、查询行情、管理资产。

## 特性

- **OAuth 2.0 推荐认证**: Bearer Token，无需 HMAC；首次 `oauth_login` 授权后 token 自动持久化与刷新
- **REST API + SDK 混合架构**: 核心交易走 REST（OAuth Bearer 或传统 API Key HMAC）；高级行情/基本面走官方 SDK
- **模拟盘/实盘切换**: 双账户或 OAuth + `enable_papertrading` / `x-papertrading`；一键切换
- **47+ 个 MCP 工具**: 覆盖认证、交易、资产、行情、期权/窝轮、基本面与财经日历等
- **yfinance 备用**: 行情数据支持 yfinance 作为备用源，可查询加密货币
- **uvx 一键部署**: 无需复杂配置

## 0.5.2 更新说明 / Changelog (Paper Auth)

- **模拟盘优先 API Key**：若配置了 `LONGBRIDGE_PAPER_*`，`get_auth_mode` / SDK 对 paper 模式优先走 HMAC，不再被 OAuth token 抢先
- 原因：OAuth 授权后交易接口常落到空账本（`account_channel=lb`），而 App 模拟盘对应 `lb_papertrading`，需用模拟盘 Access Token
- 可用 `LONGBRIDGE_AUTH_PREFER=oauth|apikey` 强制覆盖

## 0.5.1 更新说明 / Changelog (CI)

- 新增 GitHub Actions CI（`.github/workflows/ci.yml`）：多版本 unit smoke + 可选 paper 联调
- pytest marker `integration`；无仓库 secrets 时 paper job 跳过
- **勿将密钥写入仓库**；仅用 GitHub Actions repository secrets（模拟盘）

## 0.5.0 更新说明 / Changelog (OAuth 2.0)

在 0.4.0（Longbridge SDK 5.x）之上，对齐官方推荐的 OAuth 2.0 认证：

| 项目 | 0.4.0 | 新 (0.5.0) |
|------|-------|------------|
| 推荐认证 | 传统 API Key + HMAC | **OAuth 2.0 Bearer**（`LONGBRIDGE_OAUTH_CLIENT_ID` + `oauth_login`） |
| SDK Config | `Config.from_apikey` | **优先** `Config.from_oauth`；API Key 仍为回退 |
| REST | 仅 HMAC | OAuth 用 `HttpClient.from_oauth`；API Key 仍走 HMAC |
| 新工具 | — | `oauth_login` / `oauth_status` |
| Token | 环境变量 ACCESS_TOKEN | OAuth token 存 `~/.longbridge/openapi/tokens/<client_id>`，自动刷新 |
| 模拟盘 | `x-papertrading` + `enable_papertrading` | **OAuth 与 API Key 均保留** paper/live 切换 |

官方文档：https://open.longbridge.com/zh-CN/docs/getting-started · SDK：`pip install longbridge`

### 0.4.0 回顾（仍包含）

| 项目 | 旧 (≤0.3.0) | 0.4.0+ |
|------|-------------|--------|
| PyPI 包 | `longport`（已弃用） | **`longbridge>=5.2.2`** |
| 导入 | `from longport.openapi` | `from longbridge.openapi` |
| HTTP 主机 | `openapi.longportapp.com` | `openapi.longbridge.com`（`LONGBRIDGE_HTTP_URL` / `LONGBRIDGE_REGION`） |
| 环境变量 | 仅 `LONGPORT_*` | **`LONGBRIDGE_*` 优先**，`LONGPORT_*` 兼容 |

## 快速开始

### 安装

```bash
# 推荐：uvx 直接运行
uvx longport-mcp

# 或者 pip 安装
pip install longport-mcp
```

依赖会自动拉取官方 SDK：`longbridge>=5.2.2`（请勿再安装已弃用的 `longport` 包；若已安装请先 `pip uninstall longport`）。

> 本项目仍使用 MCP Python SDK 1.x（`mcp>=1.0.0,<2` / FastMCP）。MCP 2.x 将 FastMCP 重命名为 MCPServer，迁移留待后续版本。

### 配置（推荐：OAuth 2.0）

#### 1. 注册 OAuth 客户端（一次性）

```bash
curl -X POST https://openapi.longbridge.com/oauth2/register \
     -H "Content-Type: application/json" \
     -d '{
            "redirect_uris": ["http://localhost:60355/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code","refresh_token"],
            "response_types": ["code"],
            "client_name": "Longport MCP"
        }'
```

保存响应中的 `client_id`。`redirect_uris` 须与本机回调端口一致（默认 `60355`；可用 `LONGBRIDGE_OAUTH_CALLBACK_PORT` 覆盖）。

中国大陆网络可用 `https://openapi.longbridge.cn/oauth2/register`；或设置 `LONGBRIDGE_REGION=cn`。

#### 2. MCP 客户端环境变量

```json
{
  "command": "uvx",
  "args": ["longport-mcp"],
  "env": {
    "LONGBRIDGE_OAUTH_CLIENT_ID": "你的-client-id",
    "LONGBRIDGE_DEFAULT_MODE": "paper"
  }
}
```

也可写入 `longport_config.json`：

```json
{
    "oauth_client_id": "你的-client-id",
    "oauth_callback_port": 60355,
    "default_mode": "paper"
}
```

#### 3. 首次登录（交互一次）

在 MCP 客户端中调用：

1. `oauth_status` — 确认 client_id 已配置、模式为 `oauth_pending`
2. `oauth_login` — 返回浏览器授权 URL（并打印到 stderr）；在本机浏览器打开并完成授权
3. 回调 `http://localhost:60355/callback` 成功后，token 写入 `~/.longbridge/openapi/tokens/<client_id>`
4. 再调 `oauth_status` / `check_sdk_status` 确认模式为 `oauth`

之后启动会自动复用并刷新 token，无需重复授权。无图形界面时，把 `oauth_login` 返回的 URL 拷到任意浏览器即可（回调须能打回运行 MCP 的那台机器的 localhost）。

### 配置（兼容：传统 API Key）

仍支持 HMAC 签名方式，适合旧集成或无浏览器环境。

#### 环境变量（双账户）

```json
{
  "command": "uvx",
  "args": ["longport-mcp"],
  "env": {
    "LONGBRIDGE_PAPER_APP_KEY": "模拟盘 app_key",
    "LONGBRIDGE_PAPER_APP_SECRET": "模拟盘 app_secret",
    "LONGBRIDGE_PAPER_ACCESS_TOKEN": "模拟盘 access_token",
    "LONGBRIDGE_REAL_APP_KEY": "实盘 app_key",
    "LONGBRIDGE_REAL_APP_SECRET": "实盘 app_secret",
    "LONGBRIDGE_REAL_ACCESS_TOKEN": "实盘 access_token",
    "LONGBRIDGE_DEFAULT_MODE": "paper"
  }
}
```

`LONGPORT_*` 前缀仍然可用（向后兼容）。

#### 配置文件

创建 `longport_config.json`：

```json
{
    "paper": {
        "app_key": "模拟盘 app_key",
        "app_secret": "模拟盘 app_secret",
        "access_token": "模拟盘 access_token"
    },
    "real": {
        "app_key": "实盘 app_key",
        "app_secret": "实盘 app_secret",
        "access_token": "实盘 access_token"
    },
    "default_mode": "paper"
}
```

#### 单账户模式

```bash
export LONGBRIDGE_APP_KEY="your_app_key"
export LONGBRIDGE_APP_SECRET="your_app_secret"
export LONGBRIDGE_ACCESS_TOKEN="your_access_token"
```

> 优先级：已就绪的 OAuth（磁盘 token / 内存句柄）> API Key > 仅配置了 client_id 尚未授权（`oauth_pending`，需先 `oauth_login`）。

## 模拟盘/实盘切换

```
# 查看当前模式
get_account_mode()

# 切换到实盘（会显示警告）
switch_account_mode("real")

# 切换回模拟盘
switch_account_mode("paper")
```

- **API Key**：模拟盘使用 `LONGBRIDGE_PAPER_*` 凭证；REST 自动带 `x-papertrading: true`；SDK `enable_papertrading=True`
- **OAuth**：同一 OAuth 会话下，paper 模式同样设置 `enable_papertrading` / `x-papertrading`；real 模式关闭。请自行确认账户侧模拟盘权限。

下单时会显示当前模式：`[模拟盘] [OK] 订单已提交` 或 `[实盘] [OK] 订单已提交`

## 可用工具列表

### 认证 / 账户
| 工具名 | 功能 |
|--------|------|
| `oauth_login` | 启动 OAuth 2.0 授权（返回浏览器 URL） |
| `oauth_status` | 查看 OAuth / API Key 认证状态 |
| `get_account_mode` | 查看当前账户模式（模拟盘/实盘） |
| `switch_account_mode` | 切换账户模式 |
| `check_sdk_status` | 检查认证、SDK 和模块状态 |

### 交易工具
| 工具名 | 功能 |
|--------|------|
| `place_order` | 立即下单（市价/限价） |
| `submit_order_advanced` | 高级下单（条件单、盘前盘后） |
| `modify_order` | 修改订单 |
| `cancel_order` | 撤销单个订单 |
| `cancel_all_orders` | 撤销所有订单 |
| `get_open_orders` | 获取今日挂单 |
| `get_today_all_orders` | 获取今日所有订单 |
| `get_history_orders` | 获取历史订单 |
| `get_order_detail` | 获取订单详情 |
| `estimate_buy_limit` | 估算最大可买数量 |

### 成交记录
| 工具名 | 功能 |
|--------|------|
| `get_today_executions` | 今日成交记录 |
| `get_history_executions` | 历史成交记录 |

### 资产工具
| 工具名 | 功能 |
|--------|------|
| `get_assets` | 账户资产余额 |
| `get_positions` | 股票持仓 |
| `get_fund_positions` | 基金持仓 |
| `get_cash_flow` | 资金流水 |
| `get_margin_ratio` | 保证金比率 |

### 行情工具
| 工具名 | 功能 | 数据源 |
|--------|------|--------|
| `get_quote` | 实时行情（股票/加密货币） | REST API / yfinance |
| `get_candlesticks` | K线数据 | SDK / yfinance |
| `get_depth` | 盘口深度 | SDK |
| `get_brokers` | 经纪商队列（港股） | SDK |
| `get_trades` | 成交明细 | SDK |
| `get_intraday` | 分时数据 | SDK |
| `get_static_info` | 标的基本信息 | REST API |
| `get_symbol_fundamentals` | 基本面信息 | yfinance |
| `get_technical_analysis` | 技术指标 | SDK / yfinance |
| `search_symbol` | 搜索股票代码 | yfinance |
| `get_exchange_rate` | 汇率查询 | yfinance |

### SDK 专属功能
| 工具名 | 功能 |
|--------|------|
| `get_trading_days` | 交易日历 |
| `get_capital_flow` | 个股资金流向 |
| `get_capital_distribution` | 资金分布 |
| `get_market_status` | 各市场交易状态（MarketContext） |
| `get_company_info` | 公司概览（FundamentalContext） |
| `get_finance_calendar` | 财经日历（CalendarContext） |

### 期权/窝轮
| 工具名 | 功能 |
|--------|------|
| `get_option_expiry_dates` | 期权到期日列表 |
| `get_warrant_issuers` | 窝轮发行商列表 |

### 定时任务
| 工具名 | 功能 |
|--------|------|
| `schedule_trade` | 定时下单 |
| `schedule_cancel_all` | 定时全撤 |
| `list_schedules` | 查看定时任务 |
| `cancel_schedule` | 取消定时任务 |

### 通用工具
| 工具名 | 功能 |
|--------|------|
| `call_longport_api` | 通用 API 调用 |
| `get_current_time` | 获取当前时间 |

## 行情代码格式

```
美股: AAPL.US, TSLA.US, NVDA.US
港股: 700.HK, 9988.HK
A股: 600519.SH, 000001.SZ
加密货币: BTC-USD, ETH-USD, DOGE-USD (注意用横杠)
```

## 获取凭证

### OAuth（推荐）

1. 注册 [Longbridge](https://longbridge.com) / [长桥证券](https://longportapp.com) 并开户
2. 用上文 `curl` 注册 OAuth client，保存 `client_id`
3. 设置 `LONGBRIDGE_OAUTH_CLIENT_ID`，调用 `oauth_login` 完成首次授权

### 传统 API Key（兼容）

1. 进入 [开发者后台](https://open.longbridge.com)
2. 创建应用获取 `app_key` 和 `app_secret`
3. 生成 `access_token`（模拟盘和实盘分别生成）


## CI & Secrets（GitHub Actions）

仓库已配置 `.github/workflows/ci.yml`：

| Job | 何时运行 | 做什么 |
|-----|----------|--------|
| `unit` | push `main` / 本分支、对 `main` 的 PR | Python 3.10–3.13，`pytest -m "not integration"` |
| `paper-integration` | 仅当仓库 secret `LONGBRIDGE_PAPER_ACCESS_TOKEN` 非空 | 只读模拟盘联调（资产 / SDK 状态）；**默认不下单** |

### 需要配置的 Secrets

在 GitHub → **Settings → Secrets and variables → Actions** 中新增（**仅模拟盘 / paper**）：

| Secret 名称 | 说明 |
|-------------|------|
| `LONGBRIDGE_PAPER_APP_KEY` | 模拟盘 App Key |
| `LONGBRIDGE_PAPER_APP_SECRET` | 模拟盘 App Secret |
| `LONGBRIDGE_PAPER_ACCESS_TOKEN` | 模拟盘 Access Token |

可选：

| Secret 名称 | 说明 |
|-------------|------|
| `LONGBRIDGE_HTTP_URL` | 例如 `https://openapi.longbridge.cn`（大陆网络） |
| `LONGBRIDGE_REGION` | 如 `cn` / `hk` |
| `LONGBRIDGE_PAPER_ALLOW_ORDER` | 设为 `1` 才允许 CI 尝试下单（**默认不要设置**） |

**安全要求**

- 只用 **paper / 模拟盘** 凭证；不要放实盘 / live keys
- **永远不要**把 key/secret/token 提交进 git、README 或 Issue
- Fork 来的 PR 通常读不到 secrets，`paper-integration` 会跳过（属预期）

本地跑联调（自行 export，勿入库）：

```bash
export LONGBRIDGE_PAPER_APP_KEY=...
export LONGBRIDGE_PAPER_APP_SECRET=...
export LONGBRIDGE_PAPER_ACCESS_TOKEN=...
pytest -q -m integration
```

## 本地校验（无需真实凭证）

```bash
pip install -e ".[dev]"
pytest -q -m "not integration"
python -c "import longport_mcp_web as m; print('HAS_SDK', m.HAS_SDK); print(m.get_auth_mode()); print(m.oauth_status())"
```

带凭证的联调（请自行设置环境变量，**勿提交密钥**）：

```bash
# OAuth
export LONGBRIDGE_OAUTH_CLIENT_ID=...
# 首次：在 MCP 中 oauth_login，浏览器授权后
# 然后 check_sdk_status / get_quote / get_market_status

# 或 API Key
export LONGBRIDGE_PAPER_APP_KEY=...
export LONGBRIDGE_PAPER_APP_SECRET=...
export LONGBRIDGE_PAPER_ACCESS_TOKEN=...
```

## 许可证

MIT License
