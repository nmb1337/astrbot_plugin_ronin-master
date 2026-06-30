# astrbot_plugin_jin10_news 🚀

金十数据重要新闻插件 —— 在 AstrBot 中通过 `/jin10` 指令获取最新财经快讯。

数据来源：[金十数据 - 重要事件](https://topic17z2k407.jin10.com/topic/jin10_important_news.html)

## ✨ 功能

- **`/jin10`** — 获取最近 5 条重要新闻（默认）
- **`/jin10 <数量>`** — 获取指定条数新闻，如 `/jin10 10`
- **`/jin10_watch`** — 订阅当前群组，有新新闻时自动推送
- **`/jin10_unwatch`** — 取消当前群组的自动推送
- **`/jin10_status`** — 查看当前推送状态
- 自动解析新闻标题与正文（支持 `【标题】` 格式）
- 内容过长自动截断，适合聊天窗口展示

## 📦 安装

1. 在 AstrBot WebUI → 插件市场 搜索 `astrbot_plugin_jin10_news` 安装
2. 或手动克隆到 `AstrBot/data/plugins/` 目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/ronin/astrbot_plugin_jin10_news.git
```

3. 安装依赖：

```bash
pip install -r requirements.txt
```

4. 在 WebUI 中启用插件即可使用。

## ⚙️ 配置

可在 AstrBot WebUI 插件管理面板中修改以下配置项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_base_url` | 金十 API 地址 |（内置） |
| `app_id` | API App ID | `EzF2s2HxxU0U5bYa` |
| `x_version` | API 版本号 | `1.0.0` |
| `default_count` | 默认获取条数 | `5` |
| `max_count` | 单次最大获取条数 | `20` |
| `push_enabled` | 是否启用自动推送 | `true` |
| `push_interval` | 推送轮询间隔（秒） | `60` |

一般无需修改，保持默认即可。

## 🔔 自动推送

1. 在需要接收新闻的群组中发送 `/jin10_watch` 订阅
2. 插件会每隔 `push_interval` 秒轮询一次金十 API
3. 发现新新闻后自动推送到所有已订阅的群组
4. 发送 `/jin10_unwatch` 取消订阅

> **注意**：请勿将 `push_interval` 设置过短（建议 ≥ 30 秒），以免触发 API 限流。

## 🖼️ 效果预览

```
📢 金十数据 · 重要新闻（最近 5 条）

📰 #1 上期所：调整黄金等期货相关合约涨跌停板幅度和交易保证金比例
🕐 2026-06-30 19:00:12
金十期货6月30日讯，上期所公告，自2026年7月2日收盘结算时起...
──────────────────────────────
📰 #2 SK海力士已提交纳斯达克上市申请
🕐 2026-06-30 18:53:22
金十数据6月30日讯，市场消息：SK海力士已提交在纳斯达克上市申请。
──────────────────────────────
...
```

## 🔧 开发

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件框架开发，使用 `aiohttp` 异步请求金十数据 API。

## 📄 License

MIT
