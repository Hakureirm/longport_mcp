# Longport MCP Server

长桥 / Longbridge 证券 MCP (Model Context Protocol) 服务器，让 AI Agent 能够进行股票交易、查询行情、管理资产。

## 特性

- **REST API + SDK 混合架构**: 核心交易功能使用 REST API，高级行情/基本面使用官方 SDK
- **模拟盘/实盘切换**: 支持双账户配置，一键切换；模拟盘 REST 自动带 `x-papertrading`，SDK 启用 `enable_papertrading`
- **44+ 个 MCP 工具**: 覆盖交易、资产、行情、期权/窝轮、基本面与财经日历等
- **yfinance 备用**: 行情数据支持 yfinance 作为备用源，可查询加密货币
- **uvx 一键部署**: 无需复杂配置

## 0.4.0 更新说明 / Changelog (Longbridge SDK)

对齐 2026 年官方 OpenAPI / Python SDK 现状：

| 项目 | 旧 (≤0.3.0) | 新 (0.4.0) |
|------|-------------|------------|
| PyPI 包 | `longport>=1.0.0`（过时/已弃用） | **`longbridge>=5.2.2`**（官方推荐） |
| Python 导入 | `from longport.openapi import ...` | `from longbridge.openapi import ...` |
| Config 构造 | `Config(app_key=...)`（5.x 已失效） | `Config.from_apikey(..., enable_papertrading=...)` |
| HTTP 主机 | `openapi.longportapp.com` | `openapi.longbridge.com`（可用 `LONGBRIDGE_HTTP_URL` 覆盖） |
| 环境变量 | 仅 `LONGPORT_*` | **`LONGBRIDGE_*` 优先**，`LONGPORT_*` 仍兼容 |
| 模拟盘 | 仅双凭证切换 | REST 增加 `x-papertrading: true`；SDK `enable_papertrading` |
| 新工具 | — | `get_market_status` / `get_company_info` / `get_finance_calendar` |

官方文档：https://open.longbridge.com/ · SDK：`pip install longbridge`

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

### 配置

#### 方式 1: 环境变量（支持双账户）

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

#### 方式 2: 配置文件

创建 `longport_config.json`:

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

#### 方式 3: 单账户模式（向后兼容）

```bash
export LONGBRIDGE_APP_KEY="your_app_key"
export LONGBRIDGE_APP_SECRET="your_app_secret"
export LONGBRIDGE_ACCESS_TOKEN="your_access_token"
```

## 模拟盘/实盘切换

```
# 查看当前模式
get_account_mode()

# 切换到实盘（会显示警告）
switch_account_mode("real")

# 切换回模拟盘
switch_account_mode("paper")
```

下单时会显示当前模式：`[模拟盘] [OK] 订单已提交` 或 `[实盘] [OK] 订单已提交`

## 可用工具列表

### 账户管理
| 工具名 | 功能 |
|--------|------|
| `get_account_mode` | 查看当前账户模式（模拟盘/实盘） |
| `switch_account_mode` | 切换账户模式 |
| `check_sdk_status` | 检查 SDK 和模块状态 |

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

## 获取 API 凭证

1. 注册 [Longbridge](https://longbridge.com) / [长桥证券](https://longportapp.com) 账户
2. 进入 [开发者后台](https://open.longbridge.com)
3. 创建应用获取 `app_key` 和 `app_secret`
4. 生成 `access_token`（模拟盘和实盘分别生成）

## 本地校验（无需真实凭证）

```bash
pip install -e ".[dev]"
pytest -q
python -c "import longport_mcp_web as m; print('HAS_SDK', m.HAS_SDK); print('tools', len(m.mcp._tool_manager._tools) if hasattr(m.mcp, '_tool_manager') else 'ok')"
```

带凭证的联调（请自行设置环境变量，**勿提交密钥**）：

```bash
export LONGBRIDGE_PAPER_APP_KEY=...
export LONGBRIDGE_PAPER_APP_SECRET=...
export LONGBRIDGE_PAPER_ACCESS_TOKEN=...
# 然后在 MCP 客户端中调用 check_sdk_status / get_quote / get_market_status 等
```

## 许可证

MIT License
