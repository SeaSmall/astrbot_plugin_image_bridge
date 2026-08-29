"""
astrbot_plugin_image_bridge — 图片问答桥接插件（Image Bridge）

功能：
1. 用户发送图片时，插件按优先级依次调用识别服务（配置了对应 key 才启用，异常自动降级）：
   小米 MiMo Token Plan 识图模型 → 百度智能云图像识别 → OCR.space 免费 OCR。
2. 若用户只发了图片（未附带文字问题），AI 不会回答，插件静默挂起识别内容，后台等待用户输入问题。
3. 用户随后发送文字问题时，插件把图片识别内容一并注入本次 LLM 请求，
   让 AI 结合图片内容回答（即"图片门控问答"：先发图，再提问，AI 才作答）。

适用于不支持多模态（图片直接进 LLM）的平台（如 QQ 个人号 aiocqhttp 等），
也适用于希望先把图片识别成文字、再交给 LLM 的场景。
"""

from __future__ import annotations

import base64
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
DEFAULT_OCR_TIMEOUT = 60  # 识别请求超时（秒）
DEFAULT_PENDING_TTL = 1800  # 图片识别内容有效期（秒），超时后需重新发送图片
DEFAULT_EMOJI_WAIT_PENDING = True  # 表情是否也参与「发送后等待提问」门控
# 小米 MiMo Token Plan（OpenAI 兼容；Key 格式 tp-xxxxx）
DEFAULT_XIAOMI_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
DEFAULT_XIAOMI_MODEL = "mimo-v2.5"  # 支持图片理解；也可用 mimo-v2.5-pro
# 百度智能云图像识别（通用物体和场景识别 advanced_general）
DEFAULT_BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
DEFAULT_BAIDU_API_URL = "https://aip.baidubce.com/rest/2.0/image-classify/v2/advanced_general"
DEFAULT_PROMPT_TEMPLATE = (
    "<用户上传的图片识别内容>\n"
    "{image_content}\n"
    "</用户上传的图片识别内容>\n"
    "以上是用户刚上传图片的识别结果（文字 OCR / 图像识别描述），请结合该图片内容回答用户的问题。"
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
        # 百度 access_token 缓存：{"token": str, "exp": float}
        self._baidu_token: dict | None = None

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

    def _gate_event(self, event: AstrMessageEvent) -> None:
        """静默拦截消息：禁止默认 LLM 请求并停止事件传播（双重保险）。

        - `stop_event()`：停止事件继续传播；
        - `should_call_llm(True)`：AstrBot 官方「禁止默认 LLM 请求」接口，
          在事件管道中显式跳过 LLM 调用，避免不同版本管道行为差异导致
          纯图片消息仍被 LLM 处理（AI 直接回复）。
        """
        event.stop_event()
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            try:
                should_call_llm(True)
            except Exception as e:
                logger.debug(f"[image_bridge] should_call_llm 调用失败: {e}")
        logger.info(
            f"[image_bridge] 已拦截消息等待提问 (session={event.unified_msg_origin})"
        )

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
        """按优先级链识别单张图片：小米 MiMo → 百度图像识别 → OCR.space。

        - 配置了对应 key 的服务才启用：小米填了 xiaomi_api_key 才启用，
          百度填了 baidu_api_key + baidu_secret_key 才启用；OCR.space 兜底。
        - 某服务返回异常（抛错）自动降级到下一个；全部失败才报错。
        - gif 动图自动切帧为静态图后再识别（让 AI 看懂表情包）。
        """
        data = Path(file_path).read_bytes()
        ext = Path(file_path).suffix.lower().lstrip(".")
        if ext not in ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"):
            ext = self._sniff_image_ext(data)
        # gif 动图：切第一帧为 JPEG 静态图，所有识别服务都能处理
        if ext == "gif" or self._sniff_image_ext(data) == "gif":
            jpg_path = self._gif_to_static_frame(file_path)
            if jpg_path != file_path:
                data = Path(jpg_path).read_bytes()
                ext = "jpg"
        mime, _ = mimetypes.guess_type(f"image.{ext}") if ext else (None, None)
        mime = mime or "application/octet-stream"

        # 三级优先链（小米 → 百度 → OCR.space）
        providers: list[tuple[str, object]] = []
        if self._cfg("xiaomi_api_key", ""):
            providers.append(("小米 MiMo", self._recognize_xiaomi))
        if self._cfg("baidu_api_key", "") and self._cfg("baidu_secret_key", ""):
            providers.append(("百度图像识别", self._recognize_baidu))
        providers.append(("OCR.space", self._recognize_ocr))

        errors: list[str] = []
        for name, fn in providers:
            try:
                text = (await fn(data, ext, mime)).strip()
                if text:
                    logger.debug(f"[image_bridge] 使用 {name} 识别成功")
                    return text
            except Exception as e:
                errors.append(f"{name}: {e}")
                logger.warning(f"[image_bridge] {name} 识别失败，尝试下一服务: {e}")
        if errors:
            raise RuntimeError("；".join(errors))
        raise RuntimeError("未配置任何可用的识别服务")

    async def _recognize_xiaomi(self, data: bytes, ext: str, mime: str) -> str:
        """小米 MiMo Token Plan 识图模型（OpenAI 兼容，图片以 base64 data URI 传入）。"""
        api_key = str(self._cfg("xiaomi_api_key", "") or "").strip()
        base_url = str(
            self._cfg("xiaomi_base_url", DEFAULT_XIAOMI_BASE_URL) or DEFAULT_XIAOMI_BASE_URL
        ).rstrip("/")
        model = str(self._cfg("xiaomi_model", DEFAULT_XIAOMI_MODEL) or DEFAULT_XIAOMI_MODEL).strip()
        timeout = int(self._cfg("ocr_timeout", DEFAULT_OCR_TIMEOUT) or DEFAULT_OCR_TIMEOUT)
        if not api_key:
            raise RuntimeError("未配置小米 API Key（xiaomi_api_key）")

        # 未知 mime 时按 JPEG 处理，保证 data URI 合法
        if not str(mime).startswith("image/"):
            mime = "image/jpeg"

        b64 = base64.b64encode(data).decode("ascii")
        prompt = (
            "请识别并描述这张图片，用中文简洁回答：\n"
            "1) 图片中的文字内容（如有，请原样输出）；\n"
            "2) 图片的主要内容、场景或物体。"
        )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_completion_tokens": 1024,
        }
        headers = {
            "api-key": api_key,  # 官方文档 curl 示例使用的鉴权头
            "Content-Type": "application/json",
        }
        url = f"{base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"小米接口 HTTP {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        try:
            content = (
                ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
        except Exception:
            raise RuntimeError(f"小米接口返回异常: {str(result)[:200]}") from None
        if not content.strip():
            raise RuntimeError("小米接口未返回识别内容")
        return content.strip()

    async def _baidu_access_token(self) -> str:
        """获取（并缓存）百度 access_token，有效期 30 天，提前 10 分钟自动刷新。"""
        ak = str(self._cfg("baidu_api_key", "") or "").strip()
        sk = str(self._cfg("baidu_secret_key", "") or "").strip()
        timeout = int(self._cfg("ocr_timeout", DEFAULT_OCR_TIMEOUT) or DEFAULT_OCR_TIMEOUT)
        if not ak or not sk:
            raise RuntimeError("未配置百度 API Key/Secret Key")
        cached = self._baidu_token
        if cached and cached["exp"] > time.time() + 600:
            return cached["token"]
        params = {"grant_type": "client_credentials", "client_id": ak, "client_secret": sk}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(DEFAULT_BAIDU_TOKEN_URL, params=params)
        resp.raise_for_status()
        result = resp.json()
        token = result.get("access_token")
        if not token:
            raise RuntimeError(
                f"百度获取 access_token 失败: "
                f"{result.get('error_description') or result.get('error_msg') or result}"
            )
        expires_in = int(result.get("expires_in") or 2592000)
        self._baidu_token = {"token": token, "exp": time.time() + expires_in}
        return token

    async def _recognize_baidu(self, data: bytes, ext: str, mime: str) -> str:
        """百度智能云图像识别（通用物体和场景识别 advanced_general）。"""
        token = await self._baidu_access_token()
        timeout = int(self._cfg("ocr_timeout", DEFAULT_OCR_TIMEOUT) or DEFAULT_OCR_TIMEOUT)
        b64 = base64.b64encode(data).decode("ascii")
        params = {"access_token": token}
        form = {"image": b64}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(DEFAULT_BAIDU_API_URL, params=params, data=form)
        resp.raise_for_status()
        result = resp.json()
        if result.get("error_code"):
            raise RuntimeError(
                f"百度接口错误 {result.get('error_code')}: {result.get('error_msg')}"
            )
        items = result.get("result") or []
        parts = []
        for it in items:
            keyword = str(it.get("keyword") or "").strip()
            if not keyword:
                continue
            score = int(float(it.get("score") or 0) * 100)
            root = str(it.get("root") or "").strip()
            line = f"{keyword}（{score}%）" + (f"[{root}]" if root else "")
            parts.append(line)
        if not parts:
            raise RuntimeError("百度接口未返回有效识别结果")
        return "百度图像识别：" + "、".join(parts)

    async def _recognize_ocr(self, data: bytes, ext: str, mime: str) -> str:
        """OCR.space 免费 OCR 接口（三级链的兜底服务）。"""
        api_url = self._cfg("ocr_api_url", DEFAULT_OCR_API_URL)
        api_key = self._cfg("ocr_api_key", DEFAULT_OCR_API_KEY)
        language = self._cfg("ocr_language", DEFAULT_OCR_LANGUAGE)
        timeout = int(self._cfg("ocr_timeout", DEFAULT_OCR_TIMEOUT) or DEFAULT_OCR_TIMEOUT)
        if not api_key:
            raise RuntimeError("未配置 OCR API Key（ocr_api_key）")

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
                # 识别失败也不打扰用户：挂起占位内容，等待用户提问时让 AI 知道图片没读到
                content_parts.append("[图片识别失败，未能获取图片内容]")

        content = "\n\n".join(p for p in content_parts if p)
        if not content:
            # 有图片/表情但 OCR 未识别到文字：不能放行，否则 AI 会直接回复。
            # 挂起一个占位标记并继续拦截等待提问，保证"发图→等待提问→回答"流程完整。
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
        self._gate_event(event)

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

