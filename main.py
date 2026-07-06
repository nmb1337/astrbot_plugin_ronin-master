import re
import asyncio
import json
import os
import subprocess
import tempfile
from functools import lru_cache
import aiohttp
from PIL import Image, ImageDraw, ImageFont
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

KV_KEY_SUBSCRIPTIONS = "push_subscriptions"
KV_KEY_KNOWN_IDS = "known_news_ids"
MAX_KNOWN_IDS = 500

JIN10_DETAIL_URL = "https://flash.jin10.com/detail/{item_id}"

# ──────────────── Pillow 本地图片渲染 ────────────────

_FONT_EXTS = (".ttf", ".ttc", ".otf")
_FONT_NAME_KEYWORDS = (
    "cjk", "noto", "sourcehan", "source-han", "wqy", "wenquanyi",
    "droidsansfallback", "fallback", "simsun", "simhei", "msyh",
    "yahei", "pingfang", "hiragino", "song", "hei", "kaiti",
    "fangsong", "sarasa", "harmonyos", "miui", "oppo",
)


def _is_font_file(path: str | None) -> bool:
    return bool(path and os.path.isfile(path) and path.lower().endswith(_FONT_EXTS))


def _looks_like_chinese_font(path: str) -> bool:
    name = os.path.basename(path).lower()
    return any(keyword in name for keyword in _FONT_NAME_KEYWORDS)


@lru_cache(maxsize=16)
def _find_chinese_font(config_font_path: str | None = None) -> str | None:
    """查找系统中可用的中文字体，优先使用用户配置路径。"""
    preferred = [
        config_font_path,
        os.getenv("ASTRBOT_JIN10_FONT"),
        os.getenv("JIN10_FONT_PATH"),
    ]
    for path in preferred:
        if _is_font_file(path):
            return path

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(plugin_dir, "fonts", "NotoSansCJK-Regular.ttc"),
        os.path.join(plugin_dir, "fonts", "NotoSansCJKsc-Regular.otf"),
        os.path.join(plugin_dir, "fonts", "NotoSansSC-Regular.ttf"),
        os.path.join(plugin_dir, "fonts", "SourceHanSansSC-Regular.otf"),
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/Deng.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Medium.otf",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]
    for path in candidates:
        if _is_font_file(path):
            return path

    search_roots = [
        os.path.join(plugin_dir, "fonts"),
        os.path.join(plugin_dir, "assets"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/app/fonts",
        "/fonts",
    ]
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if _is_font_file(path) and _looks_like_chinese_font(path):
                    return path

    for family in ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "SimHei"):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", family],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            continue
        path = result.stdout.strip()
        if _is_font_file(path) and _looks_like_chinese_font(path):
            return path

    return None


def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()


def _stop_event(event: AstrMessageEvent):
    stop_event = getattr(event, "stop_event", None)
    if callable(stop_event):
        stop_event()

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str]:
    """将文本按像素宽度换行"""
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph.strip():
            lines.append('')
            continue
        current = ""
        for ch in paragraph:
            test = current + ch
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width and current:
                lines.append(current)
                current = ch
            else:
                current = test
        if current:
            lines.append(current)
    return lines

def _render_news_card_sync(tag: str, title: str, time_str: str,
                           content: str, image_paths: list[str],
                           config_font_path: str | None = None) -> str:
    """使用 Pillow 本地渲染新闻卡片图片，返回临时文件路径"""
    W = 640
    PAD = 28
    CW = W - 2 * PAD  # content width
    BG = (26, 26, 46)          # #1a1a2e
    CARD_BG = (22, 33, 62)     # #16213e
    RED = (231, 76, 60)        # #e74c3c
    WHITE = (240, 240, 240)
    GRAY = (153, 153, 153)
    LIGHT_GRAY = (208, 208, 208)
    DARK_GRAY = (102, 102, 102)
    LINE_COLOR = (255, 255, 255, 25)

    font_path = _find_chinese_font(config_font_path)
    if not font_path:
        logger.warning("未找到中文字体，图片渲染可能出现方块/乱码。请在配置 font_path 中填写中文字体文件路径。")
    try:
        ft_title = _load_font(font_path, 22)
        ft_body = _load_font(font_path, 15)
        ft_time = _load_font(font_path, 13)
        ft_tag = _load_font(font_path, 12)
        ft_footer = _load_font(font_path, 11)
    except Exception as exc:
        logger.warning(f"加载字体失败：{font_path}, {exc}")
        ft_title = ft_body = ft_time = ft_tag = ft_footer = ImageFont.load_default()

    # 准备绘制用的 ImageDraw（用于测量）
    dummy = Image.new('RGB', (W, 100))
    d_draw = ImageDraw.Draw(dummy)

    # 标题换行
    title_lines = _wrap_text(d_draw, title, ft_title, CW)
    # 正文换行
    body_lines = _wrap_text(d_draw, content, ft_body, CW)

    # 计算总高度
    line_h_title = ft_title.size + 6 if hasattr(ft_title, 'size') else 28
    line_h_body = ft_body.size + 6 if hasattr(ft_body, 'size') else 24

    h = 0
    h += PAD  # top padding
    # tag bar
    h += 24 + 16  # tag height + gap
    # title
    h += len(title_lines) * line_h_title + 12
    # time
    h += line_h_body + 18
    # body
    h += len(body_lines) * line_h_body + 20
    # footer
    h += 14 + PAD

    # 图片预留高度（每张图 max 300px 高）
    img_region_h = 0
    for img_path in image_paths:
        try:
            im = Image.open(img_path)
            iw, ih = im.size
            scale = min(CW / iw, 300 / ih, 1.0) if iw > 0 else 1.0
            img_region_h += int(ih * scale) + 16
            im.close()
        except Exception:
            pass
    h += img_region_h

    # 创建画布
    img = Image.new('RGB', (W, max(h, 200)), BG)
    draw = ImageDraw.Draw(img)

    y = PAD

    # ── 顶部分隔线 ──
    draw.line([(0, 0), (W, 0)], fill=RED, width=3)

    # ── 标签栏 ──
    tag_text = "金十数据"
    tag_bbox = draw.textbbox((0, 0), tag_text, font=ft_tag)
    tag_w = tag_bbox[2] - tag_bbox[0] + 20
    tag_h = tag_bbox[3] - tag_bbox[1] + 8
    draw.rounded_rectangle([(PAD, y), (PAD + tag_w, y + tag_h)], radius=4, fill=RED)
    draw.text((PAD + 10, y + 4), tag_text, fill=(255, 255, 255), font=ft_tag)
    # index tag
    idx_bbox = draw.textbbox((0, 0), tag, font=ft_tag)
    draw.text((PAD + tag_w + 12, y + 5), tag, fill=GRAY, font=ft_tag)
    y += tag_h + 16

    # ── 分隔线 ──
    draw.line([(PAD, y - 4), (W - PAD, y - 4)], fill=LINE_COLOR, width=1)

    # ── 标题 ──
    for line in title_lines:
        draw.text((PAD, y), line, fill=WHITE, font=ft_title)
        y += line_h_title
    y += 12

    # ── 时间 ──
    time_label = f"🕐 {time_str}"
    draw.text((PAD, y), time_label, fill=GRAY, font=ft_time)
    y += line_h_body + 18

    # ── 正文 ──
    for line in body_lines:
        draw.text((PAD, y), line, fill=LIGHT_GRAY, font=ft_body)
        y += line_h_body
    y += 20

    # ── 图片 ──
    for img_path in image_paths:
        try:
            im = Image.open(img_path).convert('RGB')
            iw, ih = im.size
            scale = min(CW / iw, 300 / ih, 1.0) if iw > 0 else 1.0
            new_w, new_h = int(iw * scale), int(ih * scale)
            im = im.resize((new_w, new_h), Image.LANCZOS)
            img.paste(im, (PAD, y))
            im.close()
            y += new_h + 16
        except Exception:
            pass

    # ── 底部分隔线 ──
    draw.line([(PAD, y - 8), (W - PAD, y - 8)], fill=LINE_COLOR, width=1)

    # ── 页脚 ──
    footer_text = "— 金十新闻自动推送 —"
    draw.text((PAD, y), footer_text, fill=DARK_GRAY, font=ft_footer)

    # 保存到临时文件
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    img.save(tmp, format='PNG')
    tmp.close()
    return tmp.name


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
        self.font_path = config.get("font_path", "")
        self._poll_task: asyncio.Task | None = None

    async def initialize(self):
        logger.info("金十重要新闻插件已加载")
        if self.push_enabled:
            self._poll_task = asyncio.create_task(self._polling_loop())
            logger.info(f"金十新闻自动推送已启动，轮询间隔 {self.push_interval}s, 输出模式={self.output_mode}")

    # ──────────────── 工具方法 ────────────────

    @staticmethod
    def _strip_html(text: str) -> str:
        if not text or not isinstance(text, str):
            return ""
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ')
        return text.strip()

    def _build_news_data(self, item: dict, full_content: str = "") -> dict:
        """从 item 中提取标题、时间、内容等结构化数据"""
        data = item.get("data", {}) or {}
        time_str = data.get("time") or "未知时间"
        inner = data.get("data") or {}
        title = self._strip_html(inner.get("title") or "")
        content = self._strip_html(inner.get("content") or "")

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
            inner = (item.get("data") or {}).get("data") or {}
            pic = inner.get("pic") or ""
            if pic and isinstance(pic, str) and pic.startswith("http"):
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
        try:
            inner = item.get("data", {}).get("data", {}) or {}
            content = self._strip_html(inner.get("content") or "")
            return len(content) < 150 and "点击查看" in content
        except Exception:
            return False

    # ──────────────── 发送逻辑 ────────────────

    # ──────────────── 图片下载与发送 ────────────────

    async def _download_image(self, url: str) -> str | None:
        """下载图片到临时文件，返回本地路径"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
            # 推断扩展名
            ext = ".jpg"
            url_lower = url.split("?")[0].lower()
            if ".png" in url_lower:
                ext = ".png"
            elif ".gif" in url_lower:
                ext = ".gif"
            elif ".webp" in url_lower:
                ext = ".webp"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(data)
            tmp.close()
            return tmp.name
        except Exception as e:
            logger.warning(f"下载图片失败 {url[:60]}: {e}")
            return None

    async def _send_image_to(self, umo: str, img_url: str) -> bool:
        """下载并发送单张图片，返回是否成功"""
        local_path = await self._download_image(img_url)
        if not local_path:
            return False
        try:
            chain = MessageChain().file_image(local_path)
            await self.context.send_message(umo, chain)
            return True
        except Exception as e:
            logger.warning(f"发送图片失败 {img_url[:60]}: {e}")
            return False
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    # ──────────────── 图片渲染 ────────────────

    async def _render_news_card(self, tag: str, title: str, time_str: str,
                                 content: str, image_urls: list[str]) -> str | None:
        """异步渲染新闻卡片：先下载图片，再调用 Pillow 渲染"""
        # 下载文中图片到本地
        local_paths = []
        for url in image_urls[:5]:
            path = await self._download_image(url)
            if path:
                local_paths.append(path)

        try:
            loop = asyncio.get_running_loop()
            card_path = await loop.run_in_executor(
                None, _render_news_card_sync, tag, title, time_str, content, local_paths, self.font_path
            )
            return card_path
        except Exception as e:
            logger.error(f"Pillow 渲染失败: {e}")
            return None
        finally:
            # 清理下载的临时图片
            for p in local_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # ──────────────── 发送逻辑 ────────────────

    async def _send_news(self, umo: str, index: int, count: int, news_data: dict,
                         images: list[str], is_push: bool):
        """发送单条新闻到指定会话（支持文字/图片两种模式）"""
        tag = f"🔔 金十数据 · 重要新闻 ({index}/{count})" if is_push else \
              f"📢 金十数据 · 重要新闻 ({index}/{count})"
        title = news_data["title"] or "重要新闻"
        time_str = news_data["time"]
        content = news_data["content"]
        show_imgs = images if self.show_images else []

        if self.output_mode == "image":
            # ── 图片渲染模式：本地 Pillow 渲染 ──
            card_path = await self._render_news_card(tag, title, time_str, content, show_imgs)
            if card_path:
                try:
                    chain = MessageChain().file_image(card_path)
                    await self.context.send_message(umo, chain)
                    return
                except Exception as e:
                    logger.error(f"发送渲染图片失败: {e}")
                finally:
                    try:
                        os.unlink(card_path)
                    except OSError:
                        pass
            # 渲染失败则回退文字模式
            logger.warning("图片渲染失败，回退文字模式")
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
                await self._send_image_to(umo, img_url)
                await asyncio.sleep(0.3)

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
        _stop_event(event)
        if count <= 0:
            count = self.default_count
        count = min(count, self.max_count)

        result = await self._fetch_news_api()
        if result is None:
            yield event.plain_result("⚠️ 获取新闻失败，请稍后再试。")
            _stop_event(event)
            return

        news_list = result.get("data", [])[:count]
        if not news_list:
            yield event.plain_result("📭 当前暂无重要新闻。")
            _stop_event(event)
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
        _stop_event(event)

    @filter.command("jin10_watch")
    async def watch_news(self, event: AstrMessageEvent):
        _stop_event(event)
        if not self.push_enabled:
            yield event.plain_result("⚠️ 自动推送功能未启用，请在插件配置中开启 push_enabled。")
            _stop_event(event)
            return
        umo = event.unified_msg_origin
        added = await self._add_subscription(umo)
        if added:
            yield event.plain_result("✅ 已订阅金十重要新闻自动推送，有新新闻时将自动发送到本群。\n"
                                     "使用 /jin10_unwatch 可取消订阅。")
        else:
            yield event.plain_result("ℹ️ 本群已订阅，无需重复操作。")
        _stop_event(event)

    @filter.command("jin10_unwatch")
    async def unwatch_news(self, event: AstrMessageEvent):
        _stop_event(event)
        umo = event.unified_msg_origin
        removed = await self._remove_subscription(umo)
        if removed:
            yield event.plain_result("✅ 已取消金十新闻自动推送。")
        else:
            yield event.plain_result("ℹ️ 本群尚未订阅推送。")
        _stop_event(event)

    @filter.command("jin10_status")
    async def status_news(self, event: AstrMessageEvent):
        _stop_event(event)
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
        _stop_event(event)

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("金十重要新闻插件已卸载")
