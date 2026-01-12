#!/usr/bin/env python3
"""
Longport MCP Server - Web API Version
=====================================
基于 REST API 重写的长桥证券 MCP 服务，完全不依赖 longport SDK。
使用 HMAC-SHA256 签名机制进行 API 鉴权。

配置文件: longport_config.json
{
    "app_key": "your_app_key",
    "app_secret": "your_app_secret",
    "access_token": "your_access_token"
}
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
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化客户端

        Args:
            config_path: 配置文件路径，默认为当前目录下的 longport_config.json
        """
        self.config = self._load_config(config_path)
        self.app_key = self.config.get("app_key") or os.getenv("LONGPORT_APP_KEY")
        self.app_secret = self.config.get("app_secret") or os.getenv("LONGPORT_APP_SECRET")
        self.access_token = self.config.get("access_token") or os.getenv("LONGPORT_ACCESS_TOKEN")

        if not all([self.app_key, self.app_secret, self.access_token]):
            raise ValueError("缺少必要的配置: app_key, app_secret, access_token")

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
        url = f"{BASE_URL}{endpoint}"
        params_str = ""

        if params:
            # 构建查询字符串（按字母顺序排序）
            sorted_params = sorted(params.items())
            params_str = "&".join(f"{k}={v}" for k, v in sorted_params if v is not None)

        headers = self._build_headers("GET", endpoint, params_str)

        response = self.session.get(url, params=params, headers=headers, timeout=30)
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
        url = f"{BASE_URL}{endpoint}"
        params_str = ""

        if params:
            sorted_params = sorted(params.items())
            params_str = "&".join(f"{k}={v}" for k, v in sorted_params if v is not None)

        headers = self._build_headers("DELETE", endpoint, params_str)

        response = self.session.delete(url, params=params, headers=headers, timeout=30)
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
_client: Optional[LongportClient] = None

def get_client() -> LongportClient:
    """获取全局客户端实例（懒加载）"""
    global _client
    if _client is None:
        _client = LongportClient()
    return _client
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
    result = _core_execute_trade(symbol, side, quantity, price, "Manual")
    if result:
        return f"[OK] 订单已提交，ID: {result}"
    return "[X] 下单失败，请检查日志"


# ------------------------------------------
# 工具 6: 获取汇率（使用 yfinance）
# ------------------------------------------
@mcp.tool()
def get_exchange_rate(from_currency: str = "USD", to_currency: str = "CNY") -> str:
    """
    获取汇率。

    Args:
        from_currency: 源货币（默认 USD）
        to_currency: 目标货币（默认 CNY）

    Returns:
        汇率信息
    """
    if not HAS_YFINANCE:
        return "[X] yfinance 未安装，无法获取汇率"

    try:
        symbol = f"{from_currency.upper()}{to_currency.upper()}=X"
        ticker = yf.Ticker(symbol)

        if hasattr(ticker, 'fast_info'):
            price = ticker.fast_info.last_price
        else:
            price = ticker.info.get('regularMarketPrice')

        if not price:
            return f"[X] 无汇率数据"

        return f"[i] 1 {from_currency.upper()} = {price:.4f} {to_currency.upper()}"

    except Exception:
        return "[X] 汇率接口异常"


# ------------------------------------------
# 工具 7: 搜索股票代码（使用 Yahoo Finance）
# ------------------------------------------
@mcp.tool()
def search_symbol(keyword: str) -> str:
    """
    搜索股票代码。

    Args:
        keyword: 搜索关键词（公司名或代码）

    Returns:
        搜索结果列表
    """
    try:
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        params = {
            "q": keyword,
            "lang": "zh-CN",
            "region": "HK",
            "quotesCount": 5
        }
        resp = requests.get(
            url,
            params=params,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5
        )
        data = resp.json()

        if "quotes" not in data or not data["quotes"]:
            return f"[X] 未找到: {keyword}"

        results = []
        for item in data["quotes"]:
            sym = to_sdk_symbol(item.get("symbol", ""))
            name = item.get("shortname") or item.get("longname", keyword)
            results.append(f"  {name} -> {sym}")

        return "[i] 搜索结果:\n" + "\n".join(results)

    except Exception:
        return "[X] 搜索异常"


# ------------------------------------------
# 工具 8: 获取实时行情
# ------------------------------------------
@mcp.tool()
def get_quote(symbols: list[str]) -> str:
    """
    获取股票实时行情。

    Args:
        symbols: 股票代码列表（如 ["AAPL.US", "700.HK"]）

    Returns:
        行情信息
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

        positions = resp.get("data", {}).get("list", [])

        if not positions:
            return "[i] 无持仓"

        results = ["**持仓列表**"]
        for p in positions:
            quantity = int(p.get("quantity", 0))
            if quantity == 0:
                continue

            symbol = p.get("symbol", "")
            cost_price = p.get("cost_price", "N/A")
            available = p.get("available_quantity", quantity)

            results.append(
                f"  {symbol}: {quantity}股 (可用: {available}, 成本: {cost_price})"
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

        data = resp.get("data", {})

        results = ["**账户资产**"]

        # 总资产信息
        total_cash = data.get("total_cash", "N/A")
        net_assets = data.get("net_assets", "N/A")

        results.append(f"  总现金: {total_cash}")
        results.append(f"  净资产: {net_assets}")

        # 各币种现金
        cash_info = data.get("cash_infos", [])
        if cash_info:
            results.append("  各币种现金:")
            for c in cash_info:
                curr = c.get("currency", "")
                available = c.get("available_cash", c.get("cash", "N/A"))
                results.append(f"    {curr}: {available}")

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
# 工具 23: 获取K线数据
# ------------------------------------------
@mcp.tool()
def get_candlesticks(
    symbol: str,
    period: str = "1d",
    count: int = 100
) -> str:
    """
    获取K线数据。

    Args:
        symbol: 股票代码
        period: K线周期 (1m/5m/15m/30m/60m/1d/1w/1M/1y)
        count: K线数量（最大1000）

    Returns:
        K线数据
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_candlesticks(target, period, count)

        candles = resp.get("data", {}).get("candlesticks", [])

        if not candles:
            return "[i] 无K线数据"

        results = [f"**{target} {period} K线 (最近{len(candles)}根)**"]

        # 显示最近5根K线
        for c in candles[-5:]:
            o = c.get("open", "")
            h = c.get("high", "")
            l = c.get("low", "")
            close = c.get("close", "")
            vol = c.get("volume", "")
            ts = c.get("timestamp", "")
            results.append(f"  {ts}: O:{o} H:{h} L:{l} C:{close} V:{vol}")

        if len(candles) > 5:
            results.append(f"  ... 共 {len(candles)} 根K线")

        return "\n".join(results)

    except Exception as e:
        return f"[X] K线查询错误: {e}"


# ------------------------------------------
# 工具 24: 获取盘口深度
# ------------------------------------------
@mcp.tool()
def get_depth(symbol: str) -> str:
    """
    获取盘口深度（买卖盘）。

    Args:
        symbol: 股票代码

    Returns:
        盘口深度信息
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_depth(target)

        data = resp.get("data", {})
        asks = data.get("ask", [])
        bids = data.get("bid", [])

        results = [f"**{target} 盘口深度**"]

        results.append("  卖盘:")
        for a in asks[:5]:
            price = a.get("price", "")
            vol = a.get("volume", "")
            results.append(f"    {price} x {vol}")

        results.append("  买盘:")
        for b in bids[:5]:
            price = b.get("price", "")
            vol = b.get("volume", "")
            results.append(f"    {price} x {vol}")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 盘口查询错误: {e}"


# ------------------------------------------
# 工具 25: 获取经纪商队列
# ------------------------------------------
@mcp.tool()
def get_brokers(symbol: str) -> str:
    """
    获取经纪商队列（港股专用）。

    Args:
        symbol: 股票代码

    Returns:
        经纪商队列信息
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_brokers(target)

        data = resp.get("data", {})
        ask_brokers = data.get("ask_brokers", [])
        bid_brokers = data.get("bid_brokers", [])

        results = [f"**{target} 经纪商队列**"]

        results.append("  卖方经纪商:")
        for b in ask_brokers[:5]:
            price = b.get("price", "")
            broker_ids = b.get("broker_ids", [])
            results.append(f"    {price}: {broker_ids[:3]}...")

        results.append("  买方经纪商:")
        for b in bid_brokers[:5]:
            price = b.get("price", "")
            broker_ids = b.get("broker_ids", [])
            results.append(f"    {price}: {broker_ids[:3]}...")

        return "\n".join(results)

    except Exception as e:
        return f"[X] 经纪商队列查询错误: {e}"


# ------------------------------------------
# 工具 26: 获取成交明细
# ------------------------------------------
@mcp.tool()
def get_trades(symbol: str, count: int = 20) -> str:
    """
    获取最近成交明细。

    Args:
        symbol: 股票代码
        count: 获取数量（默认20）

    Returns:
        成交明细
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_trades(target, count)

        trades = resp.get("data", {}).get("trades", [])

        if not trades:
            return "[i] 无成交明细"

        results = [f"**{target} 最近成交**"]
        for t in trades[:10]:
            price = t.get("price", "")
            vol = t.get("volume", "")
            direction = t.get("direction", "")
            ts = t.get("timestamp", "")
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
    获取分时数据。

    Args:
        symbol: 股票代码

    Returns:
        分时数据
    """
    try:
        client = get_client()
        target = to_sdk_symbol(symbol)
        resp = client.get_intraday(target)

        lines = resp.get("data", {}).get("lines", [])

        if not lines:
            return "[i] 无分时数据"

        results = [f"**{target} 分时数据**"]

        # 显示最近10个点
        for l in lines[-10:]:
            price = l.get("price", "")
            vol = l.get("volume", "")
            ts = l.get("timestamp", "")
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


# ================= 主入口 =================
if __name__ == "__main__":
    mcp.run()
