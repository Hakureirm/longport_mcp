# Longport MCP Server

基于 REST API 的长桥证券 MCP (Model Context Protocol) 服务器，完全不依赖 longport SDK。

## 特性

- **无 SDK 依赖**: 直接使用 REST API，通过 HMAC-SHA256 签名进行鉴权
- **34 个 MCP 工具**: 覆盖交易、资产、行情、期权/窝轮等全部功能
- **stdio 协议**: 支持所有兼容 MCP 的 AI Agent
- **uvx 部署**: 一键安装运行

## 快速开始

### 方式 1: uvx (推荐)

```bash
# 直接运行
uvx longport-mcp

# 或者安装后运行
uv tool install longport-mcp
longport-mcp
```

### 方式 2: pip 安装

```bash
pip install longport-mcp
longport-mcp
```

### 方式 3: 从源码运行

```bash
git clone https://github.com/Hakureirm/longport_mcp.git
cd longport_mcp
pip install -e .
python longport_mcp_web.py
```

## 配置

### 环境变量配置

```bash
export LONGPORT_APP_KEY="your_app_key"
export LONGPORT_APP_SECRET="your_app_secret"
export LONGPORT_ACCESS_TOKEN="your_access_token"
```

### 配置文件配置

创建 `longport_config.json`:

```json
{
    "app_key": "your_app_key",
    "app_secret": "your_app_secret",
    "access_token": "your_access_token"
}
```

## Agent 接入配置

### Claude Desktop

将以下内容添加到 Claude Desktop 配置文件:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "longport-trading": {
      "command": "uvx",
      "args": ["longport-mcp"],
      "env": {
        "LONGPORT_APP_KEY": "your_app_key",
        "LONGPORT_APP_SECRET": "your_app_secret",
        "LONGPORT_ACCESS_TOKEN": "your_access_token"
      }
    }
  }
}
```

### 通用 MCP 配置 (mcp.json)

```json
{
  "mcpServers": {
    "longport-mcp": {
      "command": "uvx",
      "args": ["longport-mcp"],
      "env": {
        "LONGPORT_APP_KEY": "${LONGPORT_APP_KEY}",
        "LONGPORT_APP_SECRET": "${LONGPORT_APP_SECRET}",
        "LONGPORT_ACCESS_TOKEN": "${LONGPORT_ACCESS_TOKEN}"
      }
    }
  }
}
```

### 使用 Python 直接运行

```json
{
  "mcpServers": {
    "longport-mcp": {
      "command": "python",
      "args": ["-m", "longport_mcp_web"],
      "cwd": "/path/to/longport_mcp",
      "env": {
        "LONGPORT_APP_KEY": "your_app_key",
        "LONGPORT_APP_SECRET": "your_app_secret",
        "LONGPORT_ACCESS_TOKEN": "your_access_token"
      }
    }
  }
}
```

### 使用 uv run

```json
{
  "mcpServers": {
    "longport-mcp": {
      "command": "uv",
      "args": ["run", "longport-mcp"],
      "env": {
        "LONGPORT_APP_KEY": "your_app_key",
        "LONGPORT_APP_SECRET": "your_app_secret",
        "LONGPORT_ACCESS_TOKEN": "your_access_token"
      }
    }
  }
}
```

## 可用工具列表

### 交易工具 (Trading)

| 工具名 | 功能 |
|--------|------|
| `place_order` | 立即下单（市价/限价） |
| `submit_order_advanced` | 高级下单（支持条件单、盘前盘后等） |
| `modify_order` | 修改订单 |
| `cancel_order` | 撤销单个订单 |
| `cancel_all_orders` | 撤销所有订单 |
| `get_open_orders` | 获取今日挂单 |
| `get_today_all_orders` | 获取今日所有订单 |
| `get_history_orders` | 获取历史订单 |
| `get_order_detail` | 获取订单详情 |
| `estimate_buy_limit` | 估算最大可买数量 |

### 成交记录 (Executions)

| 工具名 | 功能 |
|--------|------|
| `get_today_executions` | 今日成交记录 |
| `get_history_executions` | 历史成交记录 |

### 资产工具 (Assets)

| 工具名 | 功能 |
|--------|------|
| `get_assets` | 账户资产余额 |
| `get_positions` | 股票持仓 |
| `get_fund_positions` | 基金持仓 |
| `get_cash_flow` | 资金流水 |
| `get_margin_ratio` | 保证金比率 |

### 行情工具 (Quotes)

| 工具名 | 功能 |
|--------|------|
| `get_quote` | 实时行情 |
| `get_candlesticks` | K线数据 |
| `get_depth` | 盘口深度 |
| `get_brokers` | 经纪商队列（港股） |
| `get_trades` | 成交明细 |
| `get_intraday` | 分时数据 |
| `get_static_info` | 标的基本信息 |
| `get_symbol_fundamentals` | 基本面信息 |

### 期权/窝轮 (Options/Warrants)

| 工具名 | 功能 |
|--------|------|
| `get_option_expiry_dates` | 期权到期日列表 |
| `get_warrant_issuers` | 窝轮发行商列表 |

### 定时任务 (Scheduled Tasks)

| 工具名 | 功能 |
|--------|------|
| `schedule_trade` | 定时下单 |
| `schedule_cancel_all` | 定时全撤 |
| `list_schedules` | 查看定时任务 |
| `cancel_schedule` | 取消定时任务 |

### 通用工具 (Utilities)

| 工具名 | 功能 |
|--------|------|
| `call_longport_api` | 通用 API 调用 |
| `search_symbol` | 搜索股票代码 |
| `get_exchange_rate` | 汇率查询 |

## API 端点参考

| 功能 | HTTP 方法 | 端点 |
|------|----------|------|
| 账户余额 | GET | `/v1/asset/account` |
| 股票持仓 | GET | `/v1/asset/stock` |
| 基金持仓 | GET | `/v1/asset/fund` |
| 资金流水 | GET | `/v1/asset/cashflow` |
| 下单 | POST | `/v1/trade/order` |
| 改单 | PUT | `/v1/trade/order` |
| 撤单 | DELETE | `/v1/trade/order` |
| 订单详情 | GET | `/v1/trade/order` |
| 今日订单 | GET | `/v1/trade/order/today` |
| 历史订单 | GET | `/v1/trade/order/history` |
| 今日成交 | GET | `/v1/trade/execution/today` |
| 历史成交 | GET | `/v1/trade/execution/history` |

## 获取 API 凭证

1. 注册 [长桥证券](https://longportapp.com) 账户
2. 进入 [开发者后台](https://open.longportapp.com)
3. 创建应用获取 `app_key` 和 `app_secret`
4. 生成 `access_token`

## 许可证

MIT License
