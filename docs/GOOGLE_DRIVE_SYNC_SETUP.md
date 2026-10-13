# Google Drive 云端同步配置

电脑端使用 Google Drive 的 `appDataFolder` 保存完整 `state.json` 快照。云端文件只对同一 Google Cloud 项目下、获得 `drive.appdata` 授权的客户端可见。

## Google Cloud 配置

1. 在 Google Cloud Console 创建或选择项目。
2. 启用 **Google Drive API**。
3. 配置 OAuth consent screen；应用处于测试状态时，把实际登录的 Google 账户加入 Test users。
4. 创建 OAuth client，Application type 选择 **Desktop app**。
5. 下载客户端 JSON，重命名为 `google_oauth_client.json`。

程序依次从以下位置读取配置：

1. 环境变量 `TRADE_VIEW_GOOGLE_OAUTH_CLIENT_FILE` 指向的文件。
2. `%LOCALAPPDATA%\StockPriceViewer\google_oauth_client.json`。
3. 程序所在目录下的 `google_oauth_client.json`（源码运行时即当前工程目录）。

真实客户端 JSON 已被 `.gitignore` 排除，禁止提交到仓库。仓库中的 `google_oauth_client.example.json` 仅用于展示格式。

## 首次使用

1. 确认电脑已安装 Google Chrome。
2. 点击“登录谷歌”。
3. 程序会调用 Google Chrome，在新窗口中打开 Google 账户选择与授权页面。
4. 选择 Google 账户并允许应用访问自己的应用专用 Drive 数据。
5. 授权结果通过本机 `127.0.0.1` 临时回调传回程序。
6. 登录成功后，再点击“上传云端”或“同步云端”。这两个操作不会自行打开浏览器。

OAuth 访问令牌和刷新令牌使用 Windows DPAPI 加密，保存在当前 Windows 用户的 `%LOCALAPPDATA%\StockPriceViewer\google_oauth_token.bin`，不会写入 `state.json` 或 Git 仓库。

## 同步内容与覆盖规则

上传内容是完整状态文件，包括股票、交易记录、T 交易分组、出入金记录、窗口位置、列宽、当前页及其他界面状态。

“上传云端”以本地完整状态覆盖云端快照并增加云端 revision。“同步云端”下载并校验完整快照，在本地创建 `state.json.bak.<时间戳>` 后替换当前状态。同步成功后程序关闭，重新打开即可载入云端状态。
