import base64
import random
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ParamsCollector:
    """参数收集类"""

    def __init__(self, config: AstrBotConfig):
        self.conf = config
        self.session = aiohttp.ClientSession()

    async def _download_image(self, url: str, prefer_http: bool = True) -> bytes | None:
        """下载图片"""
        candidates = [url]
        if prefer_http and url.startswith("https://"):
            candidates.insert(0, url.replace("https://", "http://", 1))

        last_error = None
        for target in candidates:
            try:
                async with self.session.get(target) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    last_error = f"HTTP {resp.status}"
            except Exception as e:
                last_error = e
        logger.error(f"图片下载失败: {last_error}")
        return None

    async def get_avatar(self, user_id: str) -> bytes | None:
        """根据 QQ 号下载头像"""
        if not user_id.isdigit():
            user_id = f"{random.randint(10_000_000, 999_999_999)}"
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        return await self._download_image(avatar_url)

    async def _decode_image(self, src: str) -> bytes | None:
        """统一把 src 转成 bytes"""
        raw: bytes | None = None
        if Path(src).is_file():
            raw = Path(src).read_bytes()
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            return base64.b64decode(src[9:])
        return raw if isinstance(raw, bytes) else None

    async def get_extra(self, event: AstrMessageEvent, target_id: str):
        """从消息平台获取昵称/性别"""
        if event.get_platform_name() == "aiocqhttp":
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            assert isinstance(event, AiocqhttpMessageEvent)
            user_info = await event.bot.get_stranger_info(user_id=int(target_id))
            raw_nickname = user_info.get("nickname")
            nickname = str(raw_nickname if raw_nickname is not None else "Unknown")
            sex = user_info.get("sex")
            return nickname, sex
        return None

    async def _append_id_info(
        self,
        event: AstrMessageEvent,
        target_id: str,
        images: list,
        options: dict,
    ):
        """补齐昵称、性别、头像信息"""
        if result := await self.get_extra(event, target_id):
            nickname, sex = result
            options["name"], options["gender"] = nickname, sex
            if avatar := await self.get_avatar(target_id):
                images.append((nickname, avatar))
        else:
            # 非 QQ 平台时仍尝试补头像
            if avatar := await self.get_avatar(target_id):
                images.append((f"User{target_id}", avatar))

    async def collect_params(
        self,
        event: AstrMessageEvent,
        params,
        force_sender_as_target: bool = False,
        protected_user_id: str | None = None,
    ):
        """收集参数，返回 (images, texts, options)"""
        images: list[tuple[str, bytes]] = []
        texts: list[str] = []
        options: dict[str, bool | str | int | float] = {}

        chain = event.get_messages()
        send_id: str = event.get_sender_id()
        self_id: str = event.get_self_id()
        sender_name: str = str(event.get_sender_name())

        async def _process_segment(seg, name: str):
            if isinstance(seg, Image):
                if src := seg.url or seg.file:
                    if image := await self._decode_image(src):
                        images.append((name, image))
            elif isinstance(seg, At) and seg != chain[0]:
                if not force_sender_as_target:
                    await self._append_id_info(event, str(seg.qq), images, options)
            elif isinstance(seg, Plain):
                plains: list[str] = seg.text.strip().split(" ")
                if len(plains) > 1:
                    for text in plains[1:]:
                        if "=" in text:
                            k, v = text.split("=", 1)
                            options[k] = v
                        elif text.startswith("@"):
                            if not force_sender_as_target:
                                target_id = text[1:]
                                if target_id.isdigit():
                                    await self._append_id_info(
                                        event, target_id, images, options
                                    )
                        elif text:
                            texts.append(text)

        if not force_sender_as_target:
            reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
            if reply_seg and reply_seg.chain:
                name = str(reply_seg.sender_nickname or reply_seg.sender_id)
                for seg in reply_seg.chain:
                    await _process_segment(seg, name)

        for seg in chain:
            await _process_segment(seg, sender_name)

        if len(images) < params.min_images:
            if force_sender_as_target and protected_user_id:
                if protected_result := await self.get_extra(event, protected_user_id):
                    protected_name, _ = protected_result
                else:
                    protected_name = f"User{protected_user_id}"
                if protected_avatar := await self.get_avatar(protected_user_id):
                    images.insert(0, (protected_name, protected_avatar))
            else:
                if sender_avatar := await self.get_avatar(send_id):
                    images.insert(0, (sender_name, sender_avatar))

        if len(images) < params.min_images:
            if force_sender_as_target:
                if sender_avatar := await self.get_avatar(send_id):
                    images.append((sender_name, sender_avatar))
            else:
                if bot_avatar := await self.get_avatar(self_id):
                    images.insert(0, ("bot", bot_avatar))
        images = images[: params.max_images]

        if len(texts) < params.min_texts and params.default_texts:
            texts.extend(params.default_texts)
        texts = texts[: params.max_texts]

        return images, texts, options

    async def close(self):
        if hasattr(self, "session") and not self.session.closed:
            await self.session.close()
