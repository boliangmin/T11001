#!/usr/bin/env python3
"""免账号查询美股和港股公开行情。

数据源：Yahoo Finance、腾讯自选股、新浪财经。
这些都是面向个人查询的公开接口，并非交易所授权的稳定 API；返回结果可能
延迟、缓存或临时不可用，不应作为自动下单的唯一价格依据。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def _configure_console_encoding() -> None:
    """让 Windows 终端稳定显示中文，同时不影响被重定向的输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


class QuoteError(RuntimeError):
    """某个行情源无法返回有效数据。"""


@dataclass(frozen=True)
class StockSymbol:
    original: str
    market: str
    canonical: str
    yahoo: str
    tencent: str
    sina: str
    default_currency: str


@dataclass
class PriceQuote:
    source: str
    symbol: str
    name: str
    price: float
    currency: str
    quote_time: str | None
    timezone: str
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    bid: float | None = None
    ask: float | None = None
    session: str = "未知"
    fetched_at_utc: str = ""
    # 只用于比较同一市场不同来源的新旧，不写入 JSON。
    _sort_time: datetime | None = None

    def public_dict(self) -> dict[str, object]:
        result = asdict(self)
        result.pop("_sort_time", None)
        return result


@dataclass
class StockSearchResult:
    symbol: StockSymbol
    name: str
    source: str
    provider_rank: int = 0

    @property
    def display_code(self) -> str:
        if self.symbol.market == "HK":
            return self.symbol.yahoo
        return self.symbol.canonical.removeprefix("US.")


def normalize_symbol(raw: str) -> StockSymbol:
    """把常见输入转换成三个行情源所需的代码。"""
    token = raw.strip().upper()
    if not token:
        raise ValueError("股票代码不能为空")

    hk_digits: str | None = None
    if token.startswith("HK."):
        hk_digits = token[3:]
    elif token.endswith(".HK"):
        hk_digits = token[:-3]
    elif token.isdigit():
        hk_digits = token

    if hk_digits is not None:
        if not hk_digits.isdigit() or not 1 <= len(hk_digits) <= 5:
            raise ValueError(f"无效港股代码：{raw}")
        number = hk_digits.lstrip("0") or "0"
        hkex_code = number.zfill(5)
        # Yahoo 对传统港股代码使用四位形式，如 0700.HK、9988.HK。
        yahoo_code = number.zfill(4) if len(number) <= 4 else number
        return StockSymbol(
            original=raw,
            market="HK",
            canonical=f"HK.{hkex_code}",
            yahoo=f"{yahoo_code}.HK",
            tencent=f"hk{hkex_code}",
            sina=f"hk{hkex_code}",
            default_currency="HKD",
        )

    if token.startswith("US."):
        token = token[3:]
    elif token.endswith(".US"):
        token = token[:-3]

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", token):
        raise ValueError(f"无效美股代码：{raw}")

    # Yahoo 用 BRK-B 表示类别股；腾讯/新浪通常接受交易所常用的 BRK.B。
    yahoo_code = token.replace(".", "-")
    return StockSymbol(
        original=raw,
        market="US",
        canonical=f"US.{token}",
        yahoo=yahoo_code,
        tencent=f"us{token}",
        sina=f"gb_{token.lower()}",
        default_currency="USD",
    )


def _fetch_bytes(
    url: str,
    timeout: float,
    *,
    referer: str | None = None,
    attempts: int = 2,
) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.35 * (attempt + 1))

    raise QuoteError(f"网络请求失败：{last_error}")


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _change(price: float, previous_close: float | None) -> tuple[float | None, float | None]:
    if previous_close is None or previous_close == 0:
        return None, None
    difference = price - previous_close
    return difference, difference / previous_close * 100


def _parse_local_time(value: str) -> datetime | None:
    value = value.strip().replace("/", "-")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def _format_epoch_local(epoch: int, offset_seconds: int) -> tuple[str, datetime]:
    tz = timezone(timedelta(seconds=offset_seconds))
    aware = datetime.fromtimestamp(epoch, timezone.utc).astimezone(tz)
    return aware.strftime("%Y-%m-%d %H:%M:%S"), aware.replace(tzinfo=None)


def _session_for_epoch(meta: dict[str, object], epoch: int) -> str:
    periods = meta.get("currentTradingPeriod")
    if not isinstance(periods, dict):
        return "未知"
    labels = {"pre": "盘前", "regular": "正常交易", "post": "盘后"}
    for key, label in labels.items():
        period = periods.get(key)
        if not isinstance(period, dict):
            continue
        start = period.get("start")
        end = period.get("end")
        if isinstance(start, int) and isinstance(end, int) and start <= epoch <= end:
            return label
    return "最近交易时段"


def fetch_yahoo(symbol: StockSymbol, timeout: float) -> PriceQuote:
    encoded = url_quote(symbol.yahoo, safe="-.^")
    query = "interval=1m&range=1d&includePrePost=true&events=div%2Csplits"
    errors: list[str] = []
    result: dict[str, object] | None = None

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            raw = _fetch_bytes(
                f"https://{host}/v8/finance/chart/{encoded}?{query}",
                timeout,
                attempts=1,
            )
            payload = json.loads(raw.decode("utf-8"))
            chart = payload.get("chart", {})
            if chart.get("error"):
                raise QuoteError(str(chart["error"]))
            results = chart.get("result") or []
            if not results:
                raise QuoteError("Yahoo 未返回该代码的数据")
            result = results[0]
            break
        except (QuoteError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{host}: {exc}")

    if result is None:
        raise QuoteError("；".join(errors))

    meta = result.get("meta") or {}
    if not isinstance(meta, dict):
        raise QuoteError("Yahoo 返回的数据格式异常")

    regular_price = _to_float(meta.get("regularMarketPrice"))
    regular_epoch = meta.get("regularMarketTime")
    selected_price = regular_price
    selected_epoch = regular_epoch if isinstance(regular_epoch, int) else None

    # 分钟线包含盘前、盘中和盘后；取最后一个非空值可获得更接近刷新时刻的价格。
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quote_groups = indicators.get("quote") or [] if isinstance(indicators, dict) else []
    closes = quote_groups[0].get("close", []) if quote_groups else []
    if isinstance(timestamps, list) and isinstance(closes, list):
        for epoch, close in reversed(list(zip(timestamps, closes))):
            candle_price = _to_float(close)
            if isinstance(epoch, int) and candle_price is not None:
                if selected_epoch is None or epoch >= selected_epoch:
                    selected_epoch = epoch
                    selected_price = candle_price
                break

    if selected_price is None:
        raise QuoteError("Yahoo 没有返回有效价格")

    offset = meta.get("gmtoffset")
    offset_seconds = int(offset) if isinstance(offset, (int, float)) else 0
    display_time: str | None = None
    sort_time: datetime | None = None
    session = "最近交易时段"
    if selected_epoch is not None:
        display_time, sort_time = _format_epoch_local(selected_epoch, offset_seconds)
        session = _session_for_epoch(meta, selected_epoch)

    previous_close = _to_float(meta.get("previousClose"))
    if previous_close is None:
        previous_close = _to_float(meta.get("chartPreviousClose"))
    difference, percentage = _change(selected_price, previous_close)

    return PriceQuote(
        source="Yahoo Finance",
        symbol=symbol.canonical,
        name=str(meta.get("longName") or meta.get("shortName") or ""),
        price=selected_price,
        currency=str(meta.get("currency") or symbol.default_currency),
        quote_time=display_time,
        timezone=str(meta.get("timezone") or meta.get("exchangeTimezoneName") or "交易所当地时间"),
        previous_close=previous_close,
        change=difference,
        change_percent=percentage,
        session=session,
        fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _sort_time=sort_time,
    )


def _decode_cn_market_data(raw: bytes) -> str:
    for encoding in ("gb18030", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("gb18030", errors="replace")


def fetch_tencent(symbol: StockSymbol, timeout: float) -> PriceQuote:
    raw = _fetch_bytes(
        f"https://qt.gtimg.cn/q={url_quote(symbol.tencent, safe='.')}",
        timeout,
        referer="https://gu.qq.com/",
    )
    text = _decode_cn_market_data(raw)
    match = re.search(r'v_[^=]+="(.*?)";', text, re.S)
    if not match or not match.group(1):
        raise QuoteError("腾讯未返回该代码的数据")

    fields = match.group(1).split("~")
    if len(fields) < 36:
        raise QuoteError("腾讯返回的数据格式异常")

    price = _to_float(fields[3])
    if price is None or price <= 0:
        raise QuoteError("腾讯没有返回有效价格")

    previous_close = _to_float(fields[4])
    difference = _to_float(fields[31])
    percentage = _to_float(fields[32])
    if difference is None or percentage is None:
        difference, percentage = _change(price, previous_close)

    display_time = fields[30].strip() or None
    sort_time = _parse_local_time(display_time) if display_time else None
    # 美股和港股返回结构长度不同，币种不总在相同下标。
    currency = next(
        (
            value.strip().upper()
            for value in fields[35:]
            if value.strip().upper() in {"USD", "HKD", "CNY", "JPY", "SGD"}
        ),
        symbol.default_currency,
    )

    return PriceQuote(
        source="腾讯自选股",
        symbol=symbol.canonical,
        name=fields[1].strip(),
        price=price,
        currency=currency,
        quote_time=display_time,
        timezone="香港时间" if symbol.market == "HK" else "美东时间",
        previous_close=previous_close,
        change=difference,
        change_percent=percentage,
        bid=_to_float(fields[9]),
        ask=_to_float(fields[19]),
        session="最新公开行情",
        fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _sort_time=sort_time,
    )


def _parse_sina_us_time(value: str, year: int) -> tuple[str | None, datetime | None, str]:
    match = re.fullmatch(
        r"([A-Za-z]{3}\s+\d{1,2}\s+\d{1,2}:\d{2}(?:AM|PM))\s+([A-Z]{2,5})",
        value.strip(),
    )
    if not match:
        return None, None, "美东时间"
    try:
        parsed = datetime.strptime(f"{year} {match.group(1)}", "%Y %b %d %I:%M%p")
    except ValueError:
        return None, None, match.group(2)
    return parsed.strftime("%Y-%m-%d %H:%M:%S"), parsed, match.group(2)


def fetch_sina(symbol: StockSymbol, timeout: float) -> PriceQuote:
    raw = _fetch_bytes(
        f"https://hq.sinajs.cn/list={url_quote(symbol.sina, safe='_.')}",
        timeout,
        referer="https://finance.sina.com.cn/",
    )
    text = _decode_cn_market_data(raw)
    match = re.search(r'var\s+hq_str_[^=]+="(.*?)";', text, re.S)
    if not match or not match.group(1):
        raise QuoteError("新浪未返回该代码的数据")
    fields = match.group(1).split(",")

    if symbol.market == "HK":
        if len(fields) < 19:
            raise QuoteError("新浪港股数据格式异常")
        price = _to_float(fields[6])
        if price is None or price <= 0:
            raise QuoteError("新浪没有返回有效价格")
        previous_close = _to_float(fields[3])
        display_time = f"{fields[17]} {fields[18]}".strip()
        sort_time = _parse_local_time(display_time)
        return PriceQuote(
            source="新浪财经",
            symbol=symbol.canonical,
            name=fields[1].strip() or fields[0].strip(),
            price=price,
            currency="HKD",
            quote_time=display_time or None,
            timezone="香港时间",
            previous_close=previous_close,
            change=_to_float(fields[7]),
            change_percent=_to_float(fields[8]),
            bid=_to_float(fields[9]),
            ask=_to_float(fields[10]),
            session="最新公开行情",
            fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _sort_time=sort_time,
        )

    if len(fields) < 30:
        raise QuoteError("新浪美股数据格式异常")
    regular_price = _to_float(fields[1])
    if regular_price is None or regular_price <= 0:
        raise QuoteError("新浪没有返回有效价格")

    year = int(fields[29]) if fields[29].strip().isdigit() else datetime.now().year
    regular_time, regular_sort, tz_label = _parse_sina_us_time(fields[25], year)
    price = regular_price
    display_time = regular_time
    sort_time = regular_sort
    session = "正常交易"
    difference = _to_float(fields[4])
    percentage = _to_float(fields[2])

    # 新浪美股数据若包含盘后价，字段 21/24 会比正常交易时段更新。
    extended_price = _to_float(fields[21])
    extended_time, extended_sort, extended_tz = _parse_sina_us_time(fields[24], year)
    if (
        extended_price is not None
        and extended_price > 0
        and extended_sort is not None
        and (sort_time is None or extended_sort > sort_time)
    ):
        price = extended_price
        display_time = extended_time
        sort_time = extended_sort
        tz_label = extended_tz
        session = "盘前/盘后"
        difference = _to_float(fields[22])
        percentage = _to_float(fields[23])

    previous_close = _to_float(fields[26])
    if difference is None or percentage is None:
        difference, percentage = _change(price, previous_close)

    return PriceQuote(
        source="新浪财经",
        symbol=symbol.canonical,
        name=fields[0].strip(),
        price=price,
        currency="USD",
        quote_time=display_time,
        timezone=tz_label,
        previous_close=previous_close,
        change=difference,
        change_percent=percentage,
        session=session,
        fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _sort_time=sort_time,
    )


def _search_symbol(market: str, code: str) -> StockSymbol | None:
    code = code.strip().upper()
    try:
        if market == "HK":
            return normalize_symbol(f"HK.{code}")
        if market == "US":
            # 腾讯会在美股代码后附交易所标识，例如 AAPL.OQ、BABA.N。
            code = re.sub(r"\.(?:OQ|N|A|P|PS|PK)$", "", code)
            return normalize_symbol(f"US.{code}")
    except ValueError:
        return None
    return None


def _search_term(query: str, provider: str) -> str:
    value = query.strip()
    upper = value.upper()
    explicit_hk = upper.startswith("HK.") or upper.endswith(".HK")
    complete_numeric_hk = value.isdigit() and len(value) in {4, 5}
    if explicit_hk or complete_numeric_hk:
        try:
            symbol = normalize_symbol(value)
        except ValueError:
            return value
        if provider == "yahoo":
            return symbol.yahoo
        return symbol.canonical.removeprefix("HK.")
    if upper.startswith("US."):
        return value[3:]
    if upper.endswith(".US"):
        return value[:-3]
    return value


def search_yahoo(query: str, timeout: float) -> list[StockSearchResult]:
    params = urlencode(
        {
            "q": _search_term(query, "yahoo"),
            "quotesCount": 12,
            "newsCount": 0,
            "enableFuzzyQuery": "true",
        }
    )
    raw = _fetch_bytes(
        f"https://query1.finance.yahoo.com/v1/finance/search?{params}",
        timeout,
        attempts=1,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuoteError(f"Yahoo 搜索数据格式异常：{exc}") from exc

    supported_us_exchanges = {
        "NMS",
        "NYQ",
        "NGM",
        "NCM",
        "ASE",
        "PCX",
        "BTS",
        "PNK",
        "OEM",
        "OQX",
        "OQB",
        "NASDAQ",
        "NYSE",
        "AMEX",
    }
    results: list[StockSearchResult] = []
    for rank, item in enumerate(payload.get("quotes") or []):
        if not isinstance(item, dict) or item.get("quoteType") != "EQUITY":
            continue
        code = str(item.get("symbol") or "").strip().upper()
        exchange = str(item.get("exchange") or "").strip().upper()
        if code.endswith(".HK") or exchange == "HKG":
            symbol = _search_symbol("HK", code.removesuffix(".HK"))
        elif exchange in supported_us_exchanges:
            symbol = _search_symbol("US", code)
        else:
            continue
        if symbol is None:
            continue
        name = str(item.get("longname") or item.get("shortname") or code).strip()
        results.append(
            StockSearchResult(
                symbol=symbol,
                name=name,
                source="Yahoo",
                provider_rank=rank,
            )
        )
    return results


def _decode_tencent_escapes(value: str) -> str:
    return re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


def search_tencent(query: str, timeout: float) -> list[StockSearchResult]:
    encoded = url_quote(_search_term(query, "tencent"), safe="")
    raw = _fetch_bytes(
        f"https://smartbox.gtimg.cn/s3/?q={encoded}&t=all",
        timeout,
        referer="https://gu.qq.com/",
        attempts=1,
    )
    text = _decode_cn_market_data(raw)
    # 腾讯搜索响应有时以引号结束、有时追加分号，不能强制要求分号。
    match = re.search(r'v_hint="([^"]*)"', text, re.S)
    if not match or match.group(1) == "N":
        return []

    results: list[StockSearchResult] = []
    body = _decode_tencent_escapes(match.group(1))
    for rank, record in enumerate(body.split("^")):
        fields = record.split("~")
        if len(fields) < 5 or fields[4].strip().upper() != "GP":
            continue
        market = fields[0].strip().upper()
        if market not in {"HK", "US"}:
            continue
        symbol = _search_symbol(market, fields[1])
        if symbol is None:
            continue
        results.append(
            StockSearchResult(
                symbol=symbol,
                name=fields[2].strip() or symbol.canonical,
                source="腾讯",
                provider_rank=rank,
            )
        )
    return results


def search_sina(query: str, timeout: float) -> list[StockSearchResult]:
    encoded = url_quote(_search_term(query, "sina"), safe="")
    raw = _fetch_bytes(
        f"https://suggest3.sinajs.cn/suggest/type=&key={encoded}",
        timeout,
        referer="https://finance.sina.com.cn/",
        attempts=1,
    )
    text = _decode_cn_market_data(raw)
    match = re.search(r'var\s+suggestvalue="(.*?)";', text, re.S)
    if not match or not match.group(1):
        return []

    results: list[StockSearchResult] = []
    for rank, record in enumerate(match.group(1).split(";")):
        fields = record.split(",")
        if len(fields) < 7:
            continue
        type_code = fields[1].strip()
        market = "HK" if type_code == "31" else "US" if type_code == "41" else ""
        if not market:
            continue
        symbol = _search_symbol(market, fields[2])
        if symbol is None:
            continue
        name = fields[4].strip() or fields[6].strip() or fields[0].strip()
        results.append(
            StockSearchResult(
                symbol=symbol,
                name=name,
                source="新浪",
                provider_rank=rank,
            )
        )
    return results


SEARCH_PROVIDERS: dict[
    str, Callable[[str, float], list[StockSearchResult]]
] = {
    "tencent": search_tencent,
    "sina": search_sina,
    "yahoo": search_yahoo,
}


def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _search_score(result: StockSearchResult, query: str) -> tuple[int, int, str]:
    term = query.strip().casefold()
    compact_term = re.sub(r"^(?:hk\.|us\.)|(?:\.hk|\.us)$", "", term)
    codes = {
        result.display_code.casefold(),
        result.symbol.canonical.casefold(),
        result.symbol.yahoo.casefold(),
    }
    compact_codes = {
        re.sub(r"^(?:hk\.|us\.)|(?:\.hk|\.us)$", "", code) for code in codes
    }
    name = result.name.casefold()
    if term in codes or compact_term in compact_codes:
        category = 0
    elif any(code.startswith(term) for code in codes) or any(
        code.startswith(compact_term) for code in compact_codes
    ):
        category = 1
    elif name == term:
        category = 2
    elif term and term in name:
        category = 3
    else:
        category = 4
    return category, result.provider_rank, result.display_code


def search_stocks(
    query: str,
    timeout: float = 8.0,
    limit: int = 10,
) -> tuple[list[StockSearchResult], dict[str, str]]:
    """合并三家公开搜索结果，仅保留美股和港股候选。"""
    query = query.strip()
    if not query:
        return [], {}

    provider_results: dict[str, list[StockSearchResult]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(SEARCH_PROVIDERS)) as executor:
        future_map = {
            executor.submit(provider, query, timeout): name
            for name, provider in SEARCH_PROVIDERS.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                provider_results[name] = future.result()
            except Exception as exc:
                errors[name] = str(exc)

    merged: dict[str, StockSearchResult] = {}
    # 中文名称优先采用腾讯、其次新浪；Yahoo 用于补充代码和英文名称搜索。
    for provider_name in ("tencent", "sina", "yahoo"):
        for result in provider_results.get(provider_name, []):
            key = result.symbol.canonical
            existing = merged.get(key)
            if existing is None:
                merged[key] = result
                continue
            sources = existing.source.split("/")
            if result.source not in sources:
                existing.source += f"/{result.source}"
            if _contains_chinese(result.name) and (
                not _contains_chinese(existing.name)
                or len(result.name) > len(existing.name)
            ):
                existing.name = result.name
            existing.provider_rank = min(existing.provider_rank, result.provider_rank)

    results = sorted(merged.values(), key=lambda item: _search_score(item, query))
    return results[: max(1, limit)], errors


PROVIDERS: dict[str, Callable[[StockSymbol, float], PriceQuote]] = {
    "yahoo": fetch_yahoo,
    "tencent": fetch_tencent,
    "sina": fetch_sina,
}


def fetch_quotes(
    symbol: StockSymbol,
    provider_names: Iterable[str],
    timeout: float,
) -> tuple[list[PriceQuote], dict[str, str]]:
    quotes: list[PriceQuote] = []
    errors: dict[str, str] = {}
    names = list(provider_names)

    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        future_map = {
            executor.submit(PROVIDERS[name], symbol, timeout): name for name in names
        }
        for future in as_completed(future_map):
            provider_name = future_map[future]
            try:
                quotes.append(future.result())
            except Exception as exc:  # 单源失败不影响另外两个来源。
                errors[provider_name] = str(exc)

    order = {"Yahoo Finance": 0, "腾讯自选股": 1, "新浪财经": 2}
    quotes.sort(key=lambda item: order.get(item.source, 99))
    return quotes, errors


def choose_freshest(quotes: list[PriceQuote]) -> PriceQuote:
    with_time = [item for item in quotes if item._sort_time is not None]
    if with_time:
        return max(with_time, key=lambda item: item._sort_time or datetime.min)
    # 没有可比较时间时，优先使用腾讯，其次 Yahoo，再其次新浪。
    priority = {"腾讯自选股": 0, "Yahoo Finance": 1, "新浪财经": 2}
    return min(quotes, key=lambda item: priority.get(item.source, 99))


def _format_number(value: float | None, digits: int = 3) -> str:
    return "--" if value is None else f"{value:,.{digits}f}"


def print_quotes(symbol: StockSymbol, quotes: list[PriceQuote], errors: dict[str, str]) -> None:
    print(f"\n{symbol.canonical}")
    print("-" * 88)
    print(f"{'来源':<16}{'价格':>14}{'涨跌幅':>12}  {'行情时间':<22}{'时区/时段'}")
    print("-" * 88)
    for item in quotes:
        price = f"{_format_number(item.price)} {item.currency}"
        percentage = "--" if item.change_percent is None else f"{item.change_percent:+.2f}%"
        quote_time = item.quote_time or "未提供"
        print(
            f"{item.source:<16}{price:>14}{percentage:>12}  "
            f"{quote_time:<22}{item.timezone} / {item.session}"
        )

    if quotes:
        best = choose_freshest(quotes)
        print("-" * 88)
        print(
            f"采用最新结果：{best.source}  "
            f"{_format_number(best.price)} {best.currency}  "
            f"（{best.quote_time or '时间未知'} {best.timezone}）"
        )
        if best.bid is not None or best.ask is not None:
            print(
                f"买一/卖一：{_format_number(best.bid)} / {_format_number(best.ask)} "
                f"{best.currency}"
            )

        prices = [item.price for item in quotes]
        if len(prices) >= 2:
            midpoint = sorted(prices)[len(prices) // 2]
            if midpoint and (max(prices) - min(prices)) / midpoint > 0.01:
                print("警告：不同来源价格相差超过 1%，请检查各自的行情时间和交易时段。")

    for provider, message in errors.items():
        print(f"[{provider} 失败] {message}", file=sys.stderr)


def quote_as_json(
    symbol: StockSymbol,
    quotes: list[PriceQuote],
    errors: dict[str, str],
) -> dict[str, object]:
    freshest = choose_freshest(quotes).public_dict() if quotes else None
    return {
        "symbol": symbol.canonical,
        "best": freshest,
        "quotes": [item.public_dict() for item in quotes],
        "errors": errors,
        "notice": "公开行情可能延迟、缓存或中断，不应作为自动下单的唯一依据。",
    }


def _split_symbols(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        result.extend(part for part in re.split(r"[,，\s]+", value.strip()) if part)
    return result


def run_once(raw_symbols: Iterable[str], source: str, timeout: float, as_json: bool) -> int:
    provider_names = list(PROVIDERS) if source == "all" else [source]
    output: list[dict[str, object]] = []
    failures = 0

    for raw_symbol in _split_symbols(raw_symbols):
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError as exc:
            failures += 1
            if as_json:
                output.append({"input": raw_symbol, "error": str(exc)})
            else:
                print(f"输入错误：{exc}", file=sys.stderr)
            continue

        quotes, errors = fetch_quotes(symbol, provider_names, timeout)
        if not quotes:
            failures += 1
        if as_json:
            output.append(quote_as_json(symbol, quotes, errors))
        else:
            print_quotes(symbol, quotes, errors)

    if as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def interactive(source: str, timeout: float, as_json: bool) -> int:
    print("输入美股代码（如 AAPL）或港股代码（如 00700、HK.09988）。")
    print("可一次输入多个代码，用空格或逗号分隔；输入 q 退出。")
    exit_code = 0
    while True:
        try:
            value = input("\n股票代码> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return exit_code
        if value.lower() in {"q", "quit", "exit"}:
            return exit_code
        if not value:
            continue
        exit_code = max(exit_code, run_once([value], source, timeout, as_json))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 Yahoo Finance、腾讯自选股和新浪财经查询美股/港股公开行情。"
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        help="股票代码；例如 AAPL、US.TSLA、00700、HK.09988",
    )
    parser.add_argument(
        "--source",
        choices=["all", "yahoo", "tencent", "sina"],
        default="all",
        help="指定数据源，默认 all",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--timeout", type=float, default=12.0, help="单次网络请求超时秒数")
    return parser


def main() -> int:
    _configure_console_encoding()
    args = build_parser().parse_args()
    if args.timeout <= 0:
        print("--timeout 必须大于 0", file=sys.stderr)
        return 2
    if args.symbols:
        return run_once(args.symbols, args.source, args.timeout, args.json)
    return interactive(args.source, args.timeout, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
