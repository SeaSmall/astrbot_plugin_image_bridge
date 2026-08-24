"""
astrbot_plugin_image_bridge — 图片问答桥接插件（Image Bridge）

功能：
1. 用户发送图片时，插件调用免费 OCR 接口（OCR.space）识别图片中的文字。
2. 若用户只发了图片（未附带文字问题），AI 不会回答，插件会提示用户继续输入问题。
3. 用户随后发送文字问题时，插件把图片识别内容一并注入本次 LLM 请求，
   让 AI 结合图片内容回答（即"图片门控问答"：先发图，再提问，AI 才作答）。

适用于不支持多模态（图片直接进 LLM）的平台（如 QQ 个人号 aiocqhttp 等），
也适用于希望先把图片识别成文字、再交给 LLM 的场景。
"""

from __future__ import annotations

import mimetypes
import time
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
DEFAULT_PROMPT_TEMPLATE = (
    "<用户上传的图片识别内容>\n"
    "{image_content}\n"
    "</用户上传的图片识别内容>\n"
    "以上是用户刚上传图片的 OCR 识别文字，请结合该图片内容回答用户的问题。"
)
DEFAULT_WAIT_HINT = "✅ 已收到图片并完成识别，请继续输入你的问题～"
DEFAULT_SHOW_PREVIEW = True  # 收到图片的提示语中是否附带识别结果摘要


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
    def _extract_images(event: AstrMessageEvent) -> list:
        """从消息链中提取所有 Image 组件。"""
        components = getattr(event.message_obj, "message", None) or []
        images = []
        for comp in components:
            ctype = getattr(comp, "type", None)
            if ctype == "image" or type(comp).__name__ == "Image":
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

    async def _recognize_image(self, file_path: str) -> str:
        """调用 OCR 接口识别单张图片，返回识别文字。"""
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

    async def _recognize_images(self, images: list) -> str:
        """逐张识别图片，拼接多图识别结果。"""
        parts = []
        for i, img in enumerate(images, start=1):
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
        """处理图片消息：识别并挂起识别内容；纯图片消息不让 AI 回答。"""
        images = self._extract_images(event)
        text = (event.message_str or "").strip()
        key = self._pending_key(event)
        if not images:
            return  # 纯文字消息：由 on_llm_request 判断是否有挂起的图片内容

        try:
            content = await self._recognize_images(images)
        except Exception as e:
            logger.error(f"图片识别失败: {e}")
            event.stop_event()
            yield event.plain_result(f"⚠️ 图片识别失败：{e}，请稍后重试或换一张更清晰的图片。")
            return

        if not content:
            event.stop_event()
            yield event.plain_result("⚠️ 未能从图片中识别出文字内容，请换一张更清晰的图片后重试。")
            return

        self._pending[key] = {"text": content, "ts": time.time()}
        self._prune_pending()

        if text:
            # 图片 + 文字同时发送：不拦截，on_llm_request 会把识别内容注入本次提问
            logger.info(
                f"[image_bridge] 收到图片+文字消息 (session={event.unified_msg_origin})"
            )
            return

        # 只发了图片：拦截本次消息（AI 不回答），提示用户继续输入问题
        event.stop_event()
        hint = self._cfg("wait_hint", DEFAULT_WAIT_HINT)
        if self._cfg("show_ocr_preview", DEFAULT_SHOW_PREVIEW):
            preview = content if len(content) <= 80 else content[:77] + "..."
            hint = f"{hint}\n（识别摘要：{preview}）"
        yield event.plain_result(hint)

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

