#!/usr/bin/env python3
"""
Longport MCP Server - Hybrid Version
=====================================
长桥证券 MCP 服务，支持 REST API + SDK 混合模式。
- REST API: 交易、资产、持仓、订单
- SDK (可选): 交易日历、资金流向、实时推送等高级功能

配置方式：
1. 环境变量（推荐）: LONGPORT_APP_KEY, LONGPORT_APP_SECRET, LONGPORT_ACCESS_TOKEN
2. 配置文件: longport_config.json
"""

import sys
import logging
import requests
import re
import os
import json
import time
import hmac
import hashlib
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Union
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from mcp.server.fastmcp import FastMCP

# 尝试导入 yfinance 作为备用行情源
try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

# 尝试导入 longport SDK（用于高级功能）
try:
    from longport.openapi import Config, QuoteContext, TradeContext, Market, Period, AdjustType
    HAS_SDK = True
except ImportError:
    HAS_SDK = False

# ================= 配置区域 =================
logging.basicConfig(
    stream=sys.stderr,
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("longport_mcp")
# ===========================================

# ================= 常量定义 =================
BASE_URL = "https://openapi.longportapp.com"
CONFIG_FILE = "longport_config.json"

# 订单类型映射
ORDER_TYPE_MAP = {
    "LO": "LO",      # 限价单 Limit Order
    "MO": "MO",      # 市价单 Market Order
    "ELO": "ELO",    # 增强限价单
    "ALO": "ALO",    # 竞价限价单
    "AO": "AO",      # 竞价单
    "ODD": "ODD",    # 碎股单
}

# 订单方向映射
ORDER_SIDE_MAP = {
    "buy": "Buy",
    "sell": "Sell",
}

# 订单状态映射（用于判断是否可撤）
TERMINAL_STATUSES = [
    "FilledStatus",
    "CancelledStatus",
    "RejectedStatus",
    "ExpiredStatus",
    "Filled",
    "Cancelled",
    "Canceled",
    "Rejected",
    "Expired",
]
# ===========================================


class LongportClient:
    """
    长桥 OpenAPI HTTP 客户端

    负责：
    1. 读取 JSON 配置
    2. 生成符合文档要求的签名 Header
    3. 封装 GET/POST/PUT/DELETE 方法
    4. 处理 JSON 序列化和错误回显

    配置文件格式（支持双账户）：
    {
        "paper": {
            "app_key": "xxx",
            "app_secret": "xxx",
            "access_token": "xxx"
        },
        "real": {
            "app_key": "xxx",
            "app_secret": "xxx",
            "access_token": "xxx"
        },
        "default_mode": "paper"
    }

    也支持旧的单账户格式（向后兼容）：
    {
        "app_key": "xxx",
        "app_secret": "xxx",
        "access_token": "xxx"
    }
    """

    def __init__(self, config_path: Optional[str] = None, mode: str = "paper"):
        """
        初始化客户端

        Args:
            config_path: 配置文件路径，默认为当前目录下的 longport_config.json
            mode: 账户模式，"paper"=模拟盘，"real"=实盘

        支持的环境变量（双账户模式）：
            LONGPORT_PAPER_APP_KEY, LONGPORT_PAPER_APP_SECRET, LONGPORT_PAPER_ACCESS_TOKEN
            LONGPORT_REAL_APP_KEY, LONGPORT_REAL_APP_SECRET, LONGPORT_REAL_ACCESS_TOKEN
            LONGPORT_DEFAULT_MODE (可选，默认 "paper")

        也支持旧的单账户环境变量：
            LONGPORT_APP_KEY, LONGPORT_APP_SECRET, LONGPORT_ACCESS_TOKEN
        """
        self.mode = mode
        self.config = self._load_config(config_path)

        # 优先级: 1. 配置文件 2. 带模式前缀的环境变量 3. 旧环境变量

        # 检查是否为新格式配置文件（双账户）
        if "paper" in self.config or "real" in self.config:
            mode_config = self.config.get(mode, {})
            self.app_key = mode_config.get("app_key")
            self.app_secret = mode_config.get("app_secret")
            self.access_token = mode_config.get("access_token")
        else:
            self.app_key = self.config.get("app_key")
            self.app_secret = self.config.get("app_secret")
            self.access_token = self.config.get("access_token")

        # 如果配置文件没有，尝试环境变量（带模式前缀）
        mode_upper = mode.upper()
        if not self.app_key:
            self.app_key = os.getenv(f"LONGPORT_{mode_upper}_APP_KEY") or os.getenv("LONGPORT_APP_KEY")
        if not self.app_secret:
            self.app_secret = os.getenv(f"LONGPORT_{mode_upper}_APP_SECRET") or os.getenv("LONGPORT_APP_SECRET")
        if not self.access_token:
            self.access_token = os.getenv(f"LONGPORT_{mode_upper}_ACCESS_TOKEN") or os.getenv("LONGPORT_ACCESS_TOKEN")

        if not all([self.app_key, self.app_secret, self.access_token]):
            raise ValueError(f"缺少必要的配置 ({mode}): app_key, app_secret, access_token")

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json; charset=utf-8"
        })

    def _load_config(self, config_path: Optional[str] = None) -> Dict[str, str]:
        """加载配置文件"""
        paths_to_try = []

        if config_path:
            paths_to_try.append(config_path)

        # 尝试多个可能的配置文件位置
        paths_to_try.extend([
            CONFIG_FILE,
            os.path.join(os.path.dirname(__file__), CONFIG_FILE),
            os.path.expanduser(f"~/{CONFIG_FILE}"),
            "/etc/longport/config.json",
        ])

        for path in paths_to_try:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"无法读取配置文件 {path}: {e}")

        # 如果没有配置文件，返回空字典（将使用环境变量）
        return {}

    def _generate_signature(
        self,
        method: str,
        uri: str,
        params: str = "",
        body: str = ""
    ) -> str:
        """
        生成 API 签名

        签名算法：
        1. 构造 canonical_request = "{METHOD}|{URI}|{PARAMS}|{HEADERS}|{SIGNED_HEADERS}|{PAYLOAD_HASH}"
        2. sign_str = "HMAC-SHA256|" + SHA1(canonical_request)
        3. signature = HMAC-SHA256(app_secret, sign_str)

        Returns:
            签名字符串，格式：HMAC-SHA256 SignedHeaders=authorization;x-api-key;x-timestamp, Signature={signature}
        """
        timestamp = str(time.time())

        # 构造 headers 部分
        headers_str = f"authorization:{self.access_token}\nx-api-key:{self.app_key}\nx-timestamp:{timestamp}\n"
        signed_headers = "authorization;x-api-key;x-timestamp"

        # 构造 payload hash（如果有 body）
        payload_hash = ""
        if body:
            payload_hash = hashlib.sha1(body.encode('utf-8')).hexdigest()

        # 构造 canonical request
        canonical_request = f"{method.upper()}|{uri}|{params}|{headers_str}|{signed_headers}|{payload_hash}"

        # 生成签名
        sign_str = "HMAC-SHA256|" + hashlib.sha1(canonical_request.encode('utf-8')).hexdigest()
        signature = hmac.new(
            self.app_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return timestamp, f"HMAC-SHA256 SignedHeaders={signed_headers}, Signature={signature}"

    def _build_headers(self, method: str, uri: str, params: str = "", body: str = "") -> Dict[str, str]:
        """构建请求头"""
        timestamp, signature = self._generate_signature(method, uri, params, body)

        return {
            "X-Api-Key": self.app_key,
            "Authorization": self.access_token,
            "X-Timestamp": timestamp,
            "X-Api-Signature": signature,
            "Content-Type": "application/json; charset=utf-8"
        }

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """
        处理 API 响应

        Args:
            response: requests 响应对象

        Returns:
            解析后的 JSON 数据

        Raises:
            Exception: 如果 HTTP 状态码非 200 或 API 返回错误码
        """
        if response.status_code != 200:
            raise Exception(f"HTTP 错误: {response.status_code} - {response.text}")

        try:
            data = response.json()
        except json.JSONDecodeError:
            raise Exception(f"无法解析响应 JSON: {response.text}")

        # 检查 API 返回的业务错误码
        code = data.get("code", 0)
        if code != 0:
            message = data.get("message", "未知错误")
            raise Exception(f"API 错误 [{code}]: {message}")

        return data

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        发送 GET 请求

        Args:
            endpoint: API 端点，如 /v1/asset/account
            params: 查询参数

        Returns:
            API 响应数据
        """
        params_str = ""

        if params:
            # 构建查询字符串（按字母顺序排序）
            # 过滤掉 None 值的参数
            filtered_params = {k: v for k, v in params.items() if v is not None}
            sorted_params = sorted(filtered_params.items())
            params_str = "&".join(f"{k}={v}" for k, v in sorted_params)

        headers = self._build_headers("GET", endpoint, params_str)

        # 直接拼接 URL，避免 requests 库的额外编码导致签名不匹配
        if params_str:
            url = f"{BASE_URL}{endpoint}?{params_str}"
        else:
            url = f"{BASE_URL}{endpoint}"

        response = self.session.get(url, headers=headers, timeout=30)
        return self._handle_response(response)

    def post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        发送 POST 请求

        Args:
            endpoint: API 端点
            data: 请求体数据

        Returns:
            API 响应数据
        """
        url = f"{BASE_URL}{endpoint}"
        body = json.dumps(data, ensure_ascii=False) if data else ""

        headers = self._build_headers("POST", endpoint, "", body)

        response = self.session.post(url, data=body.encode('utf-8'), headers=headers, timeout=30)
        return self._handle_response(response)

    def put(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送 PUT 请求"""
        url = f"{BASE_URL}{endpoint}"
        body = json.dumps(data, ensure_ascii=False) if data else ""

        headers = self._build_headers("PUT", endpoint, "", body)

        response = self.session.put(url, data=body.encode('utf-8'), headers=headers, timeout=30)
        return self._handle_response(response)

    def delete(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送 DELETE 请求"""
        params_str = ""

        if params:
            filtered_params = {k: v for k, v in params.items() if v is not None}
            sorted_params = sorted(filtered_params.items())
            params_str = "&".join(f"{k}={v}" for k, v in sorted_params)

        headers = self._build_headers("DELETE", endpoint, params_str)

        # 直接拼接 URL，避免 requests 库的额外编码导致签名不匹配
        if params_str:
            url = f"{BASE_URL}{endpoint}?{params_str}"
        else:
            url = f"{BASE_URL}{endpoint}"

        response = self.session.delete(url, headers=headers, timeout=30)
        return self._handle_response(response)

    # ============== 业务接口封装 ==============

    def get_account_balance(self, currency: Optional[str] = None) -> Dict[str, Any]:
        """获取账户资产"""
        params = {}
        if currency:
            params["currency"] = currency.upper()
        return self.get("/v1/asset/account", params if params else None)

    def get_stock_positions(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """获取股票持仓"""
        params = {}
        if symbols:
            params["symbol"] = ",".join(symbols)
        return self.get("/v1/asset/stock", params if params else None)

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LO",
        price: Optional[float] = None,
        time_in_force: str = "Day",
        remark: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        提交订单

        Args:
            symbol: 股票代码，如 AAPL.US, 700.HK
            side: 买卖方向，Buy 或 Sell
            quantity: 数量
            order_type: 订单类型，默认 LO（限价单）
            price: 价格（限价单必填）
            time_in_force: 有效期类型，Day/GTC/GTD
            remark: 备注
        """
        data = {
            "symbol": symbol,
            "side": side,
            "submitted_quantity": str(quantity),
            "order_type": order_type,
            "time_in_force": time_in_force,
        }

        if price is not None and price > 0:
            data["submitted_price"] = str(price)

        if remark:
            data["remark"] = remark[:255]  # 最多255字符

        return self.post("/v1/trade/order", data)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """撤销订单"""
        return self.delete("/v1/trade/order", {"order_id": order_id})

    def get_today_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[List[str]] = None,
        side: Optional[str] = None,
        market: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取今日订单"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        if status:
            params["status"] = ",".join(status)
        if side:
            params["side"] = side
        if market:
            params["market"] = market
        return self.get("/v1/trade/order/today", params if params else None)

    def get_order_detail(self, order_id: str) -> Dict[str, Any]:
        """获取订单详情"""
        return self.get("/v1/trade/order", {"order_id": order_id})

    def get_quote(self, symbols: List[str]) -> Dict[str, Any]:
        """
        获取实时行情

        注意：REST API 的行情接口可能有限制，建议使用 yfinance 作为备用
        """
        params = {"symbol": ",".join(symbols)}
        return self.get("/v1/quote", params)

    # ============== 订单管理接口 ==============

    def replace_order(
        self,
        order_id: str,
        quantity: int,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        remark: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        改单（修改订单）

        Args:
            order_id: 订单ID
            quantity: 新的数量
            price: 新的价格（限价单需要）
            trigger_price: 触发价格（条件单需要）
            remark: 备注（最多64字符）
        """
        data = {
            "order_id": order_id,
            "quantity": str(quantity),
        }
        if price is not None:
            data["price"] = str(price)
        if trigger_price is not None:
            data["trigger_price"] = str(trigger_price)
        if remark:
            data["remark"] = remark[:64]
        return self.put("/v1/trade/order", data)

    def get_history_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[List[str]] = None,
        side: Optional[str] = None,
        market: Optional[str] = None,
        start_at: Optional[int] = None,
        end_at: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        获取历史订单

        Args:
            symbol: 股票代码
            status: 订单状态列表
            side: Buy/Sell
            market: US/HK
            start_at: 开始时间戳（秒）
            end_at: 结束时间戳（秒）
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        if status:
            params["status"] = ",".join(status)
        if side:
            params["side"] = side
        if market:
            params["market"] = market
        if start_at:
            params["start_at"] = str(start_at)
        if end_at:
            params["end_at"] = str(end_at)
        return self.get("/v1/trade/order/history", params if params else None)

    # ============== 成交记录接口 ==============

    def get_today_executions(
        self,
        symbol: Optional[str] = None,
        order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取今日成交记录"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        if order_id:
            params["order_id"] = order_id
        return self.get("/v1/trade/execution/today", params if params else None)

    def get_history_executions(
        self,
        symbol: Optional[str] = None,
        start_at: Optional[int] = None,
        end_at: Optional[int] = None
    ) -> Dict[str, Any]:
        """获取历史成交记录"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        if start_at:
            params["start_at"] = str(start_at)
        if end_at:
            params["end_at"] = str(end_at)
        return self.get("/v1/trade/execution/history", params if params else None)

    # ============== 资产接口 ==============

    def get_fund_positions(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """获取基金持仓"""
        params = {}
        if symbols:
            params["symbol"] = ",".join(symbols)
        return self.get("/v1/asset/fund", params if params else None)

    def get_cash_flow(
        self,
        start_time: int,
        end_time: int,
        business_type: Optional[str] = None,
        symbol: Optional[str] = None,
        page: int = 1,
        size: int = 50
    ) -> Dict[str, Any]:
        """
        获取资金流水

        Args:
            start_time: 开始时间戳（秒）
            end_time: 结束时间戳（秒）
            business_type: 业务类型（1=现金, 2=股票, 3=基金）
            symbol: 股票代码
            page: 页码
            size: 每页数量（1-10000）
        """
        params = {
            "start_time": str(start_time),
            "end_time": str(end_time),
            "page": str(page),
            "size": str(size),
        }
        if business_type:
            params["business_type"] = business_type
        if symbol:
            params["symbol"] = symbol
        return self.get("/v1/asset/cashflow", params)

    def get_margin_ratio(self, symbol: str) -> Dict[str, Any]:
        """
        获取保证金比率

        Args:
            symbol: 股票代码
        """
        return self.get("/v1/risk/margin-ratio", {"symbol": symbol})

    # ============== 行情接口 ==============

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int = 100,
        adjust_type: int = 0
    ) -> Dict[str, Any]:
        """
        获取K线数据

        Args:
            symbol: 股票代码
            period: K线周期 (1m/5m/15m/30m/60m/1d/1w/1M/1y)
            count: 数量（最大1000）
            adjust_type: 复权类型 (0=不复权, 1=前复权, 2=后复权)
        """
        # 周期映射
        period_map = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
            "1d": 1000, "1w": 2000, "1M": 3000, "1y": 4000,
            # 也支持数字
        }
        period_val = period_map.get(period, int(period) if period.isdigit() else 1000)

        params = {
            "symbol": symbol,
            "period": str(period_val),
            "count": str(min(count, 1000)),
            "adjust_type": str(adjust_type),
        }
        return self.get("/v1/quote/candlestick", params)

    def get_depth(self, symbol: str) -> Dict[str, Any]:
        """
        获取盘口深度

        Args:
            symbol: 股票代码
        """
        return self.get("/v1/quote/depth", {"symbol": symbol})

    def get_brokers(self, symbol: str) -> Dict[str, Any]:
        """
        获取经纪商队列

        Args:
            symbol: 股票代码
        """
        return self.get("/v1/quote/brokers", {"symbol": symbol})

    def get_trades(self, symbol: str, count: int = 100) -> Dict[str, Any]:
        """
        获取成交明细

        Args:
            symbol: 股票代码
            count: 数量
        """
        return self.get("/v1/quote/trades", {"symbol": symbol, "count": str(count)})

    def get_intraday(self, symbol: str) -> Dict[str, Any]:
        """获取分时数据"""
        return self.get("/v1/quote/intraday", {"symbol": symbol})

    # ============== 标的信息接口 ==============

    def get_static_info(self, symbols: List[str]) -> Dict[str, Any]:
        """获取标的基本信息"""
        return self.get("/v1/quote/static_info", {"symbol": ",".join(symbols)})

    def get_warrant_quote(self, symbols: List[str]) -> Dict[str, Any]:
        """获取窝轮行情"""
        return self.get("/v1/quote/warrant", {"symbol": ",".join(symbols)})

    def get_option_quote(self, symbols: List[str]) -> Dict[str, Any]:
        """获取期权行情"""
        return self.get("/v1/quote/option", {"symbol": ",".join(symbols)})

    def get_option_chain_dates(self, symbol: str) -> Dict[str, Any]:
        """获取期权链到期日"""
        return self.get("/v1/quote/option/expiry", {"symbol": symbol})

    def get_option_chain(
        self,
        symbol: str,
        expiry_date: str,
        strike_price: Optional[float] = None,
        option_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取期权链

        Args:
            symbol: 标的代码
            expiry_date: 到期日 YYYY-MM-DD
            strike_price: 行权价
            option_type: Call/Put
        """
        params = {
            "symbol": symbol,
            "expiry_date": expiry_date,
        }
        if strike_price:
            params["strike_price"] = str(strike_price)
        if option_type:
            params["option_type"] = option_type
        return self.get("/v1/quote/option/chain", params)

    def get_warrant_issuers(self) -> Dict[str, Any]:
        """获取窝轮发行商列表"""
        return self.get("/v1/quote/warrant/issuer", None)

    def get_warrant_filter(
        self,
        symbol: str,
        filter_config: Optional[Dict] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None
    ) -> Dict[str, Any]:
        """筛选窝轮"""
        params = {"symbol": symbol}
        if filter_config:
            params.update(filter_config)
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order
        return self.get("/v1/quote/warrant/filter", params)

    # ============== 交易估算接口 ==============

    def estimate_buy_limit(
        self,
        symbol: str,
        order_type: str,
        side: str = "Buy",
        price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        估算最大可买数量

        Args:
            symbol: 股票代码
            order_type: 订单类型
            side: Buy/Sell
            price: 价格
        """
        params = {
            "symbol": symbol,
            "order_type": order_type,
            "side": side,
        }
        if price:
            params["price"] = str(price)
        return self.get("/v1/trade/estimate/buy_limit", params)



# ================= 全局客户端实例 =================
_current_mode: str = "paper"  # 当前账户模式: "paper" 或 "real"
_paper_client: Optional[LongportClient] = None
_real_client: Optional[LongportClient] = None
_paper_quote_ctx = None  # SDK QuoteContext for paper trading
_real_quote_ctx = None   # SDK QuoteContext for real trading


def _load_default_mode() -> str:
    """从环境变量或配置文件加载默认模式"""
    # 优先使用环境变量
    env_mode = os.getenv("LONGPORT_DEFAULT_MODE", "").lower()
    if env_mode in ["paper", "real"]:
        return env_mode

    # 其次从配置文件加载
    paths_to_try = [
        CONFIG_FILE,
        os.path.join(os.path.dirname(__file__), CONFIG_FILE),
        os.path.expanduser(f"~/{CONFIG_FILE}"),
    ]
    for path in paths_to_try:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    return config.get("default_mode", "paper")
            except Exception:
                pass
    return "paper"


def get_current_mode() -> str:
    """获取当前账户模式"""
    return _current_mode


def set_current_mode(mode: str) -> bool:
    """
    设置当前账户模式

    Args:
        mode: "paper" 或 "real"

    Returns:
        是否成功
    """
    global _current_mode
    if mode not in ["paper", "real"]:
        return False
    _current_mode = mode
    return True


def get_client() -> LongportClient:
    """获取当前模式的 REST API 客户端实例（懒加载）"""
    global _paper_client, _real_client, _current_mode

    if _current_mode == "paper":
        if _paper_client is None:
            _paper_client = LongportClient(mode="paper")
        return _paper_client
    else:
        if _real_client is None:
            _real_client = LongportClient(mode="real")
        return _real_client


def get_quote_context():
    """获取当前模式的 SDK QuoteContext 实例（懒加载）"""
    global _paper_quote_ctx, _real_quote_ctx, _current_mode

    if not HAS_SDK:
        return None

    # 根据当前模式选择对应的 context
    if _current_mode == "paper":
        if _paper_quote_ctx is None:
            try:
                client = get_client()
                logger.info(f"SDK初始化 (paper): app_key={client.app_key[:8] if client.app_key else 'None'}...")
                config = Config(
                    app_key=client.app_key,
                    app_secret=client.app_secret,
                    access_token=client.access_token
                )
                _paper_quote_ctx = QuoteContext(config)
                logger.info("SDK QuoteContext (paper) 初始化成功")
            except Exception as e:
                logger.error(f"SDK QuoteContext (paper) 初始化失败: {e}")
                return None
        return _paper_quote_ctx
    else:
        if _real_quote_ctx is None:
            try:
                client = get_client()
                logger.info(f"SDK初始化 (real): app_key={client.app_key[:8] if client.app_key else 'None'}...")
                config = Config(
                    app_key=client.app_key,
                    app_secret=client.app_secret,
                    access_token=client.access_token
                )
                _real_quote_ctx = QuoteContext(config)
                logger.info("SDK QuoteContext (real) 初始化成功")
            except Exception as e:
                logger.error(f"SDK QuoteContext (real) 初始化失败: {e}")
                return None
        return _real_quote_ctx


# 初始化时加载默认模式
_current_mode = _load_default_mode()
# =================================================


# ================= MCP Server =================
mcp = FastMCP("LongportTrader")

# 初始化后台调度器
scheduler = BackgroundScheduler(jobstores={'default': MemoryJobStore()})
scheduler.start()
# =============================================


def to_sdk_symbol(symbol: str) -> str:
    """
    转换股票代码为长桥 API 格式

    Examples:
        AAPL -> AAPL.US
        700.HK -> 700.HK
        0700.HK -> 700.HK
        BABA -> BABA.US
    """
    s = symbol.upper().strip()

    # 已经是标准格式
    if re.match(r'^[A-Z0-9]+\.(HK|US|SH|SZ|SG)$', s):
        # 处理港股前导零
        if s.endswith(".HK"):
            parts = s.split(".")
            return f"{int(parts[0])}.HK"
        return s

    # 港股格式转换
    if s.endswith(".HK"):
        return f"{int(s.replace('.HK', ''))}.HK"

    # 上证 .SS -> .SH
    if s.endswith(".SS"):
        return s.replace(".SS", ".SH")

    # 深证保持不变
    if s.endswith(".SZ"):
        return s

    # 纯字母视为美股
    if re.match(r'^[A-Z]+$', s):
        return f"{s}.US"

    # HK.xxxx 格式
    if s.startswith("HK."):
        return f"{int(s[3:])}.HK"

    return s


def to_yahoo_symbol(symbol: str) -> str:
    """
    转换长桥格式代码为 Yahoo Finance 格式

    Examples:
        AAPL.US -> AAPL
        700.HK -> 0700.HK
        600519.SH -> 600519.SS
        000001.SZ -> 000001.SZ
    """
    s = symbol.upper().strip()

    # 美股：去掉 .US 后缀
    if s.endswith(".US"):
        return s.replace(".US", "")

    # 港股：补齐4位数字
    if s.endswith(".HK"):
        parts = s.split(".")
        code = parts[0].zfill(4)  # 补齐前导零
        return f"{code}.HK"

    # 上证：.SH -> .SS
    if s.endswith(".SH"):
        return s.replace(".SH", ".SS")

    # 深证：保持不变
    if s.endswith(".SZ"):
        return s

    # 其他情况原样返回
    return s


# ---------------------------------------------------------
# 内部核心逻辑
# ---------------------------------------------------------

def _core_execute_trade(
    symbol: str,
    side: str,
    quantity: int,
    price: float = 0,
    remark: str = "Timer"
) -> Optional[str]:
    """
    核心交易执行函数

    Returns:
        成功返回订单ID，失败返回 None
    """
    log_prefix = f"[{remark}] {side} {symbol}"

    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        side_api = ORDER_SIDE_MAP.get(side.lower(), "Buy")

        if price > 0:
            order_type = "LO"
            desc = f"限价 {price}"
        else:
            order_type = "MO"
            price = None
            desc = "市价"

        resp = client.submit_order(
            symbol=target,
            side=side_api,
            quantity=quantity,
            order_type=order_type,
            price=price,
            time_in_force="Day",
            remark=remark
        )

        order_id = resp.get("data", {}).get("order_id")

        # 等待订单状态更新
        time.sleep(1.0)

        # 检查订单状态
        try:
            detail = client.get_order_detail(str(order_id))
            order_data = detail.get("data", {})
            status = order_data.get("status", "")

            if any(s in status for s in ["Rejected", "Failed"]):
                msg = f"[X] {log_prefix} 被拒绝! 状态: {status}"
                sys.stderr.write(msg + "\n")
                return None
        except Exception:
            pass

        msg = f"[OK] {log_prefix} 成功 (ID: {order_id}) @ {desc}"
        sys.stderr.write(msg + "\n")
        return str(order_id)

    except Exception as e:
        sys.stderr.write(f"[X] {log_prefix} 异常: {str(e)}\n")
        return None


def _core_cancel_all(remark: str = "Timer") -> int:
    """
    撤销所有订单

    Returns:
        成功撤销的订单数量
    """
    try:
        client = get_client()
        resp = client.get_today_orders()
        orders = resp.get("data", {}).get("orders", [])

        cancel_count = 0
        for order in orders:
            status = order.get("status", "")
            if not any(s in status for s in TERMINAL_STATUSES):
                try:
                    order_id = order.get("order_id")
                    client.cancel_order(str(order_id))
                    cancel_count += 1
                except Exception:
                    pass

        if cancel_count == 0:
            sys.stderr.write(f"[{remark}] 无可撤订单\n")
        else:
            sys.stderr.write(f"[OK] [{remark}] 已触发全撤，尝试撤销 {cancel_count} 个订单\n")

        return cancel_count

    except Exception as e:
        sys.stderr.write(f"[X] [{remark}] 全撤失败: {str(e)}\n")
        return 0


# ------------------------------------------
# 工具 1: 定时下单
# ------------------------------------------
@mcp.tool()
def schedule_trade(
    target_time: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float = 0
) -> str:
    """
    设置定时交易任务。

    【重要规则】
    1. 时间计算：必须根据当前时间计算出绝对时间字符串 "YYYY-MM-DD HH:MM:SS"。
    2. 参数提取：必须严格提取**本次指令**中的数值！
       - 严禁使用上一轮对话的价格或方向！
       - 如果用户只说"下单"，默认是"buy"(买入)。

    Args:
        target_time (str): 目标执行时间，格式 "YYYY-MM-DD HH:MM:SS"。
        symbol (str): 股票代码（如 AAPL, 700.HK）。
        side (str): 'buy' (买入) 或 'sell' (卖出)。默认 'buy'。
        quantity (int): 数量。
        price (float): 本次指令指定的价格。如果用户没说价格，填0（市价）。不要自己编造价格！

    Returns:
        设置结果描述
    """
    try:
        run_date = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()

        if run_date < now:
            return f"[X] 设置失败: 时间 {target_time} 是过去时间。当前: {now.strftime('%H:%M:%S')}"

        wait_seconds = (run_date - now).total_seconds()
        job_id = f"trade_{int(run_date.timestamp())}_{symbol}"

        scheduler.add_job(
            _core_execute_trade,
            'date',
            run_date=run_date,
            args=[symbol, side, quantity, price, f"Timer-{target_time}"],
            id=job_id
        )

        type_str = "限价" if price > 0 else "市价"
        side_cn = "买入" if side.lower() == "buy" else "卖出"

        return (
            f"[OK] **定时任务已锁定**\n"
            f"  执行时间: {target_time}\n"
            f"  倒计时: {int(wait_seconds)}秒\n"
            f"  内容: {side_cn} {symbol} {quantity}股 @ {type_str} {price if price > 0 else ''}\n"
            f"  ID: {job_id}"
        )

    except ValueError:
        return "[X] 时间格式错误！请使用 'YYYY-MM-DD HH:MM:SS'"
    except Exception as e:
        return f"[X] 定时设置失败: {str(e)}"


# ------------------------------------------
# 工具 2: 定时全撤
# ------------------------------------------
@mcp.tool()
def schedule_cancel_all(target_time: str) -> str:
    """
    设置定时【撤销所有订单】。

    Args:
        target_time (str): 目标执行时间 "YYYY-MM-DD HH:MM:SS"。

    Returns:
        设置结果描述
    """
    try:
        run_date = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()

        if run_date < now:
            return f"[X] 时间 {target_time} 已过。"

        job_id = f"cancel_all_{int(run_date.timestamp())}"

        scheduler.add_job(
            _core_cancel_all,
            'date',
            run_date=run_date,
            args=[f"TimerCancel-{target_time}"],
            id=job_id
        )

        return (
            f"[OK] **全撤定时已锁定**\n"
            f"  执行时间: {target_time}\n"
            f"  [!] 届时将撤销所有挂单！"
        )

    except ValueError:
        return "[X] 时间格式错误"
    except Exception as e:
        return f"[X] 定时设置失败: {str(e)}"


# ------------------------------------------
# 工具 3: 查看定时任务列表
# ------------------------------------------
@mcp.tool()
def list_schedules() -> str:
    """查看待执行的定时任务。"""
    jobs = scheduler.get_jobs()

    if not jobs:
        return "[i] 当前没有定时任务。"

    text = ["**待执行任务列表**"]
    for job in jobs:
        run_time = job.next_run_time.strftime("%H:%M:%S") if job.next_run_time else "N/A"

        if "trade" in job.id:
            args = job.args
            # args: [symbol, side, quantity, price, remark]
            price_info = f"@{args[3]}" if args[3] > 0 else "市价"
            info = f"{args[1]} {args[0]} {args[2]}股 {price_info}"
        elif "cancel" in job.id:
            info = "[!] 全撤"
        else:
            info = "未知任务"

        text.append(f"  [{run_time}] {info}")

    return "\n".join(text)


# ------------------------------------------
# 工具 4: 取消定时任务
# ------------------------------------------
@mcp.tool()
def cancel_schedule(task_id: str = "all") -> str:
    """
    取消定时任务。

    Args:
        task_id (str): 任务ID，或 'all' 取消所有。

    Returns:
        操作结果
    """
    try:
        if task_id == "all":
            scheduler.remove_all_jobs()
            return "[OK] 所有定时任务已取消。"
        else:
            scheduler.remove_job(task_id)
            return f"[OK] 任务 {task_id} 已取消。"
    except Exception:
        return f"[X] 操作失败，未找到ID: {task_id}"


# ------------------------------------------
# 工具 5: 立即下单
# ------------------------------------------
@mcp.tool()
def place_order(
    symbol: str,
    side: str,
    quantity: int,
    price: float = 0
) -> str:
    """
    立即执行交易。

    Args:
        symbol (str): 股票代码（如 AAPL.US, 700.HK）
        side (str): 'buy' 或 'sell'
        quantity (int): 数量
        price (float): 价格，0表示市价单

    Returns:
        交易结果
    """
    mode = get_current_mode()
    mode_label = "[模拟盘]" if mode == "paper" else "[实盘]"

    result = _core_execute_trade(symbol, side, quantity, price, "Manual")
    if result:
        return f"{mode_label} [OK] 订单已提交，ID: {result}"
    return f"{mode_label} [X] 下单失败，请检查日志"


# ------------------------------------------
# 工具 6: 获取汇率（使用 yfinance）
# ------------------------------------------
@mcp.tool()
def get_exchange_rate(from_currency: str = "USD", to_currency: str = "CNY") -> str:
    """
    获取实时汇率。使用 Yahoo Finance 数据源。

    常用货币代码：
    - USD: 美元
    - CNY: 人民币
    - HKD: 港币
    - JPY: 日元
    - EUR: 欧元
    - GBP: 英镑
    - SGD: 新加坡元
    - AUD: 澳元
    - CAD: 加元

    用法示例：
    - 美元换人民币: from_currency="USD", to_currency="CNY"
    - 日元换港币: from_currency="JPY", to_currency="HKD"
    - 欧元换美元: from_currency="EUR", to_currency="USD"

    Args:
        from_currency: 源货币代码（3位大写，如 USD, CNY, HKD）
        to_currency: 目标货币代码（3位大写，如 USD, CNY, HKD）

    Returns:
        汇率信息，包含正向和反向汇率
    """
    if not HAS_YFINANCE:
        return "[X] yfinance 未安装，无法获取汇率。安装: pip install yfinance"

    try:
        from_curr = from_currency.upper().strip()
        to_curr = to_currency.upper().strip()

        # 获取汇率
        symbol = f"{from_curr}{to_curr}=X"
        ticker = yf.Ticker(symbol)

        if hasattr(ticker, 'fast_info'):
            price = ticker.fast_info.last_price
        else:
            price = ticker.info.get('regularMarketPrice')

        if not price:
            return f"[X] 无法获取 {from_curr}/{to_curr} 汇率"

        # 计算反向汇率
        reverse_rate = 1 / price if price > 0 else 0

        return (
            f"**{from_curr}/{to_curr} 汇率**\n"
            f"  1 {from_curr} = {price:.4f} {to_curr}\n"
            f"  1 {to_curr} = {reverse_rate:.4f} {from_curr}"
        )

    except Exception as e:
        return f"[X] 汇率查询错误: {e}"


# ------------------------------------------
# 工具 7: 搜索股票代码（使用 Yahoo Finance）
# ------------------------------------------
# 常用股票别名映射
STOCK_ALIAS = {
    # 中文名 -> 英文关键词
    "苹果": "Apple", "苹果公司": "Apple",
    "特斯拉": "Tesla", "马斯克": "Tesla",
    "谷歌": "Google", "谷歌公司": "Google", "alphabet": "Google",
    "微软": "Microsoft", "亚马逊": "Amazon",
    "脸书": "Meta", "facebook": "Meta", "元宇宙": "Meta",
    "英伟达": "NVIDIA", "英伟达公司": "NVIDIA", "老黄": "NVIDIA",
    "阿里": "Alibaba", "阿里巴巴": "Alibaba",
    "腾讯": "Tencent", "腾讯控股": "Tencent",
    "百度": "Baidu", "京东": "JD",
    "拼多多": "Pinduoduo", "pdd": "Pinduoduo",
    "网易": "NetEase", "bilibili": "Bilibili", "b站": "Bilibili",
    "小米": "Xiaomi", "美团": "Meituan",
    "字节跳动": "ByteDance", "抖音": "ByteDance",
    "蔚来": "NIO", "理想": "Li Auto", "小鹏": "XPeng",
    "台积电": "TSMC", "三星": "Samsung",
    "索尼": "Sony", "任天堂": "Nintendo",
    "可口可乐": "Coca-Cola", "百事": "Pepsi",
    "麦当劳": "McDonald", "星巴克": "Starbucks",
    "奈飞": "Netflix", "迪士尼": "Disney",
    "波音": "Boeing", "空客": "Airbus",
    "辉瑞": "Pfizer", "强生": "Johnson",
}

@mcp.tool()
def search_symbol(query: str) -> str:
    """
    搜索股票代码。使用 Yahoo Finance 数据源。

    用法：传入公司英文名称或股票代码，返回长桥格式代码。

    常用股票示例：
    - 苹果公司: query="Apple" -> AAPL.US
    - 特斯拉: query="Tesla" -> TSLA.US
    - 腾讯控股: query="Tencent" -> 700.HK
    - 阿里巴巴: query="Alibaba" -> 9988.HK / BABA.US
    - 英伟达: query="NVIDIA" -> NVDA.US

    Args:
        query: 公司名称（英文优先）或股票代码。
               中文公司名会自动转换为英文关键词。
               如：特斯拉->Tesla, 腾讯->Tencent, 苹果->Apple

    Returns:
        搜索结果列表，包含长桥格式代码（如 AAPL.US, 700.HK）
    """
    try:
        # 清理查询字符串
        query = query.strip()

        # 移除常见的提问前缀
        prefixes = ["帮我查", "查一下", "搜索", "查询", "找一下", "股票代码", "代码是什么", "的代码"]
        for prefix in prefixes:
            query = query.replace(prefix, "")
        query = query.strip()

        # 检查别名映射
        query_lower = query.lower()
        for alias, keyword in STOCK_ALIAS.items():
            if alias in query_lower or query_lower in alias:
                query = keyword
                break

        url = "https://query2.finance.yahoo.com/v1/finance/search"
        params = {
            "q": query,
            "lang": "zh-CN",
            "region": "HK",
            "quotesCount": 8
        }
        resp = requests.get(
            url,
            params=params,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5
        )
        data = resp.json()

        if "quotes" not in data or not data["quotes"]:
            return f"[X] 未找到: {query}"

        results = [f"**搜索: {query}**"]
        for item in data["quotes"]:
            yahoo_sym = item.get("symbol", "")
            longport_sym = to_sdk_symbol(yahoo_sym)
            name = item.get("shortname") or item.get("longname", "")
            exchange = item.get("exchange", "")
            quote_type = item.get("quoteType", "")

            # 过滤掉非股票类型（如指数、基金等）
            if quote_type not in ["EQUITY", "ETF"]:
                continue

            # 格式化市场信息
            market = ""
            if ".HK" in longport_sym:
                market = "港股"
            elif ".US" in longport_sym:
                market = "美股"
            elif ".SH" in longport_sym or ".SZ" in longport_sym:
                market = "A股"

            results.append(f"  [{longport_sym}] {name} ({market})")

        if len(results) == 1:
            return f"[X] 未找到: {query}"

        results.append(f"\n提示: 使用长桥代码（如 {results[1].split(']')[0][3:]}）进行交易")
        return "\n".join(results)

    except Exception as e:
        return f"[X] 搜索异常: {e}"


# ------------------------------------------
# 工具 8: 获取实时行情
# ------------------------------------------
@mcp.tool()
def get_quote(symbols: list[str]) -> str:
    """
    获取股票/加密货币实时行情。查股价、看行情、查走势用这个工具。

    支持的格式：
    - 美股: AAPL.US, TSLA.US, NVDA.US
    - 港股: 700.HK, 9988.HK
    - 加密货币: BTC-USD, ETH-USD, DOGE-USD (注意用横杠连接，不是BTCUSD)

    常用代码：
    - 苹果: AAPL.US | 特斯拉: TSLA.US | 英伟达: NVDA.US
    - 腾讯: 700.HK | 阿里: 9988.HK
    - 比特币: BTC-USD | 以太坊: ETH-USD | 狗狗币: DOGE-USD

    Args:
        symbols: 代码列表，如 ["AAPL.US", "BTC-USD"]

    Returns:
        行情信息（价格、涨跌幅）
    """
    # 首先尝试长桥 API
    try:
        client = get_client()
        sdk_symbols = [to_sdk_symbol(s) for s in symbols]
        resp = client.get_quote(sdk_symbols)

        quotes = resp.get("data", {}).get("list", [])
        if quotes:
            results = []
            for q in quotes:
                sym = q.get("symbol", "")
                last = q.get("last_done", q.get("last_price", "N/A"))
                change = q.get("change_val", 0)
                change_rate = q.get("change_rate", 0)
                results.append(
                    f"[i] {sym}\n"
                    f"    价格: {last}\n"
                    f"    涨跌: {change} ({change_rate}%)"
                )
            return "\n".join(results)
    except Exception:
        pass  # 长桥行情接口可能有限制，fallback 到 yfinance

    # Fallback 到 yfinance
    if not HAS_YFINANCE:
        return "[X] 行情接口不可用"

    try:
        results = []
        for symbol in symbols:
            # 转换为 yfinance 格式
            yf_symbol = symbol.replace(".US", "").replace(".SH", ".SS")
            if yf_symbol.endswith(".HK"):
                # 港股需要补零到4位
                parts = yf_symbol.split(".")
                yf_symbol = f"{parts[0].zfill(4)}.HK"

            ticker = yf.Ticker(yf_symbol)
            info = ticker.fast_info if hasattr(ticker, 'fast_info') else ticker.info

            last_price = getattr(info, 'last_price', None) or info.get('regularMarketPrice', 'N/A')
            prev_close = getattr(info, 'previous_close', None) or info.get('previousClose', 0)

            if last_price != 'N/A' and prev_close:
                change = float(last_price) - float(prev_close)
                change_pct = (change / float(prev_close)) * 100
                results.append(
                    f"[i] {symbol}\n"
                    f"    价格: {last_price}\n"
                    f"    涨跌: {change:.2f} ({change_pct:.2f}%)"
                )
            else:
                results.append(f"[i] {symbol}: 价格 {last_price}")

        return "\n".join(results) if results else "[X] 无数据"

    except Exception as e:
        return f"[X] 行情错误: {e}"


# ------------------------------------------
# 工具 9: 获取持仓
# ------------------------------------------
@mcp.tool()
def get_positions() -> str:
    """
    获取当前持仓。

    Returns:
        持仓信息
    """
    try:
        client = get_client()
        resp = client.get_stock_positions()

        # API 返回结构: data.list[0].stock_info 包含持仓列表
        data_list = resp.get("data", {}).get("list", [])

        if not data_list:
            return "[i] 无持仓"

        results = ["**持仓列表**"]

        for account in data_list:
            stock_info = account.get("stock_info", [])
            account_channel = account.get("account_channel", "")

            for p in stock_info:
                quantity = int(p.get("quantity", 0))
                if quantity == 0:
                    continue

                symbol = p.get("symbol", "")
                symbol_name = p.get("symbol_name", "")
                cost_price = p.get("cost_price", "N/A")
                available = p.get("available_quantity", quantity)
                market = p.get("market", "")
                currency = p.get("currency", "")

                results.append(
                    f"  {symbol} ({symbol_name}): {quantity}股 (可用: {available}, 成本: {cost_price} {currency})"
                )

        return "\n".join(results) if len(results) > 1 else "[i] 无持仓"

    except Exception as e:
        return f"[X] 持仓查询错误: {e}"


# ------------------------------------------
# 工具 10: 获取资产
# ------------------------------------------
@mcp.tool()
def get_assets(currency: str = "") -> str:
    """
    获取账户资产。

    Args:
        currency: 可选，指定货币（HKD, USD, CNH）

    Returns:
        资产信息
    """
    try:
        client = get_client()
        resp = client.get_account_balance(currency if currency else None)

        # API 返回结构: data.list[0] 包含账户信息
        data_list = resp.get("data", {}).get("list", [])
        if not data_list:
            return "[X] 无账户数据"

        data = data_list[0]

        results = ["**账户资产**"]

        # 总资产信息
        total_cash = data.get("total_cash", "N/A")
        net_assets = data.get("net_assets", "N/A")
        buy_power = data.get("buy_power", "N/A")

        results.append(f"  总现金: {total_cash}")
        results.append(f"  净资产: {net_assets}")
        results.append(f"  购买力: {buy_power}")

        # 各币种现金
        cash_info = data.get("cash_infos", [])
        if cash_info:
            results.append("  各币种现金:")
            for c in cash_info:
                curr = c.get("currency", "")
                available = c.get("available_cash", c.get("cash", "N/A"))
                withdraw = c.get("withdraw_cash", "N/A")
                frozen = c.get("frozen_cash", "0")
                results.append(f"    {curr}: 可用 {available}, 可取 {withdraw}, 冻结 {frozen}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 资产查询错误: {e}"


# ------------------------------------------
# 工具 11: 获取今日订单
# ------------------------------------------
@mcp.tool()
def get_open_orders() -> str:
    """
    获取今日未完成订单（挂单）。

    Returns:
        订单列表
    """
    try:
        client = get_client()
        resp = client.get_today_orders()

        orders = resp.get("data", {}).get("orders", [])

        # 过滤活跃订单
        active_orders = []
        for o in orders:
            status = o.get("status", "")
            if not any(s in status for s in TERMINAL_STATUSES):
                active_orders.append(o)

        if not active_orders:
            return "[i] 无挂单"

        results = ["**今日挂单**"]
        for o in active_orders:
            order_id = o.get("order_id", "")
            symbol = o.get("symbol", "")
            side = o.get("side", "")
            qty = o.get("quantity", o.get("submitted_quantity", ""))
            price = o.get("price", o.get("submitted_price", ""))
            status = o.get("status", "")

            results.append(
                f"  [{order_id}] {side} {symbol} {qty}股 @{price} ({status})"
            )

        return "\n".join(results)

    except Exception as e:
        return f"[X] 订单查询失败: {e}"


# ------------------------------------------
# 工具 12: 撤销单个订单
# ------------------------------------------
@mcp.tool()
def cancel_order(order_id: str) -> str:
    """
    撤销指定订单。

    Args:
        order_id: 订单ID

    Returns:
        操作结果
    """
    try:
        client = get_client()
        client.cancel_order(order_id)
        return f"[OK] 已提交撤单请求: {order_id}"
    except Exception as e:
        return f"[X] 撤单失败: {e}"


# ------------------------------------------
# 工具 13: 撤销所有订单
# ------------------------------------------
@mcp.tool()
def cancel_all_orders() -> str:
    """
    撤销所有未完成订单。

    Returns:
        操作结果
    """
    count = _core_cancel_all("Manual")
    if count > 0:
        return f"[OK] 已触发全撤，尝试撤销 {count} 个订单"
    return "[i] 无可撤订单"


# ------------------------------------------
# 工具 14: 获取股票基本面信息（使用 yfinance）
# ------------------------------------------
@mcp.tool()
def get_symbol_fundamentals(symbol: str) -> str:
    """
    获取股票基本面信息。

    Args:
        symbol: 股票代码

    Returns:
        基本面信息
    """
    if not HAS_YFINANCE:
        return "[X] yfinance 未安装"

    try:
        # 转换为 yfinance 格式
        yf_symbol = symbol.replace(".US", "").replace(".SH", ".SS")
        if yf_symbol.endswith(".HK"):
            parts = yf_symbol.split(".")
            yf_symbol = f"{parts[0].zfill(4)}.HK"

        info = yf.Ticker(yf_symbol).info

        return (
            f"[i] {info.get('shortName', symbol)}\n"
            f"    市值: {info.get('marketCap', 'N/A')}\n"
            f"    PE: {info.get('trailingPE', 'N/A')}\n"
            f"    52周高: {info.get('fiftyTwoWeekHigh', 'N/A')}\n"
            f"    52周低: {info.get('fiftyTwoWeekLow', 'N/A')}"
        )

    except Exception as e:
        return f"[X] 查询失败: {e}"


# ------------------------------------------
# 工具 15: 通用 API 调用工具
# ------------------------------------------
@mcp.tool()
def call_longport_api(
    method: str,
    endpoint: str,
    params: Optional[str] = None,
    body: Optional[str] = None
) -> str:
    """
    通用长桥 API 调用工具。

    允许 LLM 通过阅读文档动态调用未封装的接口。

    Args:
        method: HTTP 方法 (GET, POST, PUT, DELETE)
        endpoint: API 端点路径（如 /v1/asset/account）
        params: 查询参数，JSON 字符串格式（用于 GET/DELETE）
        body: 请求体，JSON 字符串格式（用于 POST/PUT）

    Returns:
        API 响应（JSON 格式）

    Examples:
        - 查询账户: call_longport_api("GET", "/v1/asset/account")
        - 查询持仓: call_longport_api("GET", "/v1/asset/stock", '{"symbol": "AAPL.US"}')
        - 下单: call_longport_api("POST", "/v1/trade/order", body='{"symbol":"AAPL.US",...}')
    """
    try:
        client = get_client()
        method = method.upper()

        # 解析参数
        params_dict = None
        body_dict = None

        if params:
            try:
                params_dict = json.loads(params)
            except json.JSONDecodeError:
                return f"[X] params 不是有效的 JSON: {params}"

        if body:
            try:
                body_dict = json.loads(body)
            except json.JSONDecodeError:
                return f"[X] body 不是有效的 JSON: {body}"

        # 执行请求
        if method == "GET":
            resp = client.get(endpoint, params_dict)
        elif method == "POST":
            resp = client.post(endpoint, body_dict)
        elif method == "PUT":
            resp = client.put(endpoint, body_dict)
        elif method == "DELETE":
            resp = client.delete(endpoint, params_dict)
        else:
            return f"[X] 不支持的 HTTP 方法: {method}"

        # 格式化返回
        return json.dumps(resp, ensure_ascii=False, indent=2)

    except Exception as e:
        return f"[X] API 调用失败: {e}"


# ------------------------------------------
# 工具 16: 改单
# ------------------------------------------
@mcp.tool()
def modify_order(
    order_id: str,
    quantity: int,
    price: float = 0
) -> str:
    """
    修改订单（改单）。

    Args:
        order_id: 要修改的订单ID
        quantity: 新的数量
        price: 新的价格（限价单需要，0表示不修改价格）

    Returns:
        操作结果
    """
    try:
        client = get_client()
        price_val = price if price > 0 else None
        client.replace_order(order_id, quantity, price_val)
        return f"[OK] 改单请求已提交: {order_id} -> 数量:{quantity}" + (f", 价格:{price}" if price > 0 else "")
    except Exception as e:
        return f"[X] 改单失败: {e}"


# ------------------------------------------
# 工具 17: 获取历史订单
# ------------------------------------------
@mcp.tool()
def get_history_orders(
    days: int = 30,
    symbol: str = "",
    market: str = ""
) -> str:
    """
    获取历史订单。

    Args:
        days: 查询天数（默认30天，最多90天）
        symbol: 可选，筛选特定股票
        market: 可选，筛选市场（US/HK）

    Returns:
        历史订单列表
    """
    try:
        client = get_client()
        end_at = int(time.time())
        start_at = end_at - (min(days, 90) * 24 * 3600)

        resp = client.get_history_orders(
            symbol=symbol if symbol else None,
            market=market if market else None,
            start_at=start_at,
            end_at=end_at
        )

        orders = resp.get("data", {}).get("orders", [])

        if not orders:
            return "[i] 无历史订单"

        results = [f"**最近{days}天历史订单**"]
        for o in orders[:20]:  # 限制显示前20条
            order_id = o.get("order_id", "")
            symbol = o.get("symbol", "")
            side = o.get("side", "")
            qty = o.get("submitted_quantity", "")
            price = o.get("submitted_price", "")
            status = o.get("status", "")
            results.append(f"  [{order_id}] {side} {symbol} {qty}股 @{price} ({status})")

        has_more = resp.get("data", {}).get("has_more", False)
        if has_more:
            results.append("  ... (更多订单请缩小查询范围)")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 历史订单查询失败: {e}"


# ------------------------------------------
# 工具 18: 获取今日成交
# ------------------------------------------
@mcp.tool()
def get_today_executions(symbol: str = "") -> str:
    """
    获取今日成交记录。

    Args:
        symbol: 可选，筛选特定股票

    Returns:
        今日成交列表
    """
    try:
        client = get_client()
        resp = client.get_today_executions(symbol if symbol else None)

        trades = resp.get("data", {}).get("trades", [])

        if not trades:
            return "[i] 今日无成交"

        results = ["**今日成交**"]
        for t in trades:
            trade_id = t.get("trade_id", "")
            sym = t.get("symbol", "")
            qty = t.get("quantity", "")
            price = t.get("price", "")
            trade_time = t.get("trade_done_at", "")
            results.append(f"  [{trade_id}] {sym} {qty}股 @{price} ({trade_time})")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 成交查询失败: {e}"


# ------------------------------------------
# 工具 19: 获取历史成交
# ------------------------------------------
@mcp.tool()
def get_history_executions(days: int = 30, symbol: str = "") -> str:
    """
    获取历史成交记录。

    Args:
        days: 查询天数
        symbol: 可选，筛选特定股票

    Returns:
        历史成交列表
    """
    try:
        client = get_client()
        end_at = int(time.time())
        start_at = end_at - (days * 24 * 3600)

        resp = client.get_history_executions(
            symbol=symbol if symbol else None,
            start_at=start_at,
            end_at=end_at
        )

        trades = resp.get("data", {}).get("trades", [])

        if not trades:
            return "[i] 无历史成交"

        results = [f"**最近{days}天成交**"]
        for t in trades[:20]:
            trade_id = t.get("trade_id", "")
            sym = t.get("symbol", "")
            qty = t.get("quantity", "")
            price = t.get("price", "")
            results.append(f"  [{trade_id}] {sym} {qty}股 @{price}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 历史成交查询失败: {e}"


# ------------------------------------------
# 工具 20: 获取基金持仓
# ------------------------------------------
@mcp.tool()
def get_fund_positions() -> str:
    """
    获取基金持仓。

    Returns:
        基金持仓信息
    """
    try:
        client = get_client()
        resp = client.get_fund_positions()

        funds = resp.get("data", {}).get("list", [])

        if not funds:
            return "[i] 无基金持仓"

        results = ["**基金持仓**"]
        for f in funds:
            symbol = f.get("symbol", "")
            name = f.get("symbol_name", "")
            units = f.get("holding_units", "")
            nav = f.get("current_net_asset_value", "")
            currency = f.get("currency", "")
            results.append(f"  {name} ({symbol}): {units}份 净值:{nav} {currency}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 基金持仓查询错误: {e}"


# ------------------------------------------
# 工具 21: 获取资金流水
# ------------------------------------------
@mcp.tool()
def get_cash_flow(days: int = 30) -> str:
    """
    获取资金流水记录。

    Args:
        days: 查询天数（默认30天）

    Returns:
        资金流水列表
    """
    try:
        client = get_client()
        end_time = int(time.time())
        start_time = end_time - (days * 24 * 3600)

        resp = client.get_cash_flow(start_time, end_time)

        flows = resp.get("data", {}).get("list", [])

        if not flows:
            return "[i] 无资金流水"

        results = [f"**最近{days}天资金流水**"]
        for f in flows[:20]:
            name = f.get("name", "")
            direction = "流入" if f.get("direction") == 2 else "流出"
            amount = f.get("balance", "")
            currency = f.get("currency", "")
            symbol = f.get("symbol", "")
            results.append(f"  {name}: {direction} {amount} {currency}" + (f" ({symbol})" if symbol else ""))

        return "\n".join(results)

    except Exception as e:
        return f"[X] 资金流水查询错误: {e}"


# ------------------------------------------
# 工具 22: 获取保证金比率
# ------------------------------------------
@mcp.tool()
def get_margin_ratio(symbol: str) -> str:
    """
    获取股票保证金比率。

    Args:
        symbol: 股票代码

    Returns:
        保证金比率信息
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_margin_ratio(target)

        data = resp.get("data", {})

        im = data.get("im_factor", "N/A")
        mm = data.get("mm_factor", "N/A")
        fm = data.get("fm_factor", "N/A")

        return (
            f"**{target} 保证金比率**\n"
            f"  初始保证金: {im}\n"
            f"  维持保证金: {mm}\n"
            f"  强平保证金: {fm}"
        )

    except Exception as e:
        return f"[X] 保证金查询错误: {e}"


# ------------------------------------------
# 工具 23: 获取K线数据（SDK优先，yfinance备用）
# ------------------------------------------
@mcp.tool()
def get_candlesticks(
    symbol: str,
    period: str = "1d",
    count: int = 30
) -> str:
    """
    获取K线数据。优先使用 SDK，失败时使用 yfinance 备用。

    Args:
        symbol: 股票代码（如 AAPL.US, 700.HK, ORCL.US）
        period: K线周期 (1d=日线, 1w=周线, 1M=月线)
        count: K线数量（默认30根）

    Returns:
        K线数据（开高低收+成交量）
    """
    target = to_sdk_symbol(symbol)

    # 优先尝试 SDK
    if HAS_SDK:
        try:
            ctx = get_quote_context()
            if ctx is not None:
                # SDK Period 映射
                period_map = {
                    "1m": Period.Min_1, "5m": Period.Min_5, "15m": Period.Min_15,
                    "30m": Period.Min_30, "60m": Period.Min_60,
                    "1d": Period.Day, "day": Period.Day,
                    "1w": Period.Week, "week": Period.Week,
                    "1M": Period.Month, "month": Period.Month,
                    "1y": Period.Year, "year": Period.Year,
                }
                sdk_period = period_map.get(period.lower(), Period.Day)

                candles = ctx.candlesticks(target, sdk_period, count, AdjustType.ForwardAdjust)

                if candles:
                    results = [f"**{target} {period} K线 (SDK, 最近{len(candles)}根)**"]
                    for c in candles[-count:]:
                        date_str = c.timestamp.strftime("%Y-%m-%d")
                        o = f"{c.open:.2f}"
                        h = f"{c.high:.2f}"
                        l = f"{c.low:.2f}"
                        close = f"{c.close:.2f}"
                        v = f"{int(c.volume):,}"
                        results.append(f"  {date_str}: O:{o} H:{h} L:{l} C:{close} V:{v}")
                    return "\n".join(results)
        except Exception as e:
            logger.warning(f"SDK K线获取失败，切换到 yfinance: {e}")

    # yfinance 备用
    if not HAS_YFINANCE:
        return "[X] 无法获取K线: SDK不可用且yfinance未安装"

    try:
        yahoo_symbol = to_yahoo_symbol(target)

        # yfinance 周期映射
        interval_map = {
            "1d": "1d", "day": "1d", "daily": "1d",
            "1w": "1wk", "1wk": "1wk", "week": "1wk", "weekly": "1wk",
            "1m": "1mo", "1mo": "1mo", "1M": "1mo", "month": "1mo", "monthly": "1mo",
        }
        interval = interval_map.get(period.lower(), "1d")

        # 根据周期决定获取多长时间的数据
        if interval == "1d":
            period_str = f"{min(count, 60)}d"
        elif interval == "1wk":
            period_str = f"{min(count * 7, 365)}d"
        else:
            period_str = f"{min(count * 30, 730)}d"

        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period_str, interval=interval)

        if df.empty:
            return f"[X] 无K线数据: {symbol}"

        df = df.tail(count)
        results = [f"**{target} {period} K线 (yfinance, 最近{len(df)}根)**"]

        for idx, row in df.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            o = f"{row['Open']:.2f}"
            h = f"{row['High']:.2f}"
            l = f"{row['Low']:.2f}"
            c = f"{row['Close']:.2f}"
            v = f"{int(row['Volume']):,}"
            results.append(f"  {date_str}: O:{o} H:{h} L:{l} C:{c} V:{v}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] K线查询错误: {e}"


# ------------------------------------------
# 工具 24: 技术分析
# ------------------------------------------
@mcp.tool()
def get_technical_analysis(symbol: str, period: str = "1d", count: int = 60) -> str:
    """
    获取股票技术分析指标，包括均线、RSI、MACD、布林带等量化常用指标。

    Args:
        symbol: 股票代码（如 AAPL.US, 700.HK, NVDA.US）
        period: K线周期 (1m/5m/15m/30m/1h/4h/1d/1w/1M)
        count: K线数量（默认60，建议>=30以保证指标准确）

    Returns:
        技术分析结果，包含趋势判断和买卖信号
    """
    import statistics

    def calc_sma(prices: list, period: int) -> float:
        """计算简单移动平均"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def calc_ema(prices: list, period: int) -> float:
        """计算指数移动平均"""
        if len(prices) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def calc_rsi(prices: list, period: int = 14) -> float:
        """计算RSI"""
        if len(prices) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            gains.append(diff if diff > 0 else 0)
            losses.append(-diff if diff < 0 else 0)
        if len(gains) < period:
            return None
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calc_macd(prices: list) -> tuple:
        """计算MACD (12, 26, 9)"""
        if len(prices) < 26:
            return None, None, None
        ema12 = calc_ema(prices, 12)
        ema26 = calc_ema(prices, 26)
        if ema12 is None or ema26 is None:
            return None, None, None
        dif = ema12 - ema26
        # 简化：用最近9个DIF的平均作为DEA
        dea = dif * 0.8  # 近似
        macd = (dif - dea) * 2
        return dif, dea, macd

    def calc_bollinger(prices: list, period: int = 20) -> tuple:
        """计算布林带"""
        if len(prices) < period:
            return None, None, None
        sma = calc_sma(prices, period)
        std = statistics.stdev(prices[-period:])
        upper = sma + 2 * std
        lower = sma - 2 * std
        return upper, sma, lower

    def calc_kdj(highs: list, lows: list, closes: list, period: int = 9) -> tuple:
        """计算KDJ"""
        if len(closes) < period:
            return None, None, None
        lowest = min(lows[-period:])
        highest = max(highs[-period:])
        if highest == lowest:
            return 50, 50, 50
        rsv = (closes[-1] - lowest) / (highest - lowest) * 100
        k = rsv * 2/3 + 50 * 1/3  # 简化计算
        d = k * 2/3 + 50 * 1/3
        j = 3 * k - 2 * d
        return k, d, j

    def calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
        """计算ATR（平均真实波幅）"""
        if len(closes) < period + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)
        if len(trs) < period:
            return None
        return sum(trs[-period:]) / period

    try:
        # 获取K线数据
        closes, highs, lows, volumes = [], [], [], []

        # 尝试SDK
        if HAS_SDK:
            ctx = get_quote_context()
            if ctx:
                try:
                    target = to_sdk_symbol(symbol)
                    period_map = {
                        "1m": Period.Min_1, "5m": Period.Min_5, "15m": Period.Min_15,
                        "30m": Period.Min_30, "1h": Period.Min_60, "4h": Period.Min_60,
                        "1d": Period.Day, "1w": Period.Week, "1M": Period.Month
                    }
                    sdk_period = period_map.get(period, Period.Day)
                    candles = ctx.candlesticks(target, sdk_period, count, AdjustType.ForwardAdjust)
                    for c in candles:
                        closes.append(float(c.close))
                        highs.append(float(c.high))
                        lows.append(float(c.low))
                        volumes.append(int(c.volume))
                except:
                    pass

        # yfinance 备用
        if not closes and HAS_YFINANCE:
            yahoo_sym = to_yahoo_symbol(symbol)
            yf_period_map = {"1d": "3mo", "1w": "1y", "1M": "2y", "1h": "5d", "5m": "5d"}
            yf_interval_map = {"1d": "1d", "1w": "1wk", "1M": "1mo", "1h": "1h", "5m": "5m"}
            yf_period = yf_period_map.get(period, "3mo")
            yf_interval = yf_interval_map.get(period, "1d")
            ticker = yfinance.Ticker(yahoo_sym)
            df = ticker.history(period=yf_period, interval=yf_interval)
            if not df.empty:
                df = df.tail(count)
                closes = df['Close'].tolist()
                highs = df['High'].tolist()
                lows = df['Low'].tolist()
                volumes = df['Volume'].tolist()

        if not closes or len(closes) < 14:
            return "[X] 数据不足，无法计算技术指标（需要至少14根K线）"

        # 计算指标
        current_price = closes[-1]
        ma5 = calc_sma(closes, 5)
        ma10 = calc_sma(closes, 10)
        ma20 = calc_sma(closes, 20)
        ma60 = calc_sma(closes, 60) if len(closes) >= 60 else None

        ema12 = calc_ema(closes, 12)
        ema26 = calc_ema(closes, 26)

        rsi = calc_rsi(closes, 14)
        rsi6 = calc_rsi(closes, 6)

        dif, dea, macd = calc_macd(closes)

        bb_upper, bb_mid, bb_lower = calc_bollinger(closes, 20)

        k, d, j = calc_kdj(highs, lows, closes, 9)

        atr = calc_atr(highs, lows, closes, 14)

        # 成交量分析
        vol_ma5 = calc_sma(volumes, 5) if len(volumes) >= 5 else None
        vol_ratio = volumes[-1] / vol_ma5 if vol_ma5 and vol_ma5 > 0 else None

        # 涨跌幅
        change_1d = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0
        change_5d = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
        change_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else None

        # 构建结果
        results = [f"**{symbol} 技术分析 ({period})**"]
        results.append(f"当前价格: {current_price:.2f}")
        results.append("")

        # 均线系统
        results.append("**均线系统:**")
        if ma5: results.append(f"  MA5: {ma5:.2f} {'↑' if current_price > ma5 else '↓'}")
        if ma10: results.append(f"  MA10: {ma10:.2f} {'↑' if current_price > ma10 else '↓'}")
        if ma20: results.append(f"  MA20: {ma20:.2f} {'↑' if current_price > ma20 else '↓'}")
        if ma60: results.append(f"  MA60: {ma60:.2f} {'↑' if current_price > ma60 else '↓'}")

        # 均线多空判断
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                results.append("  趋势: 多头排列 📈")
            elif ma5 < ma10 < ma20:
                results.append("  趋势: 空头排列 📉")
            else:
                results.append("  趋势: 震荡整理")

        results.append("")

        # MACD
        results.append("**MACD:**")
        if dif is not None:
            results.append(f"  DIF: {dif:.3f}, DEA: {dea:.3f}, MACD柱: {macd:.3f}")
            if dif > dea:
                results.append("  信号: 金叉/多头 📈")
            else:
                results.append("  信号: 死叉/空头 📉")

        results.append("")

        # RSI
        results.append("**RSI:**")
        if rsi:
            results.append(f"  RSI(14): {rsi:.1f}, RSI(6): {rsi6:.1f}" if rsi6 else f"  RSI(14): {rsi:.1f}")
            if rsi > 70:
                results.append("  状态: 超买区 ⚠️")
            elif rsi < 30:
                results.append("  状态: 超卖区 ⚠️")
            elif rsi > 50:
                results.append("  状态: 偏强")
            else:
                results.append("  状态: 偏弱")

        results.append("")

        # KDJ
        results.append("**KDJ:**")
        if k is not None:
            results.append(f"  K: {k:.1f}, D: {d:.1f}, J: {j:.1f}")
            if j > 100:
                results.append("  状态: 超买 ⚠️")
            elif j < 0:
                results.append("  状态: 超卖 ⚠️")
            elif k > d:
                results.append("  信号: 金叉/看多")
            else:
                results.append("  信号: 死叉/看空")

        results.append("")

        # 布林带
        results.append("**布林带:**")
        if bb_upper:
            results.append(f"  上轨: {bb_upper:.2f}, 中轨: {bb_mid:.2f}, 下轨: {bb_lower:.2f}")
            bb_width = (bb_upper - bb_lower) / bb_mid * 100
            results.append(f"  带宽: {bb_width:.1f}%")
            if current_price > bb_upper:
                results.append("  位置: 突破上轨 ⚠️")
            elif current_price < bb_lower:
                results.append("  位置: 跌破下轨 ⚠️")
            else:
                bb_pos = (current_price - bb_lower) / (bb_upper - bb_lower) * 100
                results.append(f"  位置: {bb_pos:.0f}%")

        results.append("")

        # 波动性
        results.append("**波动性:**")
        if atr:
            atr_pct = atr / current_price * 100
            results.append(f"  ATR(14): {atr:.2f} ({atr_pct:.1f}%)")

        # 成交量
        if vol_ratio:
            results.append(f"  量比: {vol_ratio:.2f}x")

        results.append("")

        # 涨跌幅
        results.append("**涨跌幅:**")
        results.append(f"  1日: {change_1d:+.2f}%")
        results.append(f"  5日: {change_5d:+.2f}%")
        if change_20d is not None:
            results.append(f"  20日: {change_20d:+.2f}%")

        results.append("")

        # 综合信号
        results.append("**综合评估:**")
        bullish_signals = 0
        bearish_signals = 0

        if ma5 and ma10 and current_price > ma5 > ma10: bullish_signals += 1
        if ma5 and ma10 and current_price < ma5 < ma10: bearish_signals += 1
        if dif is not None and dif > dea: bullish_signals += 1
        if dif is not None and dif < dea: bearish_signals += 1
        if rsi and rsi > 50: bullish_signals += 1
        if rsi and rsi < 50: bearish_signals += 1
        if k is not None and k > d: bullish_signals += 1
        if k is not None and k < d: bearish_signals += 1

        if bullish_signals > bearish_signals + 1:
            results.append(f"  多头信号: {bullish_signals}, 空头信号: {bearish_signals}")
            results.append("  建议: 偏多，可考虑做多 📈")
        elif bearish_signals > bullish_signals + 1:
            results.append(f"  多头信号: {bullish_signals}, 空头信号: {bearish_signals}")
            results.append("  建议: 偏空，谨慎或考虑做空 📉")
        else:
            results.append(f"  多头信号: {bullish_signals}, 空头信号: {bearish_signals}")
            results.append("  建议: 震荡，观望为主 ⏸️")

        results.append("")
        results.append("⚠️ 以上仅为技术指标参考，不构成投资建议")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 技术分析错误: {e}"


# ------------------------------------------
# 工具 25: 获取盘口深度
# ------------------------------------------
@mcp.tool()
def get_depth(symbol: str) -> str:
    """
    获取盘口深度（买卖盘）。需要 SDK 支持。

    Args:
        symbol: 股票代码

    Returns:
        盘口深度信息
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败"

        target = to_sdk_symbol(symbol)
        resp = ctx.depth(target)

        results = [f"**{target} 盘口深度**"]

        if hasattr(resp, 'asks') and resp.asks:
            results.append("  卖盘:")
            for a in resp.asks[:5]:
                results.append(f"    {a.price} x {a.volume}")

        if hasattr(resp, 'bids') and resp.bids:
            results.append("  买盘:")
            for b in resp.bids[:5]:
                results.append(f"    {b.price} x {b.volume}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 盘口查询错误: {e}"


# ------------------------------------------
# 工具 25: 获取经纪商队列
# ------------------------------------------
@mcp.tool()
def get_brokers(symbol: str) -> str:
    """
    获取经纪商队列（港股专用）。需要 SDK 支持。

    Args:
        symbol: 股票代码

    Returns:
        经纪商队列信息
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败"

        target = to_sdk_symbol(symbol)
        resp = ctx.brokers(target)

        results = [f"**{target} 经纪商队列**"]

        if hasattr(resp, 'ask_brokers') and resp.ask_brokers:
            results.append("  卖方经纪商:")
            for b in resp.ask_brokers[:5]:
                broker_ids = b.broker_ids if hasattr(b, 'broker_ids') else []
                results.append(f"    {broker_ids[:5]}")

        if hasattr(resp, 'bid_brokers') and resp.bid_brokers:
            results.append("  买方经纪商:")
            for b in resp.bid_brokers[:5]:
                broker_ids = b.broker_ids if hasattr(b, 'broker_ids') else []
                results.append(f"    {broker_ids[:5]}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 经纪商队列查询错误: {e}"


# ------------------------------------------
# 工具 26: 获取成交明细
# ------------------------------------------
@mcp.tool()
def get_trades(symbol: str, count: int = 20) -> str:
    """
    获取最近成交明细。需要 SDK 支持。

    Args:
        symbol: 股票代码
        count: 获取数量（默认20）

    Returns:
        成交明细
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败"

        target = to_sdk_symbol(symbol)
        trades = ctx.trades(target, count)

        if not trades:
            return "[i] 无成交明细"

        results = [f"**{target} 最近成交**"]
        for t in trades[:10]:
            price = getattr(t, 'price', "")
            vol = getattr(t, 'volume', "")
            # direction 直接用 str() 转换，避免枚举类型问题
            direction = str(getattr(t, 'direction', "")) if hasattr(t, 'direction') else ""
            ts = getattr(t, 'timestamp', "")
            results.append(f"  {ts}: {price} x {vol} ({direction})")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 成交明细查询错误: {e}"


# ------------------------------------------
# 工具 27: 获取分时数据
# ------------------------------------------
@mcp.tool()
def get_intraday(symbol: str) -> str:
    """
    获取分时数据。需要 SDK 支持。

    Args:
        symbol: 股票代码

    Returns:
        分时数据
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败"

        target = to_sdk_symbol(symbol)
        lines = ctx.intraday(target)

        if not lines:
            return "[i] 无分时数据"

        results = [f"**{target} 分时数据**"]

        # 显示最近10个点
        for l in lines[-10:]:
            price = l.price if hasattr(l, 'price') else ""
            vol = l.volume if hasattr(l, 'volume') else ""
            ts = l.timestamp if hasattr(l, 'timestamp') else ""
            results.append(f"  {ts}: {price} (成交量:{vol})")

        results.append(f"  ... 共 {len(lines)} 个数据点")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 分时数据查询错误: {e}"


# ------------------------------------------
# 工具 28: 获取标的基本信息
# ------------------------------------------
@mcp.tool()
def get_static_info(symbols: list[str]) -> str:
    """
    获取标的基本信息。

    Args:
        symbols: 股票代码列表

    Returns:
        标的基本信息
    """
    try:
        client = get_client()
        targets = [to_sdk_symbol(s) for s in symbols]
        resp = client.get_static_info(targets)

        infos = resp.get("data", {}).get("list", [])

        if not infos:
            return "[i] 无标的信息"

        results = ["**标的基本信息**"]
        for info in infos:
            symbol = info.get("symbol", "")
            name = info.get("name_cn", info.get("name_en", ""))
            exchange = info.get("exchange", "")
            board = info.get("board", "")
            lot_size = info.get("lot_size", "")
            results.append(f"  {symbol} ({name})")
            results.append(f"    交易所:{exchange} 板块:{board} 每手:{lot_size}股")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 标的信息查询错误: {e}"


# ------------------------------------------
# 工具 29: 获取期权链到期日
# ------------------------------------------
@mcp.tool()
def get_option_expiry_dates(symbol: str) -> str:
    """
    获取期权链到期日列表。

    Args:
        symbol: 标的代码（如 AAPL.US）

    Returns:
        到期日列表
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_option_chain_dates(target)

        dates = resp.get("data", {}).get("expiry_dates", [])

        if not dates:
            return f"[i] {target} 无期权数据"

        results = [f"**{target} 期权到期日**"]
        for d in dates[:12]:
            results.append(f"  {d}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 期权到期日查询错误: {e}"


# ------------------------------------------
# 工具 30: 获取窝轮发行商
# ------------------------------------------
@mcp.tool()
def get_warrant_issuers() -> str:
    """
    获取窝轮发行商列表。

    Returns:
        发行商列表
    """
    try:
        client = get_client()
        resp = client.get_warrant_issuers()

        issuers = resp.get("data", {}).get("issuer_info", [])

        if not issuers:
            return "[i] 无发行商数据"

        results = ["**窝轮发行商**"]
        for i in issuers[:20]:
            issuer_id = i.get("id", "")
            name = i.get("name_cn", i.get("name_en", ""))
            results.append(f"  [{issuer_id}] {name}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 发行商查询错误: {e}"


# ------------------------------------------
# 工具 31: 估算最大可买数量
# ------------------------------------------
@mcp.tool()
def estimate_buy_limit(
    symbol: str,
    price: float = 0,
    order_type: str = "LO"
) -> str:
    """
    估算最大可买数量。

    Args:
        symbol: 股票代码
        price: 价格（限价单需要）
        order_type: 订单类型（默认LO限价单）

    Returns:
        最大可买数量
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.estimate_buy_limit(
            target,
            order_type,
            "Buy",
            price if price > 0 else None
        )

        data = resp.get("data", {})
        cash_max = data.get("cash_max_qty", "N/A")
        margin_max = data.get("margin_max_qty", "N/A")

        return (
            f"**{target} 可买数量估算**\n"
            f"  现金可买: {cash_max} 股\n"
            f"  融资可买: {margin_max} 股"
        )

    except Exception as e:
        return f"[X] 可买数量估算错误: {e}"


# ------------------------------------------
# 工具 32: 高级下单
# ------------------------------------------
@mcp.tool()
def submit_order_advanced(
    symbol: str,
    side: str,
    quantity: int,
    order_type: str = "LO",
    price: float = 0,
    time_in_force: str = "Day",
    trigger_price: float = 0,
    outside_rth: str = "",
    remark: str = ""
) -> str:
    """
    高级下单（支持更多订单类型）。

    Args:
        symbol: 股票代码
        side: 方向 (buy/sell)
        quantity: 数量
        order_type: 订单类型 (LO限价/MO市价/ELO增强限价/ALO竞价限价/AO竞价/ODD碎股/LIT条件限价/MIT条件市价)
        price: 价格（限价单需要）
        time_in_force: 有效期 (Day/GTC/GTD)
        trigger_price: 触发价格（条件单需要）
        outside_rth: 盘前盘后 (RTH_ONLY/ANY_TIME/OVERNIGHT)
        remark: 备注

    Returns:
        下单结果
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        side_api = ORDER_SIDE_MAP.get(side.lower(), "Buy")

        data = {
            "symbol": target,
            "side": side_api,
            "submitted_quantity": str(quantity),
            "order_type": order_type.upper(),
            "time_in_force": time_in_force,
        }

        if price > 0:
            data["submitted_price"] = str(price)
        if trigger_price > 0:
            data["trigger_price"] = str(trigger_price)
        if outside_rth:
            data["outside_rth"] = outside_rth
        if remark:
            data["remark"] = remark[:255]

        resp = client.post("/v1/trade/order", data)
        order_id = resp.get("data", {}).get("order_id")

        return f"[OK] 高级订单已提交，ID: {order_id}"

    except Exception as e:
        return f"[X] 高级下单失败: {e}"


# ------------------------------------------
# 工具 33: 获取订单详情
# ------------------------------------------
@mcp.tool()
def get_order_detail(order_id: str) -> str:
    """
    获取订单详情。

    Args:
        order_id: 订单ID

    Returns:
        订单详细信息
    """
    try:
        client = get_client()
        resp = client.get_order_detail(order_id)

        data = resp.get("data", {})

        results = [f"**订单 {order_id} 详情**"]
        results.append(f"  股票: {data.get('symbol', '')}")
        results.append(f"  方向: {data.get('side', '')}")
        results.append(f"  状态: {data.get('status', '')}")
        results.append(f"  提交数量: {data.get('submitted_quantity', '')}")
        results.append(f"  提交价格: {data.get('submitted_price', '')}")
        results.append(f"  成交数量: {data.get('executed_quantity', '')}")
        results.append(f"  成交价格: {data.get('executed_price', '')}")
        results.append(f"  订单类型: {data.get('order_type', '')}")
        results.append(f"  提交时间: {data.get('submitted_at', '')}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 订单详情查询错误: {e}"


# ------------------------------------------
# 工具 34: 获取今日所有订单（包含已完成）
# ------------------------------------------
@mcp.tool()
def get_today_all_orders() -> str:
    """
    获取今日所有订单（包含已成交、已撤销等）。

    Returns:
        今日所有订单列表
    """
    try:
        client = get_client()
        resp = client.get_today_orders()

        orders = resp.get("data", {}).get("orders", [])

        if not orders:
            return "[i] 今日无订单"

        results = ["**今日所有订单**"]
        for o in orders:
            order_id = o.get("order_id", "")
            symbol = o.get("symbol", "")
            side = o.get("side", "")
            qty = o.get("submitted_quantity", "")
            price = o.get("submitted_price", "")
            status = o.get("status", "")
            results.append(f"  [{order_id}] {side} {symbol} {qty}股 @{price} ({status})")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 今日订单查询失败: {e}"


# ------------------------------------------
# 工具 35: 获取当前时间
# ------------------------------------------
@mcp.tool()
def get_current_time() -> str:
    """
    获取当前时间（用于调试和时间计算）。

    Returns:
        当前时间信息
    """
    from datetime import datetime
    import time

    now = datetime.now()
    utc_now = datetime.utcnow()

    return (
        f"**当前时间**\n"
        f"  本地: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  UTC: {utc_now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  时间戳: {int(time.time())}"
    )


# ==================== SDK 增强功能 ====================
# 以下工具需要 longport SDK 支持，提供更丰富的行情数据

# ------------------------------------------
# 工具 36: 获取交易日历 (SDK)
# ------------------------------------------
@mcp.tool()
def get_trading_days(
    market: str = "US",
    start_date: str = "",
    end_date: str = ""
) -> str:
    """
    获取交易日历（需要 SDK 支持）。

    Args:
        market: 市场代码（US/HK/CN/SG），默认 US
        start_date: 开始日期 YYYY-MM-DD，默认为今天
        end_date: 结束日期 YYYY-MM-DD，默认为30天后

    Returns:
        交易日列表
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        from datetime import date

        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败，请检查环境变量配置"

        # 默认日期
        today = date.today()
        if not start_date:
            begin = today
        else:
            begin = date.fromisoformat(start_date)

        if not end_date:
            end = date(today.year, today.month + 1 if today.month < 12 else 1,
                      today.day if today.month < 12 else today.day)
            if today.month == 12:
                end = date(today.year + 1, 1, today.day)
        else:
            end = date.fromisoformat(end_date)

        # 市场映射
        market_map = {
            "US": Market.US,
            "HK": Market.HK,
            "CN": Market.CN,
            "SG": Market.SG,
        }
        mkt = market_map.get(market.upper(), Market.US)

        resp = ctx.trading_days(mkt, begin, end)

        results = [f"**{market} 交易日历 ({begin} ~ {end})**"]

        trading_days = resp.trading_days if hasattr(resp, 'trading_days') else []
        half_trading_days = resp.half_trading_days if hasattr(resp, 'half_trading_days') else []

        if trading_days:
            results.append(f"  交易日数: {len(trading_days)}")
            # 显示最近10个
            days_str = [d.strftime("%Y-%m-%d") for d in trading_days[:10]]
            results.append(f"  日期: {', '.join(days_str)}")
            if len(trading_days) > 10:
                results.append(f"  ... 共 {len(trading_days)} 个交易日")

        if half_trading_days:
            half_str = [d.strftime("%Y-%m-%d") for d in half_trading_days]
            results.append(f"  半日交易: {', '.join(half_str)}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 交易日历查询错误: {e}"


# ------------------------------------------
# 工具 37: 获取资金流向 (SDK)
# ------------------------------------------
@mcp.tool()
def get_capital_flow(symbol: str) -> str:
    """
    获取个股当日资金流向（需要 SDK 支持）。

    Args:
        symbol: 股票代码

    Returns:
        资金流向信息
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败，请检查环境变量配置"

        target = to_sdk_symbol(symbol)
        resp = ctx.capital_flow(target)

        if not resp:
            return f"[i] {target} 无资金流向数据"

        results = [f"**{target} 资金流向**"]

        # 显示最近5条
        for line in resp[-5:]:
            inflow = getattr(line, 'inflow', 0)
            ts = getattr(line, 'timestamp', '')
            results.append(f"  {ts}: 净流入 {inflow}")

        if len(resp) > 5:
            results.append(f"  ... 共 {len(resp)} 条记录")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 资金流向查询错误: {e}"


# ------------------------------------------
# 工具 38: 获取资金分布 (SDK)
# ------------------------------------------
@mcp.tool()
def get_capital_distribution(symbol: str) -> str:
    """
    获取个股资金分布（需要 SDK 支持）。

    Args:
        symbol: 股票代码

    Returns:
        资金分布信息（大单/中单/小单）
    """
    if not HAS_SDK:
        return "[X] 此功能需要 longport SDK，请安装: pip install longport"

    try:
        ctx = get_quote_context()
        if ctx is None:
            return "[X] SDK 初始化失败，请检查环境变量配置"

        target = to_sdk_symbol(symbol)
        resp = ctx.capital_distribution(target)

        results = [f"**{target} 资金分布**"]

        # 大单
        large_in = getattr(resp, 'capital_in_large', 0)
        large_out = getattr(resp, 'capital_out_large', 0)
        results.append(f"  大单: 流入 {large_in}, 流出 {large_out}")

        # 中单
        medium_in = getattr(resp, 'capital_in_medium', 0)
        medium_out = getattr(resp, 'capital_out_medium', 0)
        results.append(f"  中单: 流入 {medium_in}, 流出 {medium_out}")

        # 小单
        small_in = getattr(resp, 'capital_in_small', 0)
        small_out = getattr(resp, 'capital_out_small', 0)
        results.append(f"  小单: 流入 {small_in}, 流出 {small_out}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 资金分布查询错误: {e}"


# ------------------------------------------
# 工具 39: 检查 SDK 状态
# ------------------------------------------
@mcp.tool()
def check_sdk_status() -> str:
    """
    检查 SDK 和各模块的可用状态。

    Returns:
        各模块状态信息
    """
    results = ["**模块状态检查**"]

    # SDK 状态
    if HAS_SDK:
        results.append("  [OK] longport SDK 已安装")
        ctx = get_quote_context()
        if ctx:
            results.append("  [OK] SDK QuoteContext 已初始化")
        else:
            results.append("  [X] SDK QuoteContext 初始化失败")
    else:
        results.append("  [X] longport SDK 未安装")
        results.append("      安装命令: pip install longport")

    # yfinance 状态
    if HAS_YFINANCE:
        results.append("  [OK] yfinance 已安装 (备用行情源)")
    else:
        results.append("  [i] yfinance 未安装 (可选)")

    # REST API 状态
    try:
        client = get_client()
        results.append("  [OK] REST API 客户端已初始化")
    except Exception as e:
        results.append(f"  [X] REST API 初始化失败: {e}")

    return "\n".join(results)


# ------------------------------------------
# 工具 40: 获取当前账户模式
# ------------------------------------------
@mcp.tool()
def get_account_mode() -> str:
    """
    获取当前账户模式（模拟盘/实盘）。

    Returns:
        当前模式信息
    """
    mode = get_current_mode()
    mode_name = "模拟盘 (Paper Trading)" if mode == "paper" else "实盘 (Real Trading)"

    try:
        client = get_client()
        status = "[OK] 已连接"
    except Exception as e:
        status = f"[X] 未连接: {e}"

    return (
        f"**当前账户模式**\n"
        f"  模式: {mode_name}\n"
        f"  状态: {status}\n"
        f"\n"
        f"切换模式请使用 switch_account_mode 工具"
    )


# ------------------------------------------
# 工具 41: 切换账户模式
# ------------------------------------------
@mcp.tool()
def switch_account_mode(mode: str) -> str:
    """
    切换账户模式（模拟盘/实盘）。

    重要提示：
    - paper: 模拟盘，用于测试和学习，不涉及真实资金
    - real: 实盘，涉及真实资金交易，请谨慎操作！

    Args:
        mode: 目标模式，"paper"=模拟盘，"real"=实盘

    Returns:
        切换结果
    """
    mode = mode.lower().strip()

    if mode not in ["paper", "real"]:
        return "[X] 无效模式，请使用 'paper'(模拟盘) 或 'real'(实盘)"

    old_mode = get_current_mode()
    if old_mode == mode:
        mode_name = "模拟盘" if mode == "paper" else "实盘"
        return f"[i] 已经处于{mode_name}模式"

    # 切换模式
    if set_current_mode(mode):
        if mode == "real":
            return (
                "**[!] 已切换到实盘模式**\n"
                "\n"
                "⚠️ 警告：您现在处于实盘交易模式！\n"
                "  - 所有交易操作将使用真实资金\n"
                "  - 请仔细核对每笔订单\n"
                "  - 如需返回模拟盘，请使用 switch_account_mode('paper')"
            )
        else:
            return (
                "**[OK] 已切换到模拟盘模式**\n"
                "\n"
                "当前为模拟盘环境：\n"
                "  - 所有交易为模拟操作\n"
                "  - 不涉及真实资金"
            )
    else:
        return "[X] 切换失败，请检查配置文件"


# ================= 主入口 =================
def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
