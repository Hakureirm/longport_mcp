# Longport MCP Server

长桥证券 MCP (Model Context Protocol) 服务器，让 AI Agent 能够进行股票交易、查询行情、管理资产。

## 特性

- **REST API + SDK 混合架构**: 核心交易功能使用 REST API，高级行情功能使用 SDK
- **模拟盘/实盘切换**: 支持双账户配置，一键切换模拟盘和实盘
- **41 个 MCP 工具**: 覆盖交易、资产、行情、期权/窝轮等全部功能
- **yfinance 备用**: 行情数据支持 yfinance 作为备用源，可查询加密货币
- **uvx 一键部署**: 无需复杂配置

## 快速开始

### 安装

```bash
# 推荐：uvx 直接运行
uvx longport-mcp

# 或者 pip 安装
pip install longport-mcp
```

### 配置

#### 方式 1: 环境变量（支持双账户）

```json
{
  "command": "uvx",
  "args": ["longport-mcp"],
  "env": {
    "LONGPORT_PAPER_APP_KEY": "模拟盘 app_key",
    "LONGPORT_PAPER_APP_SECRET": "模拟盘 app_secret",
    "LONGPORT_PAPER_ACCESS_TOKEN": "模拟盘 access_token",
    "LONGPORT_REAL_APP_KEY": "实盘 app_key",
    "LONGPORT_REAL_APP_SECRET": "实盘 app_secret",
    "LONGPORT_REAL_ACCESS_TOKEN": "实盘 access_token",
    "LONGPORT_DEFAULT_MODE": "paper"
  }
}
```

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
export LONGPORT_APP_KEY="your_app_key"
export LONGPORT_APP_SECRET="your_app_secret"
export LONGPORT_ACCESS_TOKEN="your_access_token"
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
| `get_symbol_fundamentals` | 基本面信息 | REST API |
| `search_symbol` | 搜索股票代码 | yfinance |
| `get_exchange_rate` | 汇率查询 | yfinance |

### SDK 专属功能
| 工具名 | 功能 |
|--------|------|
| `get_trading_days` | 交易日历 |
| `get_capital_flow` | 个股资金流向 |
| `get_capital_distribution` | 资金分布 |

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

1. 注册 [长桥证券](https://longportapp.com) 账户
2. 进入 [开发者后台](https://open.longportapp.com)
3. 创建应用获取 `app_key` 和 `app_secret`
4. 生成 `access_token`（模拟盘和实盘分别生成）

## 许可证

MIT License
