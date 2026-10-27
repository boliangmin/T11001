#!/usr/bin/env python3
"""美股 / 港股公开行情桌面界面。"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import msvcrt
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter import font as tkfont

from google_drive_sync import CloudSnapshotInfo, GoogleDriveSyncClient
from stock_price import (
    ExchangeRates,
    PriceQuote,
    StockSearchResult,
    StockSymbol,
    choose_freshest,
    fetch_exchange_rates,
    fetch_quotes,
    normalize_symbol,
    search_stocks,
)


PRIMARY_PROVIDER_BY_MARKET = {"US": "yahoo", "HK": "tencent"}
FALLBACK_PROVIDERS_BY_MARKET = {
    "US": ["tencent", "sina"],
    "HK": ["yahoo", "sina"],
}
REFRESH_REQUEST_TIMEOUT_SECONDS = 2.5
REFRESH_EXCHANGE_RATE_TIMEOUT_SECONDS = 8.0
REFRESH_MAX_WORKERS = 8
MARKET_ORDER = ("US", "HK")
MARKET_TITLES = {"US": "美股", "HK": "港股"}
STATISTICS_PAGE = "STATS"
TABLE_COLUMNS = (
    "code",
    "name",
    "price",
    "change",
    "holding",
    "cost",
    "market_value",
    "profit",
    "return",
)
TABLE_HEADINGS = {
    "code": "股票代码",
    "name": "中文名称",
    "price": "最新价格",
    "change": "涨跌幅",
    "holding": "持仓数",
    "cost": "摊薄成本",
    "market_value": "总市值",
    "profit": "盈亏",
    "return": "累计收益率",
}
SORTABLE_MARKET_COLUMNS = {"market_value", "profit", "return"}
DEFAULT_COLUMN_WIDTHS = {
    "code": 95,
    "name": 135,
    "price": 110,
    "change": 75,
    "holding": 80,
    "cost": 100,
    "market_value": 120,
    "profit": 125,
    "return": 95,
}
MAIN_WINDOW_SIZE = (1256, 768)
PASSWORD_WINDOW_SIZE = (380, 210)
TITLE_ICON_MENU_UPLOAD = 0x5102
TITLE_ICON_MENU_DOWNLOAD = 0x5103
TITLE_ICON_SUBCLASS_ID = 0x54564D4E
MIN_MARKET_COLUMN_WIDTH = 60
MARKET_ROW_HEIGHT = 32
MARKET_SPACER_PREFIX = "__market_spacer__"
APP_BACKGROUND = "#F4F7FB"
SURFACE = "#FFFFFF"
SURFACE_MUTED = "#EAF0F8"
TEXT_PRIMARY = "#000000"
TEXT_SECONDARY = TEXT_PRIMARY
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
ACCENT_SOFT = "#DBEAFE"
TAB_SELECTED_BACKGROUND = "#8DB600"
BUY_COLOR = "#DC2626"
SELL_COLOR = "#059669"
HIGH_RETURN_BACKGROUND = "#FEF3C7"
HIGH_RETURN_THRESHOLD = Decimal("0.20")
DIVIDER = "#D7E0EC"
STATISTICS_BACKGROUND = "#F7F4EE"
STATISTICS_CNY_CARD = "#F3E6D2"
STATISTICS_CARD = "#FFFDFC"
STATISTICS_BORDER = "#E4D8C7"
STATISTICS_ACCENT = "#B78B58"
STATISTICS_TEXT = "#2F2922"
TRADE_COLUMNS = (
    "trade_date",
    "trade_type",
    "price",
    "quantity",
    "profit",
    "return",
    "remark",
)
TRADE_HEADINGS = {
    "trade_date": "交易日期",
    "trade_type": "交易类型",
    "price": "交易价格",
    "quantity": "交易份额",
    "profit": "盈亏",
    "return": "收益率",
    "remark": "备注",
}
DEFAULT_TRADE_COLUMN_WIDTHS = {
    "trade_date": 100,
    "trade_type": 70,
    "price": 105,
    "quantity": 80,
    "profit": 125,
    "return": 85,
    "remark": 220,
}
TRADE_WINDOW_DEFAULT_SIZE = (880, 430)
TRADE_WINDOW_MIN_SIZE = (720, 340)
SORTABLE_TRADE_COLUMNS = {"trade_date", "profit", "return"}
T_TRADE_ITEM_PREFIX = "t-trade-"
STATE_VERSION = 4
AUTO_REFRESH_INTERVAL_MS = 90 * 1000
SURGE_ALERT_WINDOW_SECONDS = 4 * 60
SURGE_ALERT_THRESHOLD = Decimal("0.10")
APP_ICON_FILENAME = "trade_view.ico"
WINDOWS_APP_USER_MODEL_ID = "Blmcc.TradeView.1.0"
PASSWORD_HASH_ITERATIONS = 310_000
PASSWORD_HASH_SALT = bytes.fromhex("9f3b7d2c4a8165e0b1d6c8f2047a9e35")
PASSWORD_HASH_DIGEST = bytes.fromhex(
    "5943ce25a58ba7a6b4a0a555a2b9035736cc74f62bc414c60eea897b8d8e7259"
)
PUT_WARRANT_MATURITY_PATTERN = re.compile(r"@EP(\d{2})(0[1-9]|1[0-2])", re.I)
PUT_WARRANT_CHINESE_PATTERN = re.compile(
    r"([零〇一二三四五六七八九])([一二三四五六七八九十甲乙])沽"
)
CHINESE_WARRANT_DIGITS = "零一二三四五六七八九"
CHINESE_WARRANT_MONTHS = "一二三四五六七八九十甲乙"


def _verify_password(password: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        PASSWORD_HASH_SALT,
        PASSWORD_HASH_ITERATIONS,
    )
    return hmac.compare_digest(candidate, PASSWORD_HASH_DIGEST)


def _resource_path(filename: str) -> Path:
    candidates: list[Path] = []
    bundle_directory = getattr(sys, "_MEIPASS", None)
    if bundle_directory:
        candidates.append(Path(bundle_directory))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path(__file__).resolve().parent)

    for directory in candidates:
        path = directory / filename
        if path.is_file():
            return path
    return candidates[-1] / filename


def _configure_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            WINDOWS_APP_USER_MODEL_ID
        )
    except (AttributeError, OSError):
        pass


def _apply_window_icon(window: tk.Misc) -> None:
    icon_path = _resource_path(APP_ICON_FILENAME)
    if not icon_path.is_file():
        return
    try:
        window.iconbitmap(default=str(icon_path))
        window.iconbitmap(str(icon_path))
    except (OSError, tk.TclError):
        pass


def _lock_main_window_controls(window: tk.Tk) -> None:
    window.resizable(False, False)
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        user32 = ctypes.windll.user32
        widget_handle = window.winfo_id()
        top_level_handle = user32.GetParent(widget_handle) or widget_handle
        style = user32.GetWindowLongW(top_level_handle, -16)
        style |= 0x00020000
        style &= ~(0x00010000 | 0x00040000)
        user32.SetWindowLongW(top_level_handle, -16, style)
        user32.SetWindowPos(
            top_level_handle,
            0,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020,
        )
    except (AttributeError, OSError, tk.TclError):
        pass


def _fit_column_widths_to_total(
    widths: dict[str, int],
    target_total: int,
) -> dict[str, int]:
    fitted = {
        column: max(MIN_MARKET_COLUMN_WIDTH, int(widths[column]))
        for column in TABLE_COLUMNS
    }
    current_total = sum(fitted.values())
    minimum_total = MIN_MARKET_COLUMN_WIDTH * len(TABLE_COLUMNS)
    target_total = max(minimum_total, int(target_total))
    if current_total <= target_total:
        return fitted

    available_extra = target_total - minimum_total
    original_extra = {
        column: fitted[column] - MIN_MARKET_COLUMN_WIDTH
        for column in TABLE_COLUMNS
    }
    original_extra_total = sum(original_extra.values())
    if original_extra_total <= 0:
        return fitted

    scaled_extra = {
        column: original_extra[column] * available_extra / original_extra_total
        for column in TABLE_COLUMNS
    }
    allocated_extra = {
        column: int(scaled_extra[column]) for column in TABLE_COLUMNS
    }
    remainder = available_extra - sum(allocated_extra.values())
    for column in sorted(
        TABLE_COLUMNS,
        key=lambda item: scaled_extra[item] - allocated_extra[item],
        reverse=True,
    )[:remainder]:
        allocated_extra[column] += 1

    return {
        column: MIN_MARKET_COLUMN_WIDTH + allocated_extra[column]
        for column in TABLE_COLUMNS
    }


def _default_state_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().with_name("state.json")
    return Path(__file__).resolve().with_name("state.json")


def _contains_chinese(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _put_warrant_maturity_month(
    quotes: list[PriceQuote],
) -> tuple[int, int] | None:
    for quote in quotes:
        match = PUT_WARRANT_MATURITY_PATTERN.search(quote.name.strip())
        if match is not None:
            return 2000 + int(match.group(1)), int(match.group(2))
    current_year = date.today().year
    for quote in quotes:
        match = PUT_WARRANT_CHINESE_PATTERN.search(quote.name.strip())
        if match is None:
            continue
        year_digit = (
            0
            if match.group(1) == "〇"
            else CHINESE_WARRANT_DIGITS.index(match.group(1))
        )
        year = current_year // 10 * 10 + year_digit
        if year < current_year - 1:
            year += 10
        if year > current_year + 5:
            continue
        month = CHINESE_WARRANT_MONTHS.index(match.group(2)) + 1
        return year, month
    return None


def _format_remaining_validity(
    maturity: tuple[int, int],
    current_date: date | None = None,
) -> str:
    year, month = maturity
    today = current_date or date.today()
    remaining_months = (year - today.year) * 12 + month - today.month
    if remaining_months < 0:
        remaining_text = "已到期"
    elif remaining_months == 0:
        remaining_text = "本月到期"
    else:
        remaining_text = f"约{remaining_months}个月"
    return f"剩余有效期：{remaining_text}（{year}年{month}月到期）"


def _metric_color(value: str) -> str:
    match = re.match(r"^\s*([+-]?\d[\d,]*(?:\.\d+)?)", value)
    if match is None:
        return TEXT_PRIMARY
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return TEXT_PRIMARY
    if number > 0:
        return BUY_COLOR
    if number < 0:
        return SELL_COLOR
    return TEXT_PRIMARY


def _center_and_show(
    window: tk.Toplevel | tk.Tk,
    owner: tk.Misc | None = None,
    fit_content: bool = True,
) -> None:
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    if fit_content:
        width = max(width, window.winfo_reqwidth())
        height = max(height, window.winfo_reqheight())
    if owner is None:
        x = max(0, (window.winfo_screenwidth() - width) // 2)
        y = max(0, (window.winfo_screenheight() - height) // 2)
    else:
        owner.update_idletasks()
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.deiconify()


@dataclass(frozen=True)
class TradeRecord:
    trade_date: str
    price: str
    quantity: str
    trade_type: str


@dataclass(frozen=True)
class CashFlowRecord:
    flow_date: str
    cny_amount: str

    @classmethod
    def from_values(cls, flow_date: str, cny_amount: str) -> CashFlowRecord:
        if not all(isinstance(value, str) for value in (flow_date, cny_amount)):
            raise ValueError("入金记录字段格式无效。")
        try:
            parsed_date = datetime.strptime(flow_date.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("请通过日历选择有效的日期。") from exc
        text = cny_amount.strip().replace(",", "")
        if not re.fullmatch(r"[0-9]{1,15}(?:\.[0-9]{1,2})?", text):
            raise ValueError("人民币金额须为正数，最多15位整数、2位小数。")
        value = Decimal(text)
        if value <= 0:
            raise ValueError("人民币金额必须大于0。")
        return cls(parsed_date.isoformat(), format(value, "f"))

    def to_payload(self) -> dict[str, str]:
        return {"flow_date": self.flow_date, "cny_amount": self.cny_amount}


@dataclass(frozen=True)
class TTradeGroup:
    group_id: str
    record_indices: tuple[int, ...]


@dataclass(frozen=True)
class TTradeSummary:
    trade_date: str
    realized_profit: Decimal
    realized_return: Decimal
    residual_record: TradeRecord | None
    record_count: int


@dataclass(frozen=True)
class MarketMetrics:
    currency: str
    has_records: bool
    complete: bool
    market_value: Decimal
    profit: Decimal
    return_rate: Decimal | None


def _calculate_cny_total_market_value(
    us_metrics: MarketMetrics,
    hk_metrics: MarketMetrics,
    rates: ExchangeRates | None,
) -> Decimal | None:
    if rates is None or not us_metrics.complete or not hk_metrics.complete:
        return None
    return (
        us_metrics.market_value * rates.usd_to_cny
        + hk_metrics.market_value * rates.hkd_to_cny
    )


class AlreadyRunningError(RuntimeError):
    """同一份本地状态文件已经由另一个窗口占用。"""


class PasswordDialog(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        _apply_window_icon(self)
        self.title("我钱呢")
        self.configure(background=APP_BACKGROUND)
        self.geometry(f"{PASSWORD_WINDOW_SIZE[0]}x{PASSWORD_WINDOW_SIZE[1]}")
        self.resizable(False, False)
        self.authenticated = False
        self.password_var = tk.StringVar()
        self.error_var = tk.StringVar()

        content = tk.Frame(self, background=APP_BACKGROUND, padx=36, pady=28)
        content.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            content,
            text="请输入密码",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor=tk.W)
        self.password_entry = tk.Entry(
            content,
            textvariable=self.password_var,
            show="*",
            font=("Microsoft YaHei UI", 11),
            relief=tk.SOLID,
            borderwidth=1,
        )
        self.password_entry.pack(fill=tk.X, pady=(12, 6), ipady=7)
        self.password_entry.bind("<FocusIn>", self._use_english_input, add="+")
        tk.Label(
            content,
            textvariable=self.error_var,
            background=APP_BACKGROUND,
            foreground=BUY_COLOR,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W)
        tk.Button(
            content,
            text="进入",
            command=self._submit,
            background=ACCENT,
            foreground=SURFACE,
            activebackground=ACCENT_HOVER,
            activeforeground=SURFACE,
            relief=tk.FLAT,
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
            cursor="hand2",
        ).pack(fill=tk.X, pady=(10, 0), ipady=6)
        self.bind("<Return>", self._submit)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _use_english_input(self, _event: tk.Event | None = None) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            imm32 = ctypes.windll.imm32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            imm32.ImmAssociateContextEx.argtypes = [
                wintypes.HWND, wintypes.HANDLE, wintypes.DWORD
            ]
            imm32.ImmAssociateContextEx.restype = wintypes.BOOL

            self.password_entry.winfo_id()
            window_handle = self.winfo_id()
            top_level_handle = user32.GetParent(window_handle) or window_handle
            # Tk's native focus can stay on the top-level wrapper. Disable IME
            # for this password window and its children, not the whole thread.
            imm32.ImmAssociateContextEx(top_level_handle, None, 0)
            imm32.ImmAssociateContextEx(top_level_handle, None, 0x0001)  # IACE_CHILDREN
        except (AttributeError, OSError, tk.TclError):
            pass

    def _submit(self, _event: tk.Event | None = None) -> None:
        if _verify_password(self.password_var.get()):
            self.authenticated = True
            self.destroy()
            return
        self.error_var.set("密码错误")
        self.password_var.set("")
        self.password_entry.focus_set()

    def run(self) -> bool:
        _center_and_show(self, fit_content=False)
        self._use_english_input()
        self.password_entry.focus_set()
        self.mainloop()
        return self.authenticated


class CalendarPopup(tk.Toplevel):
    def __init__(
        self,
        owner: tk.Toplevel,
        selected_date: str,
        on_select: Callable[[str], None],
    ) -> None:
        super().__init__(owner)
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self._on_select = on_select
        try:
            initial_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            initial_date = date.today()
        self._selected_date = initial_date
        self._year = initial_date.year
        self._month = initial_date.month

        self.title("选择交易日期")
        self.resizable(False, False)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self._close)

        outer = ttk.Frame(self, padding=(16, 14, 16, 14))
        outer.pack(fill=tk.BOTH, expand=True)
        year_row = ttk.Frame(outer)
        year_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(
            year_row,
            text="« 上一年",
            width=10,
            command=lambda: self._change_year(-1),
            style="Action.TButton",
            takefocus=False,
        ).pack(side=tk.LEFT)
        ttk.Button(
            year_row,
            text="下一年 »",
            width=10,
            command=lambda: self._change_year(1),
            style="Action.TButton",
            takefocus=False,
        ).pack(side=tk.RIGHT)
        self.year_label = ttk.Label(
            year_row,
            anchor=tk.CENTER,
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.year_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        months_row = ttk.Frame(outer)
        months_row.pack(fill=tk.X, pady=(0, 12))
        self._month_buttons: list[ttk.Button] = []
        for index in range(12):
            button = ttk.Button(
                months_row,
                text=f"{index + 1}月",
                width=-4,
                command=lambda month=index + 1: self._show_month(month),
                style="CalendarMonth.TButton",
                takefocus=False,
            )
            button.grid(row=0, column=index, padx=1, sticky="ew")
            months_row.columnconfigure(index, weight=1, uniform="month")
            self._month_buttons.append(button)

        self.days_frame = ttk.Frame(outer)
        self.days_frame.pack(fill=tk.BOTH, expand=True)
        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="今天", command=self._select_today).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="确认",
            command=self._confirm,
            style="Primary.TButton",
            takefocus=False,
        ).pack(side=tk.RIGHT)
        self.selected_label = ttk.Label(
            footer,
            anchor=tk.CENTER,
            style="Hint.TLabel",
        )
        self.selected_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.bind("<Return>", lambda _event: self._confirm())
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<MouseWheel>", self._scroll_month)
        self._render()
        _center_and_show(self, owner)
        self.grab_set()

    def _render(self) -> None:
        self.year_label.configure(text=f"{self._year} 年")
        for month, button in enumerate(self._month_buttons, start=1):
            button.configure(
                style=(
                    "SelectedCalendarMonth.TButton"
                    if month == self._month
                    else "CalendarMonth.TButton"
                )
            )
        self.selected_label.configure(
            text=f"已选：{self._selected_date.isoformat()}"
        )
        self._render_days()

    def _change_year(self, offset: int) -> None:
        year = self._year + offset
        if not 1900 <= year <= 2100:
            return
        self._year = year
        self._render()

    def _show_month(self, month: int) -> None:
        self._month = month
        self._render()

    def _change_month(self, offset: int) -> None:
        month_index = self._year * 12 + self._month - 1 + offset
        year, zero_based_month = divmod(month_index, 12)
        if not 1900 <= year <= 2100:
            return
        self._year = year
        self._month = zero_based_month + 1
        self._render()

    def _scroll_month(self, event: tk.Event) -> str:
        if event.delta:
            self._change_month(-1 if event.delta > 0 else 1)
        return "break"

    def _render_days(self) -> None:
        for widget in self.days_frame.winfo_children():
            widget.destroy()
        for column, weekday in enumerate(
            ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        ):
            ttk.Label(
                self.days_frame,
                text=weekday,
                anchor=tk.CENTER,
                style="Weekend.TLabel" if column >= 5 else "TLabel",
            ).grid(
                row=0,
                column=column,
                padx=2,
                pady=2,
                sticky="ew",
            )
            self.days_frame.columnconfigure(column, weight=1)

        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._year,
            self._month,
        )
        # The popup keeps the size it opened with, so every month renders six
        # equally tall week rows and the footer buttons never shift or clip.
        weeks += [[0] * 7 for _ in range(6 - len(weeks))]
        for row, week in enumerate(weeks, start=1):
            self.days_frame.rowconfigure(row, uniform="week")
            for column, day_number in enumerate(week):
                if not day_number:
                    ttk.Label(self.days_frame, text="", width=4).grid(
                        row=row,
                        column=column,
                        padx=2,
                        pady=2,
                    )
                    continue
                is_selected = (
                    self._selected_date.year == self._year
                    and self._selected_date.month == self._month
                    and self._selected_date.day == day_number
                )
                ttk.Button(
                    self.days_frame,
                    text=str(day_number),
                    width=5,
                    style="SelectedDay.TButton" if is_selected else "TButton",
                    command=lambda day=day_number: self._select_day(day),
                ).grid(row=row, column=column, padx=2, pady=2)

    def _select_day(self, day_number: int) -> None:
        self._selected_date = date(self._year, self._month, day_number)
        self._render()

    def _select_today(self) -> None:
        today = date.today()
        self._selected_date = today
        self._year = today.year
        self._month = today.month
        self._render()

    def _confirm(self) -> None:
        self._on_select(self._selected_date.isoformat())
        self._close()

    def _close(self) -> None:
        owner = self.master
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        try:
            if owner.winfo_exists():
                owner.grab_set()
        except tk.TclError:
            pass


class TradeEntryDialog(tk.Toplevel):
    def __init__(
        self,
        owner: tk.Toplevel,
        currency: str,
        on_save: Callable[[TradeRecord], bool],
        initial_record: TradeRecord | None = None,
    ) -> None:
        super().__init__(owner)
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self._on_save = on_save
        self.title("修改交易" if initial_record is not None else "增加交易")
        self.resizable(False, False)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        content = ttk.Frame(self, padding=(22, 18, 22, 16))
        content.pack(fill=tk.BOTH, expand=True)

        ttk.Label(content, text="交易日期").grid(
            row=0,
            column=0,
            sticky=tk.W,
            pady=(0, 12),
        )
        self.date_var = tk.StringVar(
            value=(
                initial_record.trade_date
                if initial_record is not None
                else date.today().isoformat()
            )
        )
        self.date_entry = ttk.Entry(
            content,
            textvariable=self.date_var,
            width=28,
            state="readonly",
            cursor="hand2",
        )
        self.date_entry.grid(row=0, column=1, sticky=tk.EW, pady=(0, 12))
        self.date_entry.bind("<Button-1>", self._open_calendar)

        ttk.Label(content, text="交易类型").grid(
            row=1,
            column=0,
            sticky=tk.W,
            pady=(0, 12),
        )
        initial_type = (
            initial_record.trade_type
            if initial_record is not None
            and initial_record.trade_type in {"买入", "卖出"}
            else "买入"
        )
        self.trade_type_var = tk.StringVar(value=initial_type)
        type_controls = ttk.Frame(content)
        type_controls.grid(row=1, column=1, sticky=tk.W, pady=(0, 12))
        ttk.Radiobutton(
            type_controls,
            text="买入",
            value="买入",
            variable=self.trade_type_var,
            style="Buy.TRadiobutton",
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Radiobutton(
            type_controls,
            text="卖出",
            value="卖出",
            variable=self.trade_type_var,
            style="Sell.TRadiobutton",
        ).pack(side=tk.LEFT)

        ttk.Label(content, text="交易份额").grid(
            row=2,
            column=0,
            sticky=tk.W,
            pady=(0, 12),
        )
        initial_quantity = ""
        if initial_record is not None and initial_record.quantity != "--":
            initial_quantity = initial_record.quantity
        self.quantity_var = tk.StringVar(value=initial_quantity)
        self.quantity_entry = ttk.Entry(
            content,
            textvariable=self.quantity_var,
            width=28,
            validate="key",
            validatecommand=(
                self.register(self._validate_quantity_input),
                "%P",
            ),
        )
        self.quantity_entry.grid(row=2, column=1, sticky=tk.EW, pady=(0, 12))

        ttk.Label(content, text=f"交易价格（{currency}）").grid(
            row=3,
            column=0,
            sticky=tk.W,
            pady=(0, 16),
        )
        self.price_var = tk.StringVar(
            value=initial_record.price if initial_record is not None else ""
        )
        self.price_entry = ttk.Entry(
            content,
            textvariable=self.price_var,
            width=28,
        )
        self.price_entry.grid(row=3, column=1, sticky=tk.EW, pady=(0, 16))

        buttons = ttk.Frame(content)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.LEFT,
            padx=(0, 8),
        )
        ttk.Button(
            buttons,
            text="保存",
            command=self._submit,
            style="Primary.TButton",
        ).pack(side=tk.LEFT)
        content.columnconfigure(1, weight=1)

        self.bind("<Return>", lambda _event: self._submit())
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_and_show(self, owner)
        self.grab_set()
        self.quantity_entry.focus_set()

    def _open_calendar(self, _event: tk.Event | None = None) -> str:
        CalendarPopup(self, self.date_var.get(), self.date_var.set)
        return "break"

    def _validate_quantity_input(self, proposed_value: str) -> bool:
        return proposed_value == "" or (
            proposed_value.isascii() and proposed_value.isdigit()
        )

    def _submit(self) -> None:
        raw_date = self.date_var.get().strip()
        try:
            parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror(
                "交易日期错误",
                "请通过日历选择有效的交易日期。",
                parent=self,
            )
            return

        raw_quantity = self.quantity_var.get().strip()
        if not (
            raw_quantity
            and raw_quantity.isascii()
            and raw_quantity.isdigit()
            and int(raw_quantity) > 0
        ):
            messagebox.showerror(
                "交易份额错误",
                "交易份额必须是大于 0 的整数。",
                parent=self,
            )
            self.quantity_entry.focus_set()
            return

        raw_price = self.price_var.get().strip().replace(",", "")
        try:
            price_value = Decimal(raw_price)
        except InvalidOperation:
            price_value = Decimal(0)
        if not price_value.is_finite() or price_value <= 0:
            messagebox.showerror(
                "交易价格错误",
                "请输入大于 0 的有效交易价格。",
                parent=self,
            )
            self.price_entry.focus_set()
            return

        normalized_price = format(price_value.normalize(), "f")
        normalized_quantity = str(int(raw_quantity))
        record = TradeRecord(
            trade_date=parsed_date.isoformat(),
            price=normalized_price,
            quantity=normalized_quantity,
            trade_type=self.trade_type_var.get(),
        )
        if self._on_save(record):
            self.destroy()


class StockTradeWindow(tk.Toplevel):
    def __init__(
        self,
        owner: "StockPriceApp",
        symbol: StockSymbol,
        display_name: str,
    ) -> None:
        super().__init__(owner)
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self.owner = owner
        self.symbol = symbol
        self.currency = "USD" if symbol.market == "US" else "HKD"
        self._sort_column: str | None = None
        self._sort_descending = False
        self._trade_cell_labels: dict[tuple[str, str], tk.Label] = {}
        self._overlay_after_id: str | None = None
        self._paired_column_resize_start: (
            tuple[ttk.Treeview, int, str, str, int, int] | None
        ) = None
        self.title(f"交易记录 - {display_name or symbol.canonical}")
        trade_width, trade_height = owner.trade_window_size
        self.geometry(f"{trade_width}x{trade_height}")
        self.minsize(*TRADE_WINDOW_MIN_SIZE)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self._close)

        outer = ttk.Frame(self, padding=(20, 18, 20, 16))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 14))
        title_area = ttk.Frame(header)
        title_area.pack(side=tk.LEFT, fill=tk.X, expand=True)

        metrics = ttk.Frame(title_area)
        metrics.pack(anchor=tk.W, pady=(8, 0))
        self.holding_summary_var = tk.StringVar(value="--")
        self.profit_summary_var = tk.StringVar(value="--")
        self.return_summary_var = tk.StringVar(value="--")
        ttk.Label(metrics, text="持仓数").pack(side=tk.LEFT)
        ttk.Label(
            metrics,
            textvariable=self.holding_summary_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(8, 24))
        ttk.Label(metrics, text="总盈亏").pack(side=tk.LEFT)
        self.profit_summary_label = ttk.Label(
            metrics,
            textvariable=self.profit_summary_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.profit_summary_label.pack(side=tk.LEFT, padx=(8, 28))
        ttk.Label(metrics, text="总收益率").pack(side=tk.LEFT)
        self.return_summary_label = ttk.Label(
            metrics,
            textvariable=self.return_summary_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.return_summary_label.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(
            header,
            text="增加交易",
            command=self._open_entry_dialog,
            style="Primary.TButton",
        ).pack(side=tk.RIGHT, anchor=tk.N)

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.trade_tree = ttk.Treeview(
            table_frame,
            columns=TRADE_COLUMNS,
            show="headings",
            selectmode="extended",
        )
        for column in TRADE_COLUMNS:
            heading_options: dict[str, object] = {
                "text": TRADE_HEADINGS[column],
                "anchor": tk.CENTER,
            }
            if column in SORTABLE_TRADE_COLUMNS:
                heading_options["command"] = (
                    lambda selected=column: self._sort_records(
                        selected,
                        descending=False,
                    )
                )
            self.trade_tree.heading(column, **heading_options)
            self.trade_tree.column(
                column,
                width=owner.trade_column_widths[column],
                minwidth=60,
                anchor=tk.CENTER,
                stretch=column == "remark",
            )
        self.trade_tree.tag_configure(
            "high_return",
            background=HIGH_RETURN_BACKGROUND,
        )
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self._scroll_trade_tree,
        )
        self._trade_scrollbar = scrollbar
        self.trade_tree.configure(yscrollcommand=self._on_trade_tree_scroll)
        self.trade_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.context_menu = tk.Menu(
            self,
            tearoff=False,
            background=SURFACE,
            foreground=TEXT_PRIMARY,
            activebackground=ACCENT_SOFT,
            activeforeground=TEXT_PRIMARY,
            borderwidth=1,
            relief=tk.FLAT,
        )
        self.trade_tree.bind("<Button-1>", self._clear_selection_on_blank)
        self.trade_tree.bind(
            "<ButtonPress-1>",
            self._begin_column_resize,
            add="+",
        )
        self.trade_tree.bind(
            "<B1-Motion>",
            self._resize_adjacent_columns,
            add="+",
        )
        self.trade_tree.bind("<Button-3>", self._show_context_menu)
        self.trade_tree.bind(
            "<<TreeviewSelect>>",
            self._schedule_trade_cell_labels,
        )
        self.trade_tree.bind(
            "<Configure>",
            self._schedule_trade_cell_labels,
            add="+",
        )
        self.trade_tree.bind(
            "<ButtonRelease-1>",
            self._schedule_trade_cell_labels,
            add="+",
        )
        self.trade_tree.bind(
            "<ButtonRelease-1>",
            self._remember_layout,
            add="+",
        )
        self.trade_tree.bind(
            "<ButtonRelease-1>",
            self._finish_column_resize,
            add="+",
        )
        self.bind("<Configure>", self._remember_layout, add="+")

        ttk.Label(
            outer,
            text=(
                "右键单笔记录可修改或删除；Ctrl+左键可多选买卖记录并执行 T 操作；"
                "行情接口价格不会作为交易记录保存。"
            ),
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))
        self._reload_records()
        _center_and_show(self, owner, fit_content=False)

    def _remember_layout(self, _event: tk.Event | None = None) -> None:
        self.owner._capture_trade_window_layout(self)

    def _begin_column_resize(self, event: tk.Event) -> str | None:
        if self.trade_tree.identify_region(event.x, event.y) != "separator":
            self._paired_column_resize_start = None
            return None

        column_id = self.trade_tree.identify_column(event.x)
        try:
            column_index = int(column_id.removeprefix("#")) - 1
        except ValueError:
            self._paired_column_resize_start = None
            return None
        if not 0 <= column_index < len(TRADE_COLUMNS) - 1:
            self._paired_column_resize_start = None
            return None

        current_column = TRADE_COLUMNS[column_index]
        right_column = TRADE_COLUMNS[column_index + 1]
        self._paired_column_resize_start = (
            self.trade_tree,
            event.x_root,
            current_column,
            right_column,
            int(self.trade_tree.column(current_column, "width")),
            int(self.trade_tree.column(right_column, "width")),
        )
        return "break"

    def _resize_adjacent_columns(self, event: tk.Event) -> str | None:
        resize_start = self._paired_column_resize_start
        if resize_start is None or event.widget is not resize_start[0]:
            return None

        (
            tree,
            start_x,
            current_column,
            right_column,
            current_width,
            right_width,
        ) = resize_start
        current_minimum = int(tree.column(current_column, "minwidth"))
        right_minimum = int(tree.column(right_column, "minwidth"))
        requested_delta = event.x_root - start_x
        delta = max(
            current_minimum - current_width,
            min(right_width - right_minimum, requested_delta),
        )
        tree.column(current_column, width=current_width + delta)
        tree.column(right_column, width=right_width - delta)
        self._schedule_trade_cell_labels()
        return "break"

    def _finish_column_resize(self, event: tk.Event) -> str | None:
        resize_start = self._paired_column_resize_start
        self._paired_column_resize_start = None
        if resize_start is None or event.widget is not resize_start[0]:
            return None

        self.owner._capture_trade_window_layout(self)
        self.owner._save_state()
        self._schedule_trade_cell_labels()
        return "break"

    def _reload_records(self) -> None:
        self.trade_tree.delete(*self.trade_tree.get_children(""))
        groups_by_first_index: dict[int, tuple[TTradeGroup, tuple, tuple[str, ...]]] = {}
        grouped_indices: set[int] = set()
        for group in self.owner.t_trade_groups.get(self.symbol.canonical, []):
            row = self._t_trade_row(group)
            if row is None:
                continue
            groups_by_first_index[group.record_indices[0]] = (group, row[0], row[1])
            grouped_indices.update(group.record_indices)

        records = self.owner.trades.get(self.symbol.canonical, [])
        for index, record in enumerate(records):
            group_row = groups_by_first_index.get(index)
            if group_row is not None:
                group, values, tags = group_row
                self.trade_tree.insert(
                    "",
                    tk.END,
                    iid=f"{T_TRADE_ITEM_PREFIX}{group.group_id}",
                    values=values,
                    tags=tags,
                )
                continue
            if index in grouped_indices:
                continue
            profit, return_rate = self.owner._calculate_trade_performance(
                self.symbol,
                record,
                index,
            )
            self.trade_tree.insert(
                "",
                tk.END,
                iid=f"trade-{index}",
                values=(
                    record.trade_date,
                    record.trade_type,
                    f"{record.price} {self.currency}",
                    record.quantity,
                    profit,
                    return_rate,
                    self.owner._sell_cost_remark(self.symbol, record, index),
                ),
                tags=self._trade_row_tags(record, index),
            )
        self.refresh_summary()

    def _t_trade_row(
        self,
        group: TTradeGroup,
    ) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        summary = self.owner._calculate_t_trade_summary(self.symbol, group)
        if summary is None:
            return None

        residual = summary.residual_record
        if residual is None:
            trade_type = "T交易"
            price = "--"
            quantity = "0"
            profit = "--"
            return_rate = "--"
            tags: tuple[str, ...] = ()
            cost_remark = ""
        else:
            trade_type = residual.trade_type
            price = f"{Decimal(residual.price):,.3f} {self.currency}"
            quantity = residual.quantity
            # A residual sale is realized against the holding cost before the group.
            records = self.owner.trades.get(self.symbol.canonical, [])
            anchor_index = min(
                group.record_indices,
                key=lambda index: (records[index].trade_date, index),
            )
            profit, return_rate = self.owner._calculate_trade_performance(
                self.symbol,
                residual,
                anchor_index,
            )
            tags = self._trade_row_tags(residual, anchor_index)
            cost_remark = self.owner._sell_cost_remark(
                self.symbol,
                residual,
                anchor_index,
            )

        remark = (
            f"T交易｜合并 {summary.record_count} 笔｜"
            f"获利 {summary.realized_profit:+,.2f} {self.currency}"
        )
        if cost_remark:
            remark += f"｜{cost_remark}"
        return (
            (
                summary.trade_date,
                trade_type,
                price,
                quantity,
                profit,
                return_rate,
                remark,
            ),
            tags,
        )

    def refresh_summary(self) -> None:
        holding_count, _, _, profit, return_rate = (
            self.owner._calculate_position_metrics(self.symbol)
        )
        self.holding_summary_var.set(holding_count)
        self.profit_summary_var.set(profit)
        self.return_summary_var.set(return_rate)
        self.profit_summary_label.configure(foreground=TEXT_PRIMARY)
        self.return_summary_label.configure(foreground=TEXT_PRIMARY)
        records = self.owner.trades.get(self.symbol.canonical, [])
        for index, record in enumerate(records):
            item_id = f"trade-{index}"
            if not self.trade_tree.exists(item_id):
                continue
            values = list(self.trade_tree.item(item_id, "values"))
            if len(values) != len(TRADE_COLUMNS):
                continue
            values[4], values[5] = self.owner._calculate_trade_performance(
                self.symbol,
                record,
                index,
            )
            values[6] = self.owner._sell_cost_remark(self.symbol, record, index)
            self.trade_tree.item(
                item_id,
                values=values,
                tags=self._trade_row_tags(record, index),
            )
        for group in self.owner.t_trade_groups.get(self.symbol.canonical, []):
            item_id = f"{T_TRADE_ITEM_PREFIX}{group.group_id}"
            if not self.trade_tree.exists(item_id):
                continue
            row = self._t_trade_row(group)
            if row is not None:
                self.trade_tree.item(item_id, values=row[0], tags=row[1])
        if self._sort_column is not None:
            self._sort_records(self._sort_column, self._sort_descending)
        self._schedule_trade_cell_labels()

    def _trade_row_tags(
        self,
        record: TradeRecord,
        anchor_index: int,
    ) -> tuple[str, ...]:
        tags: list[str] = []
        if self.owner._trade_return_exceeds_threshold(
            self.symbol,
            record,
            anchor_index,
        ):
            tags.append("high_return")
        return tuple(tags)

    def _sort_records(self, column: str, descending: bool) -> None:
        if column not in SORTABLE_TRADE_COLUMNS:
            return

        records = self.owner.trades.get(self.symbol.canonical, [])
        sortable_items: list[tuple[str, str | Decimal]] = []
        missing_items: list[str] = []
        for item_id in self.trade_tree.get_children(""):
            if item_id.startswith(T_TRADE_ITEM_PREFIX):
                displayed_value = self.trade_tree.set(item_id, column)
                if column == "trade_date":
                    sortable_items.append((item_id, displayed_value))
                    continue
                match = re.match(
                    r"^\s*([+-]?\d[\d,]*(?:\.\d+)?)",
                    displayed_value,
                )
                if match is None:
                    missing_items.append(item_id)
                    continue
                sortable_items.append(
                    (
                        item_id,
                        Decimal(match.group(1).replace(",", "")),
                    )
                )
                continue
            if not item_id.startswith("trade-"):
                missing_items.append(item_id)
                continue
            try:
                index = int(item_id.removeprefix("trade-"))
                record = records[index]
            except (ValueError, IndexError):
                missing_items.append(item_id)
                continue

            if column == "trade_date":
                sortable_items.append((item_id, record.trade_date))
                continue

            values = self.owner._calculate_trade_performance_values(
                self.symbol,
                record,
                index,
            )
            if values is None:
                missing_items.append(item_id)
                continue
            value = values[0] if column == "profit" else values[1]
            sortable_items.append((item_id, value))

        sortable_items.sort(key=lambda item: item[1], reverse=descending)
        ordered_items = [item_id for item_id, _value in sortable_items]
        ordered_items.extend(missing_items)
        for position, item_id in enumerate(ordered_items):
            self.trade_tree.move(item_id, "", position)

        self._sort_column = column
        self._sort_descending = descending
        for heading_column in TRADE_COLUMNS:
            heading = TRADE_HEADINGS[heading_column]
            if heading_column == column:
                heading += " ↓" if descending else " ↑"
            self.trade_tree.heading(heading_column, text=heading)
        self._schedule_trade_cell_labels()

    def _column_at(self, x: int) -> str | None:
        column_id = self.trade_tree.identify_column(x)
        if not column_id.startswith("#"):
            return None
        try:
            index = int(column_id[1:]) - 1
        except ValueError:
            return None
        return TRADE_COLUMNS[index] if 0 <= index < len(TRADE_COLUMNS) else None

    @staticmethod
    def _metric_color(value: str) -> str:
        return _metric_color(value)

    def _scroll_trade_tree(self, *args: str) -> None:
        self.trade_tree.yview(*args)
        self._schedule_trade_cell_labels()

    def _on_trade_tree_scroll(self, first: str, last: str) -> None:
        self._trade_scrollbar.set(first, last)
        self._schedule_trade_cell_labels()

    def _schedule_trade_cell_labels(
        self,
        _event: tk.Event | None = None,
    ) -> None:
        if self._overlay_after_id is not None:
            return
        try:
            self._overlay_after_id = self.after_idle(
                self._render_trade_cell_labels
            )
        except tk.TclError:
            self._overlay_after_id = None

    def _render_trade_cell_labels(self) -> None:
        self._overlay_after_id = None
        try:
            if not self.trade_tree.winfo_exists():
                return
        except tk.TclError:
            return

        visible_labels: set[tuple[str, str]] = set()
        selected_items = set(self.trade_tree.selection())
        target_columns = ("profit", "return")
        for item_id in self.trade_tree.get_children(""):
            item_tags = set(self.trade_tree.item(item_id, "tags"))
            background = (
                ACCENT_SOFT
                if item_id in selected_items
                else HIGH_RETURN_BACKGROUND
                if "high_return" in item_tags
                else SURFACE
            )
            for column in target_columns:
                bounds = self.trade_tree.bbox(item_id, column)
                if not bounds:
                    continue
                x, y, width, height = bounds
                key = (item_id, column)
                visible_labels.add(key)
                value = self.trade_tree.set(item_id, column)
                foreground = self._metric_color(value)
                font = ("Microsoft YaHei UI", 10)

                label = self._trade_cell_labels.get(key)
                if label is None:
                    label = tk.Label(
                        self.trade_tree,
                        borderwidth=0,
                        highlightthickness=0,
                        anchor=tk.CENTER,
                        padx=0,
                        pady=0,
                        takefocus=False,
                    )
                    label.bind(
                        "<Button-1>",
                        lambda event, selected=item_id: (
                            self._select_trade_from_overlay(event, selected)
                        ),
                    )
                    label.bind(
                        "<Button-3>",
                        lambda event, selected=item_id: (
                            self._show_overlay_context_menu(event, selected)
                        ),
                    )
                    label.bind("<MouseWheel>", self._scroll_from_overlay)
                    self._trade_cell_labels[key] = label
                label.configure(
                    text=value,
                    foreground=foreground,
                    background=background,
                    font=font,
                )
                label.place(
                    x=x + 1,
                    y=y,
                    width=max(1, width - 2),
                    height=height,
                )
                label.lift()

        for key, label in list(self._trade_cell_labels.items()):
            if key in visible_labels:
                continue
            label.place_forget()
            if not self.trade_tree.exists(key[0]):
                label.destroy()
                self._trade_cell_labels.pop(key, None)

    def _select_trade_from_overlay(self, event: tk.Event, item_id: str) -> str:
        selected_items = set(self.trade_tree.selection())
        if event.state & 0x0004:
            if item_id in selected_items:
                self.trade_tree.selection_remove(item_id)
            else:
                self.trade_tree.selection_add(item_id)
        else:
            self.trade_tree.selection_set(item_id)
        self.trade_tree.focus(item_id)
        self._schedule_trade_cell_labels()
        return "break"

    def _show_overlay_context_menu(
        self,
        event: tk.Event,
        item_id: str,
    ) -> str:
        if item_id not in self.trade_tree.selection():
            self.trade_tree.selection_set(item_id)
        self.trade_tree.focus(item_id)
        self._schedule_trade_cell_labels()
        self._popup_context_menu(event)
        return "break"

    def _scroll_from_overlay(self, event: tk.Event) -> str:
        if event.delta:
            steps = -int(event.delta / 120)
            if steps == 0:
                steps = -1 if event.delta > 0 else 1
            self.trade_tree.yview_scroll(steps, "units")
            self._schedule_trade_cell_labels()
        return "break"

    def _clear_selection_on_blank(self, event: tk.Event) -> None:
        row_id = self.trade_tree.identify_row(event.y)
        region = self.trade_tree.identify_region(event.x, event.y)
        if not row_id and region not in {"heading", "separator"}:
            selected_items = self.trade_tree.selection()
            if selected_items:
                self.trade_tree.selection_remove(*selected_items)
            self.trade_tree.focus("")
            self._schedule_trade_cell_labels()

    def _open_entry_dialog(self) -> None:
        TradeEntryDialog(self, self.currency, self._save_record)

    def _save_record(self, record: TradeRecord) -> bool:
        if not self.owner._add_trade_record(self.symbol, record):
            messagebox.showerror(
                "保存失败",
                "交易记录未能写入本机保存文件，请检查目录权限后重试。",
                parent=self,
            )
            return False
        self._reload_records()
        return True

    def _selected_record_index(self) -> int | None:
        selection = self.trade_tree.selection()
        if len(selection) != 1 or not selection[0].startswith("trade-"):
            return None
        try:
            index = int(selection[0].removeprefix("trade-"))
        except ValueError:
            return None
        records = self.owner.trades.get(self.symbol.canonical, [])
        return index if 0 <= index < len(records) else None

    def _selected_record_indices(self) -> tuple[int, ...]:
        records = self.owner.trades.get(self.symbol.canonical, [])
        indices: list[int] = []
        for item_id in self.trade_tree.selection():
            if not item_id.startswith("trade-"):
                return ()
            try:
                index = int(item_id.removeprefix("trade-"))
            except ValueError:
                return ()
            if not 0 <= index < len(records):
                return ()
            indices.append(index)
        return tuple(sorted(set(indices)))

    def _configure_context_menu(self) -> bool:
        selection = self.trade_tree.selection()
        self.context_menu.delete(0, tk.END)
        if len(selection) == 1 and selection[0].startswith(T_TRADE_ITEM_PREFIX):
            self.context_menu.add_command(
                label="取消T交易",
                command=self._cancel_selected_t_trade,
            )
        elif len(selection) >= 2 and all(
            item_id.startswith("trade-") for item_id in selection
        ):
            self.context_menu.add_command(label="T操作", command=self._create_t_trade)
        elif len(selection) == 1 and selection[0].startswith("trade-"):
            self.context_menu.add_command(label="修改", command=self._modify_selected)
            self.context_menu.add_command(label="删除", command=self._delete_selected)
        else:
            return False
        return True

    def _popup_context_menu(self, event: tk.Event) -> None:
        if not self._configure_context_menu():
            return
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _show_context_menu(self, event: tk.Event) -> str | None:
        if self.trade_tree.identify_region(event.x, event.y) == "heading":
            column = self._column_at(event.x)
            if column in SORTABLE_TRADE_COLUMNS:
                self._sort_records(column, descending=True)
                return "break"
            return None

        item_id = self.trade_tree.identify_row(event.y)
        if not item_id:
            return None
        if item_id not in self.trade_tree.selection():
            self.trade_tree.selection_set(item_id)
        self.trade_tree.focus(item_id)
        self._schedule_trade_cell_labels()
        self._popup_context_menu(event)
        return "break"

    def _create_t_trade(self) -> None:
        indices = self._selected_record_indices()
        if len(indices) < 2:
            return
        records = self.owner.trades.get(self.symbol.canonical, [])
        selected_records = [records[index] for index in indices]
        if not any(record.trade_type == "买入" for record in selected_records) or not any(
            record.trade_type == "卖出" for record in selected_records
        ):
            messagebox.showwarning(
                "无法执行 T 操作",
                "选中的交易必须同时包含买入和卖出记录。",
                parent=self,
            )
            return

        group = self.owner._create_t_trade_group(self.symbol, indices)
        if group is None:
            messagebox.showerror(
                "保存失败",
                "T 交易标记未能写入本机保存文件，请检查目录权限后重试。",
                parent=self,
            )
            return
        self._reload_records()
        item_id = f"{T_TRADE_ITEM_PREFIX}{group.group_id}"
        self.trade_tree.selection_set(item_id)
        self.trade_tree.focus(item_id)
        self._schedule_trade_cell_labels()

    def _cancel_selected_t_trade(self) -> None:
        selection = self.trade_tree.selection()
        if len(selection) != 1 or not selection[0].startswith(T_TRADE_ITEM_PREFIX):
            return
        group_id = selection[0].removeprefix(T_TRADE_ITEM_PREFIX)
        group = self.owner._cancel_t_trade_group(self.symbol, group_id)
        if group is None:
            messagebox.showerror(
                "取消失败",
                "T 交易标记未能从本机保存文件中移除，请检查目录权限后重试。",
                parent=self,
            )
            return
        self._reload_records()
        restored_items = [
            f"trade-{index}"
            for index in group.record_indices
            if self.trade_tree.exists(f"trade-{index}")
        ]
        if restored_items:
            self.trade_tree.selection_set(*restored_items)
            self.trade_tree.focus(restored_items[0])
        self._schedule_trade_cell_labels()

    def _modify_selected(self) -> None:
        index = self._selected_record_index()
        if index is None:
            return
        record = self.owner.trades[self.symbol.canonical][index]
        TradeEntryDialog(
            self,
            self.currency,
            lambda updated: self._save_updated_record(index, updated),
            initial_record=record,
        )

    def _save_updated_record(self, index: int, record: TradeRecord) -> bool:
        if not self.owner._update_trade_record(self.symbol, index, record):
            messagebox.showerror(
                "保存失败",
                "修改后的交易记录未能写入本机保存文件，请检查目录权限后重试。",
                parent=self,
            )
            return False
        self._reload_records()
        self.trade_tree.selection_set(f"trade-{index}")
        self._schedule_trade_cell_labels()
        return True

    def _delete_selected(self) -> None:
        index = self._selected_record_index()
        if index is None:
            return
        if not self.owner._delete_trade_record(self.symbol, index):
            messagebox.showerror(
                "删除失败",
                "交易记录未能从本机保存文件中删除，请检查目录权限后重试。",
                parent=self,
            )
            return
        self._reload_records()

    def _close(self) -> None:
        if self._overlay_after_id is not None:
            try:
                self.after_cancel(self._overlay_after_id)
            except tk.TclError:
                pass
            self._overlay_after_id = None
        self.owner._capture_trade_window_layout(self)
        self.owner._save_state()
        self.owner._trade_windows.pop(self.symbol.canonical, None)
        self.destroy()


class CashFlowEntryDialog(tk.Toplevel):
    def __init__(
        self,
        owner: CashFlowWindow,
        on_save: Callable[[CashFlowRecord], bool],
        initial_record: CashFlowRecord | None = None,
    ) -> None:
        super().__init__(owner)
        self.withdraw()
        _apply_window_icon(self)
        self.configure(background=APP_BACKGROUND)
        self.title("修改入金" if initial_record is not None else "增加入金")
        self.resizable(False, False)
        self.transient(owner)
        self._on_save = on_save
        self.date_var = tk.StringVar(value=(
            initial_record.flow_date if initial_record else date.today().isoformat()
        ))
        self.amount_var = tk.StringVar(value=(
            initial_record.cny_amount if initial_record else ""
        ))

        content = ttk.Frame(self, padding=(22, 18))
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(1, weight=1)
        for row, label in enumerate(("日期", "人民币金额")):
            ttk.Label(content, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 18), pady=(0, 12)
            )
        self.date_entry = ttk.Entry(
            content, textvariable=self.date_var, state="readonly", cursor="hand2"
        )
        self.date_entry.grid(row=0, column=1, sticky=tk.EW, pady=(0, 12))
        self.date_entry.bind("<Button-1>", self._open_calendar)
        self.amount_entry = ttk.Entry(content, textvariable=self.amount_var, width=28)
        self.amount_entry.grid(row=1, column=1, sticky=tk.EW, pady=(0, 16))
        buttons = ttk.Frame(content)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(
            buttons, text="保存", command=self._submit, style="Primary.TButton"
        ).pack(side=tk.LEFT)
        self.bind("<Return>", lambda _event: self._submit())
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_and_show(self, owner)
        self.grab_set()
        self.amount_entry.focus_set()

    def _open_calendar(self, _event: tk.Event | None = None) -> str:
        popup = CalendarPopup(self, self.date_var.get(), self.date_var.set)
        popup.title("选择入金日期")
        return "break"

    def _record(self) -> CashFlowRecord:
        return CashFlowRecord.from_values(self.date_var.get(), self.amount_var.get())

    def _submit(self) -> None:
        try:
            record = self._record()
        except ValueError as exc:
            messagebox.showerror("入金记录错误", str(exc), parent=self)
            return
        if self._on_save(record):
            self.destroy()
        else:
            messagebox.showerror(
                "保存失败", "入金记录未保存，请检查数据文件和目录权限。", parent=self
            )


class CashFlowWindow(tk.Toplevel):
    def __init__(self, owner: StockPriceApp) -> None:
        super().__init__(owner)
        self.withdraw()
        _apply_window_icon(self)
        self.configure(background=APP_BACKGROUND)
        self.owner = owner
        self.title("入金记录")
        self.geometry("880x430")
        self.minsize(760, 340)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self._close)
        outer = ttk.Frame(self, padding=(20, 18, 20, 16))
        outer.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 14))
        self.summary_var = tk.StringVar()
        ttk.Label(header, textvariable=self.summary_var).pack(side=tk.LEFT)
        ttk.Button(
            header, text="增加入金", command=self._add, style="Primary.TButton"
        ).pack(side=tk.RIGHT)
        table = ttk.Frame(outer)
        table.pack(fill=tk.BOTH, expand=True)
        columns = ("date", "cny")
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="browse"
        )
        for column, title in zip(columns, ("日期", "人民币金额")):
            self.tree.heading(column, text=title, anchor=tk.CENTER)
            self.tree.column(column, width=400, minwidth=60, anchor=tk.CENTER)
        scrollbar = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="修改", command=self._edit)
        self.menu.add_command(label="删除", command=self._delete)
        self.tree.bind("<Button-3>", self._show_menu)
        self.tree.bind("<Double-1>", self._edit)
        ttk.Label(
            outer,
            text="净入金 = 所有入金的人民币金额合计；右键记录可修改或删除。",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))
        self.reload()
        _center_and_show(self, owner, fit_content=False)

    def reload(self) -> None:
        self.summary_var.set(f"净入金：{self.owner._net_cash_outflow():,.2f} CNY")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, record in enumerate(self.owner.cash_flows):
            self.tree.insert("", tk.END, iid=str(index), values=(
                record.flow_date, f"{Decimal(record.cny_amount):,.2f}",
            ))

    def _selected_index(self) -> int | None:
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _show_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def _add(self) -> None:
        CashFlowEntryDialog(self, self.owner._add_cash_flow)

    def _edit(self, event: tk.Event | None = None) -> None:
        if event is not None:
            item = self.tree.identify_row(event.y)
            if not item:
                return
            self.tree.selection_set(item)
        index = self._selected_index()
        if index is not None:
            CashFlowEntryDialog(
                self, lambda record: self.owner._update_cash_flow(index, record),
                self.owner.cash_flows[index],
            )

    def _delete(self) -> None:
        index = self._selected_index()
        if index is not None and messagebox.askyesno(
            "删除入金", "确定删除选中的入金记录？", parent=self
        ):
            if not self.owner._delete_cash_flow(index):
                messagebox.showerror("删除失败", "记录未删除，请检查数据目录权限。", parent=self)

    def _close(self) -> None:
        self.owner._cash_flow_window = None
        self.destroy()


@dataclass(frozen=True)
class SurgeAlert:
    code: str
    name: str
    base_price: Decimal
    current_price: Decimal
    currency: str
    elapsed_seconds: float


class SurgeAlertWindow(tk.Toplevel):
    def __init__(self, owner: tk.Tk, alerts: list[SurgeAlert]) -> None:
        super().__init__(owner)
        _apply_window_icon(self)
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self.title("股价急涨提醒")
        self.resizable(False, False)
        # Not transient, so the alert stays visible while the main window is minimized.
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        outer = ttk.Frame(self, padding=(20, 16, 20, 16))
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            outer,
            text=(
                f"以下股票 {SURGE_ALERT_WINDOW_SECONDS // 60} 分钟内涨幅超过 "
                f"{SURGE_ALERT_THRESHOLD:.0%}"
            ),
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            outer,
            text=f"提醒时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(2, 10))

        rows = ttk.Frame(outer)
        rows.pack(fill=tk.X)
        for row, alert in enumerate(alerts):
            rise = (alert.current_price - alert.base_price) / alert.base_price
            elapsed = int(alert.elapsed_seconds)
            values = (
                alert.code,
                alert.name,
                f"{alert.base_price:,.3f} → {alert.current_price:,.3f} {alert.currency}",
                f"{rise:+.2%}",
                f"{elapsed // 60}分{elapsed % 60:02d}秒内",
            )
            for column, text in enumerate(values):
                ttk.Label(
                    rows,
                    text=text,
                    foreground=BUY_COLOR if column == 3 else TEXT_PRIMARY,
                ).grid(
                    row=row,
                    column=column,
                    padx=(0 if column == 0 else 14, 0),
                    pady=2,
                    sticky="w",
                )

        ttk.Label(
            outer,
            text="行情来自公开接口，可能延迟或出错，仅作参考，请以券商行情为准。",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Button(
            outer,
            text="知道了",
            command=self.destroy,
            style="Primary.TButton",
            takefocus=False,
        ).pack(anchor=tk.E, pady=(12, 0))
        _center_and_show(self)
        self.bell()


class StockPriceApp(tk.Tk):
    def __init__(self, state_path: Path | None = None) -> None:
        super().__init__()
        _apply_window_icon(self)
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self.title("我钱呢")
        self.geometry(f"{MAIN_WINDOW_SIZE[0]}x{MAIN_WINDOW_SIZE[1]}")
        self.minsize(*MAIN_WINDOW_SIZE)
        self.maxsize(*MAIN_WINDOW_SIZE)
        self.resizable(False, False)

        self._state_path = state_path or _default_state_path()
        self._state_lock_handle = None
        self._skip_state_save_on_close = False
        self._window_geometry_restored = False
        self._native_user32 = None
        self._native_comctl32 = None
        self._native_window_handle: int | None = None
        self._window_subclass_callback = None
        self._window_subclass_installed = False
        if not self._acquire_state_lock():
            messagebox.showwarning(
                "程序已在运行",
                "检测到另一个股票行情窗口正在使用同一份数据。\n"
                "请先关闭已有窗口，再重新启动，避免交易记录被旧窗口覆盖。",
                parent=self,
            )
            self.destroy()
            raise AlreadyRunningError("股票行情程序已经在运行")
        self.symbols: dict[str, StockSymbol] = {}
        self.display_names: dict[str, str] = {}
        self.trades: dict[str, list[TradeRecord]] = {}
        self.t_trade_groups: dict[str, list[TTradeGroup]] = {}
        self.cash_flows: list[CashFlowRecord] = []
        self._cash_flow_window: CashFlowWindow | None = None
        self._cash_flow_load_error = ""
        self._cash_flow_unparsed: object = None
        self.trade_window_size = TRADE_WINDOW_DEFAULT_SIZE
        self.trade_column_widths = DEFAULT_TRADE_COLUMN_WIDTHS.copy()
        self.show_closed_positions = {market: True for market in MARKET_ORDER}
        self.market_sorts: dict[str, tuple[str, bool]] = {}
        self.current_prices: dict[str, tuple[Decimal, str]] = {}
        self._price_history: dict[str, list[tuple[float, Decimal]]] = {}
        self._pending_surge_alerts: list[SurgeAlert] = []
        self.put_warrant_maturity_months: dict[str, tuple[int, int]] = {}
        self.exchange_rates: ExchangeRates | None = None
        self.exchange_rate_warning = ""
        self.last_refresh_time: datetime | None = None
        self._statistics_amounts_visible = False
        self._statistics_cny_total_text = "--"
        self._net_cash_outflow_text = "0.00 CNY"
        self._trade_windows: dict[str, StockTradeWindow] = {}
        self.trees: dict[str, ttk.Treeview] = {}
        self.market_pages: dict[str, ttk.Frame] = {}
        self._last_active_market = MARKET_ORDER[0]
        self.current_page = MARKET_ORDER[0]
        self._market_spacer_ids = {market: [] for market in MARKET_ORDER}
        self._market_cell_labels: dict[
            tuple[str, str], tk.Label
        ] = {}
        self._market_overlay_after_id: str | None = None
        self._swipe_start: tuple[ttk.Treeview, int, int] | None = None
        self._column_resize_tree: ttk.Treeview | None = None
        self._paired_market_resize_start: (
            tuple[ttk.Treeview, int, str, str, int, int] | None
        ) = None
        self._result_queue: queue.Queue[tuple] = queue.Queue()
        self._refreshing = False
        self._cloud_syncing = False
        self._queue_after_id: str | None = None
        self._auto_refresh_after_id: str | None = None
        self._layout_after_id: str | None = None
        self._startup_refresh_after_id: str | None = None
        self._search_after_id: str | None = None
        self._search_generation = 0
        self._suggestions: list[StockSearchResult] = []
        self._selected_result: StockSearchResult | None = None
        self._setting_selection = False

        self._configure_style()
        self._build_ui()
        self._restore_state()
        self._refresh_statistics_dashboard()
        self._refresh_cash_flow_summary()
        self._schedule_market_cell_labels()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _lock_main_window_controls(self)
        if self._window_geometry_restored:
            self.deiconify()
        else:
            _center_and_show(self, fit_content=False)
        self.update_idletasks()
        self._install_title_icon_menu()
        self._fit_market_columns_to_viewport()
        self._queue_after_id = self.after(100, self._process_result_queue)
        if self._startup_refresh_after_id is None:
            self._startup_refresh_after_id = self.after(
                200,
                lambda: self._refresh_restored_stocks(list(self.symbols.values())),
            )
        self._schedule_auto_refresh()

    def _configure_style(self) -> None:
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei UI", size=10)
        tkfont.nametofont("TkTextFont").configure(
            family="Microsoft YaHei UI", size=10
        )

        style = ttk.Style(self)
        available = style.theme_names()
        if "clam" in available:
            style.theme_use("clam")
        elif "vista" in available:
            style.theme_use("vista")

        style.configure("TFrame", background=APP_BACKGROUND)
        style.configure(
            "TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
        )
        style.configure(
            "Title.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Hint.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_SECONDARY,
        )
        style.configure(
            "Weekend.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
        )
        style.configure(
            "TEntry",
            fieldbackground=SURFACE,
            foreground=TEXT_PRIMARY,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            padding=5,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=SURFACE,
            foreground=TEXT_PRIMARY,
            arrowcolor=ACCENT,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            padding=4,
        )
        style.configure(
            "TButton",
            background=SURFACE_MUTED,
            foreground=TEXT_PRIMARY,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            padding=(12, 7),
        )
        style.map(
            "TButton",
            background=[("active", ACCENT_SOFT), ("pressed", "#BFDBFE")],
            foreground=[("active", TEXT_PRIMARY)],
        )
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground=TEXT_PRIMARY,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            padding=(16, 8),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#AFC5EE"),
                ("pressed", "#1E40AF"),
                ("active", ACCENT_HOVER),
            ],
            foreground=[("disabled", TEXT_PRIMARY), ("active", TEXT_PRIMARY)],
        )
        style.configure(
            "Action.TButton",
            background=SURFACE,
            foreground=TEXT_PRIMARY,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            padding=(16, 3, 16, 4),
        )
        style.map(
            "Action.TButton",
            background=[("active", ACCENT_SOFT), ("pressed", "#BFDBFE")],
            foreground=[("disabled", TEXT_PRIMARY), ("active", TEXT_PRIMARY)],
        )
        style.configure(
            "TRadiobutton",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            padding=(0, 3),
        )
        style.configure("Buy.TRadiobutton", foreground=TEXT_PRIMARY)
        style.configure("Sell.TRadiobutton", foreground=TEXT_PRIMARY)
        style.map(
            "TRadiobutton",
            background=[("active", APP_BACKGROUND)],
            indicatorcolor=[("selected", ACCENT), ("!selected", SURFACE)],
        )
        style.configure(
            "TNotebook",
            background=APP_BACKGROUND,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            tabmargins=(0, 2, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=SURFACE_MUTED,
            foreground=TEXT_SECONDARY,
            padding=(16, 6),
            font=("Microsoft YaHei UI", 10),
        )
        style.layout(
            "TNotebook.Tab",
            [
                (
                    "Notebook.tab",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Notebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [
                                        (
                                            "Notebook.label",
                                            {"side": "top", "sticky": ""},
                                        )
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", TAB_SELECTED_BACKGROUND),
                ("active", ACCENT_SOFT),
            ],
            foreground=[("selected", TEXT_PRIMARY), ("active", TEXT_PRIMARY)],
            font=[
                ("selected", ("Microsoft YaHei UI", 12, "bold")),
                ("!selected", ("Microsoft YaHei UI", 10)),
            ],
            padding=[("selected", (22, 10)), ("!selected", (16, 6))],
        )
        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT_PRIMARY,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            rowheight=MARKET_ROW_HEIGHT,
            font=("Microsoft YaHei UI", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT_PRIMARY)],
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE_MUTED,
            foreground=TEXT_PRIMARY,
            bordercolor=DIVIDER,
            lightcolor=DIVIDER,
            darkcolor=DIVIDER,
            relief=tk.FLAT,
            padding=(6, 7),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", ACCENT_SOFT)],
        )
        style.configure(
            "TScrollbar",
            background="#C3D1E4",
            troughcolor=APP_BACKGROUND,
            bordercolor=APP_BACKGROUND,
            lightcolor="#C3D1E4",
            darkcolor="#C3D1E4",
            arrowcolor=TEXT_PRIMARY,
        )
        style.map("TScrollbar", background=[("active", ACCENT)])
        style.configure(
            "SelectedDay.TButton",
            background=ACCENT,
            foreground=TEXT_PRIMARY,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "SelectedDay.TButton",
            background=[("active", ACCENT_HOVER), ("pressed", "#1E40AF")],
            foreground=[("active", TEXT_PRIMARY)],
        )
        style.configure("CalendarMonth.TButton", padding=(2, 5))
        style.configure(
            "SelectedCalendarMonth.TButton",
            background=ACCENT,
            foreground=TEXT_PRIMARY,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(2, 5),
        )
        style.map(
            "SelectedCalendarMonth.TButton",
            background=[("active", ACCENT_HOVER), ("pressed", "#1E40AF")],
            foreground=[("active", TEXT_PRIMARY)],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(22, 18, 22, 14))
        outer.pack(fill=tk.BOTH, expand=True)

        input_frame = ttk.Frame(outer)
        input_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(input_frame, text="搜索股票").pack(side=tk.LEFT, padx=(0, 8))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(
            input_frame,
            textvariable=self.code_var,
            width=28,
            font=("Consolas", 11),
        )
        self.code_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.code_entry.bind("<Return>", self._on_entry_return)
        self.code_entry.bind("<Down>", self._focus_suggestions)
        self.code_var.trace_add("write", self._on_query_changed)

        self.refresh_button = ttk.Button(
            input_frame,
            text="刷新全部",
            command=self.refresh_all,
            style="Action.TButton",
        )
        self.refresh_button.pack(side=tk.LEFT, padx=(14, 0))

        self.suggestion_frame = ttk.Frame(outer, padding=(84, 0, 136, 8))
        suggestion_border = tk.Frame(
            self.suggestion_frame,
            background=DIVIDER,
            padx=1,
            pady=1,
        )
        suggestion_border.pack(fill=tk.X)
        self.suggestion_list = tk.Listbox(
            suggestion_border,
            height=5,
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 10),
            background=SURFACE,
            foreground=TEXT_PRIMARY,
            selectbackground=ACCENT_SOFT,
            selectforeground=TEXT_PRIMARY,
        )
        suggestion_scroll = ttk.Scrollbar(
            suggestion_border,
            orient=tk.VERTICAL,
            command=self.suggestion_list.yview,
        )
        self.suggestion_list.configure(yscrollcommand=suggestion_scroll.set)
        self.suggestion_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        suggestion_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.suggestion_list.bind("<ButtonRelease-1>", self._click_suggestion)
        self.suggestion_list.bind("<Return>", self._choose_suggestion)
        self.suggestion_list.bind("<Escape>", lambda _event: self._hide_suggestions())

        self.table_frame = ttk.Notebook(outer)
        self.table_frame.pack(fill=tk.BOTH, expand=True)
        self.market_context_menu = tk.Menu(
            self,
            tearoff=False,
            background=SURFACE,
            foreground=TEXT_PRIMARY,
            activebackground=ACCENT_SOFT,
            activeforeground=TEXT_PRIMARY,
            borderwidth=1,
            relief=tk.FLAT,
        )

        for market in MARKET_ORDER:
            page = ttk.Frame(self.table_frame)
            self.market_pages[market] = page
            self.table_frame.add(page, text=MARKET_TITLES[market])

            tree = ttk.Treeview(
                page,
                columns=TABLE_COLUMNS,
                show="headings",
                selectmode="extended",
            )
            self.trees[market] = tree
            for column in TABLE_COLUMNS:
                heading_options: dict[str, object] = {
                    "text": TABLE_HEADINGS[column],
                    "anchor": tk.CENTER,
                }
                if column in SORTABLE_MARKET_COLUMNS:
                    heading_options["command"] = (
                        lambda target_market=market, selected=column: (
                            self._sort_market_rows(
                                target_market,
                                selected,
                                descending=False,
                            )
                        )
                    )
                tree.heading(column, **heading_options)
                tree.column(
                    column,
                    width=DEFAULT_COLUMN_WIDTHS[column],
                    minwidth=60,
                    anchor=tk.CENTER,
                    stretch=column in {"name", "profit"},
                )

            tree.tag_configure("up", foreground=TEXT_PRIMARY)
            tree.tag_configure("down", foreground=TEXT_PRIMARY)
            tree.tag_configure("waiting", foreground=TEXT_PRIMARY)
            tree.bind("<Button-1>", self._clear_selection_on_blank)
            tree.bind("<Button-3>", self._show_market_context_menu)
            tree.bind("<ButtonPress-1>", self._begin_market_swipe, add="+")
            tree.bind("<B1-Motion>", self._resize_market_columns, add="+")
            tree.bind("<ButtonRelease-1>", self._finish_market_swipe, add="+")
            tree.bind(
                "<<TreeviewSelect>>",
                self._on_market_tree_selection,
            )
            tree.bind(
                "<Configure>",
                self._schedule_market_cell_labels,
                add="+",
            )
            tree.bind(
                "<Configure>",
                self._fit_market_columns_to_viewport,
                add="+",
            )
            tree.bind(
                "<Configure>",
                lambda event, target_market=market: (
                    self._update_market_spacers(target_market, event)
                ),
                add="+",
            )
            tree.bind(
                "<ButtonRelease-1>",
                self._schedule_market_cell_labels,
                add="+",
            )
            tree.bind("<Shift-MouseWheel>", self._switch_market_with_wheel)
            tree.bind("<Double-1>", self._open_stock_trade_window)
            tree.bind("<Delete>", self.remove_selected)

            vertical = ttk.Scrollbar(page, orient=tk.VERTICAL)
            vertical.configure(
                command=lambda *args, target=tree: self._scroll_market_tree(
                    target,
                    *args,
                )
            )
            tree.configure(
                yscrollcommand=lambda first, last, target=tree, bar=vertical: (
                    self._on_market_tree_scroll(target, bar, first, last)
                )
            )

            tree.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            page.rowconfigure(0, weight=1)
            page.columnconfigure(0, weight=1)

        self.statistics_page = tk.Frame(
            self.table_frame,
            background=STATISTICS_BACKGROUND,
        )
        self.table_frame.add(self.statistics_page, text="统计")
        self._build_statistics_dashboard()

        self.market_summary_var = tk.StringVar(
            value="美股总市值：--    总盈亏：--    总收益率：--"
        )
        self.market_summary_label = ttk.Label(
            self.table_frame,
            textvariable=self.market_summary_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        )

        self.table_frame.bind(
            "<<NotebookTabChanged>>",
            self._on_market_tab_changed,
        )

        self.market_footer = ttk.Frame(outer)
        self.market_footer.pack(
            fill=tk.X,
            pady=(8, 0),
            after=self.table_frame,
        )
        self.remaining_validity_var = tk.StringVar(value="")
        self.remaining_validity_label = ttk.Label(
            self.market_footer,
            textvariable=self.remaining_validity_var,
            anchor=tk.W,
            takefocus=False,
        )
        self.remaining_validity_label.pack(side=tk.LEFT)

        self.latest_trade_var = tk.StringVar(value="")
        self.latest_trade_label = ttk.Label(
            self.market_footer,
            textvariable=self.latest_trade_var,
            anchor=tk.E,
        )
        self.latest_trade_label.pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="请输入股票名称或代码，并从候选项中选择。")

        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=160)
        self.code_entry.focus_set()

    @staticmethod
    def _create_statistics_card(
        parent: tk.Misc,
        background: str,
    ) -> tuple[tk.Frame, tk.Frame]:
        border = tk.Frame(parent, background=STATISTICS_BORDER, padx=1, pady=1)
        content = tk.Frame(border, background=background)
        content.pack(fill=tk.BOTH, expand=True)
        return border, content

    def _build_statistics_dashboard(self) -> None:
        page = self.statistics_page
        content = tk.Frame(page, background=STATISTICS_BACKGROUND)
        content.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)
        content.columnconfigure(0, weight=1, uniform="statistics")
        content.columnconfigure(1, weight=1, uniform="statistics")
        content.rowconfigure(4, weight=1)

        header = tk.Frame(content, background=STATISTICS_BACKGROUND)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="资产统计",
            background=STATISTICS_BACKGROUND,
            foreground=STATISTICS_TEXT,
            font=("Microsoft YaHei UI", 17, "bold"),
        ).grid(row=0, column=0, rowspan=2, sticky="w")
        self.statistics_privacy_control = tk.Label(
            header,
            text="👁",
            background=STATISTICS_BACKGROUND,
            foreground=STATISTICS_TEXT,
            font=("Segoe UI Emoji", 13),
            width=2,
            borderwidth=0,
            relief=tk.FLAT,
            highlightthickness=0,
            cursor="hand2",
            takefocus=False,
        )
        self.statistics_privacy_control.grid(row=0, column=1, sticky="e")
        self.statistics_privacy_control.bind(
            "<Button-1>",
            self._toggle_statistics_amounts,
        )
        self.statistics_warning_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.statistics_warning_var,
            background=STATISTICS_BACKGROUND,
            foreground="#8A5A23",
            font=("Microsoft YaHei UI", 9),
        ).grid(row=1, column=1, sticky="e")

        cny_border, cny_card = self._create_statistics_card(
            content,
            STATISTICS_CNY_CARD,
        )
        cny_border.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="nsew",
            pady=(0, 10),
        )
        cny_card.columnconfigure(0, weight=1)
        tk.Label(
            cny_card,
            text="人民币总市值",
            background=STATISTICS_CNY_CARD,
            foreground=STATISTICS_TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        self.statistics_cny_total_var = tk.StringVar(value="****** CNY")
        tk.Label(
            cny_card,
            textvariable=self.statistics_cny_total_var,
            background=STATISTICS_CNY_CARD,
            foreground=STATISTICS_TEXT,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))

        cash_border, cash_card = self._create_statistics_card(content, STATISTICS_CARD)
        cash_border.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10)
        )
        cash_card.columnconfigure(0, weight=1)
        cash_title = tk.Label(
            cash_card, text="净入金", background=STATISTICS_CARD,
            foreground=STATISTICS_TEXT, font=("Microsoft YaHei UI", 10, "bold"),
        )
        cash_title.grid(row=0, column=0, sticky="w", padx=18, pady=(9, 0))
        self.net_cash_outflow_var = tk.StringVar(value="****** CNY")
        cash_value = tk.Label(
            cash_card, textvariable=self.net_cash_outflow_var,
            background=STATISTICS_CARD, foreground=STATISTICS_TEXT,
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        cash_value.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 9))
        for widget in (cash_border, cash_card, cash_title, cash_value):
            widget.configure(cursor="hand2")
            widget.bind("<Double-1>", self._open_cash_flow_window)

        self.statistics_usd_rate_var = tk.StringVar(value="1 USD = -- CNY")
        self.statistics_hkd_rate_var = tk.StringVar(value="1 HKD = -- CNY")
        self.statistics_rate_date_var = tk.StringVar(value="ECB 参考日期  --")
        rate_specs = (
            (0, "美元兑人民币", self.statistics_usd_rate_var),
            (1, "港币兑人民币", self.statistics_hkd_rate_var),
        )
        for column, title, value_var in rate_specs:
            border, card = self._create_statistics_card(content, STATISTICS_CARD)
            border.grid(
                row=3,
                column=column,
                sticky="nsew",
                padx=(0, 5) if column == 0 else (5, 0),
                pady=(0, 10),
            )
            card.columnconfigure(0, weight=1)
            tk.Label(
                card,
                text=title,
                background=STATISTICS_CARD,
                foreground=STATISTICS_ACCENT,
                font=("Microsoft YaHei UI", 10, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
            tk.Label(
                card,
                textvariable=value_var,
                background=STATISTICS_CARD,
                foreground=STATISTICS_TEXT,
                font=("Microsoft YaHei UI", 15, "bold"),
            ).grid(row=1, column=0, sticky="w", padx=16)
            tk.Label(
                card,
                textvariable=self.statistics_rate_date_var,
                background=STATISTICS_CARD,
                foreground=STATISTICS_TEXT,
                font=("Microsoft YaHei UI", 8),
            ).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 9))

        self.statistics_market_value_vars: dict[str, tk.StringVar] = {}
        self.statistics_profit_vars: dict[str, tk.StringVar] = {}
        self.statistics_return_vars: dict[str, tk.StringVar] = {}
        self.statistics_latest_trade_vars: dict[str, tk.StringVar] = {}
        self.statistics_profit_labels: dict[str, tk.Label] = {}
        self.statistics_return_labels: dict[str, tk.Label] = {}
        for column, market in enumerate(MARKET_ORDER):
            border, card = self._create_statistics_card(content, STATISTICS_CARD)
            border.grid(
                row=4,
                column=column,
                sticky="nsew",
                padx=(0, 5) if column == 0 else (5, 0),
            )
            for metric_column in range(3):
                card.columnconfigure(metric_column, weight=1, uniform=f"{market}-metric")
            card.rowconfigure(4, weight=1)
            tk.Label(
                card,
                text=MARKET_TITLES[market],
                background=STATISTICS_CARD,
                foreground=STATISTICS_TEXT,
                font=("Microsoft YaHei UI", 13, "bold"),
            ).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(13, 9))

            self.statistics_market_value_vars[market] = tk.StringVar(value="--")
            self.statistics_profit_vars[market] = tk.StringVar(value="--")
            self.statistics_return_vars[market] = tk.StringVar(value="--")
            metric_specs = (
                ("总市值", self.statistics_market_value_vars[market]),
                ("总盈亏", self.statistics_profit_vars[market]),
                ("总收益率", self.statistics_return_vars[market]),
            )
            for metric_column, (title, value_var) in enumerate(metric_specs):
                tk.Label(
                    card,
                    text=title,
                    background=STATISTICS_CARD,
                    foreground=STATISTICS_ACCENT,
                    font=("Microsoft YaHei UI", 9),
                ).grid(row=1, column=metric_column, sticky="w", padx=(16, 4))
                value_label = tk.Label(
                    card,
                    textvariable=value_var,
                    background=STATISTICS_CARD,
                    foreground=STATISTICS_TEXT,
                    font=("Microsoft YaHei UI", 11, "bold"),
                )
                value_label.grid(
                    row=2,
                    column=metric_column,
                    sticky="w",
                    padx=(16, 4),
                    pady=(1, 12),
                )
                if metric_column == 1:
                    self.statistics_profit_labels[market] = value_label
                elif metric_column == 2:
                    self.statistics_return_labels[market] = value_label

            tk.Frame(card, background=STATISTICS_BORDER, height=1).grid(
                row=3,
                column=0,
                columnspan=3,
                sticky="ew",
                padx=16,
            )
            self.statistics_latest_trade_vars[market] = tk.StringVar(value="--")
            tk.Label(
                card,
                textvariable=self.statistics_latest_trade_vars[market],
                background=STATISTICS_CARD,
                foreground=STATISTICS_TEXT,
                justify=tk.LEFT,
                anchor=tk.NW,
                wraplength=510,
                font=("Microsoft YaHei UI", 10),
            ).grid(
                row=4,
                column=0,
                columnspan=3,
                sticky="nsew",
                padx=16,
                pady=13,
            )

    def _net_cash_outflow(self) -> Decimal:
        return sum(
            (Decimal(record.cny_amount) for record in self.cash_flows), Decimal("0")
        )

    def _refresh_statistics_amount_visibility(self) -> None:
        if self._statistics_amounts_visible:
            self.statistics_cny_total_var.set(self._statistics_cny_total_text)
            self.net_cash_outflow_var.set(self._net_cash_outflow_text)
        else:
            self.statistics_cny_total_var.set("****** CNY")
            self.net_cash_outflow_var.set("****** CNY")

    def _toggle_statistics_amounts(
        self,
        _event: tk.Event | None = None,
    ) -> None:
        self._statistics_amounts_visible = not self._statistics_amounts_visible
        self._refresh_statistics_amount_visibility()

    def _refresh_cash_flow_summary(self) -> None:
        self._net_cash_outflow_text = (
            "--" if self._cash_flow_load_error
            else f"{self._net_cash_outflow():,.2f} CNY"
        )
        self._refresh_statistics_amount_visibility()
        if self._cash_flow_window is not None and self._cash_flow_window.winfo_exists():
            self._cash_flow_window.reload()

    def _open_cash_flow_window(self, _event: tk.Event | None = None) -> None:
        if self._cash_flow_load_error:
            messagebox.showerror("入金数据错误", self._cash_flow_load_error, parent=self)
            return
        if self._cash_flow_window is not None and self._cash_flow_window.winfo_exists():
            self._cash_flow_window.deiconify()
            self._cash_flow_window.lift()
            self._cash_flow_window.focus_force()
            return
        self._cash_flow_window = CashFlowWindow(self)

    def _replace_cash_flows(self, records: list[CashFlowRecord]) -> bool:
        if self._cash_flow_load_error:
            return False
        try:
            validated = [CashFlowRecord.from_values(**record.to_payload()) for record in records]
        except ValueError:
            return False
        previous = self.cash_flows
        self.cash_flows = validated
        if not self._save_state():
            self.cash_flows = previous
            return False
        self._refresh_cash_flow_summary()
        return True

    def _add_cash_flow(self, record: CashFlowRecord) -> bool:
        return self._replace_cash_flows([*self.cash_flows, record])

    def _update_cash_flow(self, index: int, record: CashFlowRecord) -> bool:
        if not 0 <= index < len(self.cash_flows):
            return False
        records = self.cash_flows.copy()
        records[index] = record
        return self._replace_cash_flows(records)

    def _delete_cash_flow(self, index: int) -> bool:
        if not 0 <= index < len(self.cash_flows):
            return False
        return self._replace_cash_flows(self.cash_flows[:index] + self.cash_flows[index + 1:])

    def _restore_cash_flows(self, raw: object) -> None:
        try:
            if not isinstance(raw, list):
                raise ValueError("记录列表格式无效")
            records = []
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("记录格式无效")
                # Legacy records carried a type, currency and rate; only legacy
                # inflows convert to the RMB-only format.
                if item.get("flow_type", "入金") != "入金":
                    raise ValueError("包含旧版出金记录")
                records.append(CashFlowRecord.from_values(
                    item.get("flow_date"), item.get("cny_amount")
                ))
            self.cash_flows = records
        except ValueError:
            self._cash_flow_load_error = (
                "入金数据格式异常或包含旧版出金记录，原始记录已保留，暂不允许修改。"
            )
            self._cash_flow_unparsed = raw

    def _restore_geometry(self, geometry: object) -> bool:
        value = str(geometry or "").strip()
        match = re.fullmatch(r"\d+x\d+([+-]\d+)([+-]\d+)", value)
        if match is None:
            self.geometry(f"{MAIN_WINDOW_SIZE[0]}x{MAIN_WINDOW_SIZE[1]}")
            return False
        x_position = int(match.group(1))
        y_position = int(match.group(2))
        self.geometry(
            f"{MAIN_WINDOW_SIZE[0]}x{MAIN_WINDOW_SIZE[1]}"
            f"{x_position:+d}{y_position:+d}"
        )
        self._window_geometry_restored = True
        return True

    def _acquire_state_lock(self) -> bool:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._state_path.with_name(f"{self._state_path.name}.lock")
            lock_handle = lock_path.open("a+b")
            lock_handle.seek(0, os.SEEK_END)
            if lock_handle.tell() == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            try:
                lock_handle.close()
            except (NameError, OSError):
                pass
            return False
        self._state_lock_handle = lock_handle
        return True

    def _release_state_lock(self) -> None:
        lock_handle = self._state_lock_handle
        self._state_lock_handle = None
        if lock_handle is None:
            return
        try:
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            lock_handle.close()

    def _active_page(self) -> str:
        selected_page = self.table_frame.select()
        for market, page in self.market_pages.items():
            if str(page) == selected_page:
                self.current_page = market
                self._last_active_market = market
                return market
        if str(self.statistics_page) == selected_page:
            self.current_page = STATISTICS_PAGE
            return STATISTICS_PAGE
        return self.current_page

    def _active_market(self) -> str:
        active_page = self._active_page()
        if active_page in MARKET_ORDER:
            self._last_active_market = active_page
        return self._last_active_market

    def _active_tree(self) -> ttk.Treeview:
        return self.trees[self._active_market()]

    def _select_market(self, market: str) -> None:
        self._select_page(market)

    def _select_page(self, page_name: str) -> None:
        if page_name in self.market_pages:
            self._last_active_market = page_name
            page: tk.Misc = self.market_pages[page_name]
        elif page_name == STATISTICS_PAGE:
            page = self.statistics_page
        else:
            return
        self.current_page = page_name
        self.table_frame.select(page)
        self._refresh_statistics_dashboard()

    def _on_market_tab_changed(
        self,
        _event: tk.Event | None = None,
    ) -> None:
        self._active_page()
        self._refresh_statistics_dashboard()
        self._schedule_market_cell_labels()

    def _latest_market_trade(
        self,
        market: str,
    ) -> tuple[StockSymbol, TradeRecord, Decimal, int] | None:
        latest_key: tuple[str, int] | None = None
        latest_details: tuple[StockSymbol, TradeRecord, Decimal, int] | None = None
        saved_order = 0
        for symbol_key, symbol in self.symbols.items():
            if symbol.market != market:
                continue
            for record in self.trades.get(symbol_key, []):
                saved_order += 1
                try:
                    quantity = int(record.quantity)
                    amount = Decimal(record.price) * Decimal(record.quantity)
                except (ValueError, InvalidOperation):
                    continue
                record_key = (record.trade_date, saved_order)
                if latest_key is None or record_key >= latest_key:
                    latest_key = record_key
                    latest_details = (symbol, record, amount, quantity)
        return latest_details

    def _format_latest_market_trade(self, market: str) -> str:
        latest_details = self._latest_market_trade(market)
        if latest_details is None:
            return ""
        symbol, record, amount, quantity = latest_details
        display_name = self.display_names.get(symbol.canonical, "").strip() or "--"
        return (
            f"{record.trade_date}    "
            f"{symbol.canonical} / {display_name}    "
            f"{quantity:,}股    "
            f"{amount:,.2f} {symbol.default_currency}"
        )

    def _refresh_selected_warrant_validity(self) -> None:
        page = self._active_page()
        if page not in MARKET_ORDER:
            self.remaining_validity_var.set("")
            return
        selected_items = [
            item_id
            for item_id in self.trees[page].selection()
            if not self._is_market_spacer(item_id)
        ]
        if len(selected_items) != 1:
            self.remaining_validity_var.set("")
            return
        maturity = self.put_warrant_maturity_months.get(selected_items[0])
        self.remaining_validity_var.set(
            "" if maturity is None else _format_remaining_validity(maturity)
        )

    @staticmethod
    def _format_market_metrics(
        metrics: MarketMetrics,
    ) -> tuple[str, str, str]:
        if not metrics.has_records or not metrics.complete:
            return "--", "--", "--"
        market_value_text = f"{metrics.market_value:,.2f} {metrics.currency}"
        profit_text = f"{metrics.profit:+,.2f} {metrics.currency}"
        return_text = (
            "--"
            if metrics.return_rate is None
            else f"{metrics.return_rate * Decimal('100'):+.2f}%"
        )
        return market_value_text, profit_text, return_text

    def _refresh_market_summary(
        self,
        metrics_by_market: dict[str, MarketMetrics],
    ) -> None:
        page = self._active_page()
        if page not in MARKET_ORDER:
            self.market_summary_label.place_forget()
            self.latest_trade_var.set("")
            self.remaining_validity_var.set("")
            self.market_footer.pack_forget()
            return
        market_value, profit, return_rate = self._format_market_metrics(
            metrics_by_market[page]
        )
        self.market_summary_var.set(
            f"{MARKET_TITLES[page]}总市值：{market_value}    "
            f"总盈亏：{profit}    "
            f"总收益率：{return_rate}"
        )
        self.market_summary_label.place(x=260, y=22, anchor=tk.W)
        self.market_summary_label.lift()
        self.latest_trade_var.set(self._format_latest_market_trade(page))
        self._refresh_selected_warrant_validity()
        if not self.market_footer.winfo_manager():
            self.market_footer.pack(
                fill=tk.X,
                pady=(8, 0),
                after=self.table_frame,
            )

    def _refresh_statistics_dashboard(self) -> None:
        metrics_by_market = {
            market: self._calculate_market_metrics(market)
            for market in MARKET_ORDER
        }
        for market, metrics in metrics_by_market.items():
            market_value_text, profit_text, return_text = (
                self._format_market_metrics(metrics)
            )
            self.statistics_market_value_vars[market].set(market_value_text)
            self.statistics_profit_vars[market].set(profit_text)
            self.statistics_return_vars[market].set(return_text)
            self.statistics_profit_labels[market].configure(
                foreground=_metric_color(profit_text)
            )
            self.statistics_return_labels[market].configure(
                foreground=_metric_color(return_text)
            )

            latest_text = self._format_latest_market_trade(market)
            self.statistics_latest_trade_vars[market].set(latest_text or "--")

        rates = self.exchange_rates
        if rates is None:
            self.statistics_usd_rate_var.set("1 USD = -- CNY")
            self.statistics_hkd_rate_var.set("1 HKD = -- CNY")
            self.statistics_rate_date_var.set("ECB 参考日期  --")
        else:
            self.statistics_usd_rate_var.set(
                f"1 USD = {rates.usd_to_cny:.4f} CNY"
            )
            self.statistics_hkd_rate_var.set(
                f"1 HKD = {rates.hkd_to_cny:.4f} CNY"
            )
            self.statistics_rate_date_var.set(
                f"ECB 参考日期  {rates.rate_date}"
            )

        cny_total = _calculate_cny_total_market_value(
            metrics_by_market["US"],
            metrics_by_market["HK"],
            rates,
        )
        self._statistics_cny_total_text = (
            "--" if cny_total is None else f"{cny_total:,.2f} CNY"
        )
        self._refresh_statistics_amount_visibility()
        self.statistics_warning_var.set(self.exchange_rate_warning)
        self._refresh_market_summary(metrics_by_market)

    def _tree_for_symbol(self, symbol: StockSymbol) -> ttk.Treeview:
        return self.trees[symbol.market]

    def _apply_column_widths(
        self,
        widths_by_market: dict[str, dict[str, int]],
    ) -> None:
        self._layout_after_id = None
        for market, widths in widths_by_market.items():
            tree = self.trees.get(market)
            if tree is None:
                continue
            for column, width in widths.items():
                if column in tree["columns"]:
                    tree.column(column, width=width)
        self._schedule_market_cell_labels()

    def _fit_market_columns_to_viewport(
        self,
        event: tk.Event | None = None,
    ) -> None:
        for tree in self.trees.values():
            if event is not None and event.widget is not tree:
                continue
            visible_width = tree.winfo_width() - 2
            # Inactive notebook pages have width 1 until first displayed. Do
            # not replace restored widths using that placeholder geometry;
            # their Configure event will fit them once the viewport is ready.
            if visible_width < MIN_MARKET_COLUMN_WIDTH * len(TABLE_COLUMNS):
                continue
            current_widths = {
                column: int(tree.column(column, "width"))
                for column in TABLE_COLUMNS
            }
            fitted_widths = _fit_column_widths_to_total(
                current_widths,
                visible_width,
            )
            for column, width in fitted_widths.items():
                if width != current_widths[column]:
                    tree.column(column, width=width)
        self._schedule_market_cell_labels()

    def _capture_trade_window_layout(self, window: StockTradeWindow) -> None:
        try:
            if not window.winfo_exists():
                return
            minimum_width, minimum_height = TRADE_WINDOW_MIN_SIZE
            width = max(
                minimum_width,
                min(window.winfo_width(), window.winfo_screenwidth()),
            )
            height = max(
                minimum_height,
                min(window.winfo_height(), window.winfo_screenheight()),
            )
            self.trade_window_size = (width, height)
            self.trade_column_widths = {
                column: int(window.trade_tree.column(column, "width"))
                for column in TRADE_COLUMNS
            }
        except tk.TclError:
            return

    def _refresh_restored_stocks(self, symbols: list[StockSymbol]) -> None:
        self._startup_refresh_after_id = None
        self._start_refresh(symbols)

    def _restore_state(self) -> None:
        if not self._state_path.is_file():
            return

        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") not in {
                1,
                2,
                3,
                4,
            }:
                raise ValueError("保存文件版本不受支持")

            self._restore_cash_flows(payload.get("cash_flows", []))
            layout_restored = self._restore_geometry(payload.get("window_geometry"))
            saved_closed_visibility = payload.get("show_closed_positions")
            if isinstance(saved_closed_visibility, dict):
                for market in MARKET_ORDER:
                    value = saved_closed_visibility.get(market)
                    if isinstance(value, bool):
                        self.show_closed_positions[market] = value
                        layout_restored = True
            saved_columns = payload.get("column_widths")
            if isinstance(saved_columns, dict):
                active_market = str(payload.get("active_market") or "")
                saved_market_widths = saved_columns.get(active_market)
                if not isinstance(saved_market_widths, dict):
                    saved_market_widths = next(
                        (
                            widths
                            for market in MARKET_ORDER
                            if isinstance(
                                (widths := saved_columns.get(market)),
                                dict,
                            )
                        ),
                        saved_columns,
                    )
                valid_widths = {
                    column: width
                    for column in TABLE_COLUMNS
                    if isinstance((width := saved_market_widths.get(column)), int)
                    and 60 <= width <= 2000
                }
                legacy_columns = tuple(
                    column
                    for column in TABLE_COLUMNS
                    if column not in {"holding", "cost", "market_value"}
                )
                missing_metric_columns = [
                    column
                    for column in ("holding", "cost", "market_value")
                    if column not in saved_market_widths
                ]
                if (
                    all(column in valid_widths for column in legacy_columns)
                    and all(
                        column in valid_widths or column in missing_metric_columns
                        for column in TABLE_COLUMNS
                    )
                ):
                    for missing_column in missing_metric_columns:
                        added_width = DEFAULT_COLUMN_WIDTHS[missing_column]
                        valid_widths[missing_column] = added_width
                        remaining_reduction = added_width
                        for donor in sorted(
                            ("name", "profit"),
                            key=lambda column: valid_widths[column],
                            reverse=True,
                        ):
                            reduction = min(
                                remaining_reduction,
                                valid_widths[donor] - 60,
                            )
                            valid_widths[donor] -= reduction
                            remaining_reduction -= reduction
                            if remaining_reduction == 0:
                                break
                if len(valid_widths) == len(TABLE_COLUMNS):
                    widths_by_market = {
                        market: valid_widths.copy() for market in MARKET_ORDER
                    }
                    layout_restored = True
                    self._layout_after_id = self.after_idle(
                        lambda widths=widths_by_market: self._apply_column_widths(widths)
                    )

            trade_geometry = str(payload.get("trade_window_geometry") or "")
            trade_geometry_match = re.fullmatch(
                r"(?P<width>\d+)x(?P<height>\d+)",
                trade_geometry,
            )
            if trade_geometry_match is not None:
                minimum_width, minimum_height = TRADE_WINDOW_MIN_SIZE
                self.trade_window_size = (
                    max(
                        minimum_width,
                        min(
                            int(trade_geometry_match.group("width")),
                            self.winfo_screenwidth(),
                        ),
                    ),
                    max(
                        minimum_height,
                        min(
                            int(trade_geometry_match.group("height")),
                            self.winfo_screenheight(),
                        ),
                    ),
                )
                layout_restored = True

            saved_trade_columns = payload.get("trade_column_widths")
            if isinstance(saved_trade_columns, dict):
                restored_trade_columns = DEFAULT_TRADE_COLUMN_WIDTHS.copy()
                valid_trade_column_count = 0
                for column in TRADE_COLUMNS:
                    width = saved_trade_columns.get(column)
                    if (
                        isinstance(width, int)
                        and not isinstance(width, bool)
                        and 60 <= width <= 2000
                    ):
                        restored_trade_columns[column] = width
                        valid_trade_column_count += 1
                if valid_trade_column_count:
                    self.trade_column_widths = restored_trade_columns
                    layout_restored = True

            restored_count = 0
            first_restored_market = ""
            saved_stocks = payload.get("stocks")
            if isinstance(saved_stocks, list):
                for item in saved_stocks:
                    if not isinstance(item, dict):
                        continue
                    try:
                        symbol = normalize_symbol(str(item.get("symbol") or ""))
                    except ValueError:
                        continue
                    if symbol.canonical in self.symbols:
                        continue

                    display_name = str(item.get("display_name") or "").strip()

                    self.symbols[symbol.canonical] = symbol
                    self.display_names[symbol.canonical] = display_name
                    tree = self._tree_for_symbol(symbol)
                    tree.insert(
                        "",
                        tk.END,
                        iid=symbol.canonical,
                        values=(
                            symbol.canonical,
                            display_name or "--",
                            "--",
                            "--",
                            "--",
                            "--",
                            "--",
                            "--",
                            "--",
                        ),
                        tags=("waiting",),
                    )
                    restored_count += 1
                    if not first_restored_market:
                        first_restored_market = symbol.market

            saved_trades = payload.get("trades")
            if payload.get("version") == STATE_VERSION and isinstance(
                saved_trades,
                dict,
            ):
                for raw_symbol, raw_records in saved_trades.items():
                    try:
                        trade_symbol = normalize_symbol(str(raw_symbol))
                    except ValueError:
                        continue
                    if not isinstance(raw_records, list):
                        continue
                    records: list[TradeRecord] = []
                    for raw_record in raw_records:
                        if not isinstance(raw_record, dict):
                            continue
                        trade_date = str(raw_record.get("trade_date") or "").strip()
                        price = str(raw_record.get("price") or "").strip()
                        quantity = str(raw_record.get("quantity") or "").strip()
                        trade_type = str(raw_record.get("trade_type") or "").strip()
                        try:
                            datetime.strptime(trade_date, "%Y-%m-%d")
                            price_value = Decimal(price)
                        except (ValueError, InvalidOperation):
                            continue
                        if (
                            price_value.is_finite()
                            and price_value > 0
                            and quantity.isascii()
                            and quantity.isdigit()
                            and int(quantity) > 0
                            and trade_type in {"买入", "卖出"}
                        ):
                            records.append(
                                TradeRecord(
                                    trade_date=trade_date,
                                    price=price,
                                    quantity=quantity,
                                    trade_type=trade_type,
                                )
                            )
                    if records:
                        self.trades[trade_symbol.canonical] = records

            saved_t_trade_groups = payload.get("t_trade_groups")
            if isinstance(saved_t_trade_groups, dict):
                for raw_symbol, raw_groups in saved_t_trade_groups.items():
                    try:
                        group_symbol = normalize_symbol(str(raw_symbol))
                    except ValueError:
                        continue
                    records = self.trades.get(group_symbol.canonical, [])
                    if not records or not isinstance(raw_groups, list):
                        continue

                    loaded_groups: list[TTradeGroup] = []
                    claimed_indices: set[int] = set()
                    group_ids: set[str] = set()
                    for raw_group in raw_groups:
                        if not isinstance(raw_group, dict):
                            continue
                        group_id = str(raw_group.get("group_id") or "").strip()
                        raw_indices = raw_group.get("record_indices")
                        if (
                            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", group_id)
                            or group_id in group_ids
                            or not isinstance(raw_indices, list)
                            or len(raw_indices) < 2
                            or any(
                                isinstance(index, bool) or not isinstance(index, int)
                                for index in raw_indices
                            )
                        ):
                            continue
                        indices = tuple(sorted(set(raw_indices)))
                        if (
                            len(indices) != len(raw_indices)
                            or any(index < 0 or index >= len(records) for index in indices)
                            or claimed_indices.intersection(indices)
                        ):
                            continue
                        group = TTradeGroup(group_id, indices)
                        if self._calculate_t_trade_summary(group_symbol, group) is None:
                            continue
                        loaded_groups.append(group)
                        group_ids.add(group_id)
                        claimed_indices.update(indices)
                    if loaded_groups:
                        self.t_trade_groups[group_symbol.canonical] = loaded_groups

            for symbol in self.symbols.values():
                self._refresh_performance_cells(symbol)

            active_market = str(payload.get("active_market") or "")
            if active_market not in self.market_pages:
                active_market = first_restored_market or MARKET_ORDER[0]
            self._last_active_market = active_market
            active_page = str(payload.get("active_page") or active_market)
            if active_page not in {*MARKET_ORDER, STATISTICS_PAGE}:
                active_page = active_market
            self._select_page(active_page)

            if restored_count:
                self.status_var.set(
                    f"已恢复 {restored_count} 只股票，正在刷新最新行情……"
                )
                restored_symbols = list(self.symbols.values())
                self._startup_refresh_after_id = self.after(
                    200,
                    lambda: self._refresh_restored_stocks(restored_symbols),
                )
            elif layout_restored:
                self.status_var.set("已恢复上次界面布局。")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.status_var.set(f"未能恢复上次保存内容：{exc}")

    def _save_state(self) -> bool:
        active_page = self._active_page()
        layout_tree = self._active_tree()
        payload = {
            "version": STATE_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window_geometry": (
                f"{MAIN_WINDOW_SIZE[0]}x{MAIN_WINDOW_SIZE[1]}"
                f"{self.winfo_x():+d}{self.winfo_y():+d}"
            ),
            "column_widths": {
                column: int(layout_tree.column(column, "width"))
                for column in TABLE_COLUMNS
            },
            "trade_window_geometry": (
                f"{self.trade_window_size[0]}x{self.trade_window_size[1]}"
            ),
            "trade_column_widths": self.trade_column_widths.copy(),
            "active_market": self._active_market(),
            "active_page": active_page,
            "show_closed_positions": self.show_closed_positions.copy(),
            "stocks": [],
            "cash_flows": (
                self._cash_flow_unparsed if self._cash_flow_load_error
                else [record.to_payload() for record in self.cash_flows]
            ),
            "trades": {
                symbol: [
                    {
                        "trade_date": record.trade_date,
                        "price": record.price,
                        "quantity": record.quantity,
                        "trade_type": record.trade_type,
                    }
                    for record in records
                ]
                for symbol, records in self.trades.items()
                if records
            },
            "t_trade_groups": {
                symbol: [
                    {
                        "group_id": group.group_id,
                        "record_indices": list(group.record_indices),
                    }
                    for group in groups
                ]
                for symbol, groups in self.t_trade_groups.items()
                if groups
            },
        }
        for market in MARKET_ORDER:
            visible_ids = list(self.trees[market].get_children(""))
            hidden_ids = [
                item_id
                for item_id, symbol in self.symbols.items()
                if symbol.market == market and item_id not in visible_ids
            ]
            for item_id in visible_ids + hidden_ids:
                if item_id not in self.symbols:
                    continue
                payload["stocks"].append(
                    {
                        "symbol": item_id,
                        "display_name": self.display_names.get(item_id, ""),
                    }
                )

        temporary_path = self._state_path.with_name(
            f"{self._state_path.name}.{os.getpid()}.tmp"
        )
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self._state_path)
            return True
        except (OSError, TypeError) as exc:
            self.status_var.set(f"保存失败：{exc}")
            return False
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _install_title_icon_menu(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            self.update_idletasks()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            comctl32.DefSubclassProc.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
            subclass_callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                ctypes.c_size_t,
                ctypes.c_size_t,
            )
            comctl32.SetWindowSubclass.argtypes = [
                wintypes.HWND,
                subclass_callback_type,
                ctypes.c_size_t,
                ctypes.c_size_t,
            ]
            comctl32.SetWindowSubclass.restype = wintypes.BOOL
            comctl32.RemoveWindowSubclass.argtypes = [
                wintypes.HWND,
                subclass_callback_type,
                ctypes.c_size_t,
            ]
            comctl32.RemoveWindowSubclass.restype = wintypes.BOOL

            window_handle = self.winfo_id()
            top_level_handle = user32.GetParent(window_handle) or window_handle

            def subclass_proc(
                hwnd: int,
                message: int,
                w_param: int,
                l_param: int,
                _subclass_id: int,
                _reference_data: int,
            ) -> int:
                try:
                    if self._is_title_bar_menu_request(
                        hwnd,
                        message,
                        int(w_param),
                        int(l_param),
                    ):
                        self._show_title_icon_menu(hwnd, int(l_param))
                        return 0
                except Exception:
                    pass
                return int(
                    comctl32.DefSubclassProc(
                        hwnd,
                        message,
                        w_param,
                        l_param,
                    )
                )

            callback = subclass_callback_type(subclass_proc)
            ctypes.set_last_error(0)
            if not comctl32.SetWindowSubclass(
                top_level_handle,
                callback,
                TITLE_ICON_SUBCLASS_ID,
                0,
            ):
                raise OSError(ctypes.get_last_error(), "无法安装窗口消息子类")
            self._native_user32 = user32
            self._native_comctl32 = comctl32
            self._native_window_handle = int(top_level_handle)
            self._window_subclass_callback = callback
            self._window_subclass_installed = True
        except (AttributeError, OSError, tk.TclError) as exc:
            self.status_var.set(f"左上角云端菜单初始化失败：{exc}")

    def _restore_title_icon_menu(self) -> None:
        if (
            self._native_comctl32 is None
            or self._native_window_handle is None
            or self._window_subclass_callback is None
            or not self._window_subclass_installed
        ):
            return
        try:
            self._native_comctl32.RemoveWindowSubclass(
                self._native_window_handle,
                self._window_subclass_callback,
                TITLE_ICON_SUBCLASS_ID,
            )
        except (AttributeError, OSError):
            pass
        finally:
            self._window_subclass_installed = False

    def _is_title_bar_menu_request(
        self,
        hwnd: int,
        message: int,
        w_param: int,
        l_param: int,
    ) -> bool:
        if message == 0x00A5:
            return w_param in {2, 3}
        if message != 0x007B:
            return False
        if l_param == -1:
            return False
        return self._point_is_in_title_bar(hwnd, l_param)

    def _point_is_in_title_bar(self, hwnd: int, l_param: int) -> bool:
        import ctypes
        from ctypes import wintypes

        user32 = self._native_user32
        if user32 is None:
            return False
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        hit_test = int(user32.SendMessageW(hwnd, 0x0084, 0, l_param))
        return hit_test in {2, 3}

    def _show_title_icon_menu(self, hwnd: int, l_param: int) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = self._native_user32
        if user32 is None:
            return
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL

        menu = user32.CreatePopupMenu()
        if not menu:
            return
        item_flags = 0x0000
        if self._cloud_syncing:
            item_flags |= 0x0001 | 0x0002
        try:
            user32.AppendMenuW(
                menu,
                item_flags,
                TITLE_ICON_MENU_UPLOAD,
                "同步到云端",
            )
            user32.AppendMenuW(
                menu,
                item_flags,
                TITLE_ICON_MENU_DOWNLOAD,
                "从云端恢复",
            )
            packed_position = l_param & 0xFFFFFFFF
            x_position = ctypes.c_short(packed_position & 0xFFFF).value
            y_position = ctypes.c_short((packed_position >> 16) & 0xFFFF).value
            user32.SetForegroundWindow(hwnd)
            command = int(
                user32.TrackPopupMenu(
                    menu,
                    0x0100 | 0x0002,
                    x_position,
                    y_position,
                    0,
                    hwnd,
                    None,
                )
            )
        finally:
            user32.DestroyMenu(menu)
            user32.PostMessageW(hwnd, 0x0000, 0, 0)
        if command:
            self._result_queue.put(("title_icon_menu_command", command))

    def _dispatch_title_icon_menu_command(self, command: int) -> None:
        if self._cloud_syncing:
            self.status_var.set("Google 授权或云端同步正在进行，请稍候。")
            return
        actions = {
            TITLE_ICON_MENU_UPLOAD: self._upload_cloud_state,
            TITLE_ICON_MENU_DOWNLOAD: self._download_cloud_state,
        }
        action = actions.get(command)
        if action is not None:
            action()

    def _on_close(self) -> None:
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
            self._search_after_id = None
        if self._queue_after_id is not None:
            self.after_cancel(self._queue_after_id)
            self._queue_after_id = None
        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
            self._auto_refresh_after_id = None
        if self._layout_after_id is not None:
            self.after_cancel(self._layout_after_id)
            self._layout_after_id = None
        if self._startup_refresh_after_id is not None:
            self.after_cancel(self._startup_refresh_after_id)
            self._startup_refresh_after_id = None
        if self._market_overlay_after_id is not None:
            self.after_cancel(self._market_overlay_after_id)
            self._market_overlay_after_id = None
        if not self._skip_state_save_on_close:
            for trade_window in list(self._trade_windows.values()):
                self._capture_trade_window_layout(trade_window)
            self._save_state()
        self._restore_title_icon_menu()
        self._release_state_lock()
        self.destroy()

    def _set_cloud_sync_busy(self, busy: bool) -> None:
        self._cloud_syncing = busy

    def _cloud_status_from_worker(self, status: str) -> None:
        self._result_queue.put(("cloud_status", status))

    @staticmethod
    def _format_cloud_time(value: str) -> str:
        try:
            return datetime.fromisoformat(value).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return value

    def _upload_cloud_state(self, _event: tk.Event | None = None) -> None:
        if self._cloud_syncing:
            self.status_var.set("云端同步正在进行，请稍候。")
            return
        if not messagebox.askyesno(
            "同步到云端",
            "将把本机的完整状态同步到 Google Drive，并覆盖当前云端快照。\n\n"
            "同步内容包括股票、交易记录、T 交易、入金、窗口位置、"
            "列宽和当前页面。是否继续？",
            parent=self,
        ):
            return
        if not self._save_state():
            messagebox.showerror(
                "同步失败",
                "本地状态保存失败，未开始同步到云端。",
                parent=self,
            )
            return
        try:
            state_bytes = self._state_path.read_bytes()
        except OSError as exc:
            messagebox.showerror(
                "同步失败",
                f"无法读取本地状态：{exc}",
                parent=self,
            )
            return
        self._set_cloud_sync_busy(True)
        self.status_var.set("正在准备将状态同步到 Google Drive……")
        threading.Thread(
            target=self._cloud_upload_worker,
            args=(state_bytes,),
            daemon=True,
        ).start()

    def _cloud_upload_worker(self, state_bytes: bytes) -> None:
        try:
            client = GoogleDriveSyncClient(
                self._cloud_status_from_worker,
                authorize_if_needed=True,
            )
            info = client.upload_state(state_bytes)
            self._result_queue.put(("cloud_upload_done", info))
        except Exception as exc:
            self._result_queue.put(("cloud_error", "同步到云端", str(exc)))

    def _download_cloud_state(self, _event: tk.Event | None = None) -> None:
        if self._cloud_syncing:
            self.status_var.set("云端同步正在进行，请稍候。")
            return
        if self._refreshing:
            self.status_var.set("请等待行情刷新完成后再从云端恢复。")
            return
        if not messagebox.askyesno(
            "从云端恢复",
            "将下载 Google Drive 中的完整状态，备份本地状态后用云端内容"
            "全部替换。\n\n恢复成功后程序会自动关闭，请重新打开以载入云端信息。"
            "是否继续？",
            parent=self,
        ):
            return
        if not self._save_state():
            messagebox.showerror(
                "恢复失败",
                "本地状态保存失败，未开始从云端恢复。",
                parent=self,
            )
            return
        self._set_cloud_sync_busy(True)
        self.status_var.set("正在准备从 Google Drive 云端恢复……")
        threading.Thread(
            target=self._cloud_download_worker,
            daemon=True,
        ).start()

    def _cloud_download_worker(self) -> None:
        try:
            client = GoogleDriveSyncClient(
                self._cloud_status_from_worker,
                authorize_if_needed=True,
            )
            state_bytes, info = client.download_state()
            self._result_queue.put(("cloud_download_ready", state_bytes, info))
        except Exception as exc:
            self._result_queue.put(("cloud_error", "从云端恢复", str(exc)))

    def _apply_downloaded_cloud_state(
        self,
        state_bytes: bytes,
        info: CloudSnapshotInfo,
    ) -> None:
        temporary_path = self._state_path.with_name(
            f"{self._state_path.name}.{os.getpid()}.cloud.tmp"
        )
        backup_path: Path | None = None
        try:
            json.loads(state_bytes.decode("utf-8"))
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            if self._state_path.is_file():
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
                backup_path = self._state_path.with_name(
                    f"{self._state_path.name}.bak.{timestamp}"
                )
                shutil.copy2(self._state_path, backup_path)
            temporary_path.write_bytes(state_bytes)
            temporary_path.replace(self._state_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._set_cloud_sync_busy(False)
            self.status_var.set(f"从云端恢复失败：{exc}")
            messagebox.showerror(
                "恢复失败",
                f"无法替换本地状态：{exc}",
                parent=self,
            )
            return
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

        self._skip_state_save_on_close = True
        uploaded_time = self._format_cloud_time(info.uploaded_at)
        backup_text = str(backup_path) if backup_path is not None else "无"
        self.status_var.set("云端状态已恢复，程序即将关闭。")
        messagebox.showinfo(
            "恢复完成",
            f"已从云端恢复版本 {info.revision}（{uploaded_time}）。\n"
            f"本地备份：{backup_text}\n\n程序将关闭，请重新打开。",
            parent=self,
        )
        self.after_idle(self._on_close)

    def _on_query_changed(self, *_args: object) -> None:
        if self._setting_selection:
            return
        self._selected_result = None
        self._search_generation += 1
        generation = self._search_generation
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
            self._search_after_id = None

        query = self.code_var.get().strip()
        if not query:
            self._suggestions = []
            self._hide_suggestions()
            self.status_var.set("请输入股票名称或代码，并从候选项中选择。")
            return

        self.status_var.set(f"准备搜索“{query}”……")
        self._search_after_id = self.after(
            350,
            lambda: self._start_search(query, generation),
        )

    def _start_search(self, query: str, generation: int) -> None:
        self._search_after_id = None
        if generation != self._search_generation:
            return
        self.status_var.set(f"正在搜索“{query}”……")
        worker = threading.Thread(
            target=self._search_worker,
            args=(query, generation),
            daemon=True,
        )
        worker.start()

    def _search_worker(self, query: str, generation: int) -> None:
        try:
            results, errors = search_stocks(query, timeout=8.0, limit=10)
            self._result_queue.put(("search", generation, query, results, errors))
        except Exception as exc:
            self._result_queue.put(("search_error", generation, query, str(exc)))

    def _show_search_results(
        self,
        generation: int,
        query: str,
        results: list[StockSearchResult],
        errors: dict[str, str],
    ) -> None:
        if generation != self._search_generation or query != self.code_var.get().strip():
            return

        self._suggestions = results
        self.suggestion_list.delete(0, tk.END)
        if results:
            for result in results:
                market_name = "港股" if result.symbol.market == "HK" else "美股"
                self.suggestion_list.insert(
                    tk.END,
                    f"{result.display_code:<12}  {result.name}    {market_name} · {result.source}",
                )
            self.suggestion_list.configure(height=min(6, len(results)))
            failed = f"，{len(errors)} 个来源不可用" if errors else ""
            self.status_var.set(
                f"找到 {len(results)} 个候选{failed}；点击候选项可直接添加。"
            )
        else:
            self.suggestion_list.insert(tk.END, "未找到匹配的美股或港股")
            self.suggestion_list.configure(height=1)
            self.status_var.set("未找到匹配股票，请尝试名称、拼音或完整股票代码。")

        if not self.suggestion_frame.winfo_manager():
            self.suggestion_frame.pack(
                fill=tk.X,
                before=self.table_frame,
            )

    def _hide_suggestions(self) -> None:
        if self.suggestion_frame.winfo_manager():
            self.suggestion_frame.pack_forget()
        self.code_entry.focus_set()

    def _focus_suggestions(self, _event: tk.Event) -> str:
        if self._suggestions:
            self.suggestion_list.focus_set()
            self.suggestion_list.selection_clear(0, tk.END)
            self.suggestion_list.selection_set(0)
            self.suggestion_list.activate(0)
        return "break"

    def _on_entry_return(self, _event: tk.Event) -> str:
        if self._suggestions and self.suggestion_frame.winfo_manager():
            self.suggestion_list.selection_clear(0, tk.END)
            self.suggestion_list.selection_set(0)
            self._choose_suggestion()
            if self._selected_result is not None:
                self.add_stock()
        return "break"

    def _click_suggestion(self, event: tk.Event) -> None:
        if not self._suggestions:
            return
        index = self.suggestion_list.nearest(event.y)
        self.suggestion_list.selection_clear(0, tk.END)
        self.suggestion_list.selection_set(index)
        self._choose_suggestion()
        if self._selected_result is not None:
            self.add_stock()

    def _choose_suggestion(self, _event: tk.Event | None = None) -> str:
        selection = self.suggestion_list.curselection()
        if not selection or not self._suggestions:
            return "break"
        index = selection[0]
        if index >= len(self._suggestions):
            return "break"

        result = self._suggestions[index]
        self._selected_result = result
        self._setting_selection = True
        try:
            self.code_var.set(result.display_code)
        finally:
            self._setting_selection = False
        self._hide_suggestions()
        market_name = "港股" if result.symbol.market == "HK" else "美股"
        self.status_var.set(
            f"已选择：{result.name}（{result.display_code}，{market_name}）。"
        )
        return "break"

    def _clear_search(self) -> None:
        self._search_generation += 1
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
            self._search_after_id = None
        self._selected_result = None
        self._suggestions = []
        self._setting_selection = True
        try:
            self.code_var.set("")
        finally:
            self._setting_selection = False
        self._hide_suggestions()

    def add_stock(self) -> None:
        selected = self._selected_result
        if selected is None:
            messagebox.showinfo(
                "请先选择股票",
                "请先输入名称或代码，并点击搜索候选项完成选择。",
                parent=self,
            )
            self.code_entry.focus_set()
            return
        if self._refreshing:
            self.status_var.set("行情刷新中，将在本轮刷新完成后自动添加该股票。")
            return

        symbol = selected.symbol
        selected_name = selected.name.strip()
        tree = self._tree_for_symbol(symbol)
        self._select_market(symbol.market)

        if symbol.canonical in self.symbols:
            if selected_name and (
                _contains_chinese(selected_name)
                or not _contains_chinese(self.display_names.get(symbol.canonical, ""))
            ):
                self.display_names[symbol.canonical] = selected_name
                self._save_state()
            tree.selection_set(symbol.canonical)
            tree.see(symbol.canonical)
            self.status_var.set(f"{symbol.canonical} 已在列表中，正在重新获取行情。")
            self._clear_search()
            self._start_refresh([symbol])
            return

        self.symbols[symbol.canonical] = symbol
        self.display_names[symbol.canonical] = selected_name
        tree.insert(
            "",
            tk.END,
            iid=symbol.canonical,
            values=(
                symbol.canonical,
                selected_name or "--",
                "--",
                "--",
                "--",
                "--",
                "--",
                "--",
                "--",
            ),
            tags=("waiting",),
        )
        self._reorder_market_rows(symbol.market)
        tree.selection_set(symbol.canonical)
        tree.see(symbol.canonical)
        self._save_state()
        self._clear_search()
        self.code_entry.focus_set()
        self._start_refresh([symbol])

    def refresh_all(self) -> None:
        self._start_refresh(list(self.symbols.values()))

    def _schedule_auto_refresh(self) -> None:
        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
        self._auto_refresh_after_id = self.after(
            AUTO_REFRESH_INTERVAL_MS,
            self._run_auto_refresh,
        )

    def _run_auto_refresh(self) -> None:
        self._auto_refresh_after_id = None
        self._start_refresh(list(self.symbols.values()))

    def _clear_selection_on_blank(self, event: tk.Event) -> str | None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return None
        row_id = tree.identify_row(event.y)
        region = tree.identify_region(event.x, event.y)
        spacer_clicked = self._is_market_spacer(row_id)
        if (not row_id or spacer_clicked) and region not in {
            "heading",
            "separator",
        }:
            selected_items = tree.selection()
            if selected_items:
                tree.selection_remove(*selected_items)
            tree.focus("")
            self._schedule_market_cell_labels()
            if spacer_clicked:
                return "break"
        return None

    def _show_market_context_menu(self, event: tk.Event) -> str | None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return None
        row_id = tree.identify_row(event.y)
        region = tree.identify_region(event.x, event.y)
        market = next(
            (name for name, market_tree in self.trees.items() if market_tree is tree),
            None,
        )
        if region == "heading":
            column = self._market_column_at(tree, event.x)
            if market is not None and column in SORTABLE_MARKET_COLUMNS:
                self._sort_market_rows(market, column, descending=True)
                return "break"
            return None
        if (
            (row_id and not self._is_market_spacer(row_id))
            or region == "separator"
        ):
            return None

        if market is None:
            return None
        selected_items = tree.selection()
        if selected_items:
            tree.selection_remove(*selected_items)
        tree.focus("")
        self._configure_market_context_menu(market)
        try:
            self.market_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.market_context_menu.grab_release()
        return "break"

    def _configure_market_context_menu(self, market: str) -> None:
        self.market_context_menu.delete(0, tk.END)
        label = (
            "隐藏已清仓股票"
            if self.show_closed_positions[market]
            else "显示已清仓股票"
        )
        self.market_context_menu.add_command(
            label=label,
            command=lambda selected_market=market: (
                self._toggle_closed_positions_visibility(selected_market)
            ),
        )

    def _toggle_closed_positions_visibility(self, market: str | None = None) -> None:
        target_market = market or self._active_market()
        self.show_closed_positions[target_market] = not self.show_closed_positions[
            target_market
        ]
        self._apply_closed_position_visibility(target_market)
        self._save_state()

    def _schedule_market_cell_labels(
        self,
        _event: tk.Event | None = None,
    ) -> None:
        if self._market_overlay_after_id is not None:
            return
        try:
            self._market_overlay_after_id = self.after_idle(
                self._render_market_cell_labels
            )
        except tk.TclError:
            self._market_overlay_after_id = None

    def _on_market_tree_selection(self, event: tk.Event) -> None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return
        spacer_ids = [
            item_id
            for item_id in tree.selection()
            if self._is_market_spacer(item_id)
        ]
        if spacer_ids:
            tree.selection_remove(*spacer_ids)
            if self._is_market_spacer(tree.focus()):
                tree.focus("")
        self._refresh_selected_warrant_validity()
        self._schedule_market_cell_labels()

    @staticmethod
    def _is_market_spacer(item_id: str) -> bool:
        return item_id.startswith(MARKET_SPACER_PREFIX)

    def _update_market_spacers(
        self,
        market: str,
        _event: tk.Event | None = None,
    ) -> None:
        tree = self.trees.get(market)
        if tree is None:
            return

        visible_real_ids = [
            item_id
            for item_id in tree.get_children("")
            if item_id in self.symbols
        ]
        desired_count = 0
        if visible_real_ids:
            visible_row_count = max(1, tree.winfo_height() // MARKET_ROW_HEIGHT)
            desired_count = max(1, visible_row_count // 2)

        spacer_ids = self._market_spacer_ids[market]
        spacer_ids[:] = [item_id for item_id in spacer_ids if tree.exists(item_id)]
        while len(spacer_ids) > desired_count:
            item_id = spacer_ids.pop()
            if tree.exists(item_id):
                tree.delete(item_id)
        while len(spacer_ids) < desired_count:
            item_id = f"{MARKET_SPACER_PREFIX}{market}_{len(spacer_ids)}"
            tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=("",) * len(TABLE_COLUMNS),
                tags=("spacer",),
            )
            spacer_ids.append(item_id)
        for item_id in spacer_ids:
            tree.move(item_id, "", tk.END)

        self._schedule_market_cell_labels()

    def _render_market_cell_labels(self) -> None:
        self._market_overlay_after_id = None
        visible_labels: set[tuple[str, str]] = set()
        for tree in self.trees.values():
            try:
                if not tree.winfo_exists():
                    continue
            except tk.TclError:
                continue

            selected_items = set(tree.selection())
            for item_id in tree.get_children(""):
                if item_id not in self.symbols:
                    continue
                background = (
                    ACCENT_SOFT if item_id in selected_items else SURFACE
                )
                for column in ("change", "profit", "return"):
                    bounds = tree.bbox(item_id, column)
                    if not bounds:
                        continue
                    x, y, width, height = bounds
                    key = (item_id, column)
                    visible_labels.add(key)
                    value = tree.set(item_id, column)
                    label = self._market_cell_labels.get(key)
                    if label is None:
                        label = tk.Label(
                            tree,
                            borderwidth=0,
                            highlightthickness=0,
                            anchor=tk.CENTER,
                            padx=0,
                            pady=0,
                            takefocus=False,
                        )
                        label.bind(
                            "<ButtonPress-1>",
                            lambda event, target=tree, selected=item_id: (
                                self._begin_market_overlay_swipe(
                                    event,
                                    target,
                                    selected,
                                )
                            ),
                        )
                        label.bind(
                            "<ButtonRelease-1>",
                            lambda event, target=tree: (
                                self._finish_market_overlay_swipe(event, target)
                            ),
                        )
                        label.bind(
                            "<Double-1>",
                            lambda _event, selected=item_id: (
                                self._open_stock_trade_from_overlay(selected)
                            ),
                        )
                        label.bind(
                            "<MouseWheel>",
                            lambda event, target=tree: (
                                self._scroll_market_from_overlay(event, target)
                            ),
                        )
                        label.bind(
                            "<Shift-MouseWheel>",
                            self._switch_market_with_wheel,
                        )
                        self._market_cell_labels[key] = label
                    label.configure(
                        text=value,
                        foreground=_metric_color(value),
                        background=background,
                        font=("Microsoft YaHei UI", 10),
                    )
                    label.place(
                        x=x + 1,
                        y=y,
                        width=max(1, width - 2),
                        height=height,
                    )
                    label.lift()

        for key, label in list(self._market_cell_labels.items()):
            if key in visible_labels:
                continue
            label.place_forget()
            if not any(tree.exists(key[0]) for tree in self.trees.values()):
                label.destroy()
                self._market_cell_labels.pop(key, None)

    def _begin_market_overlay_swipe(
        self,
        event: tk.Event,
        tree: ttk.Treeview,
        item_id: str,
    ) -> str:
        selected_items = set(tree.selection())
        if event.state & 0x0004:
            if item_id in selected_items:
                tree.selection_remove(item_id)
            else:
                tree.selection_add(item_id)
        elif event.state & 0x0001 and tree.focus() in tree.get_children(""):
            children = list(tree.get_children(""))
            start = children.index(tree.focus())
            end = children.index(item_id)
            tree.selection_set(children[min(start, end) : max(start, end) + 1])
        else:
            tree.selection_set(item_id)
        tree.focus(item_id)
        tree.focus_set()
        self._refresh_selected_warrant_validity()
        self._column_resize_tree = None
        self._paired_market_resize_start = None
        self._swipe_start = (
            tree,
            event.x_root - tree.winfo_rootx(),
            event.y_root - tree.winfo_rooty(),
        )
        self._schedule_market_cell_labels()
        return "break"

    def _finish_market_overlay_swipe(
        self,
        event: tk.Event,
        tree: ttk.Treeview,
    ) -> str:
        swipe_start = self._swipe_start
        self._swipe_start = None
        if swipe_start is not None and swipe_start[0] is tree:
            current_x = event.x_root - tree.winfo_rootx()
            current_y = event.y_root - tree.winfo_rooty()
            horizontal_distance = current_x - swipe_start[1]
            vertical_distance = current_y - swipe_start[2]
            if (
                abs(horizontal_distance) >= 80
                and abs(horizontal_distance) > abs(vertical_distance) * 1.5
            ):
                self._switch_market_page(
                    1 if horizontal_distance < 0 else -1
                )
        self._schedule_market_cell_labels()
        return "break"

    def _open_stock_trade_from_overlay(self, item_id: str) -> str:
        symbol = self.symbols.get(item_id)
        if symbol is not None:
            self._show_stock_trade_window(symbol)
        return "break"

    def _scroll_market_tree(
        self,
        tree: ttk.Treeview,
        *args: str,
    ) -> None:
        tree.yview(*args)
        self._schedule_market_cell_labels()

    def _on_market_tree_scroll(
        self,
        _tree: ttk.Treeview,
        scrollbar: ttk.Scrollbar,
        first: str,
        last: str,
    ) -> None:
        scrollbar.set(first, last)
        self._schedule_market_cell_labels()

    def _scroll_market_from_overlay(
        self,
        event: tk.Event,
        tree: ttk.Treeview,
    ) -> str:
        if event.delta:
            steps = -int(event.delta / 120)
            if steps == 0:
                steps = -1 if event.delta > 0 else 1
            tree.yview_scroll(steps, "units")
            self._schedule_market_cell_labels()
        return "break"

    def _begin_market_swipe(self, event: tk.Event) -> str | None:
        tree = event.widget
        if isinstance(tree, ttk.Treeview):
            if tree.identify_region(event.x, event.y) == "separator":
                self._column_resize_tree = tree
                self._swipe_start = None
                column_id = tree.identify_column(event.x)
                try:
                    column_index = int(column_id.removeprefix("#")) - 1
                except ValueError:
                    column_index = -1
                if 0 <= column_index < len(TABLE_COLUMNS) - 1:
                    current_column = TABLE_COLUMNS[column_index]
                    right_column = TABLE_COLUMNS[column_index + 1]
                    self._paired_market_resize_start = (
                        tree,
                        event.x_root,
                        current_column,
                        right_column,
                        int(tree.column(current_column, "width")),
                        int(tree.column(right_column, "width")),
                    )
                    return "break"
                self._paired_market_resize_start = None
                return None
            self._column_resize_tree = None
            self._paired_market_resize_start = None
            self._swipe_start = (tree, event.x, event.y)
        return None

    def _resize_market_columns(self, event: tk.Event) -> str | None:
        resize_start = self._paired_market_resize_start
        if resize_start is None or event.widget is not resize_start[0]:
            return None

        (
            tree,
            start_x,
            current_column,
            right_column,
            current_width,
            right_width,
        ) = resize_start
        current_minimum = int(tree.column(current_column, "minwidth"))
        right_minimum = int(tree.column(right_column, "minwidth"))
        requested_delta = event.x_root - start_x
        delta = max(
            current_minimum - current_width,
            min(right_width - right_minimum, requested_delta),
        )
        tree.column(current_column, width=current_width + delta)
        tree.column(right_column, width=right_width - delta)
        self._schedule_market_cell_labels()
        return "break"

    def _finish_market_swipe(self, event: tk.Event) -> str | None:
        paired_resize = self._paired_market_resize_start
        self._paired_market_resize_start = None
        if paired_resize is not None and paired_resize[0] is event.widget:
            resize_tree = paired_resize[0]
            self._column_resize_tree = None
            self._swipe_start = None
            self.after_idle(
                lambda source=resize_tree: self._synchronize_column_widths(source)
            )
            return "break"

        resize_tree = self._column_resize_tree
        self._column_resize_tree = None
        if resize_tree is event.widget:
            self.after_idle(
                lambda source=resize_tree: self._synchronize_column_widths(source)
            )
            return None

        swipe_start = self._swipe_start
        self._swipe_start = None
        if swipe_start is None or event.widget is not swipe_start[0]:
            return None

        horizontal_distance = event.x - swipe_start[1]
        vertical_distance = event.y - swipe_start[2]
        if (
            abs(horizontal_distance) >= 80
            and abs(horizontal_distance) > abs(vertical_distance) * 1.5
        ):
            self._switch_market_page(1 if horizontal_distance < 0 else -1)
        return None

    def _synchronize_column_widths(self, source: ttk.Treeview) -> None:
        widths = {
            column: int(source.column(column, "width"))
            for column in TABLE_COLUMNS
        }
        for tree in self.trees.values():
            if tree is source:
                continue
            for column, width in widths.items():
                tree.column(column, width=width)
        self._schedule_market_cell_labels()
        self._save_state()

    def _switch_market_with_wheel(self, event: tk.Event) -> str:
        if event.delta:
            self._switch_market_page(1 if event.delta < 0 else -1)
        return "break"

    def _switch_market_page(self, direction: int) -> None:
        current_market = self._active_market()
        current_index = MARKET_ORDER.index(current_market)
        target_index = max(0, min(current_index + direction, len(MARKET_ORDER) - 1))
        if target_index == current_index:
            return
        target_market = MARKET_ORDER[target_index]
        self._select_market(target_market)
        if not self._refreshing:
            self.status_var.set(f"已切换至{MARKET_TITLES[target_market]}列表。")

    def _open_stock_trade_window(self, event: tk.Event) -> str | None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return None
        item_id = tree.identify_row(event.y)
        symbol = self.symbols.get(item_id)
        if symbol is None:
            return None
        self._show_stock_trade_window(symbol)
        return "break"

    def _show_stock_trade_window(self, symbol: StockSymbol) -> StockTradeWindow:
        existing = self._trade_windows.get(symbol.canonical)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return existing

        window = StockTradeWindow(
            self,
            symbol,
            self.display_names.get(symbol.canonical, ""),
        )
        self._trade_windows[symbol.canonical] = window
        return window

    def _add_trade_record(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
    ) -> bool:
        records = self.trades.setdefault(symbol.canonical, [])
        records.append(record)
        if self._save_state():
            self._refresh_performance_cells(symbol)
            self.status_var.set(
                f"已保存 {symbol.canonical} 的{record.trade_type}记录：{record.trade_date}。"
            )
            return True
        records.pop()
        if not records:
            self.trades.pop(symbol.canonical, None)
        return False

    def _update_trade_record(
        self,
        symbol: StockSymbol,
        index: int,
        record: TradeRecord,
    ) -> bool:
        records = self.trades.get(symbol.canonical, [])
        if not 0 <= index < len(records):
            return False
        if any(
            index in group.record_indices
            for group in self.t_trade_groups.get(symbol.canonical, [])
        ):
            return False
        previous = records[index]
        records[index] = record
        if self._save_state():
            self._refresh_performance_cells(symbol)
            self.status_var.set(
                f"已修改 {symbol.canonical} 的交易记录：{record.trade_date}。"
            )
            return True
        records[index] = previous
        return False

    def _delete_trade_record(self, symbol: StockSymbol, index: int) -> bool:
        records = self.trades.get(symbol.canonical, [])
        if not 0 <= index < len(records):
            return False
        previous_groups = list(self.t_trade_groups.get(symbol.canonical, []))
        if any(index in group.record_indices for group in previous_groups):
            return False
        adjusted_groups = [
            TTradeGroup(
                group.group_id,
                tuple(
                    record_index - 1 if record_index > index else record_index
                    for record_index in group.record_indices
                ),
            )
            for group in previous_groups
        ]
        if adjusted_groups:
            self.t_trade_groups[symbol.canonical] = adjusted_groups
        deleted = records.pop(index)
        if self._save_state():
            if not records:
                self.trades.pop(symbol.canonical, None)
                self.t_trade_groups.pop(symbol.canonical, None)
            self._refresh_performance_cells(symbol)
            self.status_var.set(
                f"已删除 {symbol.canonical} 的交易记录：{deleted.trade_date}。"
            )
            return True
        records.insert(index, deleted)
        if previous_groups:
            self.t_trade_groups[symbol.canonical] = previous_groups
        else:
            self.t_trade_groups.pop(symbol.canonical, None)
        return False

    def _calculate_t_trade_summary(
        self,
        symbol: StockSymbol,
        group: TTradeGroup,
    ) -> TTradeSummary | None:
        records = self.trades.get(symbol.canonical, [])
        try:
            selected_records = [records[index] for index in group.record_indices]
        except IndexError:
            return None
        if len(selected_records) < 2:
            return None

        buy_records = [
            record for record in selected_records if record.trade_type == "买入"
        ]
        sell_records = [
            record for record in selected_records if record.trade_type == "卖出"
        ]
        if not buy_records or not sell_records:
            return None

        try:
            buy_quantity = sum(
                (Decimal(record.quantity) for record in buy_records),
                Decimal("0"),
            )
            sell_quantity = sum(
                (Decimal(record.quantity) for record in sell_records),
                Decimal("0"),
            )
            buy_amount = sum(
                (
                    Decimal(record.price) * Decimal(record.quantity)
                    for record in buy_records
                ),
                Decimal("0"),
            )
            sell_amount = sum(
                (
                    Decimal(record.price) * Decimal(record.quantity)
                    for record in sell_records
                ),
                Decimal("0"),
            )
        except InvalidOperation:
            return None
        if buy_quantity <= 0 or sell_quantity <= 0:
            return None

        matched_quantity = min(buy_quantity, sell_quantity)
        average_buy_price = buy_amount / buy_quantity
        average_sell_price = sell_amount / sell_quantity
        realized_profit = (
            average_sell_price - average_buy_price
        ) * matched_quantity
        matched_buy_amount = average_buy_price * matched_quantity
        realized_return = realized_profit / matched_buy_amount
        net_quantity = buy_quantity - sell_quantity

        residual_record: TradeRecord | None = None
        if net_quantity != 0:
            residual_price = (
                average_buy_price if net_quantity > 0 else average_sell_price
            )
            residual_record = TradeRecord(
                trade_date=max(record.trade_date for record in selected_records),
                price=format(residual_price.normalize(), "f"),
                quantity=format(abs(net_quantity), "f"),
                trade_type="买入" if net_quantity > 0 else "卖出",
            )

        return TTradeSummary(
            trade_date=max(record.trade_date for record in selected_records),
            realized_profit=realized_profit,
            realized_return=realized_return,
            residual_record=residual_record,
            record_count=len(selected_records),
        )

    def _create_t_trade_group(
        self,
        symbol: StockSymbol,
        record_indices: tuple[int, ...],
    ) -> TTradeGroup | None:
        indices = tuple(sorted(set(record_indices)))
        if len(indices) < 2 or len(indices) != len(record_indices):
            return None
        claimed_indices = {
            index
            for group in self.t_trade_groups.get(symbol.canonical, [])
            for index in group.record_indices
        }
        if claimed_indices.intersection(indices):
            return None

        group = TTradeGroup(uuid.uuid4().hex, indices)
        if self._calculate_t_trade_summary(symbol, group) is None:
            return None
        groups = self.t_trade_groups.setdefault(symbol.canonical, [])
        groups.append(group)
        if self._save_state():
            self.status_var.set(
                f"已将 {len(indices)} 笔 {symbol.canonical} 交易标记为 T 交易。"
            )
            return group
        groups.pop()
        if not groups:
            self.t_trade_groups.pop(symbol.canonical, None)
        return None

    def _cancel_t_trade_group(
        self,
        symbol: StockSymbol,
        group_id: str,
    ) -> TTradeGroup | None:
        groups = self.t_trade_groups.get(symbol.canonical, [])
        group = next((item for item in groups if item.group_id == group_id), None)
        if group is None:
            return None
        index = groups.index(group)
        groups.pop(index)
        if not groups:
            self.t_trade_groups.pop(symbol.canonical, None)
        if self._save_state():
            self.status_var.set(f"已取消 {symbol.canonical} 的 T 交易标记。")
            return group
        restored_groups = self.t_trade_groups.setdefault(symbol.canonical, [])
        restored_groups.insert(index, group)
        return None

    def remove_selected(self, event: tk.Event | None = None) -> None:
        if self._refreshing:
            self.status_var.set("行情刷新中，请稍后再移除股票。")
            return
        tree = event.widget if event is not None else self._active_tree()
        if not isinstance(tree, ttk.Treeview):
            tree = self._active_tree()
        market = next(
            (name for name, market_tree in self.trees.items() if market_tree is tree),
            self._active_market(),
        )
        for item_id in tree.selection():
            if item_id not in self.symbols:
                continue
            tree.delete(item_id)
            self.symbols.pop(item_id, None)
            self.display_names.pop(item_id, None)
            self.current_prices.pop(item_id, None)
            self._price_history.pop(item_id, None)
            self.put_warrant_maturity_months.pop(item_id, None)
        self._schedule_market_cell_labels()
        self._update_market_spacers(market)
        self._refresh_statistics_dashboard()
        self._save_state()
        self.status_var.set(f"列表中共有 {len(self.symbols)} 只股票。")

    def _net_holding_quantity(self, symbol: StockSymbol) -> Decimal:
        net_quantity = Decimal("0")
        try:
            for record in self.trades.get(symbol.canonical, []):
                quantity = Decimal(record.quantity)
                if record.trade_type == "买入":
                    net_quantity += quantity
                elif record.trade_type == "卖出":
                    net_quantity -= quantity
        except InvalidOperation:
            return Decimal("0")
        return net_quantity

    def _is_closed_position(self, symbol: StockSymbol) -> bool:
        return bool(self.trades.get(symbol.canonical)) and (
            self._net_holding_quantity(symbol) == 0
        )

    def _apply_closed_position_visibility(self, market: str) -> None:
        tree = self.trees.get(market)
        if tree is None:
            return

        visible_ids = set(tree.get_children(""))
        show_closed = self.show_closed_positions[market]
        for item_id, symbol in self.symbols.items():
            if symbol.market != market or not tree.exists(item_id):
                continue
            should_show = show_closed or not self._is_closed_position(symbol)
            if should_show and item_id not in visible_ids:
                tree.move(item_id, "", tk.END)
                visible_ids.add(item_id)
            elif not should_show and item_id in visible_ids:
                tree.detach(item_id)
                visible_ids.remove(item_id)

        self._reorder_market_rows(market)

    @staticmethod
    def _market_column_at(tree: ttk.Treeview, x: int) -> str | None:
        column_id = tree.identify_column(x)
        if not column_id.startswith("#"):
            return None
        try:
            index = int(column_id[1:]) - 1
        except ValueError:
            return None
        return TABLE_COLUMNS[index] if 0 <= index < len(TABLE_COLUMNS) else None

    def _sort_market_rows(self, market: str, column: str, descending: bool) -> None:
        tree = self.trees.get(market)
        if tree is None or column not in SORTABLE_MARKET_COLUMNS:
            return

        sortable_items: list[tuple[str, Decimal]] = []
        missing_items: list[str] = []
        for item_id in tree.get_children(""):
            if item_id not in self.symbols:
                continue
            match = re.match(
                r"^\s*([+-]?\d[\d,]*(?:\.\d+)?)",
                tree.set(item_id, column),
            )
            if match is None:
                missing_items.append(item_id)
                continue
            sortable_items.append(
                (item_id, Decimal(match.group(1).replace(",", "")))
            )

        sortable_items.sort(key=lambda item: item[1], reverse=descending)
        ordered_items = [item_id for item_id, _value in sortable_items]
        ordered_items.extend(missing_items)
        for position, item_id in enumerate(ordered_items):
            tree.move(item_id, "", position)

        self.market_sorts[market] = (column, descending)
        for heading_column in TABLE_COLUMNS:
            heading = TABLE_HEADINGS[heading_column]
            if heading_column == column:
                heading += " ↓" if descending else " ↑"
            tree.heading(heading_column, text=heading)
        self._update_market_spacers(market)
        self._schedule_market_cell_labels()

    def _reorder_market_rows(self, market: str) -> None:
        market_sort = self.market_sorts.get(market)
        if market_sort is not None:
            self._sort_market_rows(market, *market_sort)
            return

        tree = self.trees.get(market)
        if tree is None:
            return

        item_ids = [
            item_id
            for item_id in tree.get_children("")
            if item_id in self.symbols
        ]
        ordered_ids = sorted(
            item_ids,
            key=lambda item_id: (
                0
                if (
                    (symbol := self.symbols.get(item_id)) is not None
                    and self._net_holding_quantity(symbol) != 0
                )
                else 1
            ),
        )
        for position, item_id in enumerate(ordered_ids):
            tree.move(item_id, "", position)
        self._update_market_spacers(market)
        self._schedule_market_cell_labels()

    def _calculate_market_metrics(
        self,
        market: str,
    ) -> MarketMetrics:
        currency = "USD" if market == "US" else "HKD"
        total_market_value = Decimal("0")
        total_buy_cost = Decimal("0")
        total_profit = Decimal("0")
        has_records = False

        try:
            for symbol in self.symbols.values():
                if symbol.market != market:
                    continue
                records = self.trades.get(symbol.canonical, [])
                if not records:
                    continue
                has_records = True

                current_quote = self.current_prices.get(symbol.canonical)
                if current_quote is None or current_quote[1] != currency:
                    return MarketMetrics(
                        currency=currency,
                        has_records=True,
                        complete=False,
                        market_value=Decimal("0"),
                        profit=Decimal("0"),
                        return_rate=None,
                    )

                buy_cost = Decimal("0")
                sell_proceeds = Decimal("0")
                net_quantity = Decimal("0")
                for record in records:
                    price = Decimal(record.price)
                    quantity = Decimal(record.quantity)
                    amount = price * quantity
                    if record.trade_type == "买入":
                        buy_cost += amount
                        net_quantity += quantity
                    elif record.trade_type == "卖出":
                        sell_proceeds += amount
                        net_quantity -= quantity

                current_price = current_quote[0]
                total_market_value += current_price * net_quantity
                total_buy_cost += buy_cost
                total_profit += (
                    current_price * net_quantity
                    + sell_proceeds
                    - buy_cost
                )
        except InvalidOperation:
            return MarketMetrics(
                currency=currency,
                has_records=has_records,
                complete=False,
                market_value=Decimal("0"),
                profit=Decimal("0"),
                return_rate=None,
            )

        return MarketMetrics(
            currency=currency,
            has_records=has_records,
            complete=True,
            market_value=total_market_value,
            profit=total_profit,
            return_rate=(
                None if total_buy_cost <= 0 else total_profit / total_buy_cost
            ),
        )

    def _calculate_position_metrics(
        self,
        symbol: StockSymbol,
    ) -> tuple[str, str, str, str, str]:
        current_quote = self.current_prices.get(symbol.canonical)
        records = self.trades.get(symbol.canonical, [])
        if not records:
            return "--", "--", "--", "--", "--"

        currency = (
            current_quote[1] if current_quote is not None else symbol.default_currency
        )
        buy_cost = Decimal("0")
        sell_proceeds = Decimal("0")
        net_quantity = Decimal("0")
        try:
            for record in records:
                amount = Decimal(record.price) * Decimal(record.quantity)
                quantity = Decimal(record.quantity)
                if record.trade_type == "买入":
                    buy_cost += amount
                    net_quantity += quantity
                elif record.trade_type == "卖出":
                    sell_proceeds += amount
                    net_quantity -= quantity
        except InvalidOperation:
            return "--", "--", "--", "--", "--"

        holding_count_text = f"{net_quantity:,.0f}"
        holding_cost_text = (
            "--"
            if net_quantity <= 0
            else f"{(buy_cost - sell_proceeds) / net_quantity:,.3f} {currency}"
        )
        if current_quote is None:
            return holding_count_text, holding_cost_text, "--", "--", "--"

        current_price, _currency = current_quote
        market_value_text = f"{current_price * net_quantity:,.2f} {currency}"
        profit = current_price * net_quantity + sell_proceeds - buy_cost
        profit_text = f"{profit:+,.2f} {currency}"
        return_text = (
            "--"
            if buy_cost <= 0
            else f"{profit / buy_cost * Decimal('100'):+.2f}%"
        )
        return (
            holding_count_text,
            holding_cost_text,
            market_value_text,
            profit_text,
            return_text,
        )

    def _calculate_performance(self, symbol: StockSymbol) -> tuple[str, str]:
        _, _, _, profit, return_rate = self._calculate_position_metrics(symbol)
        return profit, return_rate

    def _calculate_trade_performance(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
        anchor_index: int,
    ) -> tuple[str, str]:
        values = self._calculate_trade_performance_values(
            symbol,
            record,
            anchor_index,
        )
        if values is None:
            return "--", "--"

        profit, return_rate, currency = values
        return (
            f"{profit:+,.2f} {currency}",
            f"{return_rate * Decimal('100'):+.2f}%",
        )

    def _calculate_trade_performance_values(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
        anchor_index: int,
    ) -> tuple[Decimal, Decimal, str] | None:
        current_quote = self.current_prices.get(symbol.canonical)
        try:
            trade_price = Decimal(record.price)
            quantity = Decimal(record.quantity)
        except InvalidOperation:
            return None
        if trade_price <= 0 or quantity <= 0:
            return None

        if record.trade_type == "买入":
            if current_quote is None:
                return None
            current_price, currency = current_quote
            profit = (current_price - trade_price) * quantity
            return_rate = (current_price - trade_price) / trade_price
            return profit, return_rate, currency
        if record.trade_type != "卖出":
            return None

        # A sale is realized against the average holding cost right before it,
        # so its result no longer follows the live price.
        cost_basis = self._sell_cost_basis(symbol, anchor_index, quantity)
        if cost_basis is None:
            return None
        currency = (
            current_quote[1] if current_quote is not None else symbol.default_currency
        )
        profit = (trade_price - cost_basis) * quantity
        return_rate = (trade_price - cost_basis) / cost_basis
        return profit, return_rate, currency

    def _sell_cost_basis(
        self,
        symbol: StockSymbol,
        anchor_index: int,
        quantity: Decimal,
    ) -> Decimal | None:
        held_quantity, held_cost = self._holding_before_trade(symbol, anchor_index)
        if held_quantity <= 0 or quantity > held_quantity:
            return None
        return held_cost / held_quantity

    def _sell_cost_remark(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
        anchor_index: int,
    ) -> str:
        if record.trade_type != "卖出":
            return ""
        try:
            quantity = Decimal(record.quantity)
        except InvalidOperation:
            return ""
        held_quantity, held_cost = self._holding_before_trade(symbol, anchor_index)
        holding_text = f"卖出前持仓 {held_quantity:,.0f}"
        if held_quantity <= 0 or quantity > held_quantity:
            return f"{holding_text}，少于卖出份额，无法计算成本价"
        current_quote = self.current_prices.get(symbol.canonical)
        currency = (
            current_quote[1] if current_quote is not None else symbol.default_currency
        )
        return (
            f"成本价 {held_cost / held_quantity:,.3f} {currency}｜{holding_text}"
        )

    def _holding_before_trade(
        self,
        symbol: StockSymbol,
        anchor_index: int,
    ) -> tuple[Decimal, Decimal]:
        records = self.trades.get(symbol.canonical, [])
        held_quantity = Decimal("0")
        held_cost = Decimal("0")
        for index in sorted(
            range(len(records)),
            key=lambda item: (records[item].trade_date, item),
        ):
            if index == anchor_index:
                break
            record = records[index]
            try:
                price = Decimal(record.price)
                record_quantity = Decimal(record.quantity)
            except InvalidOperation:
                continue
            if price <= 0 or record_quantity <= 0:
                continue
            if record.trade_type == "买入":
                held_quantity += record_quantity
                held_cost += price * record_quantity
            elif record.trade_type == "卖出":
                if record_quantity < held_quantity:
                    held_cost -= held_cost / held_quantity * record_quantity
                    held_quantity -= record_quantity
                else:
                    held_quantity = Decimal("0")
                    held_cost = Decimal("0")
        return held_quantity, held_cost

    def _trade_return_exceeds_threshold(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
        anchor_index: int,
    ) -> bool:
        values = self._calculate_trade_performance_values(
            symbol,
            record,
            anchor_index,
        )
        return values is not None and values[1] > HIGH_RETURN_THRESHOLD

    def _refresh_performance_cells(self, symbol: StockSymbol) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        current = list(tree.item(symbol.canonical, "values"))
        if len(current) != len(TABLE_COLUMNS):
            return
        current[4], current[5], current[6], current[7], current[8] = (
            self._calculate_position_metrics(symbol)
        )
        tree.item(symbol.canonical, values=current)
        self._apply_closed_position_visibility(symbol.market)
        self._refresh_statistics_dashboard()
        trade_window = self._trade_windows.get(symbol.canonical)
        if trade_window is not None and trade_window.winfo_exists():
            trade_window.refresh_summary()

    def _start_refresh(self, symbols: list[StockSymbol]) -> None:
        if self._refreshing:
            self.status_var.set("当前刷新尚未完成，请稍候。")
            return

        self._refreshing = True
        self.refresh_button.configure(state=tk.DISABLED)
        self.progress.pack(fill=tk.X, pady=(8, 0))
        self.progress.start(12)
        self.status_var.set(
            f"正在刷新 {len(symbols)} 只股票并更新汇率……"
        )

        worker = threading.Thread(
            target=self._refresh_worker,
            args=(symbols,),
            daemon=True,
        )
        worker.start()

    def _refresh_worker(self, symbols: list[StockSymbol]) -> None:
        worker_count = min(REFRESH_MAX_WORKERS, max(1, len(symbols) + 1))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    fetch_exchange_rates,
                    REFRESH_EXCHANGE_RATE_TIMEOUT_SECONDS,
                ): ("exchange_rates", None)
            }
            for symbol in symbols:
                future = executor.submit(self._fetch_refresh_quotes, symbol)
                future_map[future] = ("quote", symbol)

            for future in as_completed(future_map):
                kind, symbol = future_map[future]
                try:
                    result = future.result()
                    if kind == "exchange_rates":
                        self._result_queue.put(("exchange_rates", result))
                    else:
                        quotes, errors = result
                        self._result_queue.put(("quote", symbol, quotes, errors))
                except Exception as exc:
                    if kind == "exchange_rates":
                        self._result_queue.put(("exchange_rate_error", str(exc)))
                    else:
                        self._result_queue.put(("error", symbol, str(exc)))
        self._result_queue.put(("done", len(symbols), datetime.now()))

    @staticmethod
    def _fetch_refresh_quotes(
        symbol: StockSymbol,
    ) -> tuple[list[PriceQuote], dict[str, str]]:
        primary = PRIMARY_PROVIDER_BY_MARKET[symbol.market]
        quotes, errors = fetch_quotes(
            symbol,
            [primary],
            timeout=REFRESH_REQUEST_TIMEOUT_SECONDS,
        )
        if quotes:
            return quotes, errors

        fallback_quotes, fallback_errors = fetch_quotes(
            symbol,
            FALLBACK_PROVIDERS_BY_MARKET[symbol.market],
            timeout=REFRESH_REQUEST_TIMEOUT_SECONDS,
        )
        errors.update(fallback_errors)
        return fallback_quotes, errors

    def _process_result_queue(self) -> None:
        try:
            while True:
                message = self._result_queue.get_nowait()
                kind = message[0]
                if kind == "quote":
                    _, symbol, quotes, errors = message
                    self._update_row(symbol, quotes, errors)
                elif kind == "error":
                    _, symbol, error = message
                    self._show_row_error(symbol, error)
                elif kind == "exchange_rates":
                    _, rates = message
                    self.exchange_rates = rates
                    self.exchange_rate_warning = ""
                    self._refresh_statistics_dashboard()
                elif kind == "exchange_rate_error":
                    self.exchange_rate_warning = (
                        "汇率刷新失败，沿用上次成功值"
                        if self.exchange_rates is not None
                        else "汇率刷新失败，暂无可用汇率"
                    )
                    self._refresh_statistics_dashboard()
                elif kind == "search":
                    _, generation, query, results, errors = message
                    self._show_search_results(
                        generation,
                        query,
                        results,
                        errors,
                    )
                elif kind == "search_error":
                    _, generation, query, error = message
                    if (
                        generation == self._search_generation
                        and query == self.code_var.get().strip()
                    ):
                        self._suggestions = []
                        self._hide_suggestions()
                        self.status_var.set(f"搜索失败：{error}")
                elif kind == "title_icon_menu_command":
                    _, command = message
                    self._dispatch_title_icon_menu_command(command)
                elif kind == "cloud_status":
                    if self._cloud_syncing:
                        self.status_var.set(message[1])
                elif kind == "cloud_upload_done":
                    _, info = message
                    self._set_cloud_sync_busy(False)
                    uploaded_time = self._format_cloud_time(info.uploaded_at)
                    self.status_var.set(
                        f"完整状态已同步到 Google Drive · 版本 {info.revision}"
                    )
                    messagebox.showinfo(
                        "同步完成",
                        f"完整状态已同步到 Google Drive。\n"
                        f"云端版本：{info.revision}\n上传时间：{uploaded_time}",
                        parent=self,
                    )
                elif kind == "cloud_download_ready":
                    _, state_bytes, info = message
                    self._set_cloud_sync_busy(False)
                    self._apply_downloaded_cloud_state(state_bytes, info)
                elif kind == "cloud_error":
                    _, action, error = message
                    self._set_cloud_sync_busy(False)
                    self.status_var.set(f"{action}失败：{error}")
                    messagebox.showerror(
                        f"{action}失败",
                        error,
                        parent=self,
                    )
                elif kind == "done":
                    _, count, finished_at = message
                    self._finish_refresh(count, finished_at)
        except queue.Empty:
            pass
        finally:
            self._queue_after_id = self.after(100, self._process_result_queue)

    def _update_row(
        self,
        symbol: StockSymbol,
        quotes: list[PriceQuote],
        errors: dict[str, str],
    ) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        if not quotes:
            detail = "；".join(errors.values()) or "所有数据源均未返回价格"
            self._show_row_error(symbol, detail)
            return

        best = choose_freshest(quotes)
        display_name = self.display_names.get(symbol.canonical, "").strip()
        if not _contains_chinese(display_name):
            chinese_name = next(
                (
                    item.name.strip()
                    for item in quotes
                    if item.name.strip() and _contains_chinese(item.name)
                ),
                "",
            )
            if chinese_name:
                display_name = chinese_name
                self.display_names[symbol.canonical] = chinese_name
        if not display_name:
            display_name = best.name or "--"
        maturity = _put_warrant_maturity_month(quotes)
        if maturity is not None:
            self.put_warrant_maturity_months[symbol.canonical] = maturity
        percentage = (
            "--" if best.change_percent is None else f"{best.change_percent:+.2f}%"
        )
        price = f"{best.price:,.3f} {best.currency}"
        current_price = Decimal(str(best.price))
        self.current_prices[symbol.canonical] = (
            current_price,
            best.currency,
        )
        surge = self._check_price_surge(symbol.canonical, current_price)
        if surge is not None:
            base_price, elapsed_seconds = surge
            self._pending_surge_alerts.append(
                SurgeAlert(
                    symbol.canonical,
                    display_name,
                    base_price,
                    current_price,
                    best.currency,
                    elapsed_seconds,
                )
            )
        holding_count, holding_cost, market_value, profit, return_rate = (
            self._calculate_position_metrics(symbol)
        )

        tag = "waiting"
        if best.change_percent is not None:
            tag = "up" if best.change_percent > 0 else "down" if best.change_percent < 0 else ""

        tree.item(
            symbol.canonical,
            values=(
                symbol.canonical,
                display_name,
                price,
                percentage,
                holding_count,
                holding_cost,
                market_value,
                profit,
                return_rate,
            ),
            tags=(tag,) if tag else (),
        )
        market_sort = self.market_sorts.get(symbol.market)
        if market_sort is not None:
            self._sort_market_rows(symbol.market, *market_sort)
        self._schedule_market_cell_labels()
        self._refresh_selected_warrant_validity()
        self._refresh_statistics_dashboard()
        trade_window = self._trade_windows.get(symbol.canonical)
        if trade_window is not None and trade_window.winfo_exists():
            trade_window.refresh_summary()
        self._save_state()

    def _check_price_surge(
        self,
        code: str,
        price: Decimal,
    ) -> tuple[Decimal, float] | None:
        if price <= 0:
            return None
        now = time.monotonic()
        history = [
            sample
            for sample in self._price_history.get(code, [])
            if now - sample[0] <= SURGE_ALERT_WINDOW_SECONDS
        ]
        if history:
            base_time, base_price = min(history, key=lambda sample: sample[1])
            if price > base_price * (1 + SURGE_ALERT_THRESHOLD):
                # Start over from the alerted price so one surge only alerts once.
                self._price_history[code] = [(now, price)]
                return base_price, now - base_time
        history.append((now, price))
        self._price_history[code] = history
        return None

    def _show_row_error(self, symbol: StockSymbol, error: str) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        current = list(tree.item(symbol.canonical, "values"))
        if len(current) == len(TABLE_COLUMNS) and current[2] == "--":
            tree.item(symbol.canonical, values=current, tags=("waiting",))
        self._schedule_market_cell_labels()
        self._refresh_statistics_dashboard()
        self._save_state()
        self.status_var.set(f"{symbol.canonical} 行情获取失败：{error[:100]}")

    def _finish_refresh(self, count: int, finished_at: datetime) -> None:
        self._refreshing = False
        self.last_refresh_time = finished_at
        self.progress.stop()
        self.progress.pack_forget()
        self.refresh_button.configure(state=tk.NORMAL)
        self.status_var.set(
            f"已刷新 {count} 只股票 · {finished_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._refresh_statistics_dashboard()
        self._schedule_auto_refresh()
        self._save_state()
        self.code_entry.focus_set()
        if self._selected_result is not None:
            self.add_stock()
        if self._pending_surge_alerts:
            alerts = self._pending_surge_alerts
            self._pending_surge_alerts = []
            SurgeAlertWindow(self, alerts)


def main() -> None:
    _configure_windows_app_identity()
    password_dialog = PasswordDialog()
    if not password_dialog.run():
        return
    try:
        app = StockPriceApp()
    except AlreadyRunningError:
        return
    app.mainloop()


if __name__ == "__main__":
    main()
