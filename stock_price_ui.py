#!/usr/bin/env python3
"""美股 / 港股公开行情桌面界面。"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
TABLE_COLUMNS = ("code", "name", "price", "change", "time")
TABLE_HEADINGS = {
    "code": "股票代码",
    "name": "中文名称",
    "price": "最新价格",
    "change": "涨跌幅",
    "time": "行情时间",
}
DEFAULT_COLUMN_WIDTHS = {
    "code": 105,
    "name": 155,
    "price": 120,
    "change": 90,
    "time": 220,
}
STATE_VERSION = 3
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


@dataclass(frozen=True)
class TradeRecord:
    trade_time: str
    price: str


class TradeEntryDialog(tk.Toplevel):
    def __init__(
        self,
        owner: tk.Toplevel,
        currency: str,
        on_save: Callable[[TradeRecord], bool],
    ) -> None:
        super().__init__(owner)
        self._on_save = on_save
        self.title("增加交易")
        self.resizable(False, False)
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        content = ttk.Frame(self, padding=(22, 18, 22, 16))
        content.pack(fill=tk.BOTH, expand=True)

        ttk.Label(content, text="交易时间").grid(
            row=0,
            column=0,
            sticky=tk.W,
            pady=(0, 12),
        )
        self.time_var = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.time_entry = ttk.Entry(
            content,
            textvariable=self.time_var,
            width=28,
        )
        self.time_entry.grid(row=0, column=1, sticky=tk.EW, pady=(0, 12))

        ttk.Label(content, text=f"交易价格（{currency}）").grid(
            row=1,
            column=0,
            sticky=tk.W,
            pady=(0, 16),
        )
        self.price_var = tk.StringVar()
        self.price_entry = ttk.Entry(
            content,
            textvariable=self.price_var,
            width=28,
        )
        self.price_entry.grid(row=1, column=1, sticky=tk.EW, pady=(0, 16))

        buttons = ttk.Frame(content)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.E)
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
        self.grab_set()
        self.after_idle(self._place_and_focus)

    def _place_and_focus(self) -> None:
        self.update_idletasks()
        owner = self.master
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - self.winfo_width()) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.price_entry.focus_set()

    def _submit(self) -> None:
        raw_time = self.time_var.get().strip()
        parsed_time: datetime | None = None
        for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed_time = datetime.strptime(raw_time, time_format)
                break
            except ValueError:
                continue
        if parsed_time is None:
            messagebox.showerror(
                "交易时间格式错误",
                "请输入 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS 格式的时间。",
                parent=self,
            )
            self.time_entry.focus_set()
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
        record = TradeRecord(
            trade_time=parsed_time.strftime("%Y-%m-%d %H:%M:%S"),
            price=normalized_price,
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
        ttk.Label(
            header,
            text=f"{display_name or '--'}  ·  {symbol.canonical}",
            style="Title.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Button(
            header,
            text="增加交易",
            command=self._open_entry_dialog,
            style="Primary.TButton",
        ).pack(side=tk.RIGHT)

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.trade_tree = ttk.Treeview(
            table_frame,
            columns=("trade_time", "price"),
            show="headings",
        )
        self.trade_tree.heading("trade_time", text="交易时间", anchor=tk.CENTER)
        self.trade_tree.heading("price", text="交易价格", anchor=tk.CENTER)
        self.trade_tree.column("trade_time", width=280, anchor=tk.CENTER)
        self.trade_tree.column("price", width=220, anchor=tk.CENTER)
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

        ttk.Label(
            outer,
            text="交易记录保存在本机；行情接口价格不会作为交易记录保存。",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(10, 0))
        self._reload_records()

    def _reload_records(self) -> None:
        self.trade_tree.delete(*self.trade_tree.get_children(""))
        for index, record in enumerate(
            self.owner.trades.get(self.symbol.canonical, [])
        ):
            self.trade_tree.insert(
                "",
                tk.END,
                iid=f"trade-{index}",
                values=(record.trade_time, f"{record.price} {self.currency}"),
            )

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

    def _close(self) -> None:
        self.owner._trade_windows.pop(self.symbol.canonical, None)
        self.destroy()


class StockPriceApp(tk.Tk):
    def __init__(self, state_path: Path | None = None) -> None:
        super().__init__()
        self.title("美股 / 港股行情")
        self.geometry("1050x600")
        self.minsize(820, 430)

        self._state_path = state_path or _default_state_path()
        self.symbols: dict[str, StockSymbol] = {}
        self.display_names: dict[str, str] = {}
        self.trades: dict[str, list[TradeRecord]] = {}
        self._trade_windows: dict[str, StockTradeWindow] = {}
        self.trees: dict[str, ttk.Treeview] = {}
        self.market_pages: dict[str, ttk.Frame] = {}
        self._swipe_start: tuple[ttk.Treeview, int, int] | None = None
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
        self._queue_after_id = self.after(100, self._process_result_queue)

    def _configure_style(self) -> None:
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei UI", size=10)
        tkfont.nametofont("TkTextFont").configure(
            family="Microsoft YaHei UI", size=10
        )

        style = ttk.Style(self)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")

        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6368")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure(
            "Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold")
        )
        style.configure("Primary.TButton", padding=(16, 7))
        style.configure("Action.TButton", padding=(16, 7))

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

        self.add_button = ttk.Button(
            input_frame,
            text="添加",
            command=self.add_stock,
            style="Primary.TButton",
            state=tk.DISABLED,
        )
        self.add_button.pack(side=tk.LEFT, padx=(10, 8))

        self.refresh_button = ttk.Button(
            input_frame,
            text="刷新全部",
            command=self.refresh_all,
            style="Action.TButton",
        )
        self.refresh_button.pack(side=tk.LEFT)

        self.suggestion_frame = ttk.Frame(outer, padding=(84, 0, 136, 8))
        suggestion_border = tk.Frame(
            self.suggestion_frame,
            background="#c7cbd1",
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
            selectbackground="#dbeafe",
            selectforeground="#111827",
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
                    minwidth=70,
                    anchor=tk.CENTER,
                    stretch=column in {"name", "time"},
                )

            tree.tag_configure("up", foreground="#d93025")
            tree.tag_configure("down", foreground="#188038")
            tree.tag_configure("waiting", foreground="#6b7280")
            tree.bind("<Button-1>", self._clear_selection_on_blank)
            tree.bind("<ButtonPress-1>", self._begin_market_swipe, add="+")
            tree.bind("<ButtonRelease-1>", self._finish_market_swipe, add="+")
            tree.bind("<Shift-MouseWheel>", self._switch_market_with_wheel)
            tree.bind("<Double-1>", self._open_stock_trade_window)
            tree.bind("<Delete>", self.remove_selected)

            vertical = ttk.Scrollbar(page, orient=tk.VERTICAL, command=tree.yview)
            horizontal = ttk.Scrollbar(
                page,
                orient=tk.HORIZONTAL,
                command=tree.xview,
            )
            tree.configure(
                yscrollcommand=vertical.set,
                xscrollcommand=horizontal.set,
            )

            tree.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
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
        x = max(0, min(int(match.group("x")), max(0, screen_width - width)))
        y = max(0, min(int(match.group("y")), max(0, screen_height - height)))
        self.geometry(f"{width}x{height}+{x}+{y}")
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
            if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
                raise ValueError("保存文件版本不受支持")

            layout_restored = self._restore_geometry(payload.get("window_geometry"))
            saved_columns = payload.get("column_widths")
            if isinstance(saved_columns, dict):
                widths_by_market: dict[str, dict[str, int]] = {}
                for market in MARKET_ORDER:
                    market_widths = saved_columns.get(market)
                    if not isinstance(market_widths, dict):
                        market_widths = saved_columns
                    valid_widths = {
                        column: width
                        for column in TABLE_COLUMNS
                        if isinstance((width := market_widths.get(column)), int)
                        and 70 <= width <= 2000
                    }
                    if valid_widths:
                        widths_by_market[market] = valid_widths
                        layout_restored = True
                if widths_by_market:
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
                            "等待刷新",
                        ),
                        tags=("waiting",),
                    )
                    restored_count += 1
                    if not first_restored_market:
                        first_restored_market = symbol.market

            saved_trades = payload.get("trades")
            if isinstance(saved_trades, dict):
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
                        trade_time = str(raw_record.get("trade_time") or "").strip()
                        price = str(raw_record.get("price") or "").strip()
                        if trade_time and price:
                            records.append(
                                TradeRecord(trade_time=trade_time, price=price)
                            )
                    if records:
                        self.trades[trade_symbol.canonical] = records

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
        payload = {
            "version": STATE_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window_geometry": self.geometry(),
            "column_widths": {
                market: {
                    column: int(tree.column(column, "width"))
                    for column in tree["columns"]
                }
                for market, tree in self.trees.items()
            },
            "active_market": self._active_market(),
            "stocks": [],
            "trades": {
                symbol: [
                    {
                        "trade_time": record.trade_time,
                        "price": record.price,
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
        self.add_button.configure(state=tk.DISABLED)
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
        if not self._refreshing:
            self.add_button.configure(state=tk.NORMAL)
        market_name = "港股" if result.symbol.market == "HK" else "美股"
        self.status_var.set(
            f"已选择：{result.name}（{result.display_code}，{market_name}），点击“添加”加入列表。"
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
        self.add_button.configure(state=tk.DISABLED)
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
            self.status_var.set("行情刷新中，请稍后再添加股票。")
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
            values=(symbol.canonical, selected_name or "--", "--", "--", "等待刷新"),
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

    def _begin_market_swipe(self, event: tk.Event) -> None:
        tree = event.widget
        if isinstance(tree, ttk.Treeview):
            self._swipe_start = (tree, event.x, event.y)

    def _finish_market_swipe(self, event: tk.Event) -> None:
        swipe_start = self._swipe_start
        self._swipe_start = None
        if swipe_start is None or event.widget is not swipe_start[0]:
            return

        horizontal_distance = event.x - swipe_start[1]
        vertical_distance = event.y - swipe_start[2]
        if (
            abs(horizontal_distance) >= 80
            and abs(horizontal_distance) > abs(vertical_distance) * 1.5
        ):
            self._switch_market_page(1 if horizontal_distance < 0 else -1)

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
            self.status_var.set(
                f"已保存 {symbol.canonical} 的交易记录：{record.trade_time}。"
            )
            return True
        records.pop()
        if not records:
            self.trades.pop(symbol.canonical, None)
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
        self._save_state()
        self.status_var.set(f"列表中共有 {len(self.symbols)} 只股票。")

    def _start_refresh(self, symbols: list[StockSymbol]) -> None:
        if self._refreshing:
            self.status_var.set("当前刷新尚未完成，请稍候。")
            return

        self._refreshing = True
        self.add_button.configure(state=tk.DISABLED)
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
        quote_time = best.quote_time or "未提供"

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
                f"{quote_time} {best.timezone}",
            ),
            tags=(tag,) if tag else (),
        )
        self._save_state()

    def _show_row_error(self, symbol: StockSymbol, error: str) -> None:
        tree = self._tree_for_symbol(symbol)
        if not tree.exists(symbol.canonical):
            return
        current = list(tree.item(symbol.canonical, "values"))
        if len(current) == 5 and current[2] == "--":
            current[-1] = "获取失败"
            tree.item(symbol.canonical, values=current, tags=("waiting",))
        self._save_state()
        self.status_var.set(f"{symbol.canonical} 行情获取失败：{error[:100]}")

    def _finish_refresh(self, count: int, finished_at: datetime) -> None:
        self._refreshing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.add_button.configure(
            state=tk.NORMAL if self._selected_result is not None else tk.DISABLED
        )
        self.refresh_button.configure(state=tk.NORMAL)
        self.status_var.set(
            f"已刷新 {count} 只股票 · {finished_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._save_state()
        self.code_entry.focus_set()


def main() -> None:
    app = StockPriceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
