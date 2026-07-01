import re
import asyncio
import json
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig

KV_KEY_SUBSCRIPTIONS = "push_subscriptions"
KV_KEY_KNOWN_IDS = "known_news_ids"
MAX_KNOWN_IDS = 500  # 最多记录多少条已推送 ID

# "点击查看" 类新闻的详情页 URL 模板
JIN10_DETAIL_URL = "https://flash.jin10.com/detail/{item_id}"
# 金十详情页 API（如果存在的话，比抓 HTML 更干净）
JIN10_DETAIL_API = "https://xnews.jin10.com/api/details/{numeric_id}"


class Jin10NewsPlugin(Star):
    """金十数据重要新闻插件

    通过 /jin10 指令获取金十数据的重要新闻快讯。
    支持 /jin10_watch 订阅群组自动推送新新闻。
    自动抓取"点击查看"类新闻的全文内容。
    适配企业微信、QQ、Telegram 等所有 AstrBot 支持平台。
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
        self.content_max_length = config.get("content_max_length", 0)
        self.fetch_detail = config.get("fetch_detail", True)
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
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ')
        return text.strip()

    def _format_news_item(self, index: int, item: dict, full_content: str = "") -> str:
        """格式化单条新闻"""
        data = item.get("data", {})
        time_str = data.get("time", "未知时间")
        inner = data.get("data", {})
        title = inner.get("title", "")
        content = inner.get("content", "")

        title = self._strip_html(title)
        content = self._strip_html(content)

        if not title and content:
            match = re.match(r'【(.+?)】', content)
            if match:
                title = match.group(1)
                content = content[content.index('】') + 1:].strip()

        # 如果有抓取到的全文，使用全文替代摘要
        if full_content:
            content = full_content

        lines = [f"📰 #{index} {title}" if title else f"📰 #{index}"]
        lines.append(f"🕐 {time_str}")
        if content:
            limit = self.content_max_length
            if limit > 0 and len(content) > limit:
                content = content[:limit] + "……"
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

    # ──────────────── 详情页抓取 ────────────────

    async def _fetch_article_detail(self, item_id: str, item: dict) -> str:
        """抓取"点击查看"类新闻的全文内容

        通过 flash.jin10.com/detail/{item_id} 获取详情页，
        提取正文段落。失败时返回空字符串。
        """
        if not self.fetch_detail:
            return ""

        url = JIN10_DETAIL_URL.format(item_id=item_id)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with aiohttp.ClientSession() as session:
                # allow_redirects=True 自动跟随 302 跳转
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        logger.warning(f"抓取详情页失败 {url}: HTTP {resp.status}")
                        return ""
                    html = await resp.text()

            # 从 HTML 中提取正文段落
            content = self._extract_article_text(html)
            if content:
                logger.info(f"成功抓取详情全文 {item_id}: {len(content)} 字")
            return content

        except asyncio.TimeoutError:
            logger.warning(f"抓取详情页超时 {item_id}")
            return ""
        except Exception as e:
            logger.warning(f"抓取详情页异常 {item_id}: {e}")
            return ""

    @staticmethod
    def _extract_article_text(html: str) -> str:
        """从 xnews.jin10.com 详情页 HTML 中提取正文纯文本"""
        # 尝试匹配 <div class="content">...</div> 或 <article>...</article>
        # 金十 xnews 页面正文通常在 <div class="details-content"> 或类似结构中
        patterns = [
            r'<div[^>]*class="[^"]*details-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*content\s+detail[^"]*"[^>]*>(.*?)</div>',
            r'<article[^>]*>(.*?)</article>',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                text = match.group(1)
                # 清理 HTML 标签
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                text = re.sub(r'<br\s*/?>', '\n', text)
                text = re.sub(r'</?p[^>]*>', '\n', text)
                text = re.sub(r'</?div[^>]*>', '\n', text)
                text = re.sub(r'<[^>]+>', '', text)
                text = re.sub(r'&nbsp;', ' ', text)
                text = re.sub(r'&lt;', '<', text)
                text = re.sub(r'&gt;', '>', text)
                text = re.sub(r'&amp;', '&', text)
                text = re.sub(r'\n{3,}', '\n\n', text)
                text = text.strip()
                if len(text) > 50:  # 有效内容至少 50 字
                    return text

        # fallback: 尝试提取所有 <p> 标签内容
        para_matches = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
        if para_matches:
            paragraphs = []
            for p in para_matches:
                clean = re.sub(r'<[^>]+>', '', p).strip()
                if len(clean) > 20:
                    paragraphs.append(clean)
            if paragraphs:
                return '\n\n'.join(paragraphs)

        return ""

    def _needs_detail_fetch(self, item: dict) -> bool:
        """判断新闻是否需要抓取详情页全文"""
        if not self.fetch_detail:
            return False
        inner = item.get("data", {}).get("data", {})
        content = inner.get("content", "")
        content_clean = self._strip_html(content)
        # 内容较短且包含"点击查看"
        return len(content_clean) < 150 and "点击查看" in content_clean

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

    # ──────────────── 已知 ID 管理 ────────────────

    async def _get_known_ids(self) -> set:
        """获取所有已知新闻 ID 集合"""
        raw = await self.get_kv_data(KV_KEY_KNOWN_IDS, "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return set(ids) if isinstance(ids, list) else set()
        except (json.JSONDecodeError, TypeError):
            return set()

    async def _save_known_ids(self, known_ids: set):
        """保存已知 ID（限制数量防止无限增长）"""
        ids_list = list(known_ids)
        if len(ids_list) > MAX_KNOWN_IDS:
            ids_list = sorted(ids_list, reverse=True)[:MAX_KNOWN_IDS]
        await self.put_kv_data(KV_KEY_KNOWN_IDS, json.dumps(ids_list))

    async def _mark_ids_seen(self, *item_ids: str):
        """标记 ID 为已见"""
        known = await self._get_known_ids()
        known.update(item_ids)
        await self._save_known_ids(known)

    # ──────────────── 后台轮询 ────────────────

    async def _polling_loop(self):
        """后台轮询任务：定时检查新新闻并逐条推送到订阅群组

        使用已知 ID 集合对比，避免因 API 排序变化导致漏新闻。
        """
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

                known_ids = await self._get_known_ids()
                new_items = []
                for item in news_list:
                    nid = self._get_news_id(item)
                    if nid and nid not in known_ids:
                        new_items.append(item)
                        known_ids.add(nid)

                if not new_items:
                    continue

                await self._save_known_ids(known_ids)

                # 逐条推送（旧→新顺序），并抓取"点击查看"全文
                count = len(new_items)
                for i, item in enumerate(reversed(new_items), 1):
                    item_id = self._get_news_id(item)

                    # 尝试抓取全文
                    full = ""
                    if self._needs_detail_fetch(item):
                        full = await self._fetch_article_detail(item_id, item)

                    header = f"🔔 金十数据 · 重要新闻 ({i}/{count})\n\n"
                    text = header + self._format_news_item(i, item, full)
                    for umo in subscriptions:
                        try:
                            chain = MessageChain().message(text)
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

        for i, item in enumerate(news_list, 1):
            item_id = self._get_news_id(item)
            full = ""
            if self._needs_detail_fetch(item):
                yield event.plain_result(f"⏳ 正在获取第 {i} 条新闻全文...")
                full = await self._fetch_article_detail(item_id, item)

            header = f"📢 金十数据 · 重要新闻 ({i}/{len(news_list)})\n\n"
            text = header + self._format_news_item(i, item, full)
            yield event.plain_result(text)

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
        known_ids = await self._get_known_ids()

        lines = [
            "📊 金十新闻 · 推送状态",
            f"▪ 自动推送：{'✅ 已启用' if self.push_enabled else '❌ 已禁用'}",
            f"▪ 轮询间隔：{self.push_interval} 秒",
            f"▪ 内容长度限制：{'无限制' if self.content_max_length <= 0 else str(self.content_max_length) + ' 字'}",
            f"▪ 抓取全文：{'✅ 已启用' if self.fetch_detail else '❌ 已禁用'}",
            f"▪ 本群订阅：{'✅ 已订阅' if subscribed else '❌ 未订阅'}",
            f"▪ 订阅总数：{len(subscriptions)} 个群",
            f"▪ 已跟踪新闻数：{len(known_ids)} 条",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时调用"""
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("金十重要新闻插件已卸载")
