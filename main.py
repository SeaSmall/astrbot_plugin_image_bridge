"""
astrbot_plugin_image_bridge — 图片问答桥接插件（Image Bridge）

功能：
1. 用户发送图片时，插件调用免费 OCR 接口（OCR.space）识别图片中的文字。
2. 若用户只发了图片（未附带文字问题），AI 不会回答，插件静默挂起识别内容，后台等待用户输入问题。
3. 用户随后发送文字问题时，插件把图片识别内容一并注入本次 LLM 请求，
   让 AI 结合图片内容回答（即"图片门控问答"：先发图，再提问，AI 才作答）。

适用于不支持多模态（图片直接进 LLM）的平台（如 QQ 个人号 aiocqhttp 等），
也适用于希望先把图片识别成文字、再交给 LLM 的场景。
"""

from __future__ import annotations

import mimetypes
import re
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 默认配置值（用户可在 AstrBot WebUI 的插件配置弹窗中修改）
# ---------------------------------------------------------------------------
DEFAULT_OCR_API_URL = "https://api.ocr.space/parse/image"
DEFAULT_OCR_API_KEY = "helloworld"  # OCR.space 公开测试 key（免费、有限流）
DEFAULT_OCR_LANGUAGE = "chs"  # chs=简体中文 / eng=英文 / chinese_tra=繁体中文 ...
DEFAULT_OCR_TIMEOUT = 60  # OCR 请求超时（秒）
DEFAULT_PENDING_TTL = 1800  # 图片识别内容有效期（秒），超时后需重新发送图片
DEFAULT_EMOJI_WAIT_PENDING = True  # 表情是否也参与「发送后等待提问」门控
DEFAULT_PROMPT_TEMPLATE = (
    "<用户上传的图片识别内容>\n"
    "{image_content}\n"
    "</用户上传的图片识别内容>\n"
    "以上是用户刚上传图片的 OCR 识别文字，请结合该图片内容回答用户的问题。"
)

# QQ 官方平台表情标记：<faceType=...> 被 _parse_face_message 转成 [表情] 或 [表情:赞]
EMOJI_RE = re.compile(r"\[表情(?::([^\]]+))?\]")


class ImageBridgePlugin(Star):
    """图片问答桥接插件：图片先识别，文字提问后 AI 一并作答。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # 挂起的图片识别内容：{key: {"text": str, "ts": float}}
        # key = 会话 + 发送者，避免群聊中 A 的图片被 B 的问题消费
        self._pending: dict[str, dict] = {}

    # ------------------------------------------------------------------ 工具
    def _cfg(self, key: str, default):
        """读取插件配置，异常时回退默认值。"""
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _pending_key(self, event: AstrMessageEvent) -> str:
        """构造挂起内容的键：同一会话内按发送者区分。"""
        return f"{event.unified_msg_origin}:::{event.get_sender_id()}"

    @staticmethod
    def _extract_emoji_desc(text: str) -> str | None:
        """从消息文本提取 QQ 表情语义描述（AI 可理解），无表情标记返回 None。

        - `[表情:赞]` -> "用户发送了一个表情：[赞]"
        - `[表情]`（自定义表情包，无具体名）-> "用户发送了一个表情包（内容见下方 OCR 识别文字）"
        """
        m = EMOJI_RE.search(text or "")
        if not m:
            return None
        name = (m.group(1) or "").strip()
        if name:
            return f"用户发送了一个表情：[{name}]"
        return "用户发送了一个表情包（具体内容见下方 OCR 识别文字）"

    @staticmethod
    def _is_emoji_image(comp, text: str) -> bool:
        """判断图片组件是否疑似平台表情图（如 QQ 官方把表情解析为 [表情] 文本 + 图片附件）。

        命中条件（任一）：
        - 消息文本含 QQ 表情标记 `[表情`（`_parse_face_message` 输出，如 `[表情]`、`[表情:赞]`）；
        - 图片 url/file 带常见表情特征（emoticon / sticker / qqface / face / emoji / biaoqing 等）。
        """
        if "[表情" in (text or ""):
            return True
        url = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "").lower()
        for kw in ("emoticon", "sticker", "qqface", "face/", "face_", "emoji", "biaoqing", "emotion"):
            if kw in url:
                return True
        return False

    def _extract_images(self, event: AstrMessageEvent, text: str = "") -> list:
        """从消息链中提取所有 Image 组件。

        表情图（QQ 官方把表情解析为 [表情] 文本 + 图片附件）**不跳过**——
        表情包 gif 会切帧后进 OCR，让 AI 识别出表情包上的文字。
        """
        components = getattr(event.message_obj, "message", None) or []
        images = []
        for comp in components:
            ctype = getattr(comp, "type", None)
            if ctype == "image" or type(comp).__name__ == "Image":
                if self._is_emoji_image(comp, text):
                    logger.debug("[image_bridge] 表情图进入切帧 OCR（识别表情包文字）")
                images.append(comp)
        return images

    @staticmethod
    def _sniff_image_ext(data: bytes) -> str:
        """根据文件头判断图片扩展名，供无扩展名的缓存文件使用。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:2] == b"BM":
            return "bmp"
        return ""

    @staticmethod
    def _gif_to_static_frame(file_path: str) -> str:
        """GIF 动图取第一帧转为 JPEG 静态图（供 OCR 识别）。

        表情包多为 gif 动图，OCR 无法直接识别动图；切第一帧后即可识别
        表情包上的文字（参考 AstrBot 生态 smart_imagechat_hub 的做法）。
        返回临时 JPEG 路径；失败时返回原路径。
        """
        try:
            from PIL import Image as PILImage
        except ImportError:
            logger.warning("[image_bridge] 未安装 Pillow，gif 切帧不可用（pip install Pillow）")
            return file_path
        try:
            with PILImage.open(file_path) as img:
                img.seek(0)  # 第一帧
                frame = img.convert("RGB")
                tmp = Path(tempfile.gettempdir()) / f"image_bridge_frame_{uuid.uuid4().hex}.jpg"
                frame.save(tmp, "JPEG", quality=92)
                frame.close()
                logger.debug(f"[image_bridge] gif 已切帧: {file_path} -> {tmp}")
                return str(tmp)
        except Exception as e:
            logger.warning(f"[image_bridge] gif 切帧失败，使用原图: {e}")
            return file_path

    async def _recognize_image(self, file_path: str) -> str:
        """调用 OCR 接口识别单张图片，返回识别文字（gif 动图自动切帧为静态图）。"""
        api_url = self._cfg("ocr_api_url", DEFAULT_OCR_API_URL)
        api_key = self._cfg("ocr_api_key", DEFAULT_OCR_API_KEY)
        language = self._cfg("ocr_language", DEFAULT_OCR_LANGUAGE)
        timeout = int(self._cfg("ocr_timeout", DEFAULT_OCR_TIMEOUT) or DEFAULT_OCR_TIMEOUT)

        if not api_key:
            raise RuntimeError("未配置 OCR API Key（ocr_api_key）")

        data = Path(file_path).read_bytes()
        ext = Path(file_path).suffix.lower().lstrip(".")
        if ext not in ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"):
            ext = self._sniff_image_ext(data)
        # gif 动图（后缀或文件头识别）：切第一帧为 JPEG 再 OCR
        if ext == "gif" or self._sniff_image_ext(data) == "gif":
            file_path = self._gif_to_static_frame(file_path)
            data = Path(file_path).read_bytes()
            ext = "jpg"
        mime, _ = mimetypes.guess_type(f"image.{ext}") if ext else (None, None)
        mime = mime or "application/octet-stream"
        filename = f"image.{ext}" if ext else "image.bin"

        form = {
            "apikey": api_key,
            "language": language,
            "isOverlayRequired": "false",
            "scale": "true",
        }
        timeout_cfg = httpx.Timeout(timeout)
        async with httpx.AsyncClient(timeout=timeout_cfg) as client:
            files = {"file": (filename, data, mime)}
            resp = await client.post(api_url, data=form, files=files)
        resp.raise_for_status()
        result = resp.json()

        if result.get("IsErroredOnProcessing") or result.get("OCRExitCode") not in (1, 2):
            err = result.get("ErrorMessage") or result.get("ErrorDetails") or "未知错误"
            raise RuntimeError(f"OCR 接口返回错误: {err}")

        parsed = result.get("ParsedResults") or []
        if not parsed:
            raise RuntimeError("OCR 接口未返回识别结果")
        return (parsed[0].get("ParsedText") or "").strip()

    @staticmethod
    def _is_gif(images: list) -> bool:
        """判断图片组件列表是否均为 GIF 动图（按 url/file 后缀，或本地文件头）。"""
        for img in images:
            src = str(
                getattr(img, "url", "") or getattr(img, "file", "") or ""
            ).lower()
            if src.endswith(".gif"):
                continue
            return False
        return bool(images)

    async def _recognize_images(self, images: list) -> str:
        """逐张识别图片，拼接多图识别结果（gif 动图自动切帧后识别，让 AI 看懂表情包）。"""
        parts = []
        for i, img in enumerate(images, start=1):
            if self._is_gif([img]):
                logger.debug("[image_bridge] 动图将切帧后识别（表情包文字）")
            try:
                file_path = await img.convert_to_file_path()
            except Exception as e:
                logger.warning(f"图片转本地路径失败: {e}")
                raise RuntimeError(f"无法获取第 {i} 张图片的数据") from e
            text = await self._recognize_image(file_path)
            if len(images) > 1:
                parts.append(f"图片{i}:\n{text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _prune_pending(self) -> None:
        """清理过期项，并防止挂起字典无限增长。"""
        now = time.time()
        ttl = int(self._cfg("pending_ttl", DEFAULT_PENDING_TTL) or DEFAULT_PENDING_TTL)
        expired = [k for k, v in self._pending.items() if now - v["ts"] > ttl]
        for k in expired:
            self._pending.pop(k, None)
        if len(self._pending) > 500:  # 兜底：防止极端情况下内存膨胀
            self._pending.clear()

    # ---------------------------------------------------------------- 事件
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理图片/表情消息：识别并挂起内容；纯图片/表情消息不让 AI 回答（等待提问）。

        表情处理：QQ 官方把表情解析为 `[表情]`/`[表情:赞]` 文本 + 图片附件。
        - `[表情:赞]`（带名字）：文本本身有语义，AI 可直接理解 -> 永不拦截，直接回复；
        - `[表情]`（无名字，自定义表情包）：切帧 OCR 识别文字后，默认拦截等待提问，
          受 `emoji_wait_pending` 开关控制（关闭则直接回复）；
        - 表情包图片（多为 gif 动图）**切帧为静态图后进 OCR**，识别出表情包上的文字。
        """
        text = (event.message_str or "").strip()
        # 去掉表情标记后的"真实文字"：QQ 把表情转成 [表情]/[表情:赞] 文本，
        # 只有真实文字才算"文字提问"；纯表情标记视为"只发了表情"
        pure_text = EMOJI_RE.sub("", text).strip()
        key = self._pending_key(event)
        emoji_desc = self._extract_emoji_desc(text)
        images = self._extract_images(event, text)

        if not images and not emoji_desc:
            return  # 纯文字消息：由 on_llm_request 判断是否有挂起的图片内容

        # 组装挂起内容：表情语义（AI 可理解）+ OCR 识别文字
        content_parts: list[str] = []
        if emoji_desc:
            content_parts.append(emoji_desc)
        if images:
            try:
                ocr = await self._recognize_images(images)
                if ocr:
                    content_parts.append(ocr)
            except Exception as e:
                logger.error(f"图片识别失败: {e}")
                if not emoji_desc:
                    # 无表情兜底时，OCR 失败才打扰用户；有表情语义则保留表情部分继续
                    event.stop_event()
                    yield event.plain_result(
                        f"⚠️ 图片识别失败：{e}，请稍后重试或换一张更清晰的图片。"
                    )
                    return
                logger.warning(f"[image_bridge] 图片 OCR 失败，仅保留表情语义: {e}")

        content = "\n\n".join(p for p in content_parts if p)
        if not content:
            # 有图片/表情但 OCR 未识别到文字：不能放行，否则 AI 会直接回复。
            # 挂起一个占位标记并继续拦截等待提问（兼容 Elaina 表情包插件）。
            if not images and not emoji_desc:
                return
            content = "[用户发送了一张图片，但未识别到其中的文字内容]"

        self._pending[key] = {"text": content, "ts": time.time()}
        self._prune_pending()

        if pure_text:
            # 图片/表情 + 真实文字同时发送：不拦截，on_llm_request 会把内容注入本次提问
            logger.info(
                f"[image_bridge] 收到图片+文字消息 (session={event.unified_msg_origin})"
            )
            return

        # 表情放行规则（仅当无真实文字时判断）：
        # - [表情:xxx]（带名字）：文本本身有语义，AI 可直接理解 -> 永不拦截，直接回复
        # - [表情]（无名字，自定义表情包）：需 OCR 才能理解 -> 默认拦截等待提问，
        #   受 emoji_wait_pending 开关控制（关闭则放行，AI 直接回复）
        if emoji_desc:
            m = EMOJI_RE.search(text)
            named = bool(m and (m.group(1) or "").strip())
            if named:
                logger.debug("[image_bridge] 带名字表情 [表情:xxx]，AI 可直接理解，放行")
                return
            if not bool(self._cfg("emoji_wait_pending", DEFAULT_EMOJI_WAIT_PENDING)):
                logger.debug("[image_bridge] 表情包不参与等待（emoji_wait_pending=false），放行")
                return

        # 只发了图片/无名字表情包：静默挂起识别内容，拦截本次消息（AI 不回答），后台等待用户提问
        event.stop_event()

    @filter.command("picreset")
    async def picreset(self, event: AstrMessageEvent):
        """清除本会话挂起的图片识别内容。"""
        self._pending.pop(self._pending_key(event), None)
        yield event.plain_result("🗑 已清除挂起的图片识别内容，请重新发送图片。")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前：若本会话有挂起的图片识别内容，注入并消费（一次性）。"""
        key = self._pending_key(event)
        pending = self._pending.get(key)
        if not pending:
            return
        ttl = int(self._cfg("pending_ttl", DEFAULT_PENDING_TTL) or DEFAULT_PENDING_TTL)
        if time.time() - pending["ts"] > ttl:
            self._pending.pop(key, None)
            return

        template = self._cfg("prompt_template", DEFAULT_PROMPT_TEMPLATE)
        content = template.format(image_content=pending["text"])
        try:
            from astrbot.core.agent.message import TextPart  # v4.16+ 推荐方式

            part: object = TextPart(text=content).mark_as_temp()
        except Exception:
            # 兜底：旧版本以 dict 形式追加
            part = {"type": "text", "text": content}
        req.extra_user_content_parts.append(part)
        self._pending.pop(key, None)
        logger.info(
            f"[image_bridge] 已将图片识别内容注入 LLM 请求 (session={event.unified_msg_origin})"
        )

    async def terminate(self) -> None:
        """插件卸载/停用时清空挂起内容。"""
        self._pending.clear()

