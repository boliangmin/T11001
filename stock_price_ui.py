#!/usr/bin/env python3
"""美股 / 港股公开行情桌面界面。"""

from __future__ import annotations

import calendar
import json
import os
import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter import font as tkfont

from stock_price import (
    PriceQuote,
    StockSearchResult,
    StockSymbol,
    choose_freshest,
    fetch_quotes,
    normalize_symbol,
    search_stocks,
)


PROVIDER_NAMES = ["yahoo", "tencent", "sina"]
MARKET_ORDER = ("US", "HK")
MARKET_TITLES = {"US": "美股", "HK": "港股"}
TABLE_COLUMNS = (
    "code",
    "name",
    "price",
    "change",
    "cost",
    "profit",
    "return",
)
TABLE_HEADINGS = {
    "code": "股票代码",
    "name": "中文名称",
    "price": "最新价格",
    "change": "涨跌幅",
    "cost": "持仓成本",
    "profit": "盈亏",
    "return": "收益率",
}
DEFAULT_COLUMN_WIDTHS = {
    "code": 95,
    "name": 135,
    "price": 110,
    "change": 75,
    "cost": 100,
    "profit": 125,
    "return": 95,
}
APP_BACKGROUND = "#F4F7FB"
SURFACE = "#FFFFFF"
SURFACE_MUTED = "#EAF0F8"
TEXT_PRIMARY = "#172033"
TEXT_SECONDARY = "#667085"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
ACCENT_SOFT = "#DBEAFE"
BUY_COLOR = "#DC2626"
SELL_COLOR = "#059669"
HIGH_RETURN_BACKGROUND = "#FEF3C7"
HIGH_RETURN_THRESHOLD = Decimal("0.20")
DIVIDER = "#D7E0EC"
STATE_VERSION = 4
WINDOW_GEOMETRY_PATTERN = re.compile(
    r"^(?P<width>\d+)x(?P<height>\d+)(?P<x>[+-]\d+)(?P<y>[+-]\d+)$"
)


def _default_state_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "StockPriceViewer" / "state.json"
    return Path.home() / ".stock_price_viewer" / "state.json"


def _contains_chinese(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


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

        outer = ttk.Frame(self, padding=(14, 12, 14, 12))
        outer.pack(fill=tk.BOTH, expand=True)
        controls = ttk.Frame(outer)
        controls.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(controls, text="‹", width=3, command=self._previous_month).pack(
            side=tk.LEFT
        )
        self.year_var = tk.IntVar(value=self._year)
        year_spinner = ttk.Spinbox(
            controls,
            from_=1900,
            to=2100,
            textvariable=self.year_var,
            width=7,
            command=self._render_from_controls,
        )
        year_spinner.pack(side=tk.LEFT, padx=(18, 4))
        ttk.Label(controls, text="年").pack(side=tk.LEFT)
        self.month_var = tk.IntVar(value=self._month)
        month_spinner = ttk.Spinbox(
            controls,
            from_=1,
            to=12,
            textvariable=self.month_var,
            width=4,
            command=self._render_from_controls,
        )
        month_spinner.pack(side=tk.LEFT, padx=(10, 4))
        ttk.Label(controls, text="月").pack(side=tk.LEFT)
        ttk.Button(controls, text="›", width=3, command=self._next_month).pack(
            side=tk.RIGHT
        )
        year_spinner.bind("<Return>", self._render_from_controls)
        month_spinner.bind("<Return>", self._render_from_controls)
        year_spinner.bind("<FocusOut>", self._render_from_controls)
        month_spinner.bind("<FocusOut>", self._render_from_controls)

        self.days_frame = ttk.Frame(outer)
        self.days_frame.pack(fill=tk.BOTH, expand=True)
        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="今天", command=self._select_today).pack(side=tk.LEFT)
        ttk.Button(footer, text="取消", command=self._close).pack(side=tk.RIGHT)

        self._render_days()
        _center_and_show(self, owner)
        self.grab_set()

    def _render_from_controls(self, _event: tk.Event | None = None) -> None:
        try:
            year = int(self.year_var.get())
            month = int(self.month_var.get())
        except (tk.TclError, ValueError):
            return
        if not 1900 <= year <= 2100 or not 1 <= month <= 12:
            return
        self._year = year
        self._month = month
        self._render_days()

    def _change_month(self, offset: int) -> None:
        month_index = self._year * 12 + self._month - 1 + offset
        year, zero_based_month = divmod(month_index, 12)
        if not 1900 <= year <= 2100:
            return
        self._year = year
        self._month = zero_based_month + 1
        self.year_var.set(self._year)
        self.month_var.set(self._month)
        self._render_days()

    def _previous_month(self) -> None:
        self._change_month(-1)

    def _next_month(self) -> None:
        self._change_month(1)

    def _render_days(self) -> None:
        for widget in self.days_frame.winfo_children():
            widget.destroy()
        for column, weekday in enumerate(("一", "二", "三", "四", "五", "六", "日")):
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
        for row, week in enumerate(weeks, start=1):
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
                    width=4,
                    style="SelectedDay.TButton" if is_selected else "TButton",
                    command=lambda day=day_number: self._select_day(day),
                ).grid(row=row, column=column, padx=2, pady=2)

    def _select_day(self, day_number: int) -> None:
        chosen = date(self._year, self._month, day_number)
        self._on_select(chosen.isoformat())
        self._close()

    def _select_today(self) -> None:
        self._on_select(date.today().isoformat())
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

        ttk.Label(content, text=f"交易价格（{currency}）").grid(
            row=2,
            column=0,
            sticky=tk.W,
            pady=(0, 12),
        )
        self.price_var = tk.StringVar(
            value=initial_record.price if initial_record is not None else ""
        )
        self.price_entry = ttk.Entry(
            content,
            textvariable=self.price_var,
            width=28,
        )
        self.price_entry.grid(row=2, column=1, sticky=tk.EW, pady=(0, 12))

        ttk.Label(content, text="交易份额").grid(
            row=3,
            column=0,
            sticky=tk.W,
            pady=(0, 16),
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
        self.quantity_entry.grid(row=3, column=1, sticky=tk.EW, pady=(0, 16))

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
        self.price_entry.focus_set()

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
        self.title(f"交易记录 - {display_name or symbol.canonical}")
        self.geometry("650x430")
        self.minsize(520, 340)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self._close)

        outer = ttk.Frame(self, padding=(20, 18, 20, 16))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 14))
        title_area = ttk.Frame(header)
        title_area.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            title_area,
            text=f"{display_name or '--'}  ·  {symbol.canonical}",
            style="Title.TLabel",
        ).pack(anchor=tk.W)

        metrics = ttk.Frame(title_area)
        metrics.pack(anchor=tk.W, pady=(7, 0))
        self.profit_summary_var = tk.StringVar(value="--")
        self.return_summary_var = tk.StringVar(value="--")
        ttk.Label(metrics, text="总盈亏", style="Hint.TLabel").pack(side=tk.LEFT)
        self.profit_summary_label = ttk.Label(
            metrics,
            textvariable=self.profit_summary_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.profit_summary_label.pack(side=tk.LEFT, padx=(8, 28))
        ttk.Label(metrics, text="总收益率", style="Hint.TLabel").pack(side=tk.LEFT)
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
            columns=(
                "trade_date",
                "trade_type",
                "price",
                "quantity",
                "profit",
                "return",
            ),
            show="headings",
            selectmode="browse",
        )
        self.trade_tree.heading("trade_date", text="交易日期", anchor=tk.CENTER)
        self.trade_tree.heading("trade_type", text="交易类型", anchor=tk.CENTER)
        self.trade_tree.heading("price", text="交易价格", anchor=tk.CENTER)
        self.trade_tree.heading("quantity", text="交易份额", anchor=tk.CENTER)
        self.trade_tree.heading("profit", text="盈亏", anchor=tk.CENTER)
        self.trade_tree.heading("return", text="收益率", anchor=tk.CENTER)
        self.trade_tree.column("trade_date", width=100, anchor=tk.CENTER)
        self.trade_tree.column("trade_type", width=70, anchor=tk.CENTER)
        self.trade_tree.column("price", width=105, anchor=tk.CENTER)
        self.trade_tree.column("quantity", width=80, anchor=tk.CENTER)
        self.trade_tree.column("profit", width=125, anchor=tk.CENTER)
        self.trade_tree.column("return", width=85, anchor=tk.CENTER)
        self.trade_tree.tag_configure("buy", foreground=BUY_COLOR)
        self.trade_tree.tag_configure("sell", foreground=SELL_COLOR)
        self.trade_tree.tag_configure(
            "high_return",
            background=HIGH_RETURN_BACKGROUND,
        )
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.trade_tree.yview,
        )
        self.trade_tree.configure(yscrollcommand=scrollbar.set)
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
        self.context_menu.add_command(label="修改", command=self._modify_selected)
        self.context_menu.add_command(label="删除", command=self._delete_selected)
        self.trade_tree.bind("<Button-1>", self._clear_selection_on_blank)
        self.trade_tree.bind("<Button-3>", self._show_context_menu)

        ttk.Label(
            outer,
            text="右键交易记录可修改或删除；行情接口价格不会作为交易记录保存。",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))
        self._reload_records()
        _center_and_show(self, owner, fit_content=False)

    def _reload_records(self) -> None:
        self.trade_tree.delete(*self.trade_tree.get_children(""))
        for index, record in enumerate(
            self.owner.trades.get(self.symbol.canonical, [])
        ):
            profit, return_rate = self.owner._calculate_trade_performance(
                self.symbol,
                record,
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
                ),
                tags=self._trade_row_tags(record),
            )
        self.refresh_summary()

    def refresh_summary(self) -> None:
        profit, return_rate = self.owner._calculate_performance(self.symbol)
        self.profit_summary_var.set(profit)
        self.return_summary_var.set(return_rate)
        self.profit_summary_label.configure(
            foreground=self._metric_color(profit)
        )
        self.return_summary_label.configure(
            foreground=self._metric_color(return_rate)
        )
        records = self.owner.trades.get(self.symbol.canonical, [])
        for index, record in enumerate(records):
            item_id = f"trade-{index}"
            if not self.trade_tree.exists(item_id):
                continue
            values = list(self.trade_tree.item(item_id, "values"))
            if len(values) != 6:
                continue
            values[4], values[5] = self.owner._calculate_trade_performance(
                self.symbol,
                record,
            )
            self.trade_tree.item(
                item_id,
                values=values,
                tags=self._trade_row_tags(record),
            )

    def _trade_row_tags(self, record: TradeRecord) -> tuple[str, ...]:
        tags = ["buy" if record.trade_type == "买入" else "sell"]
        if self.owner._trade_return_exceeds_threshold(self.symbol, record):
            tags.append("high_return")
        return tuple(tags)

    @staticmethod
    def _metric_color(value: str) -> str:
        if value.startswith("+"):
            return BUY_COLOR
        if value.startswith("-"):
            return SELL_COLOR
        return TEXT_SECONDARY

    def _clear_selection_on_blank(self, event: tk.Event) -> None:
        row_id = self.trade_tree.identify_row(event.y)
        region = self.trade_tree.identify_region(event.x, event.y)
        if not row_id and region not in {"heading", "separator"}:
            selected_items = self.trade_tree.selection()
            if selected_items:
                self.trade_tree.selection_remove(*selected_items)
            self.trade_tree.focus("")

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
        if not selection or not selection[0].startswith("trade-"):
            return None
        try:
            index = int(selection[0].removeprefix("trade-"))
        except ValueError:
            return None
        records = self.owner.trades.get(self.symbol.canonical, [])
        return index if 0 <= index < len(records) else None

    def _show_context_menu(self, event: tk.Event) -> str | None:
        item_id = self.trade_tree.identify_row(event.y)
        if not item_id:
            return None
        self.trade_tree.selection_set(item_id)
        self.trade_tree.focus(item_id)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

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
        self.owner._trade_windows.pop(self.symbol.canonical, None)
        self.destroy()


class StockPriceApp(tk.Tk):
    def __init__(self, state_path: Path | None = None) -> None:
        super().__init__()
        self.withdraw()
        self.configure(background=APP_BACKGROUND)
        self.title("美股 / 港股行情")
        self.geometry("1050x600")
        self.minsize(820, 430)

        self._state_path = state_path or _default_state_path()
        self.symbols: dict[str, StockSymbol] = {}
        self.display_names: dict[str, str] = {}
        self.trades: dict[str, list[TradeRecord]] = {}
        self.current_prices: dict[str, tuple[Decimal, str]] = {}
        self._trade_windows: dict[str, StockTradeWindow] = {}
        self.trees: dict[str, ttk.Treeview] = {}
        self.market_pages: dict[str, ttk.Frame] = {}
        self._swipe_start: tuple[ttk.Treeview, int, int] | None = None
        self._column_resize_tree: ttk.Treeview | None = None
        self._profit_return_resize_start: (
            tuple[ttk.Treeview, int, int, int] | None
        ) = None
        self._result_queue: queue.Queue[tuple] = queue.Queue()
        self._refreshing = False
        self._queue_after_id: str | None = None
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
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _center_and_show(self, fit_content=False)
        self._queue_after_id = self.after(100, self._process_result_queue)

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
            foreground=BUY_COLOR,
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
            foreground=SURFACE,
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
            foreground=[("disabled", "#F8FAFC"), ("active", SURFACE)],
        )
        style.configure(
            "Action.TButton",
            background=SURFACE,
            foreground=ACCENT,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            padding=(16, 3, 16, 4),
        )
        style.map(
            "Action.TButton",
            background=[("active", ACCENT_SOFT), ("pressed", "#BFDBFE")],
            foreground=[("disabled", "#98A2B3"), ("active", ACCENT_HOVER)],
        )
        style.configure(
            "TRadiobutton",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            padding=(0, 3),
        )
        style.configure("Buy.TRadiobutton", foreground=BUY_COLOR)
        style.configure("Sell.TRadiobutton", foreground=SELL_COLOR)
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
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT), ("active", ACCENT_SOFT)],
            foreground=[("selected", SURFACE), ("active", TEXT_PRIMARY)],
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
            rowheight=32,
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
            arrowcolor=TEXT_SECONDARY,
        )
        style.map("TScrollbar", background=[("active", ACCENT)])
        style.configure(
            "SelectedDay.TButton",
            background=ACCENT,
            foreground=SURFACE,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "SelectedDay.TButton",
            background=[("active", ACCENT_HOVER), ("pressed", "#1E40AF")],
            foreground=[("active", SURFACE)],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(22, 18, 22, 14))
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text="股票行情", style="Title.TLabel").pack(
            anchor=tk.W,
            pady=(0, 16),
        )

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
                tree.heading(
                    column,
                    text=TABLE_HEADINGS[column],
                    anchor=tk.CENTER,
                )
                tree.column(
                    column,
                    width=DEFAULT_COLUMN_WIDTHS[column],
                    minwidth=60,
                    anchor=tk.CENTER,
                    stretch=column in {"name", "profit"},
                )

            tree.tag_configure("up", foreground=BUY_COLOR)
            tree.tag_configure("down", foreground=SELL_COLOR)
            tree.tag_configure("waiting", foreground=TEXT_SECONDARY)
            tree.bind("<Button-1>", self._clear_selection_on_blank)
            tree.bind("<ButtonPress-1>", self._begin_market_swipe, add="+")
            tree.bind("<B1-Motion>", self._resize_profit_and_return, add="+")
            tree.bind("<ButtonRelease-1>", self._finish_market_swipe, add="+")
            tree.bind("<Shift-MouseWheel>", self._switch_market_with_wheel)
            tree.bind("<Double-1>", self._open_stock_trade_window)
            tree.bind("<Delete>", self.remove_selected)

            vertical = ttk.Scrollbar(page, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=vertical.set)

            tree.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            page.rowconfigure(0, weight=1)
            page.columnconfigure(0, weight=1)

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="请输入股票名称或代码，并从候选项中选择。")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Label(
            footer,
            text="公开行情可能延迟｜左右滑动切换市场｜Delete 可移除",
            style="Hint.TLabel",
        ).pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=160)
        self.code_entry.focus_set()

    def _restore_geometry(self, geometry: object) -> bool:
        match = WINDOW_GEOMETRY_PATTERN.fullmatch(str(geometry))
        if match is None:
            return False

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = max(820, min(int(match.group("width")), screen_width))
        height = max(430, min(int(match.group("height")), screen_height))
        self.geometry(f"{width}x{height}")
        return True

    def _active_market(self) -> str:
        selected_page = self.table_frame.select()
        for market, page in self.market_pages.items():
            if str(page) == selected_page:
                return market
        return MARKET_ORDER[0]

    def _active_tree(self) -> ttk.Treeview:
        return self.trees[self._active_market()]

    def _select_market(self, market: str) -> None:
        page = self.market_pages.get(market)
        if page is not None:
            self.table_frame.select(page)

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

            layout_restored = self._restore_geometry(payload.get("window_geometry"))
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
                    column for column in TABLE_COLUMNS if column != "cost"
                )
                if (
                    "cost" not in saved_market_widths
                    and all(column in valid_widths for column in legacy_columns)
                ):
                    cost_width = DEFAULT_COLUMN_WIDTHS["cost"]
                    valid_widths["cost"] = cost_width
                    remaining_reduction = cost_width
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

            for symbol in self.symbols.values():
                self._refresh_performance_cells(symbol)

            active_market = str(payload.get("active_market") or "")
            if active_market not in self.market_pages:
                active_market = first_restored_market
            if active_market:
                self._select_market(active_market)

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
        layout_tree = self._active_tree()
        payload = {
            "version": STATE_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window_geometry": f"{self.winfo_width()}x{self.winfo_height()}+0+0",
            "column_widths": {
                column: int(layout_tree.column(column, "width"))
                for column in TABLE_COLUMNS
            },
            "active_market": self._active_market(),
            "stocks": [],
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
        }
        for market in MARKET_ORDER:
            for item_id in self.trees[market].get_children(""):
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

    def _on_close(self) -> None:
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
            self._search_after_id = None
        if self._queue_after_id is not None:
            self.after_cancel(self._queue_after_id)
            self._queue_after_id = None
        if self._layout_after_id is not None:
            self.after_cancel(self._layout_after_id)
            self._layout_after_id = None
        if self._startup_refresh_after_id is not None:
            self.after_cancel(self._startup_refresh_after_id)
            self._startup_refresh_after_id = None
        self._save_state()
        self.destroy()

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
            ),
            tags=("waiting",),
        )
        tree.selection_set(symbol.canonical)
        tree.see(symbol.canonical)
        self._save_state()
        self._clear_search()
        self.code_entry.focus_set()
        self._start_refresh([symbol])

    def refresh_all(self) -> None:
        if not self.symbols:
            messagebox.showinfo("列表为空", "请先添加至少一个股票代码。", parent=self)
            self.code_entry.focus_set()
            return
        self._start_refresh(list(self.symbols.values()))

    def _clear_selection_on_blank(self, event: tk.Event) -> None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return
        row_id = tree.identify_row(event.y)
        region = tree.identify_region(event.x, event.y)
        if not row_id and region not in {"heading", "separator"}:
            selected_items = tree.selection()
            if selected_items:
                tree.selection_remove(*selected_items)
            tree.focus("")

    def _begin_market_swipe(self, event: tk.Event) -> str | None:
        tree = event.widget
        if isinstance(tree, ttk.Treeview):
            if tree.identify_region(event.x, event.y) == "separator":
                self._column_resize_tree = tree
                self._swipe_start = None
                if tree.identify_column(event.x) == (
                    f"#{TABLE_COLUMNS.index('profit') + 1}"
                ):
                    self._profit_return_resize_start = (
                        tree,
                        event.x_root,
                        int(tree.column("profit", "width")),
                        int(tree.column("return", "width")),
                    )
                    return "break"
                self._profit_return_resize_start = None
                return None
            self._column_resize_tree = None
            self._profit_return_resize_start = None
            self._swipe_start = (tree, event.x, event.y)
        return None

    def _resize_profit_and_return(self, event: tk.Event) -> str | None:
        resize_start = self._profit_return_resize_start
        if resize_start is None or event.widget is not resize_start[0]:
            return None

        tree, start_x, start_profit_width, start_return_width = resize_start
        profit_minimum = int(tree.column("profit", "minwidth"))
        return_minimum = int(tree.column("return", "minwidth"))
        requested_delta = event.x_root - start_x
        delta = max(
            profit_minimum - start_profit_width,
            min(start_return_width - return_minimum, requested_delta),
        )
        tree.column("profit", width=start_profit_width + delta)
        tree.column("return", width=start_return_width - delta)
        return "break"

    def _finish_market_swipe(self, event: tk.Event) -> str | None:
        paired_resize = self._profit_return_resize_start
        self._profit_return_resize_start = None
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
        deleted = records.pop(index)
        if self._save_state():
            if not records:
                self.trades.pop(symbol.canonical, None)
            self._refresh_performance_cells(symbol)
            self.status_var.set(
                f"已删除 {symbol.canonical} 的交易记录：{deleted.trade_date}。"
            )
            return True
        records.insert(index, deleted)
        return False

    def remove_selected(self, event: tk.Event | None = None) -> None:
        if self._refreshing:
            self.status_var.set("行情刷新中，请稍后再移除股票。")
            return
        tree = event.widget if event is not None else self._active_tree()
        if not isinstance(tree, ttk.Treeview):
            tree = self._active_tree()
        for item_id in tree.selection():
            tree.delete(item_id)
            self.symbols.pop(item_id, None)
            self.display_names.pop(item_id, None)
            self.current_prices.pop(item_id, None)
        self._save_state()
        self.status_var.set(f"列表中共有 {len(self.symbols)} 只股票。")

    def _calculate_position_metrics(
        self,
        symbol: StockSymbol,
    ) -> tuple[str, str, str]:
        current_quote = self.current_prices.get(symbol.canonical)
        records = self.trades.get(symbol.canonical, [])
        if not records:
            return "--", "--", "--"

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
            return "--", "--", "--"

        holding_cost_text = (
            "--"
            if net_quantity <= 0
            else f"{(buy_cost - sell_proceeds) / net_quantity:,.3f} {currency}"
        )
        if current_quote is None:
            return holding_cost_text, "--", "--"

        current_price, _currency = current_quote
        profit = current_price * net_quantity + sell_proceeds - buy_cost
        profit_text = f"{profit:+,.2f} {currency}"
        return_text = (
            "--"
            if buy_cost <= 0
            else f"{profit / buy_cost * Decimal('100'):+.2f}%"
        )
        return holding_cost_text, profit_text, return_text

    def _calculate_performance(self, symbol: StockSymbol) -> tuple[str, str]:
        _, profit, return_rate = self._calculate_position_metrics(symbol)
        return profit, return_rate

    def _calculate_trade_performance(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
    ) -> tuple[str, str]:
        values = self._calculate_trade_performance_values(symbol, record)
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
    ) -> tuple[Decimal, Decimal, str] | None:
        current_quote = self.current_prices.get(symbol.canonical)
        if current_quote is None:
            return None

        try:
            trade_price = Decimal(record.price)
            quantity = Decimal(record.quantity)
        except InvalidOperation:
            return None
        if (
            trade_price <= 0
            or quantity <= 0
            or record.trade_type not in {"买入", "卖出"}
        ):
            return None

        current_price, currency = current_quote
        direction = Decimal("1") if record.trade_type == "买入" else Decimal("-1")
        profit = (current_price - trade_price) * quantity * direction
        return_rate = (current_price - trade_price) / trade_price * direction
        return profit, return_rate, currency

    def _trade_return_exceeds_threshold(
        self,
        symbol: StockSymbol,
        record: TradeRecord,
    ) -> bool:
        values = self._calculate_trade_performance_values(symbol, record)
        return values is not None and values[1] > HIGH_RETURN_THRESHOLD

    def _refresh_performance_cells(self, symbol: StockSymbol) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        current = list(tree.item(symbol.canonical, "values"))
        if len(current) != len(TABLE_COLUMNS):
            return
        current[4], current[5], current[6] = self._calculate_position_metrics(
            symbol
        )
        tree.item(symbol.canonical, values=current)
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
        self.status_var.set(f"正在刷新 {len(symbols)} 只股票……")

        worker = threading.Thread(
            target=self._refresh_worker,
            args=(symbols,),
            daemon=True,
        )
        worker.start()

    def _refresh_worker(self, symbols: list[StockSymbol]) -> None:
        for symbol in symbols:
            try:
                quotes, errors = fetch_quotes(symbol, PROVIDER_NAMES, timeout=12.0)
                self._result_queue.put(("quote", symbol, quotes, errors))
            except Exception as exc:
                self._result_queue.put(("error", symbol, str(exc)))
        self._result_queue.put(("done", len(symbols), datetime.now()))

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
        percentage = (
            "--" if best.change_percent is None else f"{best.change_percent:+.2f}%"
        )
        price = f"{best.price:,.3f} {best.currency}"
        self.current_prices[symbol.canonical] = (
            Decimal(str(best.price)),
            best.currency,
        )
        holding_cost, profit, return_rate = self._calculate_position_metrics(symbol)

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
                holding_cost,
                profit,
                return_rate,
            ),
            tags=(tag,) if tag else (),
        )
        trade_window = self._trade_windows.get(symbol.canonical)
        if trade_window is not None and trade_window.winfo_exists():
            trade_window.refresh_summary()
        self._save_state()

    def _show_row_error(self, symbol: StockSymbol, error: str) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        current = list(tree.item(symbol.canonical, "values"))
        if len(current) == len(TABLE_COLUMNS) and current[2] == "--":
            tree.item(symbol.canonical, values=current, tags=("waiting",))
        self._save_state()
        self.status_var.set(f"{symbol.canonical} 行情获取失败：{error[:100]}")

    def _finish_refresh(self, count: int, finished_at: datetime) -> None:
        self._refreshing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.refresh_button.configure(state=tk.NORMAL)
        self.status_var.set(
            f"已刷新 {count} 只股票 · {finished_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._save_state()
        self.code_entry.focus_set()
        if self._selected_result is not None:
            self.add_stock()


def main() -> None:
    app = StockPriceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
