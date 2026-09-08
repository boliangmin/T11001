#!/usr/bin/env python3
"""美股 / 港股公开行情桌面界面。"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
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
STATE_VERSION = 1
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


class StockPriceApp(tk.Tk):
    def __init__(self, state_path: Path | None = None) -> None:
        super().__init__()
        self.title("美股 / 港股行情")
        self.geometry("1050x600")
        self.minsize(820, 430)

        self._state_path = state_path or _default_state_path()
        self.symbols: dict[str, StockSymbol] = {}
        self.display_names: dict[str, str] = {}
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

        self.table_frame = ttk.Frame(outer)
        self.table_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "code",
            "name",
            "price",
            "change",
            "time",
        )
        self.tree = ttk.Treeview(
            self.table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "code": "股票代码",
            "name": "中文名称",
            "price": "最新价格",
            "change": "涨跌幅",
            "time": "行情时间",
        }
        widths = {
            "code": 105,
            "name": 155,
            "price": 120,
            "change": 90,
            "time": 220,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column], anchor=tk.CENTER)
            self.tree.column(
                column,
                width=widths[column],
                minwidth=70,
                anchor=tk.CENTER,
                stretch=column in {"name", "time"},
            )

        self.tree.tag_configure("up", foreground="#d93025")
        self.tree.tag_configure("down", foreground="#188038")
        self.tree.tag_configure("waiting", foreground="#6b7280")
        self.tree.bind("<Button-1>", self._clear_selection_on_blank)
        self.tree.bind("<Delete>", self.remove_selected)

        vertical = ttk.Scrollbar(
            self.table_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        horizontal = ttk.Scrollbar(
            self.table_frame, orient=tk.HORIZONTAL, command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )

        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table_frame.rowconfigure(0, weight=1)
        self.table_frame.columnconfigure(0, weight=1)

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="请输入股票名称或代码，并从候选项中选择。")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Label(
            footer,
            text="公开行情可能延迟｜选中行后按 Delete 可移除",
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

    def _apply_column_widths(self, widths: dict[str, int]) -> None:
        self._layout_after_id = None
        for column, width in widths.items():
            if column in self.tree["columns"]:
                self.tree.column(column, width=width)

    def _refresh_restored_stocks(self, symbols: list[StockSymbol]) -> None:
        self._startup_refresh_after_id = None
        self._start_refresh(symbols)

    def _restore_state(self) -> None:
        if not self._state_path.is_file():
            return

        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
                raise ValueError("保存文件版本不受支持")

            layout_restored = self._restore_geometry(payload.get("window_geometry"))
            saved_columns = payload.get("column_widths")
            if isinstance(saved_columns, dict):
                valid_widths: dict[str, int] = {}
                for column in self.tree["columns"]:
                    width = saved_columns.get(column)
                    if isinstance(width, int) and 70 <= width <= 2000:
                        valid_widths[column] = width
                        layout_restored = True
                if valid_widths:
                    self._layout_after_id = self.after_idle(
                        lambda widths=valid_widths: self._apply_column_widths(widths)
                    )

            restored_count = 0
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
                    self.tree.insert(
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
                column: int(self.tree.column(column, "width"))
                for column in self.tree["columns"]
            },
            "stocks": [],
        }
        for item_id in self.tree.get_children(""):
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
                f"找到 {len(results)} 个候选{failed}；点击候选项完成选择。"
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

        if symbol.canonical in self.symbols:
            if selected_name and (
                _contains_chinese(selected_name)
                or not _contains_chinese(self.display_names.get(symbol.canonical, ""))
            ):
                self.display_names[symbol.canonical] = selected_name
                self._save_state()
            self.tree.selection_set(symbol.canonical)
            self.tree.see(symbol.canonical)
            self.status_var.set(f"{symbol.canonical} 已在列表中，正在重新获取行情。")
            self._clear_search()
            self._start_refresh([symbol])
            return

        self.symbols[symbol.canonical] = symbol
        self.display_names[symbol.canonical] = selected_name
        self.tree.insert(
            "",
            tk.END,
            iid=symbol.canonical,
            values=(symbol.canonical, selected_name or "--", "--", "--", "等待刷新"),
            tags=("waiting",),
        )
        self.tree.selection_set(symbol.canonical)
        self.tree.see(symbol.canonical)
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
        row_id = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not row_id and region not in {"heading", "separator"}:
            selected_items = self.tree.selection()
            if selected_items:
                self.tree.selection_remove(*selected_items)
            self.tree.focus("")

    def remove_selected(self, _event: tk.Event | None = None) -> None:
        if self._refreshing:
            self.status_var.set("行情刷新中，请稍后再移除股票。")
            return
        for item_id in self.tree.selection():
            self.tree.delete(item_id)
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
        if not self.tree.exists(symbol.canonical):
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

        self.tree.item(
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
        if not self.tree.exists(symbol.canonical):
            return
        current = list(self.tree.item(symbol.canonical, "values"))
        if len(current) == 5 and current[2] == "--":
            current[-1] = "获取失败"
            self.tree.item(symbol.canonical, values=current, tags=("waiting",))
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
