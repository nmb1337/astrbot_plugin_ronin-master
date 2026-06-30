import re
import asyncio
import json
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig

KV_KEY_SUBSCRIPTIONS = "push_subscriptions"
KV_KEY_LAST_NEWS_ID = "last_news_id"


class Jin10NewsPlugin(Star):
    """金十数据重要新闻插件

    通过 /jin10 指令获取金十数据的重要新闻快讯。
    支持 /jin10 watch 订阅群组自动推送新新闻。
    数据来源：https://topic17z2k407.jin10.com/topic/jin10_important_news.html
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base_url = config.get("api_base_url", "https://1b8d6028d99849668a6d8755c79e650f.z3c.jin10.com/top/flashsByTime")
        self.app_id = config.get("app_id", "EzF2s2HxxU0U5bYa")
        self.x_version = config.get("x_version", "1.0.0")
        self.default_count = config.get("default_count", 5)
        self.max_count = config.get("max_count", 20)
        self.push_enabled = config.get("push_enabled", True)
        self.push_interval = config.get("push_interval", 60)
        self._poll_task: asyncio.Task | None = None

    async def initialize(self):
        """插件初始化时启动后台轮询任务"""
        logger.info("金十重要新闻插件已加载")
        if self.push_enabled:
            self._poll_task = asyncio.create_task(self._polling_loop())
            logger.info(f"金十新闻自动推送已启动，轮询间隔 {self.push_interval}s")

    # ──────────────── 工具方法 ────────────────

    @staticmethod
    def _strip_html(text: str) -> str:
        """去除 HTML 标签，<br/> 转为换行"""
        text = re.sub(r'<br\\s*/?>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ')
        return text.strip()

    @staticmethod
    def _format_news_item(index: int, item: dict) -> str:
        """格式化单条新闻"""
        data = item.get("data", {})
        time_str = data.get("time", "未知时间")
        inner = data.get("data", {})
        title = inner.get("title", "")
        content = inner.get("content", "")

        title = Jin10NewsPlugin._strip_html(title)
        content = Jin10NewsPlugin._strip_html(content)

        if not title and content:
            match = re.match(r'【(.+?)】', content)
            if match:
                title = match.group(1)
                content = content[content.index('】') + 1:].strip()

        lines = [f"📰 #{index} {title}" if title else f"📰 #{index}"]
        lines.append(f"🕐 {time_str}")
        if content:
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(content)
        return "\n".join(lines)

    async def _fetch_news_api(self) -> dict | None:
        """调用 Jin10 API 获取新闻数据，返回 JSON 或 None"""
        url = f"{self.api_base_url}?time_type=time&sort=priority"
        headers = {
            "x-version": self.x_version,
            "x-app-id": self.app_id,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.error(f"金十 API 请求失败，状态码: {resp.status}")
                        return None
                    result = await resp.json()
            if result.get("status") != 200 or "data" not in result:
                logger.error(f"金十 API 返回异常: {result}")
                return None
            return result
        except Exception as e:
            logger.error(f"金十 API 网络请求异常: {e}")
            return None

    def _get_news_id(self, item: dict) -> str:
        """从新闻条目中提取唯一标识"""
        return item.get("item_id", "") or item.get("data", {}).get("id", "")

    # ──────────────── 订阅管理 ────────────────

    async def _get_subscriptions(self) -> list[str]:
        """获取所有订阅会话"""
        raw = await self.get_kv_data(KV_KEY_SUBSCRIPTIONS, "[]")
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []

    async def _save_subscriptions(self, sessions: list[str]):
        """保存订阅会话"""
        await self.put_kv_data(KV_KEY_SUBSCRIPTIONS, json.dumps(sessions, ensure_ascii=False))

    async def _add_subscription(self, umo: str) -> bool:
        """添加订阅，返回是否新增"""
        sessions = await self._get_subscriptions()
        if umo in sessions:
            return False
        sessions.append(umo)
        await self._save_subscriptions(sessions)
        return True

    async def _remove_subscription(self, umo: str) -> bool:
        """移除订阅，返回是否移除"""
        sessions = await self._get_subscriptions()
        if umo not in sessions:
            return False
        sessions.remove(umo)
        await self._save_subscriptions(sessions)
        return True

    # ──────────────── 后台轮询 ────────────────

    async def _polling_loop(self):
        """后台轮询任务：定时检查新新闻并推送到订阅群组"""
        while True:
            try:
                await asyncio.sleep(self.push_interval)
                subscriptions = await self._get_subscriptions()
                if not subscriptions:
                    continue

                result = await self._fetch_news_api()
                if not result:
                    continue

                news_list = result.get("data", [])
                if not news_list:
                    continue

                last_id = await self.get_kv_data(KV_KEY_LAST_NEWS_ID, "")
                new_items = []
                for item in news_list:
                    nid = self._get_news_id(item)
                    if nid == last_id:
                        break
                    new_items.append(item)

                if not new_items:
                    continue

                # 更新最新 ID
                await self.put_kv_data(KV_KEY_LAST_NEWS_ID, self._get_news_id(news_list[0]))

                # 构建推送消息
                push_text = f"🔔 金十数据 · 新重要新闻（{len(new_items)} 条）\n\n"
                for i, item in enumerate(reversed(new_items), 1):
                    push_text += self._format_news_item(i, item) + "\n"
                    push_text += "─" * 30 + "\n"

                # 推送到所有订阅会话
                for umo in subscriptions:
                    try:
                        chain = MessageChain().message(push_text.rstrip("\n"))
                        await self.context.send_message(umo, chain)
                    except Exception as e:
                        logger.error(f"推送消息到 {umo[:30]}... 失败: {e}")

            except asyncio.CancelledError:
                logger.info("金十新闻后台轮询任务已取消")
                break
            except Exception as e:
                logger.error(f"金十新闻轮询异常: {e}")

    # ──────────────── 指令 ────────────────

    @filter.command("jin10")
    async def fetch_news(self, event: AstrMessageEvent, count: int = 0):
        """获取金十数据重要新闻 /jin10 [数量]"""
        if count <= 0:
            count = self.default_count
        count = min(count, self.max_count)

        result = await self._fetch_news_api()
        if result is None:
            yield event.plain_result("⚠️ 获取新闻失败，请稍后再试。")
            return

        news_list = result.get("data", [])[:count]
        if not news_list:
            yield event.plain_result("📭 当前暂无重要新闻。")
            return

        output_lines = [f"📢 金十数据 · 重要新闻（最近 {len(news_list)} 条）\n"]
        for i, item in enumerate(news_list, 1):
            output_lines.append(self._format_news_item(i, item))
            output_lines.append("─" * 30)

        yield event.plain_result("\n".join(output_lines))

    @filter.command("jin10_watch")
    async def watch_news(self, event: AstrMessageEvent):
        """订阅当前群组，自动推送新新闻 /jin10_watch"""
        if not self.push_enabled:
            yield event.plain_result("⚠️ 自动推送功能未启用，请在插件配置中开启 push_enabled。")
            return

        umo = event.unified_msg_origin
        added = await self._add_subscription(umo)
        if added:
            yield event.plain_result("✅ 已订阅金十重要新闻自动推送，有新新闻时将自动发送到本群。\n"
                                     "使用 /jin10_unwatch 可取消订阅。")
        else:
            yield event.plain_result("ℹ️ 本群已订阅，无需重复操作。")

    @filter.command("jin10_unwatch")
    async def unwatch_news(self, event: AstrMessageEvent):
        """取消当前群组的新闻推送 /jin10_unwatch"""
        umo = event.unified_msg_origin
        removed = await self._remove_subscription(umo)
        if removed:
            yield event.plain_result("✅ 已取消金十新闻自动推送。")
        else:
            yield event.plain_result("ℹ️ 本群尚未订阅推送。")

    @filter.command("jin10_status")
    async def status_news(self, event: AstrMessageEvent):
        """查看当前推送状态 /jin10_status"""
        subscriptions = await self._get_subscriptions()
        subscribed = event.unified_msg_origin in subscriptions
        last_id = await self.get_kv_data(KV_KEY_LAST_NEWS_ID, "")

        lines = [
            "📊 金十新闻 · 推送状态",
            f"▪ 自动推送：{'✅ 已启用' if self.push_enabled else '❌ 已禁用'}",
            f"▪ 轮询间隔：{self.push_interval} 秒",
            f"▪ 本群订阅：{'✅ 已订阅' if subscribed else '❌ 未订阅'}",
            f"▪ 订阅总数：{len(subscriptions)} 个群",
            f"▪ 已记录最新：{last_id[:20] + '...' if last_id else '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时调用"""
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("金十重要新闻插件已卸载")
