import base64
import random
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

# AstrBot ?? QQ ??????
QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
# ?? self_id / ?? id????????????
_INVALID_AVATAR_IDS = {
    "",
    "all",
    "qq_official",
    "unknown_selfid",
    "unknown",
    "none",
    "null",
}


class ParamsCollector:
    """?????"""

    def __init__(self, config: AstrBotConfig):
        self.conf = config
        self.session = aiohttp.ClientSession()

    async def _download_image(self, url: str, prefer_http: bool = True) -> bytes | None:
        """????"""
        if not url:
            return None
        candidates = [url]
        if prefer_http and url.startswith("https://"):
            candidates.insert(0, url.replace("https://", "http://", 1))
        elif (not prefer_http) and url.startswith("http://"):
            candidates.insert(0, url.replace("http://", "https://", 1))

        last_error = None
        for target in candidates:
            try:
                async with self.session.get(target) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data:
                            return data
                        last_error = "empty body"
                    else:
                        last_error = f"HTTP {resp.status}"
            except Exception as e:
                last_error = e
        logger.error(f"??????: {last_error} | url={url}")
        return None

    @staticmethod
    def _platform_name(event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        try:
            return str(event.get_platform_name() or "").strip().lower()
        except Exception:
            return ""

    def _avatar_fetch_mode(self) -> str:
        """avatar_fetch_mode: auto / onebot / qq_official."""
        raw = str(self.conf.get("avatar_fetch_mode") or "auto").strip().lower()
        aliases = {
            "auto": "auto",
            "automatic": "auto",
            "detect": "auto",
            "onebot": "onebot",
            "napcat": "onebot",
            "llonebot": "onebot",
            "qq": "onebot",
            "qq_number": "onebot",
            "number": "onebot",
            "uin": "onebot",
            "qq_official": "qq_official",
            "qqofficial": "qq_official",
            "official": "qq_official",
            "openid": "qq_official",
            "qqapp": "qq_official",
        }
        return aliases.get(raw, "auto")

    def _is_qq_official_platform(self, event: AstrMessageEvent | None) -> bool:
        return self._platform_name(event) in QQ_OFFICIAL_PLATFORMS

    def _is_qq_official(self, event: AstrMessageEvent | None) -> bool:
        """Compatibility helper: platform is QQ official adapter."""
        return self._is_qq_official_platform(event)

    def _use_qq_official_avatar(
        self,
        event: AstrMessageEvent | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Whether to prefer QQ official openid/qqapp avatar strategy."""
        mode = self._avatar_fetch_mode()
        if mode == "qq_official":
            return True
        if mode == "onebot":
            return False
        # auto: platform first, then non-digit id + resolvable appid
        if self._is_qq_official_platform(event):
            return True
        uid = str(user_id or "").strip()
        if uid and (not uid.isdigit()) and self._resolve_qq_appid(event):
            return True
        return False


    @staticmethod
    def _looks_like_openid(value: str) -> bool:
        """??????? QQ ?? openid?????????"""
        text = str(value or "").strip()
        if not text or text.isdigit() or text.startswith("/"):
            return False
        if len(text) < 8 or len(text) > 128:
            return False
        # ?? openid????? _ - 
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        return all(ch in allowed for ch in text)

    @staticmethod
    def _looks_like_url(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith(("http://", "https://", "file://", "base64://")):
            return True
        try:
            parsed = urlparse(text)
            return bool(parsed.scheme and parsed.netloc)
        except Exception:
            return False

    @classmethod
    def _pick_avatar_url(cls, obj: Any) -> str | None:
        """? dict / ????????? URL?"""
        if obj is None:
            return None
        if isinstance(obj, str):
            return obj.strip() if cls._looks_like_url(obj) else None

        candidates: list[Any] = []
        if isinstance(obj, dict):
            for key in (
                "avatar",
                "avatarUrl",
                "avatar_url",
                "user_avatar",
                "headimgurl",
                "head_img",
                "icon",
                "picture",
                "image",
            ):
                if key in obj and obj.get(key):
                    candidates.append(obj.get(key))
            # OneBot ????
            for key in ("user_id", "userId", "id", "uin"):
                _ = obj.get(key)
        else:
            for key in (
                "avatar",
                "avatarUrl",
                "avatar_url",
                "user_avatar",
                "headimgurl",
                "icon",
            ):
                val = getattr(obj, key, None)
                if val:
                    candidates.append(val)
            # nested user
            for key in ("user", "member", "author", "data"):
                nested = getattr(obj, key, None)
                if nested is not None and nested is not obj:
                    nested_url = cls._pick_avatar_url(nested)
                    if nested_url:
                        return nested_url

        for item in candidates:
            if isinstance(item, dict):
                nested = cls._pick_avatar_url(item)
                if nested:
                    return nested
                continue
            text = str(item or "").strip()
            if cls._looks_like_url(text):
                return text
        return None

    @staticmethod
    def _object_user_id(obj: Any) -> str:
        if obj is None:
            return ""
        if isinstance(obj, dict):
            for key in (
                "id",
                "user_id",
                "userId",
                "member_openid",
                "user_openid",
                "openid",
                "uin",
                "qq",
            ):
                val = obj.get(key)
                if val is not None and str(val).strip():
                    return str(val).strip()
            return ""
        for key in (
            "id",
            "user_id",
            "userId",
            "member_openid",
            "user_openid",
            "openid",
            "uin",
            "qq",
        ):
            val = getattr(obj, key, None)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    def _resolve_qq_appid(self, event: AstrMessageEvent | None = None) -> str:
        """?? QQ ????? appid?bot.config / platform / ??????"""
        conf_appid = str(self.conf.get("qq_official_appid") or "").strip()
        if conf_appid:
            return conf_appid

        if event is None:
            return ""

        bot = getattr(event, "bot", None)
        if bot is not None:
            # botpy Client.http._token.app_id
            http = getattr(bot, "http", None)
            token = getattr(http, "_token", None) if http is not None else None
            app_id = getattr(token, "app_id", None) if token is not None else None
            if app_id:
                return str(app_id).strip()

            # QQOfficial botClient.platform.appid / config
            platform = getattr(bot, "platform", None)
            if platform is not None:
                appid = getattr(platform, "appid", None)
                if appid:
                    return str(appid).strip()
                cfg = getattr(platform, "config", None) or {}
                if isinstance(cfg, dict) and cfg.get("appid"):
                    return str(cfg.get("appid")).strip()

            # ???bot.config.id / appid?Satori / adapter-qq ???
            cfg = getattr(bot, "config", None)
            if isinstance(cfg, dict):
                for key in ("appid", "app_id", "id"):
                    if cfg.get(key):
                        # id ??????? id????? qq appid??????????
                        if key == "id" and not self._is_qq_official(event):
                            continue
                        return str(cfg.get(key)).strip()
            elif cfg is not None:
                for key in ("appid", "app_id", "id"):
                    val = getattr(cfg, key, None)
                    if val:
                        if key == "id" and not self._is_qq_official(event):
                            continue
                        return str(val).strip()

        return ""

    def _extract_event_avatar_url(
        self, event: AstrMessageEvent, user_id: str
    ) -> str | None:
        """??????? payload / mentions ????????? URL?"""
        uid = str(user_id or "").strip()
        if not uid:
            return None

        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)

        # ?????
        try:
            if str(event.get_sender_id()) == uid:
                # Satori / ??????event.user.avatar / message.user
                for obj in (
                    getattr(event, "user", None),
                    getattr(message_obj, "user", None),
                    getattr(message_obj, "sender", None),
                ):
                    url = self._pick_avatar_url(obj)
                    if url:
                        return url
        except Exception:
            pass

        if raw is None:
            return None

        # author
        author = getattr(raw, "author", None)
        if author is not None:
            aid = self._object_user_id(author)
            if not aid and isinstance(getattr(raw, "raw_data", None), dict):
                aid = self._object_user_id((raw.raw_data or {}).get("author"))
            if aid == uid or (not aid and str(event.get_sender_id()) == uid):
                url = self._pick_avatar_url(author)
                if url:
                    return url
                # raw_data.author.avatar
                raw_data = getattr(raw, "raw_data", None)
                if isinstance(raw_data, dict):
                    url = self._pick_avatar_url(raw_data.get("author"))
                    if url:
                        return url

        # mentions ??
        mentions = getattr(raw, "mentions", None) or []
        if isinstance(mentions, (list, tuple)):
            for mention in mentions:
                if self._object_user_id(mention) == uid:
                    url = self._pick_avatar_url(mention)
                    if url:
                        return url

        # dict ?? raw
        if isinstance(raw, dict):
            author = raw.get("author") or raw.get("sender") or raw.get("user")
            if self._object_user_id(author) == uid or str(event.get_sender_id()) == uid:
                url = self._pick_avatar_url(author)
                if url:
                    return url
            for mention in raw.get("mentions") or []:
                if self._object_user_id(mention) == uid:
                    url = self._pick_avatar_url(mention)
                    if url:
                        return url

        return None

    async def _avatar_from_bot_api(
        self, event: AstrMessageEvent, user_id: str
    ) -> bytes | None:
        """?? bot.getUser / get_user / get_stranger_info ??????"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return None

        uid = str(user_id).strip()
        call_plans: list[tuple[str, tuple, dict]] = []

        # Satori / adapter-qq: getUser(userId) / get_user(user_id)
        for name in ("getUser", "get_user", "user", "get_friend_info"):
            fn = getattr(bot, name, None)
            if not callable(fn):
                continue
            call_plans.append((name, (uid,), {}))
            call_plans.append((name, (), {"userId": uid}))
            call_plans.append((name, (), {"user_id": uid}))
            call_plans.append((name, (), {"id": uid}))

        # OneBot: get_stranger_info
        if uid.isdigit():
            fn = getattr(bot, "get_stranger_info", None)
            if callable(fn):
                call_plans.append(("get_stranger_info", (), {"user_id": int(uid)}))
                call_plans.append(("get_stranger_info", (), {"user_id": uid}))

        seen: set[tuple[str, tuple, tuple]] = set()
        for name, args, kwargs in call_plans:
            key = (name, args, tuple(sorted((k, str(v)) for k, v in kwargs.items())))
            if key in seen:
                continue
            seen.add(key)
            fn = getattr(bot, name, None)
            if not callable(fn):
                continue
            try:
                result = fn(*args, **kwargs)
                if hasattr(result, "__await__"):
                    result = await result
            except TypeError:
                continue
            except Exception as exc:
                logger.debug("bot.%s ??????: %s", name, exc)
                continue

            url = self._pick_avatar_url(result)
            if not url and isinstance(result, dict):
                # OneBot sometimes nests under data
                url = self._pick_avatar_url(result.get("data") or result.get("user"))
            if url:
                data = await self._download_image(url)
                if data:
                    return data
        return None

    def _build_avatar_urls(
        self, user_id: str, event: AstrMessageEvent | None = None
    ) -> list[str]:
        """按优先级构造候选头像 URL 列表。"""
        uid = str(user_id or "").strip()
        mode = self._avatar_fetch_mode()
        urls: list[str] = []

        if event is not None:
            event_url = self._extract_event_avatar_url(event, uid)
            if event_url:
                urls.append(event_url)

        use_official = self._use_qq_official_avatar(event, uid)
        use_onebot = mode == "onebot" or (mode == "auto" and not use_official) or uid.isdigit()

        # QQ 官方机器人：openid 头像
        # http://q.qlogo.cn/qqapp/${appid}/${openid}/640
        if use_official:
            appid = self._resolve_qq_appid(event)
            if appid and uid:
                urls.append(f"http://q.qlogo.cn/qqapp/{appid}/{uid}/640")
                urls.append(f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640")
            elif mode == "qq_official":
                logger.warning(
                    "avatar_fetch_mode=qq_official but appid missing; user_id=%s",
                    uid,
                )

        # OneBot / 普通 QQ 号
        # 强制 onebot 时：即使不是数字也只走数字链路（由上层兜底）
        if use_onebot and uid.isdigit():
            urls.extend(
                [
                    f"https://q4.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                    f"http://q4.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                    f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640",
                    f"http://q1.qlogo.cn/g?b=qq&nk={uid}&s=640",
                ]
            )

        # 去重保序
        out: list[str] = []
        seen: set[str] = set()
        for url in urls:
            u = str(url or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    async def get_avatar(
        self,
        user_id: str,
        event: AstrMessageEvent | None = None,
    ) -> bytes | None:
        """下载用户头像。

        模式（配置 avatar_fetch_mode）：
        - auto: 按平台/ID 自动选择
        - onebot: 强制数字 QQ 号 qlogo
        - qq_official: 强制 qqapp/{appid}/{openid}

        另外仍会尝试事件自带 avatar、bot.getUser / get_stranger_info 等。
        """
        uid = str(user_id or "").strip()
        if not uid or uid.lower() in _INVALID_AVATAR_IDS:
            return None

        mode = self._avatar_fetch_mode()

        # 1) 事件内现成 avatar / 平台构造 URL
        for url in self._build_avatar_urls(uid, event):
            data = await self._download_image(url)
            if data:
                return data

        # 2) bot API 查询（指定其他用户时）
        # onebot 强制模式下非数字 ID 跳过 stranger 接口无意义调用
        if event is not None and not (mode == "onebot" and not uid.isdigit()):
            data = await self._avatar_from_bot_api(event, uid)
            if data:
                return data

        # 3) 兜底策略
        # - onebot 模式：保持旧行为，非数字时用随机头像，避免生成直接失败
        # - qq_official / auto+openid：不静默换成路人，避免“头像张冠李戴”
        if mode == "onebot" and not uid.isdigit():
            logger.warning(
                "avatar_fetch_mode=onebot but user_id is not QQ number, fallback random avatar: %s",
                uid,
            )
            fallback_id = f"{random.randint(10_000_000, 999_999_999)}"
            return await self._download_image(
                f"https://q4.qlogo.cn/headimg_dl?dst_uin={fallback_id}&spec=640"
            )

        if (
            mode == "auto"
            and not uid.isdigit()
            and not self._use_qq_official_avatar(event, uid)
            and not self._resolve_qq_appid(event)
        ):
            logger.warning(
                "无法解析头像且无 QQ appid，使用随机头像兜底: user_id=%s platform=%s mode=%s",
                uid,
                self._platform_name(event) or "unknown",
                mode,
            )
            fallback_id = f"{random.randint(10_000_000, 999_999_999)}"
            return await self._download_image(
                f"https://q4.qlogo.cn/headimg_dl?dst_uin={fallback_id}&spec=640"
            )

        logger.warning(
            "获取用户头像失败: user_id=%s platform=%s mode=%s appid=%s",
            uid,
            self._platform_name(event) or "unknown",
            mode,
            self._resolve_qq_appid(event) or "-",
        )
        return None

    async def _decode_image(self, src: str) -> bytes | None:
        """??? src ?? bytes"""
        raw: bytes | None = None
        if Path(src).is_file():
            raw = Path(src).read_bytes()
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            return base64.b64decode(src[9:])
        return raw if isinstance(raw, bytes) else None

    async def get_extra(self, event: AstrMessageEvent, target_id: str):
        """?????????/??"""
        if event.get_platform_name() == "aiocqhttp":
            if not str(target_id).isdigit():
                return None
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
        seen_ids: set[str] | None = None,
    ):
        """????????????"""
        target_id = str(target_id or "").strip()
        if not target_id or target_id.lower() in _INVALID_AVATAR_IDS:
            return
        if seen_ids is not None:
            if target_id in seen_ids:
                return
            seen_ids.add(target_id)

        if result := await self.get_extra(event, target_id):
            nickname, sex = result
            options["name"], options["gender"] = nickname, sex
            if avatar := await self.get_avatar(target_id, event):
                images.append((nickname, avatar))
        else:
            # ? aiocqhttp / QQ ?? openid???????
            display = f"User{target_id[-6:]}" if len(target_id) > 6 else f"User{target_id}"
            # ?????????? @ ?????
            try:
                if str(event.get_sender_id()) == target_id:
                    display = str(event.get_sender_name() or display)
            except Exception:
                pass
            if avatar := await self.get_avatar(target_id, event):
                images.append((display, avatar))
                options.setdefault("name", display)

    async def collect_params(
        self,
        event: AstrMessageEvent,
        params,
        force_sender_as_target: bool = False,
        protected_user_id: str | None = None,
        trigger_keyword: str | None = None,
    ):
        """??????? (images, texts, options)

        force_sender_as_target=True ???????
        - ????? @/??
        - ???????????????????
        - ??????? 1 ?=??????????? 2 ?=?????????
        """
        images: list[tuple[str, bytes]] = []
        texts: list[str] = []
        options: dict[str, bool | str | int | float] = {}
        seen_avatar_ids: set[str] = set()

        chain = event.get_messages()
        send_id: str = str(event.get_sender_id())
        self_id: str = str(event.get_self_id())
        sender_name: str = str(event.get_sender_name())

        async def _process_segment(seg, name: str, allow_target_avatar: bool = True):
            if isinstance(seg, Image):
                if src := seg.url or seg.file:
                    if image := await self._decode_image(src):
                        images.append((name, image))
            elif isinstance(seg, At):
                # ?????????? @bot?????
                if chain and seg is chain[0]:
                    return
                at_id = str(getattr(seg, "qq", "") or "").strip()
                if at_id.lower() in _INVALID_AVATAR_IDS or at_id == self_id:
                    return
                if allow_target_avatar and not force_sender_as_target:
                    await self._append_id_info(event, at_id, images, options, seen_avatar_ids)
            elif isinstance(seg, Plain):
                plain_text = str(getattr(seg, "text", "") or "")
                # QQ official: mentions stay as <@OPENID> inside Plain text
                if allow_target_avatar and not force_sender_as_target:
                    from ..utils import extract_mention_ids

                    for mid in extract_mention_ids(plain_text):
                        if mid == self_id or mid.lower() in _INVALID_AVATAR_IDS:
                            continue
                        await self._append_id_info(event, mid, images, options, seen_avatar_ids)

                from ..utils import strip_mention_tags

                cleaned = strip_mention_tags(plain_text)
                plains: list[str] = cleaned.split() if cleaned else []
                for text in plains:
                    # OneBot @123456 / text @openid
                    if text.startswith("@"):
                        target_id = text[1:].strip()
                        if (
                            target_id
                            and allow_target_avatar
                            and not force_sender_as_target
                            and target_id.lower() not in _INVALID_AVATAR_IDS
                        ):
                            if target_id.isdigit() or (
                                self._use_qq_official_avatar(event, target_id)
                                and self._looks_like_openid(target_id)
                            ):
                                await self._append_id_info(event, target_id, images, options, seen_avatar_ids)
                        continue
                    if text:
                        texts.append(text)

        # ????????????/????????????
        if not force_sender_as_target:
            reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
            if reply_seg and reply_seg.chain:
                name = str(reply_seg.sender_nickname or reply_seg.sender_id)
                for seg in reply_seg.chain:
                    await _process_segment(seg, name, allow_target_avatar=True)
                # ???????????? openid / OneBot QQ ?
                reply_uid = str(
                    getattr(reply_seg, "sender_id", None)
                    or getattr(reply_seg, "qq", None)
                    or ""
                ).strip()
                if reply_uid and reply_uid.lower() not in _INVALID_AVATAR_IDS:
                    # ?????????????????
                    if not any(isinstance(s, Image) for s in (reply_seg.chain or [])):
                        await self._append_id_info(event, reply_uid, images, options, seen_avatar_ids)

        # Harvest QQ official mentions from raw message / message_str once.
        # Adapter often keeps user mentions as <@openid> text instead of At segments.
        if not force_sender_as_target:
            from ..utils import extract_mention_ids

            mention_ids: list[str] = []
            try:
                mention_ids.extend(extract_mention_ids(event.get_message_str()))
            except Exception:
                pass
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            mentions = getattr(raw, "mentions", None) if raw is not None else None
            if isinstance(mentions, (list, tuple)):
                for mention in mentions:
                    if getattr(mention, "is_you", False) is True:
                        continue
                    mid = self._object_user_id(mention)
                    if mid:
                        mention_ids.append(mid)
            # dedupe preserve order
            seen_m: set[str] = set()
            for mid in mention_ids:
                mid = str(mid or "").strip()
                if (
                    not mid
                    or mid in seen_m
                    or mid == self_id
                    or mid.lower() in _INVALID_AVATAR_IDS
                ):
                    continue
                seen_m.add(mid)
                await self._append_id_info(event, mid, images, options, seen_avatar_ids)

        for seg in chain:
            await _process_segment(

                seg, sender_name, allow_target_avatar=not force_sender_as_target
            )

        # ????
        if force_sender_as_target:
            # ???
            # - 1 ????????????
            # - 2 ????????????????????????????
            rebuilt: list[tuple[str, bytes]] = []
            max_images = int(getattr(params, "max_images", 0) or 0)
            min_images = int(getattr(params, "min_images", 0) or 0)

            sender_display = sender_name
            sender_extra = await self.get_extra(event, send_id)
            if sender_extra:
                sender_display = str(sender_extra[0] or sender_name)
            sender_avatar = await self.get_avatar(send_id, event)

            if max_images <= 1:
                # ?????????????
                if sender_extra:
                    options["name"], options["gender"] = sender_extra[0], sender_extra[1]
                if sender_avatar:
                    rebuilt.append((sender_display, sender_avatar))
            else:
                # ???????????= ? 1 ???????????= ? 2 ?
                protected_id = str(protected_user_id or "").strip()
                if protected_id:
                    protected_name = f"User{protected_id}"
                    protected_extra = await self.get_extra(event, protected_id)
                    if protected_extra:
                        protected_name = str(protected_extra[0] or protected_name)
                        options["name"], options["gender"] = (
                            protected_extra[0],
                            protected_extra[1],
                        )
                    if protected_avatar := await self.get_avatar(protected_id, event):
                        rebuilt.append((protected_name, protected_avatar))

                if sender_avatar:
                    rebuilt.append((sender_display, sender_avatar))
                elif not rebuilt and sender_extra:
                    # ???????????????
                    options.setdefault("name", sender_display)

            # ?????? bot / ???????
            if len(rebuilt) < min_images:
                if bot_avatar := await self.get_avatar(self_id, event):
                    rebuilt.append(("bot", bot_avatar))
            if len(rebuilt) < min_images:
                for item in images:
                    if item not in rebuilt:
                        rebuilt.append(item)
                    if len(rebuilt) >= min_images:
                        break
            images = rebuilt[: max_images if max_images > 0 else len(rebuilt)]
        else:
            if len(images) < params.min_images:
                if sender_avatar := await self.get_avatar(send_id, event):
                    images.insert(0, (sender_name, sender_avatar))
            if len(images) < params.min_images:
                if bot_avatar := await self.get_avatar(self_id, event):
                    images.insert(0, ("bot", bot_avatar))
            images = images[: params.max_images]

        # Drop trigger keyword itself from text args (e.g. "拍" in "拍<@id>")
        kw = str(trigger_keyword or "").strip()
        if kw:
            texts = [t for t in texts if t != kw]
            # also drop if first token still equals keyword after partial cleanup
            if texts and texts[0] == kw:
                texts = texts[1:]

        if len(texts) < params.min_texts and params.default_texts:

            texts.extend(params.default_texts)
        texts = texts[: params.max_texts]

        return images, texts, options

    async def close(self):
        if hasattr(self, "session") and not self.session.closed:
            await self.session.close()
