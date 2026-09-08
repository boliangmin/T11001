# 美股 / 港股公开行情查询

`stock_price.py` 无需账户、API Key 或第三方 Python 包。它会同时查询：

- Yahoo Finance
- 腾讯自选股
- 新浪财经

默认显示三个来源，并根据各来源提供的行情时间选择最新结果。单个来源失败时，
另外两个来源仍可继续使用。

## 桌面界面

直接双击 `启动股票行情.vbs`。启动器会直接调用本机 Python 3.13 的
`pythonw.exe`，只显示图形界面，不会弹出 CMD/控制台窗口，也不依赖 `.pyw`
文件关联。

需要排查问题时，也可以在 PowerShell 中运行开发入口：

```powershell
python .\stock_price_ui.py
```

在输入框填写 `AAPL` 或 `00700`，点击“添加”后股票会出现在下方列表中并自动
获取一次行情。“刷新全部”会重新获取列表中所有股票的价格。选中一行并按
Delete 键可以将其移除。

## 使用方式

需要 Python 3.10 或更高版本。

交互式运行：

```powershell
python .\stock_price.py
```

直接查询一个或多个代码：

```powershell
python .\stock_price.py AAPL 00700 09988
```

输入规则：

- `AAPL`、`US.AAPL`、`AAPL.US`：美股
- `700`、`00700`、`HK.00700`、`0700.HK`：港股
- 纯数字代码默认按港股处理

只使用指定来源：

```powershell
python .\stock_price.py AAPL --source yahoo
python .\stock_price.py 00700 --source tencent
python .\stock_price.py 09988 --source sina
```

输出 JSON：

```powershell
python .\stock_price.py AAPL 00700 --json
```

## 行情说明

这三个网站的公开接口都不是面向程序开发者提供的、有服务等级保证的正式行情
API。接口可能变更，也可能出现延迟、缓存、限流或短暂不可用。因此脚本会同时
显示数据源和行情时间；用于交易判断时，请优先核对券商的买一/卖一价格，不要
将本脚本作为自动下单的唯一行情依据。

## 修改记录

以下内容由 `auto_commit.ps1` 自动维护。最新提交位于最上方，每条记录与对应
Git 提交使用相同的编号和信息。

<!-- AUTO_COMMIT_LOG_START -->
### T11005-0001

修改日期： 2026.09.08 11:00:36
1.修改编号： T11005-0001
2.修改内容： 移植非安卓通用开发规则
3.细节：
1).新增代码验证、问题修复、UI人工确认、修改范围与敏感信息规范
2).新增T11005递增编号、README记录和本地自动提交机制
3).初始化Python项目Git忽略规则并排除Android专属流程
4.修改备注： 未执行单元测试；Python编译、界面模块导入、启动器路径及自动提交脚本语法检查通过；未移植Gradle、ADB、APK等Android规则
<!-- AUTO_COMMIT_LOG_END -->
