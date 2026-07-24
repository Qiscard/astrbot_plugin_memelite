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

from .core.env_check import detect_environment
from .core.meme import MemeManager
from .core.param import ParamsCollector
from .core.resources import ResourceInstaller, count_installed_meme_fonts, count_meme_image_files, get_memes_target_dir
from .utils import compress_image


class MemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.collector = ParamsCollector(config)
        self.manager = MemeManager(config, self.collector)
        self.resources = ResourceInstaller(
            repo=str(config.get("resource_repo") or "Qiscard/astrbot_plugin_memelite"),
            release_tag=str(config.get("resource_release_tag") or "assets-v1"),
            gitee_repo=str(config.get("gitee_resource_repo") or "qiscard/astrbot_plugin_memelite"),
            gitee_release_tag=str(config.get("gitee_resource_release_tag") or "assets-v1"),
            gitee_memes_url=str(config.get("gitee_memes_url") or ""),
            gitee_fonts_url=str(config.get("gitee_fonts_url") or ""),
            memes_url=str(config.get("memes_url") or ""),
            fonts_url=str(config.get("fonts_url") or ""),
            local_memes_dir=str(config.get("local_memes_dir") or ""),
            local_fonts_dir=str(config.get("local_fonts_dir") or ""),
            local_memes_zip=str(config.get("local_memes_zip") or ""),
            local_fonts_zip=str(config.get("local_fonts_zip") or ""),
        )
        self._bootstrap_task: asyncio.Task | None = None
        self._env_report = detect_environment()
        self._env_ok = self._env_report.ok

    async def initialize(self):
        # Always log environment status on startup
        if not self._env_ok:
            logger.error(
                "astrbot_plugin_memelite 环境检查未通过:\n%s",
                self._env_report.format_message(),
            )
        else:
            logger.info("astrbot_plugin_memelite 环境检查通过")

        self._bootstrap_task = asyncio.create_task(self._bootstrap())

    async def _bootstrap(self):
        """Startup checks only. Heavy assets are installed via repair commands."""
        try:
            # Optional auto-fix if enabled and resources look missing
            auto_fix = bool(self.conf.get("auto_fix_resources_on_start", False))
            if auto_fix and self._env_ok:
                memes_dir = get_memes_target_dir()
                if count_meme_image_files(memes_dir) < 50:
                    logger.info("检测到表情资源不足，自动执行表情修复...")
                    msg = await self.resources.fix_memes()
                    logger.info(msg)
                if count_installed_meme_fonts() < 3:
                    logger.info("检测到字体资源不足，自动执行字体修复...")
                    msg = await self.resources.fix_fonts()
                    logger.info(msg)

            # Legacy online check_resources (official upstream images) - off by default
            if self.conf.get("is_check_resources", False):
                await self.manager.check_resources()
            else:
                self.manager._load_memes()

            img_count = count_meme_image_files(get_memes_target_dir())
            font_count = count_installed_meme_fonts()
            if img_count < 50:
                logger.warning(
                    "表情图片资源不足(当前 %s 个文件)。请管理员发送: /meme表情修复",
                    img_count,
                )
            if font_count < 3:
                logger.warning(
                    "字体资源不足(当前 %s 个文件)。请管理员发送: /meme字体修复",
                    font_count,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"插件启动检查失败: {exc}")

    @filter.command("meme帮助", alias={"表情帮助", "meme菜单", "meme列表"})
    async def memes_help(self, event):
        if not self._env_ok:
            yield event.plain_result(self._env_report.format_message())
            return
        if output := await self.manager.render_meme_list_image():
            yield event.chain_result([Comp.Image.fromBytes(output)])
        else:
            yield event.plain_result(
                "meme列表图生成失败。请先执行 /meme检查 查看依赖与资源状态，"
                "必要时使用 /meme表情修复 与 /meme字体修复"
            )

    @filter.command("meme详情", alias={"表情详情", "meme信息"})
    async def meme_details_show(
        self, event: AstrMessageEvent, keyword: str | int | None = None
    ):
        if not self._env_ok:
            yield event.plain_result(self._env_report.format_message())
            return
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

    @filter.command("meme检查", alias={"meme环境", "meme状态", "meme依赖"})
    async def meme_check(self, event: AstrMessageEvent):
        """检查系统依赖与资源状态"""
        self._env_report = detect_environment()
        self._env_ok = self._env_report.ok
        parts = [self._env_report.format_message(), "", self.resources.status_text()]
        if self._env_ok:
            parts.append("")
            parts.append("资源修复命令：")
            parts.append("- /meme表情修复  (GitHub)")
            parts.append("- /meme字体修复  (GitHub)")
            parts.append("- /meme表情修复2 (Gitee 国内镜像)")
            parts.append("- /meme字体修复2 (Gitee 国内镜像)")
        yield event.plain_result("\n".join(parts))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme表情修复", alias={"表情修复", "meme下载表情", "meme资源修复"})
    async def meme_fix_images(self, event: AstrMessageEvent):
        """下载/解压表情资源包到 meme_generator/memes（GitHub）"""
        if not self._env_ok:
            # still allow install of assets, but warn
            yield event.plain_result(
                "警告：系统依赖检查未通过，先尝试修复表情资源。\n"
                + self._env_report.format_message()
                + "\n\n开始表情修复..."
            )
        else:
            yield event.plain_result("开始表情修复（GitHub），请稍候...")

        result = await self.resources.fix_memes()
        # reload meme list after install
        self.manager._load_memes()
        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme字体修复", alias={"字体修复", "meme下载字体", "meme安装字体"})
    async def meme_fix_fonts(self, event: AstrMessageEvent):
        """下载/安装表情字体到用户字体目录（GitHub）"""
        yield event.plain_result("开始字体修复（GitHub），请稍候...")
        result = await self.resources.fix_fonts()
        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme表情修复2", alias={"表情修复2", "meme下载表情2", "meme资源修复2", "meme表情修复gitee"})
    async def meme_fix_images_gitee(self, event: AstrMessageEvent):
        """从 Gitee 下载/解压表情资源包到 meme_generator/memes"""
        if not self._env_ok:
            yield event.plain_result(
                "警告：系统依赖检查未通过，先尝试从 Gitee 修复表情资源。\n"
                + self._env_report.format_message()
                + "\n\n开始表情修复（Gitee）..."
            )
        else:
            yield event.plain_result("开始表情修复（Gitee 国内镜像），请稍候...")

        result = await self.resources.fix_memes_gitee()
        self.manager._load_memes()
        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme字体修复2", alias={"字体修复2", "meme下载字体2", "meme安装字体2", "meme字体修复gitee"})
    async def meme_fix_fonts_gitee(self, event: AstrMessageEvent):
        """从 Gitee 下载/安装表情字体到用户字体目录"""
        yield event.plain_result("开始字体修复（Gitee 国内镜像），请稍候...")
        result = await self.resources.fix_fonts_gitee()
        yield event.plain_result(result)

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
        if not self._env_ok:
            return

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

        # 保护反弹：目标命中保护名单时，强制把触发者作为被制作对象
        protected_users = self._parse_csv_set(self.conf.get("protected_users", ""))
        bounce_memes = self._parse_csv_set(self.conf.get("bounce_back_memes", ""))
        should_bounce = False
        protected_user_id = None
        if protected_users:
            # bounce_back_memes 为空 = 全部表情反弹；非空则关键词或其别名命中即反弹
            meme_should_bounce = (not bounce_memes) or self._keyword_in_bounce_list(
                keyword, bounce_memes
            )
            if meme_should_bounce:
                protected_user_id = self._check_target_protected(event, protected_users)
                if protected_user_id:
                    should_bounce = True
                    logger.info(
                        "meme 反弹触发: keyword=%s sender=%s protected=%s",
                        keyword,
                        event.get_sender_id(),
                        protected_user_id,
                    )

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

    def _normalize_uid(self, value) -> str:
        """Normalize platform user id for set membership checks."""
        if value is None:
            return ""
        text = str(value).strip()
        if text.endswith(".0") and text.replace(".", "", 1).isdigit():
            text = text[:-2]
        return text

    def _check_target_protected(
        self, event: AstrMessageEvent, protected_users: set
    ) -> str | None:
        """检查消息目标是否在保护名单中，命中则返回用户ID。

        支持：
        - @保护用户
        - 文本 @QQ号
        - 引用保护用户的消息
        """
        if not protected_users:
            return None

        protected = {self._normalize_uid(x) for x in protected_users if self._normalize_uid(x)}
        self_id = self._normalize_uid(event.get_self_id())
        sender_id = self._normalize_uid(event.get_sender_id())
        chain = event.get_messages() or []

        # 1) 引用消息：优先（常见玩法：回复某人再发“摸”）
        for seg in chain:
            if isinstance(seg, Comp.Reply):
                rid = self._normalize_uid(getattr(seg, "sender_id", None) or getattr(seg, "qq", None))
                if rid and rid in protected and rid != sender_id:
                    return rid

        # 2) @ 段（跳过 @bot / @all）
        for seg in chain:
            if isinstance(seg, Comp.At):
                qq = self._normalize_uid(getattr(seg, "qq", None))
                if not qq or qq.lower() == "all" or qq == self_id:
                    continue
                if qq in protected and qq != sender_id:
                    return qq

        # 3) 纯文本 @123456
        for seg in chain:
            if isinstance(seg, Comp.Plain):
                for word in (seg.text or "").replace("​", " ").split():
                    token = word.strip()
                    if token.startswith("@") and token[1:].isdigit():
                        qq = self._normalize_uid(token[1:])
                        if qq in protected and qq != sender_id:
                            return qq

        return None

    def _parse_csv_set(self, value) -> set[str]:
        """解析逗号/列表配置为集合"""
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(item).strip() for item in value if str(item).strip()}
        text = str(value).strip()
        if not text:
            return set()
        # 兼容中文逗号
        text = text.replace("，", ",")
        return {item.strip() for item in text.split(",") if item.strip()}

    async def terminate(self):
        """插件终止时清理资源检查任务与 HTTP 会话"""
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._bootstrap_task
        await self.resources.close()
        await self.collector.close()
