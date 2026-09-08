#!/usr/bin/env python3
"""美股 / 港股公开行情桌面界面。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter import font as tkfont

from stock_price import (
    PriceQuote,
    StockSymbol,
    choose_freshest,
    fetch_quotes,
    normalize_symbol,
)


PROVIDER_NAMES = ["yahoo", "tencent", "sina"]


class StockPriceApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("美股 / 港股行情")
        self.geometry("1050x600")
        self.minsize(820, 430)

        self.symbols: dict[str, StockSymbol] = {}
        self._result_queue: queue.Queue[tuple] = queue.Queue()
        self._refreshing = False

        self._configure_style()
        self._build_ui()
        self.after(100, self._process_result_queue)

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

        ttk.Label(outer, text="股票行情", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            outer,
            text="支持美股代码（AAPL）和港股代码（00700）；数据来自 Yahoo、腾讯和新浪。",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, pady=(3, 16))

        input_frame = ttk.Frame(outer)
        input_frame.pack(fill=tk.X, pady=(0, 14))

        ttk.Label(input_frame, text="股票代码").pack(side=tk.LEFT, padx=(0, 8))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(
            input_frame,
            textvariable=self.code_var,
            width=28,
            font=("Consolas", 11),
        )
        self.code_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.code_entry.bind("<Return>", lambda _event: self.add_stock())

        self.add_button = ttk.Button(
            input_frame,
            text="添加",
            command=self.add_stock,
            style="Primary.TButton",
        )
        self.add_button.pack(side=tk.LEFT, padx=(10, 8))

        self.refresh_button = ttk.Button(
            input_frame,
            text="刷新全部",
            command=self.refresh_all,
            style="Action.TButton",
        )
        self.refresh_button.pack(side=tk.LEFT)

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "code",
            "name",
            "price",
            "change",
            "time",
            "source",
            "status",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "code": "股票代码",
            "name": "名称",
            "price": "最新价格",
            "change": "涨跌幅",
            "time": "行情时间",
            "source": "采用来源",
            "status": "状态",
        }
        widths = {
            "code": 105,
            "name": 155,
            "price": 120,
            "change": 90,
            "time": 170,
            "source": 115,
            "status": 170,
        }
        for column in columns:
            anchor = tk.E if column in {"price", "change"} else tk.W
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=70,
                anchor=anchor,
                stretch=column in {"name", "status"},
            )

        self.tree.tag_configure("up", foreground="#d93025")
        self.tree.tag_configure("down", foreground="#188038")
        self.tree.tag_configure("waiting", foreground="#6b7280")
        self.tree.bind("<Delete>", self.remove_selected)

        vertical = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(
            table_frame, orient=tk.HORIZONTAL, command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )

        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="请输入股票代码，然后点击“添加”。")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Label(
            footer,
            text="选中行后按 Delete 可移除",
            style="Hint.TLabel",
        ).pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=160)
        self.code_entry.focus_set()

    def add_stock(self) -> None:
        raw = self.code_var.get().strip()
        if not raw:
            messagebox.showinfo("请输入代码", "请输入美股或港股代码。", parent=self)
            self.code_entry.focus_set()
            return

        try:
            symbol = normalize_symbol(raw)
        except ValueError as exc:
            messagebox.showerror("代码无效", str(exc), parent=self)
            self.code_entry.select_range(0, tk.END)
            self.code_entry.focus_set()
            return

        if symbol.canonical in self.symbols:
            self.tree.selection_set(symbol.canonical)
            self.tree.see(symbol.canonical)
            self.status_var.set(f"{symbol.canonical} 已在列表中，正在重新获取行情。")
            self.code_var.set("")
            self._start_refresh([symbol])
            return

        self.symbols[symbol.canonical] = symbol
        self.tree.insert(
            "",
            tk.END,
            iid=symbol.canonical,
            values=(symbol.canonical, "--", "--", "--", "--", "--", "等待刷新"),
            tags=("waiting",),
        )
        self.tree.selection_set(symbol.canonical)
        self.tree.see(symbol.canonical)
        self.code_var.set("")
        self.code_entry.focus_set()
        self._start_refresh([symbol])

    def refresh_all(self) -> None:
        if not self.symbols:
            messagebox.showinfo("列表为空", "请先添加至少一个股票代码。", parent=self)
            self.code_entry.focus_set()
            return
        self._start_refresh(list(self.symbols.values()))

    def remove_selected(self, _event: tk.Event | None = None) -> None:
        if self._refreshing:
            self.status_var.set("行情刷新中，请稍后再移除股票。")
            return
        for item_id in self.tree.selection():
            self.tree.delete(item_id)
            self.symbols.pop(item_id, None)
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

        for symbol in symbols:
            if self.tree.exists(symbol.canonical):
                current = list(self.tree.item(symbol.canonical, "values"))
                current[-1] = "刷新中……"
                self.tree.item(symbol.canonical, values=current, tags=("waiting",))

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
                elif kind == "done":
                    _, count, finished_at = message
                    self._finish_refresh(count, finished_at)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._process_result_queue)

    def _update_row(
        self,
        symbol: StockSymbol,
        quotes: list[PriceQuote],
        errors: dict[str, str],
    ) -> None:
        if not self.tree.exists(symbol.canonical):
            return
        if not quotes:
            detail = "；".join(errors.values()) or "所有数据源均未返回价格"
            self._show_row_error(symbol, detail)
            return

        best = choose_freshest(quotes)
        percentage = (
            "--" if best.change_percent is None else f"{best.change_percent:+.2f}%"
        )
        price = f"{best.price:,.3f} {best.currency}"
        quote_time = best.quote_time or "未提供"
        failed_count = len(errors)
        status = f"{len(quotes)}/3 来源有效"
        if failed_count:
            status += f"，{failed_count} 个失败"

        tag = "waiting"
        if best.change_percent is not None:
            tag = "up" if best.change_percent > 0 else "down" if best.change_percent < 0 else ""

        self.tree.item(
            symbol.canonical,
            values=(
                symbol.canonical,
                best.name or "--",
                price,
                percentage,
                f"{quote_time} {best.timezone}",
                best.source,
                status,
            ),
            tags=(tag,) if tag else (),
        )

    def _show_row_error(self, symbol: StockSymbol, error: str) -> None:
        if not self.tree.exists(symbol.canonical):
            return
        current = list(self.tree.item(symbol.canonical, "values"))
        current[-1] = f"失败：{error[:80]}"
        self.tree.item(symbol.canonical, values=current, tags=("waiting",))

    def _finish_refresh(self, count: int, finished_at: datetime) -> None:
        self._refreshing = False
        self.progress.stop()
        self.progress.pack_forget()
        self.add_button.configure(state=tk.NORMAL)
        self.refresh_button.configure(state=tk.NORMAL)
        self.status_var.set(
            f"已刷新 {count} 只股票 · {finished_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.code_entry.focus_set()


def main() -> None:
    app = StockPriceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
