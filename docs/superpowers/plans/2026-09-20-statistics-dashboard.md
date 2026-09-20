# Statistics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third statistics dashboard page that centralizes US/HK market summaries, latest trades, official exchange rates, and a combined CNY market value.

**Architecture:** Keep `US` and `HK` as the only stock markets and add an independent `STATS` notebook page. Fetch and parse official ECB rates in `stock_price.py`, then pass immutable rate snapshots through the existing UI worker queue; `stock_price_ui.py` owns warm-light dashboard rendering, raw market aggregates, refresh coordination, and page state.

**Tech Stack:** Python 3.13 standard library (`urllib`, `xml.etree.ElementTree`, `decimal`), Tkinter/ttk, PyInstaller, JSON state persistence.

**Spec:** `docs/superpowers/specs/2026-09-20-statistics-dashboard-design.md`

## Global Constraints

- Do not run the repository's unit-test suite unless the user explicitly requests it; use focused red/green behavior checks plus the required baseline verification.
- Keep the fixed `1256×768` main window, minimization, title-bar dragging, disabled maximization, and disabled resizing unchanged.
- Keep the existing selected-tab styling and all US/HK table and trade-window behavior unchanged.
- Use no blue or light apple color inside the statistics dashboard; use `#F7F4EE`, `#F3E6D2`, `#FFFDFC`, `#E4D8C7`, `#B78B58`, and `#2F2922`.
- Keep positive values red, negative values green, and zero values black.
- Do not store exchange rates in `state.json`; preserve backward compatibility with existing version-4 state files.
- Do not add third-party Python dependencies or credentials.
- Do not create intermediate runtime commits: AGENTS.md requires a complete validation, PyInstaller build, hidden startup check, one final auto-commit, deployment, and state synchronization for runtime changes.
- Do not push Git commits.

## Review Focus

- ECB XML missing USD, HKD, or CNY must fail without replacing the last successful in-memory rates; Task 1 and Task 5 pin this behavior.
- A refresh with no stocks must still fetch rates and update the unified refresh time; Task 5 pins this behavior.
- Activating `STATS` must not break shared column-width saving or any operation that needs the last selected real market; Task 3 pins this behavior.
- A held stock without a current quote must make the relevant market and combined CNY value incomplete instead of silently undercounting; Task 2 and Task 4 pin this behavior.
- No trades in one market must render that market as empty while contributing zero, not an error, to the combined CNY value; Task 2 and Task 4 pin this behavior.

---

### Task 1: Official ECB exchange-rate boundary

**Files:**
- Modify: `stock_price.py` imports and network helper section

**Interfaces:**
- Produces: `ExchangeRates(rate_date: str, usd_to_cny: Decimal, hkd_to_cny: Decimal)`
- Produces: `parse_ecb_exchange_rates(payload: bytes) -> ExchangeRates`
- Produces: `fetch_exchange_rates(timeout: float = 12.0) -> ExchangeRates`

- [ ] **Step 1: Run a failing parser behavior check**

Run:

```powershell
python -c "from stock_price import parse_ecb_exchange_rates; print(parse_ecb_exchange_rates(b'<xml/>'))"
```

Expected: FAIL because `parse_ecb_exchange_rates` does not exist.

- [ ] **Step 2: Add the immutable rate type and strict XML parser**

Add imports for `Decimal`, `InvalidOperation`, and `xml.etree.ElementTree`. Add the following boundary:

```python
ECB_DAILY_RATES_URL = (
    "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
)

@dataclass(frozen=True)
class ExchangeRates:
    rate_date: str
    usd_to_cny: Decimal
    hkd_to_cny: Decimal

def parse_ecb_exchange_rates(payload: bytes) -> ExchangeRates:
    root = ElementTree.fromstring(payload)
    rate_date = ""
    rates: dict[str, Decimal] = {}
    for element in root.iter():
        if "time" in element.attrib:
            rate_date = element.attrib["time"].strip()
        currency = element.attrib.get("currency", "").strip().upper()
        if currency in {"USD", "HKD", "CNY"}:
            rates[currency] = Decimal(element.attrib["rate"])
    if not rate_date or set(rates) != {"USD", "HKD", "CNY"}:
        raise ValueError("欧洲央行汇率数据不完整")
    if any(not value.is_finite() or value <= 0 for value in rates.values()):
        raise ValueError("欧洲央行汇率数值无效")
    return ExchangeRates(
        rate_date=rate_date,
        usd_to_cny=rates["CNY"] / rates["USD"],
        hkd_to_cny=rates["CNY"] / rates["HKD"],
    )
```

Wrap XML syntax errors, missing attributes, and `InvalidOperation` as a user-readable `ValueError`.

- [ ] **Step 3: Add the network fetcher**

Use the existing request conventions and no new dependency:

```python
def fetch_exchange_rates(timeout: float = 12.0) -> ExchangeRates:
    request = Request(
        ECB_DAILY_RATES_URL,
        headers={"User-Agent": "Mozilla/5.0 StockPriceViewer/1.0"},
    )
    with urlopen(request, timeout=timeout) as response:
        return parse_ecb_exchange_rates(response.read())
```

Convert `HTTPError`, `URLError`, timeout, and parse failures into a concise `RuntimeError` while retaining the original exception as the cause.

- [ ] **Step 4: Run green parser checks including review-focus failures**

Run a literal ECB-shaped XML fixture with EUR-relative values `USD=1.2`, `HKD=9.6`, `CNY=7.8`; assert `USD/CNY=6.5`, `HKD/CNY=0.8125`, and the date. Repeat with missing HKD and non-numeric CNY; both must raise `ValueError`.

### Task 2: Raw market metrics and CNY aggregation

**Files:**
- Modify: `stock_price_ui.py` data classes and market metric methods

**Interfaces:**
- Consumes: `ExchangeRates` from Task 1
- Produces: `MarketMetrics(currency: str, has_records: bool, complete: bool, market_value: Decimal, profit: Decimal, return_rate: Decimal | None)`
- Produces: `_calculate_cny_total_market_value(us_metrics: MarketMetrics, hk_metrics: MarketMetrics, rates: ExchangeRates | None) -> Decimal | None`

- [ ] **Step 1: Run a failing pure aggregation check**

Run an inline check importing `MarketMetrics` and `_calculate_cny_total_market_value`; create complete US/HK metrics and literal rates, and expect `us_market_value * usd_to_cny + hk_market_value * hkd_to_cny`.

Expected: FAIL because the data type and function do not exist.

- [ ] **Step 2: Implement the immutable metric type and pure conversion**

Add:

```python
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
```

- [ ] **Step 3: Refactor `_calculate_market_metrics` to return raw values**

Preserve the current buy/sell/net-holding formulas. No records returns `has_records=False`, `complete=True`, and zero values. Any recorded symbol without a valid matching-currency quote returns `complete=False`. Positive buy cost produces the current weighted aggregate return; otherwise `return_rate=None`.

- [ ] **Step 4: Run green pure checks**

Cover complete values, missing rates, one incomplete market, an empty market contributing zero, and a held stock missing its quote. Expected: a `Decimal` only for complete inputs; otherwise `None`.

### Task 3: Third page, warm-light dashboard, and page persistence

**Files:**
- Modify: `stock_price_ui.py` constants, `StockPriceApp.__init__`, `_build_ui`, page helpers, state restore/save

**Interfaces:**
- Produces: `STATISTICS_PAGE = "STATS"`
- Produces: `_active_page() -> str`
- Produces: `_select_page(page_name: str) -> None`
- Preserves: `_active_market() -> str` using the most recently selected `US` or `HK` while statistics is active

- [ ] **Step 1: Run a failing hidden UI structure check**

Create `StockPriceApp` with a temporary state path, immediately cancel scheduled refresh callbacks, and assert notebook tab texts are `美股`, `港股`, `统计`; assert the old notebook summary overlay and bottom latest-trade label are absent; assert the statistics page has CNY-total, two rate, two market, two latest-trade, warning, and unified-time variables.

Expected: FAIL because only two tabs and the old labels exist.

- [ ] **Step 2: Add page identity without extending `MARKET_ORDER`**

Track `self.statistics_page`, `self._last_active_market`, and `self.current_page`. `_active_page` identifies all three notebook pages; `_active_market` updates only on US/HK and returns `_last_active_market` on statistics. Existing swipe logic remains limited to `MARKET_ORDER`.

- [ ] **Step 3: Build the dashboard and remove duplicated market-page summaries**

Delete `market_summary_label`, `market_summary_var`, `latest_trade_label`, and `latest_trade_var`. Add a `STATS` page after HK. Build:

- Warm canvas `#F7F4EE`.
- Full-width CNY total card `#F3E6D2`.
- Two `#FFFDFC` rate cards.
- Two `#FFFDFC` market cards with three metric cells and one latest-trade section each.
- Top-right unified refresh time and a small warning label.

Use `tk.Frame` and `tk.Label` for per-card backgrounds; keep the existing notebook style untouched.

- [ ] **Step 4: Persist `active_page` compatibly**

Save both `active_page` and `active_market` while retaining `STATE_VERSION = 4`. Restore `active_page` when valid; otherwise fall back to existing `active_market` behavior. When statistics is active, save shared column widths from `_last_active_market`.

- [ ] **Step 5: Run green hidden UI and state checks**

Verify all dashboard widgets and warm colors, absence of old summaries, all three pages, statistics-page save/restore, and correct shared column-width saving while `STATS` is selected.

### Task 4: Dashboard projections and formatting

**Files:**
- Modify: `stock_price_ui.py` summary refresh, latest-trade extraction, trade mutation call sites

**Interfaces:**
- Produces: `_latest_market_trade(market: str) -> tuple[StockSymbol, TradeRecord, Decimal, int] | None`
- Produces: `_refresh_statistics_dashboard() -> None`
- Consumes: raw `MarketMetrics` and `ExchangeRates`

- [ ] **Step 1: Run a failing latest-trade and dashboard behavior check**

Construct an app with temporary state, insert one US trade and one later HK trade, seed US/HK prices and literal exchange rates, call `_refresh_statistics_dashboard`, and assert each market card receives its own latest trade, market metrics retain native currencies, and CNY total matches the hand-calculated literal.

Expected: FAIL because dashboard projection methods do not exist.

- [ ] **Step 2: Extract market-specific latest trade selection**

Move current date-plus-saved-order selection into `_latest_market_trade(market)`. Format each result directly into the matching market card. An empty market uses `--` instead of an empty label.

- [ ] **Step 3: Implement dashboard formatting**

Format market value and profit with two decimals and currency, return rate with two percentage decimals, rates with four decimals, and CNY total with two decimals. Apply `_metric_color` only to profit and return labels. Empty markets display `--` but contribute zero to CNY total; incomplete markets make CNY total `--`.

- [ ] **Step 4: Replace existing refresh call sites**

Replace `_refresh_market_summary()` calls after quote updates, trade add/edit/delete/T-operation changes, stock removal, and state restoration with `_refresh_statistics_dashboard()`. Keep row and trade-window refreshes unchanged.

- [ ] **Step 5: Run green hidden projection checks**

Cover US/HK separation, same-date saved-order tie breaking, empty market, incomplete quote, profit/return colors, and no cross-market profit/return output.

### Task 5: Unified stock-and-rate refresh cycle

**Files:**
- Modify: `stock_price_ui.py` startup scheduling, manual/automatic refresh, worker, queue processing, finish handling

**Interfaces:**
- Consumes: `fetch_exchange_rates(timeout=12.0)`
- Queue messages: `("exchange_rates", ExchangeRates)`, `("exchange_rate_error", str)`, existing quote/error messages, then `("done", count, datetime)`

- [ ] **Step 1: Run a failing worker queue behavior check**

Replace network functions in-memory with deterministic local functions, call `_refresh_worker([])`, and inspect the real queue. Expect an exchange-rate message followed by `done` even with zero stocks.

Expected: FAIL because the current worker only emits `done` for an empty list.

- [ ] **Step 2: Fetch exchange rates once per refresh cycle**

Call `fetch_exchange_rates` in `_refresh_worker` and enqueue either success or error without aborting stock refresh. Preserve the most recent successful `self.exchange_rates`; on failure set the agreed warning and retain the previous object.

- [ ] **Step 3: Make every refresh path unified**

Startup, restored stocks, manual refresh, five-minute auto refresh, and single-stock add refresh all use the unified worker. Manual and automatic refresh with no stocks still fetch rates. Schedule one startup refresh even when no stocks were restored.

- [ ] **Step 4: Update the unified timestamp only on `done`**

Set the dashboard time from `finished_at` after all stock and rate work ends, then refresh the dashboard. The label text is `统一刷新时间  YYYY-MM-DD HH:MM:SS`.

- [ ] **Step 5: Run green refresh checks**

Verify empty-stock rate refresh, successful rate replacement, failure retaining the previous rate, first failure leaving `--`, exactly one `done`, unified timestamp update, and five-minute rescheduling.

### Task 6: Documentation, required verification, packaging, and deployment

**Files:**
- Modify: `README.md`
- Generated outside Git: temporary PyInstaller build directory
- Deploy: `D:\5.tools\39.trade_view\trade_view.exe`, manifest, assets, synchronized state files

**Interfaces:**
- Documents: statistics page layout, formulas, ECB daily reference-rate caveat, unified refresh, failure behavior, and CNY market-value scope

- [ ] **Step 1: Update README**

Remove statements that summaries/latest trades live on market pages. Document the new statistics page, formulas, warm-light palette, official daily reference-rate limitation, and the fact that total CNY value excludes combined profit and return.

- [ ] **Step 2: Run all focused behavior checks and baseline validation**

Run parser, aggregation, hidden UI, state compatibility, dashboard projection, and refresh-queue checks from Tasks 1–5. Then run:

```powershell
python -m py_compile stock_price.py stock_price_ui.py
python -c "import tkinter; import stock_price_ui"
git diff --check
```

- [ ] **Step 3: Run required real network queries**

Query at least one US symbol and one HK symbol through the existing providers, and call `fetch_exchange_rates()` once. Report exact returned currencies/rates and failures; do not describe failed calls as verified.

- [ ] **Step 4: Run hidden source startup check**

Verify the password window still appears first and state data remains unchanged before authentication. Then use a hidden app with temporary state to verify the third page without desktop automation.

- [ ] **Step 5: Build and inspect the packaged EXE**

Run `trade_view.spec` in a unique system temporary directory. Hidden-start the generated EXE, verify the password window, close it without input, confirm no state is created in the temporary distribution, and record SHA-256.

- [ ] **Step 6: Create one final runtime auto-commit**

Use `auto_commit.ps1` with only `stock_price.py`, `stock_price_ui.py`, `README.md`, and this plan if changed after its documentation commit. The script adds README and `.auto_commit_state.json`. Do not stage the user's three existing untracked files and do not push.

- [ ] **Step 7: Deploy and synchronize data**

Follow AGENTS.md exactly: ensure no source or deployed instance holds either state file; select the newer state by parseable `saved_at` then UTC mtime fallback; stop on equal-time/different-hash conflict; back up the older file; synchronize; copy EXE/manifest/assets; verify deployed EXE hash and both JSON hashes, stock counts, and trade counts.

- [ ] **Step 8: Clean the validated temporary build and report**

Delete only the exact validated temporary build directory. Report UI changes, functional changes, unchanged areas, focused checks, no unit-test-suite run, real queries, hidden startup, target path/hash, data source/basis/hash/counts/backup, local commit, no push, and that visual confirmation remains manual.
