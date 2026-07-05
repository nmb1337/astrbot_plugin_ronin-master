import re
import asyncio
import json
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

KV_KEY_SUBSCRIPTIONS = "push_subscriptions"
KV_KEY_KNOWN_IDS = "known_news_ids"
MAX_KNOWN_IDS = 500

JIN10_DETAIL_URL = "https://flash.jin10.com/detail/{item_id}"

# 图片渲染 HTML 模板（Jinja2）
NEWS_CARD_TMPL = '''
<div style="width:620px; padding:24px 28px; background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); font-family:'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="display:flex;align-items:center;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid rgba(255,255,255,0.1);">
    <span style="background:#e74c3c;color:#fff;font-size:12px;padding:3px 10px;border-radius:3px;font-weight:bold;">金十数据</span>
    <span style="color:#888;font-size:12px;margin-left:10px;">{{ index_tag }}</span>
  </div>
  <h2 style="color:#f0f0f0;font-size:22px;line-height:1.4;margin:0 0 10px 0;">{{ title }}</h2>
  <div style="color:#999;font-size:13px;margin-bottom:18px;">🕐 {{ time }}</div>
  <div style="color:#d0d0d0;font-size:15px;line-height:1.9;word-break:break-all;">{{ content }}</div>
  {% for img in images %}
  <img src="{{ img }}" style="max-width:100%;margin-top:16px;border-radius:8px;" />
  {% endfor %}
  <div style="margin-top:20px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.06);color:#666;font-size:11px;">— 金十新闻自动推送 —</div>
</div>
'''


class Jin10NewsPlugin(Star):
    """金十数据重要新闻插件

    通过 /jin10 指令获取金十数据的重要新闻快讯。
    支持 /jin10_watch 订阅群组自动推送新新闻。
    自动抓取"点击查看"类新闻的全文内容和图片。
    支持文字/图片渲染两种输出模式。
    适配企业微信、QQ、Telegram 等所有 AstrBot 支持平台。
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
        self.output_mode = config.get("output_mode", "text")
        self.show_images = config.get("show_images", True)
        self._poll_task: asyncio.Task | None = None

    async def initialize(self):
        logger.info("金十重要新闻插件已加载")
        if self.push_enabled:
            self._poll_task = asyncio.create_task(self._polling_loop())
            logger.info(f"金十新闻自动推送已启动，轮询间隔 {self.push_interval}s, 输出模式={self.output_mode}")

    # ──────────────── 工具方法 ────────────────

    @staticmethod
    def _strip_html(text: str) -> str:
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ')
        return text.strip()

    def _build_news_data(self, item: dict, full_content: str = "") -> dict:
        """从 item 中提取标题、时间、内容等结构化数据"""
        data = item.get("data", {})
        time_str = data.get("time", "未知时间")
        inner = data.get("data", {})
        title = self._strip_html(inner.get("title", ""))
        content = self._strip_html(inner.get("content", ""))

        if not title and content:
            match = re.match(r'【(.+?)】', content)
            if match:
                title = match.group(1)
                content = content[content.index('】') + 1:].strip()

        if full_content:
            content = full_content

        limit = self.content_max_length
        if limit > 0 and len(content) > limit:
            content = content[:limit] + "……"

        return {"title": title, "time": time_str, "content": content}

    def _get_api_images(self, item: dict) -> list[str]:
        """从 API 数据中提取图片 URL"""
        images = []
        try:
            inner = item.get("data", {}).get("data", {})
            pic = inner.get("pic", "")
            if pic and pic.startswith("http"):
                images.append(pic)
        except Exception:
            pass
        return images

    async def _fetch_news_api(self) -> dict | None:
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
        return item.get("item_id", "") or item.get("data", {}).get("id", "")

    # ──────────────── 详情页抓取 ────────────────

    async def _fetch_article_detail(self, item_id: str) -> tuple[str, list[str]]:
        """抓取详情页，返回 (全文文本, 图片URL列表)"""
        if not self.fetch_detail:
            return "", []

        url = JIN10_DETAIL_URL.format(item_id=item_id)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        return "", []
                    html = await resp.text()

            text, images = self._extract_article_content(html)
            if text:
                logger.info(f"抓取详情 {item_id}: {len(text)} 字, {len(images)} 图")
            return text, images

        except asyncio.TimeoutError:
            logger.warning(f"抓取详情页超时 {item_id}")
        except Exception as e:
            logger.warning(f"抓取详情页异常 {item_id}: {e}")
        return "", []

    @staticmethod
    def _extract_article_content(html: str) -> tuple[str, list[str]]:
        """从详情页 HTML 提取正文和图片"""
        text = ""
        images = []

        # 正文提取
        patterns = [
            r'<div[^>]*class="[^"]*details-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*content\s+detail[^"]*"[^>]*>(.*?)</div>',
            r'<article[^>]*>(.*?)</article>',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                raw = match.group(1)
                # 提取图片
                imgs = re.findall(r'<img[^>]+src="([^"]+)"', raw, re.IGNORECASE)
                images.extend(imgs)
                # 清理文本
                raw = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
                raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL)
                raw = re.sub(r'<br\s*/?>', '\n', raw)
                raw = re.sub(r'</?p[^>]*>', '\n', raw)
                raw = re.sub(r'</?div[^>]*>', '\n', raw)
                raw = re.sub(r'<[^>]+>', '', raw)
                raw = re.sub(r'&nbsp;', ' ', raw)
                raw = re.sub(r'&lt;', '<', raw)
                raw = re.sub(r'&gt;', '>', raw)
                raw = re.sub(r'&amp;', '&', raw)
                raw = re.sub(r'\n{3,}', '\n\n', raw)
                text = raw.strip()
                if len(text) > 50:
                    break

        # fallback: <p> 标签提取
        if not text:
            para_matches = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
            if para_matches:
                paragraphs = []
                for p in para_matches:
                    clean = re.sub(r'<[^>]+>', '', p).strip()
                    if len(clean) > 20:
                        paragraphs.append(clean)
                if paragraphs:
                    text = '\n\n'.join(paragraphs)

        # 全局图片提取（如果上面没提到）
        if not images:
            all_imgs = re.findall(r'<img[^>]+src="([^"]+)"', html, re.IGNORECASE)
            # 过滤掉头像、图标等小图
            for img in all_imgs:
                if any(k in img.lower() for k in ['avatar', 'icon', 'logo', 'emoji', 'svg', '1x1', 'pixel']):
                    continue
                if img.startswith('http'):
                    images.append(img)

        # 去重
        seen = set()
        unique_images = []
        for img in images:
            if img not in seen:
                seen.add(img)
                unique_images.append(img)

        return text, unique_images

    def _needs_detail_fetch(self, item: dict) -> bool:
        if not self.fetch_detail:
            return False
        inner = item.get("data", {}).get("data", {})
        content = self._strip_html(inner.get("content", ""))
        return len(content) < 150 and "点击查看" in content

    # ──────────────── 发送逻辑 ────────────────

    async def _send_news(self, umo: str, index: int, count: int, news_data: dict,
                         images: list[str], is_push: bool):
        """发送单条新闻到指定会话（支持文字/图片两种模式）"""
        tag = f"🔔 金十数据 · 重要新闻 ({index}/{count})" if is_push else \
              f"📢 金十数据 · 重要新闻 ({index}/{count})"

        if self.output_mode == "image":
            # ── 图片渲染模式 ──
            try:
                render_data = {
                    "index_tag": tag,
                    "title": news_data["title"] or "重要新闻",
                    "time": news_data["time"],
                    "content": news_data["content"],
                    "images": images if self.show_images else [],
                }
                img_url = await self.html_render(NEWS_CARD_TMPL, render_data,
                                                 options={"type": "jpeg", "quality": 90})
                chain = [Comp.Image.fromURL(img_url)]
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"图片渲染失败，回退文字模式: {e}")
                await self._send_news_text(umo, tag, news_data, images)
        else:
            # ── 文字模式 ──
            await self._send_news_text(umo, tag, news_data, images)

    async def _send_news_text(self, umo: str, tag: str, news_data: dict, images: list[str]):
        """文字模式发送：文字消息 + 可选图片"""
        lines = [tag, ""]
        if news_data["title"]:
            lines.append(f"📰 {news_data['title']}")
        lines.append(f"🕐 {news_data['time']}")
        if news_data["content"]:
            lines.append("")
            lines.append(news_data["content"])

        text = "\n".join(lines)
        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)

        # 发送图片（文字模式下单独发图）
        if self.show_images and images:
            for img_url in images[:3]:  # 最多3张
                try:
                    img_chain = [Comp.Image.fromURL(img_url)]
                    await self.context.send_message(umo, img_chain)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"发送图片失败 {img_url[:60]}: {e}")

    # ──────────────── 订阅管理 ────────────────

    async def _get_subscriptions(self) -> list[str]:
        raw = await self.get_kv_data(KV_KEY_SUBSCRIPTIONS, "[]")
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []

    async def _save_subscriptions(self, sessions: list[str]):
        await self.put_kv_data(KV_KEY_SUBSCRIPTIONS, json.dumps(sessions, ensure_ascii=False))

    async def _add_subscription(self, umo: str) -> bool:
        sessions = await self._get_subscriptions()
        if umo in sessions:
            return False
        sessions.append(umo)
        await self._save_subscriptions(sessions)
        return True

    async def _remove_subscription(self, umo: str) -> bool:
        sessions = await self._get_subscriptions()
        if umo not in sessions:
            return False
        sessions.remove(umo)
        await self._save_subscriptions(sessions)
        return True

    # ──────────────── 已知 ID 管理 ────────────────

    async def _get_known_ids(self) -> set:
        raw = await self.get_kv_data(KV_KEY_KNOWN_IDS, "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return set(ids) if isinstance(ids, list) else set()
        except (json.JSONDecodeError, TypeError):
            return set()

    async def _save_known_ids(self, known_ids: set):
        ids_list = list(known_ids)
        if len(ids_list) > MAX_KNOWN_IDS:
            ids_list = sorted(ids_list, reverse=True)[:MAX_KNOWN_IDS]
        await self.put_kv_data(KV_KEY_KNOWN_IDS, json.dumps(ids_list))

    # ──────────────── 后台轮询 ────────────────

    async def _polling_loop(self):
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

                count = len(new_items)
                for i, item in enumerate(reversed(new_items), 1):
                    item_id = self._get_news_id(item)
                    images = list(self._get_api_images(item))

                    full_text = ""
                    if self._needs_detail_fetch(item):
                        full_text, detail_images = await self._fetch_article_detail(item_id)
                        images.extend(detail_images)

                    news_data = self._build_news_data(item, full_text)
                    for umo in subscriptions:
                        await self._send_news(umo, i, count, news_data, images, is_push=True)

            except asyncio.CancelledError:
                logger.info("金十新闻后台轮询任务已取消")
                break
            except Exception as e:
                logger.error(f"金十新闻轮询异常: {e}")

    # ──────────────── 指令 ────────────────

    @filter.command("jin10")
    async def fetch_news(self, event: AstrMessageEvent, count: int = 0):
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

        umo = event.unified_msg_origin
        for i, item in enumerate(news_list, 1):
            item_id = self._get_news_id(item)
            images = list(self._get_api_images(item))

            full_text = ""
            if self._needs_detail_fetch(item):
                yield event.plain_result(f"⏳ 正在获取第 {i} 条新闻全文及图片...")
                full_text, detail_images = await self._fetch_article_detail(item_id)
                images.extend(detail_images)

            news_data = self._build_news_data(item, full_text)
            await self._send_news(umo, i, len(news_list), news_data, images, is_push=False)

    @filter.command("jin10_watch")
    async def watch_news(self, event: AstrMessageEvent):
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
        umo = event.unified_msg_origin
        removed = await self._remove_subscription(umo)
        if removed:
            yield event.plain_result("✅ 已取消金十新闻自动推送。")
        else:
            yield event.plain_result("ℹ️ 本群尚未订阅推送。")

    @filter.command("jin10_status")
    async def status_news(self, event: AstrMessageEvent):
        subscriptions = await self._get_subscriptions()
        subscribed = event.unified_msg_origin in subscriptions
        known_ids = await self._get_known_ids()

        lines = [
            "📊 金十新闻 · 推送状态",
            f"▪ 自动推送：{'✅ 已启用' if self.push_enabled else '❌ 已禁用'}",
            f"▪ 轮询间隔：{self.push_interval} 秒",
            f"▪ 输出模式：{'🖼️ 图片渲染' if self.output_mode == 'image' else '📝 文字'}",
            f"▪ 文章图片：{'✅ 显示' if self.show_images else '❌ 隐藏'}",
            f"▪ 内容截断：{'无限制' if self.content_max_length <= 0 else str(self.content_max_length) + ' 字'}",
            f"▪ 抓取全文：{'✅ 已启用' if self.fetch_detail else '❌ 已禁用'}",
            f"▪ 本群订阅：{'✅ 已订阅' if subscribed else '❌ 未订阅'}",
            f"▪ 订阅总数：{len(subscriptions)} 个群",
            f"▪ 已跟踪新闻数：{len(known_ids)} 条",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("金十重要新闻插件已卸载")
