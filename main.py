import asyncio
from contextlib import suppress

import astrbot.core.message.components as Comp
from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.star.filter.permission import PermissionType

from .core.meme import MemeManager
from .core.param import ParamsCollector
from .utils import compress_image


class MemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.collector = ParamsCollector(config)
        self.manager = MemeManager(config, self.collector)
        self._resource_task: asyncio.Task | None = None

    async def initialize(self):
        self._resource_task = asyncio.create_task(self.manager.check_resources())

    @filter.command("meme帮助", alias={"表情帮助", "meme菜单", "meme列表"})
    async def memes_help(self, event):
        if output := await self.manager.render_meme_list_image():
            yield event.chain_result([Comp.Image.fromBytes(output)])
        else:
            yield event.plain_result("meme列表图生成失败")

    @filter.command("meme详情", alias={"表情详情", "meme信息"})
    async def meme_details_show(
        self, event: AstrMessageEvent, keyword: str | int | None = None
    ):
        if not keyword:
            yield event.plain_result("未指定要查看的meme")
            return
        keyword = str(keyword)

        result = self.manager.get_meme_info(keyword)
        if not result:
            yield event.plain_result("未找到相关meme")
            return

        meme_info, preview = result
        chain = [
            Comp.Plain(meme_info),
            Comp.Image.fromBytes(preview),
        ]
        yield event.chain_result(chain)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("禁用meme")
    async def add_supervisor(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """禁用meme"""
        if not meme_name:
            yield event.plain_result("未指定要禁用的meme")
            return
        if not self.manager.is_meme_keyword(meme_name):
            yield event.plain_result(f"meme: {meme_name} 不存在")
            return
        if meme_name in self.conf["memes_disabled_list"]:
            yield event.plain_result(f"meme: {meme_name} 已被禁用")
            return
        self.conf["memes_disabled_list"].append(meme_name)
        self.conf.save_config()
        yield event.plain_result(f"已禁用meme: {meme_name}")
        logger.info(f"当前禁用meme: {self.conf['memes_disabled_list']}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("启用meme")
    async def remove_supervisor(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """启用meme"""
        if not meme_name:
            yield event.plain_result("未指定要启用的meme")
            return
        if not self.manager.is_meme_keyword(meme_name):
            yield event.plain_result(f"meme: {meme_name} 不存在")
            return
        if meme_name not in self.conf["memes_disabled_list"]:
            yield event.plain_result(f"meme: {meme_name} 未被禁用")
            return
        self.conf["memes_disabled_list"].remove(meme_name)
        self.conf.save_config()
        yield event.plain_result(f"已启用meme: {meme_name}")
        logger.info(f"当前禁用meme: {self.conf['memes_disabled_list']}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme黑名单")
    async def list_supervisors(self, event: AstrMessageEvent):
        """查看禁用的meme"""
        yield event.plain_result(f"当前禁用的meme: {self.conf['memes_disabled_list']}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("添加保护", alias={"加入保护名单"})
    async def add_protected_user(
        self, event: AstrMessageEvent, user_id: str | None = None
    ):
        """添加保护用户"""
        if not user_id:
            yield event.plain_result("未指定要保护的用户ID")
            return
        if not user_id.isdigit():
            yield event.plain_result("用户ID必须是数字")
            return

        protected_list = self._parse_csv_set(self.conf.get("protected_users", ""))
        if user_id in protected_list:
            yield event.plain_result(f"用户 {user_id} 已在保护名单中")
            return

        protected_list.add(user_id)
        self.conf["protected_users"] = ",".join(sorted(protected_list))
        self.conf.save_config()
        yield event.plain_result(f"已添加用户 {user_id} 到保护名单")
        logger.info(f"当前保护名单: {self.conf['protected_users']}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("移除保护", alias={"移出保护名单"})
    async def remove_protected_user(
        self, event: AstrMessageEvent, user_id: str | None = None
    ):
        """移除保护用户"""
        if not user_id:
            yield event.plain_result("未指定要移除的用户ID")
            return

        protected_list = self._parse_csv_set(self.conf.get("protected_users", ""))
        if user_id not in protected_list:
            yield event.plain_result(f"用户 {user_id} 不在保护名单中")
            return

        protected_list.remove(user_id)
        self.conf["protected_users"] = ",".join(sorted(protected_list))
        self.conf.save_config()
        yield event.plain_result(f"已从保护名单移除用户 {user_id}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("保护名单")
    async def list_protected_users(self, event: AstrMessageEvent):
        """查看保护名单"""
        protected = self.conf.get("protected_users", "")
        if not protected:
            yield event.plain_result("保护名单为空")
        else:
            yield event.plain_result(f"当前保护名单: {protected}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("添加反弹表情", alias={"添加反弹meme"})
    async def add_bounce_meme(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """添加反弹表情"""
        if not meme_name:
            yield event.plain_result("未指定要添加的反弹表情")
            return
        if not self.manager.is_meme_keyword(meme_name):
            yield event.plain_result(f"meme: {meme_name} 不存在")
            return

        bounce_list = self._parse_csv_set(self.conf.get("bounce_back_memes", ""))
        if meme_name in bounce_list:
            yield event.plain_result(f"表情 {meme_name} 已在反弹列表中")
            return

        bounce_list.add(meme_name)
        self.conf["bounce_back_memes"] = ",".join(sorted(bounce_list))
        self.conf.save_config()
        yield event.plain_result(f"已添加表情 {meme_name} 到反弹列表")
        logger.info(f"当前反弹表情: {self.conf['bounce_back_memes']}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("移除反弹表情", alias={"移除反弹meme"})
    async def remove_bounce_meme(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """移除反弹表情"""
        if not meme_name:
            yield event.plain_result("未指定要移除的反弹表情")
            return

        bounce_list = self._parse_csv_set(self.conf.get("bounce_back_memes", ""))
        if meme_name not in bounce_list:
            yield event.plain_result(f"表情 {meme_name} 不在反弹列表中")
            return

        bounce_list.remove(meme_name)
        self.conf["bounce_back_memes"] = ",".join(sorted(bounce_list))
        self.conf.save_config()
        yield event.plain_result(f"已从反弹列表移除表情 {meme_name}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("反弹表情列表", alias={"反弹meme列表"})
    async def list_bounce_memes(self, event: AstrMessageEvent):
        """查看反弹表情列表"""
        bounce_memes = self.conf.get("bounce_back_memes", "")
        if not bounce_memes:
            yield event.plain_result("反弹表情列表为空（所有表情都会触发反弹保护）")
        else:
            yield event.plain_result(f"当前反弹表情: {bounce_memes}")

    @filter.event_message_type(EventMessageType.ALL)
    async def meme_handle(self, event: AstrMessageEvent):
        """处理 meme 生成的主流程"""
        if self.conf["need_prefix"] and not event.is_at_or_wake_command:
            return

        extra_prefix = self.conf.get("extra_prefix") or ""
        if extra_prefix and not event.message_str.startswith(extra_prefix):
            return

        # 用户黑名单
        user_id = str(event.get_sender_id())
        blacklist = self._parse_csv_set(self.conf.get("user_blacklist", ""))
        if user_id in blacklist:
            return

        param = event.message_str
        if extra_prefix:
            param = param.removeprefix(extra_prefix).lstrip()
        if not param:
            return

        # 仅精确匹配首词
        keyword = self.manager.match_meme_keyword(text=param)
        if not keyword or keyword in self.conf["memes_disabled_list"]:
            return

        # 保护反弹
        protected_users = self._parse_csv_set(self.conf.get("protected_users", ""))
        bounce_memes = self._parse_csv_set(self.conf.get("bounce_back_memes", ""))
        should_bounce = False
        protected_user_id = None
        if protected_users:
            meme_should_bounce = not bounce_memes or keyword in bounce_memes
            if meme_should_bounce:
                protected_user_id = self._check_target_protected(event, protected_users)
                if protected_user_id:
                    should_bounce = True

        try:
            if should_bounce:
                image = await asyncio.wait_for(
                    self.manager.generate_meme(
                        event,
                        keyword,
                        force_sender_as_target=True,
                        protected_user_id=protected_user_id,
                    ),
                    timeout=self.conf["meme_timeout"],
                )
            else:
                image = await asyncio.wait_for(
                    self.manager.generate_meme(event, keyword),
                    timeout=self.conf["meme_timeout"],
                )
        except asyncio.TimeoutError:
            logger.warning(f"meme生成超时: {keyword}")
            yield event.plain_result("meme生成超时")
            return
        except Exception as e:
            logger.error(f"meme生成异常: {e}")
            return

        if image and self.conf["is_compress_image"]:
            try:
                image = compress_image(image) or image
            except Exception:
                pass

        if image:
            yield event.chain_result([Comp.Image.fromBytes(image)])  # type: ignore

    def _check_target_protected(
        self, event: AstrMessageEvent, protected_users: set
    ) -> str | None:
        """检查消息目标是否在保护名单中，命中则返回用户ID"""
        chain = event.get_messages()

        for seg in chain:
            if isinstance(seg, Comp.At):
                if str(seg.qq) in protected_users:
                    return str(seg.qq)
            elif isinstance(seg, Comp.Plain):
                for word in seg.text.strip().split():
                    if word.startswith("@") and word[1:].isdigit():
                        if word[1:] in protected_users:
                            return word[1:]

        reply_seg = next((seg for seg in chain if isinstance(seg, Comp.Reply)), None)
        if reply_seg and reply_seg.sender_id:
            if str(reply_seg.sender_id) in protected_users:
                return str(reply_seg.sender_id)
        return None

    def _parse_csv_set(self, value: str) -> set[str]:
        """解析逗号分隔配置为集合"""
        if not value or not str(value).strip():
            return set()
        return {item.strip() for item in str(value).split(",") if item.strip()}

    async def terminate(self):
        """插件终止时清理资源检查任务与 HTTP 会话"""
        if self._resource_task and not self._resource_task.done():
            self._resource_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._resource_task
        await self.collector.close()
