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
            extra_meme_resource_urls=config.get("meme_resource_urls")
            or config.get("extra_meme_resource_urls")
            or [],
            download_timeout=config.get("download_timeout", 180),
            use_github_proxy=bool(config.get("use_github_proxy", True)),
            github_proxy=str(config.get("github_proxy") or ""),
            proxy_probe_on_fix=bool(config.get("proxy_probe_on_fix", True)),
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

    @filter.command(
        "meme列表",
        alias={"meme资源列表", "资源列表", "meme源列表", "meme资源", "表情资源列表", "表情列表"},
    )
    async def meme_resource_list(self, event: AstrMessageEvent):
        """仅发送按名称排序的表情列表图片；资源状态详情只写日志。"""
        self._sync_resource_settings()

        # detailed status/index -> logs only
        try:
            detail = self.resources.resource_list_text()
            pending = self._pending_config_urls()
            if pending:
                detail += "\n\n配置中尚未入库的链接:\n" + "\n".join(
                    f"- {u}" for u in pending
                )
                detail += "\n执行 /meme表情修复 后写入清单。"
            logger.info("meme列表详情:\n%s", detail)
            logger.info("meme资源状态:\n%s", self.resources.status_text())
        except Exception as exc:
            logger.warning("输出列表日志失败: %s", exc)

        if not self._env_ok:
            logger.warning("环境检查未通过，仍尝试渲染表情列表图")

        # ensure latest modules are visible
        try:
            self.manager.reload_memes()
        except Exception:
            self.manager._load_memes()

        try:
            images = await self.manager.render_meme_list_images()
        except Exception as exc:
            logger.error("渲染表情列表失败: %s", exc)
            images = []
        if not images:
            yield event.plain_result("表情列表图生成失败，详情见日志")
            return

        # only images in chat; multi-page if needed
        chain = [Comp.Image.fromBytes(img) for img in images]
        logger.info(
            "发送 meme 列表图: pages=%s style=%s",
            len(images),
            self.conf.get("meme_list_style", "standard"),
        )
        yield event.chain_result(chain)

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


    @filter.command("meme排行", alias={"表情排行", "meme热门", "热门表情", "meme top"})
    async def meme_ranking(self, event: AstrMessageEvent):
        """输出全局热门表情 TOP20（不足则按实际数量）。"""
        from .core.usage import top_usage, usage_summary_text

        top = top_usage(20)
        logger.info("meme排行详情:\n%s", usage_summary_text())
        if not top:
            yield event.plain_result("暂无使用记录。触发任意表情后会开始全局计数。")
            return

        lines = [f"🔥 meme 热门排行（全局 TOP{len(top)}）"]
        for idx, (key, count, _fs, _lu) in enumerate(top, 1):
            meme = self.manager.find_meme(key)
            if meme:
                kws = self.manager._get_keywords(meme)
                title = " / ".join(kws[:3]) if kws else key
            else:
                title = key
            try:
                hot_min = int(self.conf.get("meme_hot_min_count", 3) or 3)
            except Exception:
                hot_min = 3
            mark = " 🔥" if hot_min > 0 and count >= hot_min else ""
            lines.append(f"{idx}. {title}（{key}）— {count}次{mark}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme重置", alias={"重置meme统计", "meme统计重置", "清空meme排行"})
    async def meme_reset_stats(self, event: AstrMessageEvent):
        """重置全局 hot/new/使用计数。"""
        from .core.usage import reset_usage, usage_file

        prev = reset_usage()
        msg = (
            "已重置全局表情统计（hot / new / 计数器）\n"
            f"清理前：登记 {prev.get('keys', 0)}，总触发 {prev.get('uses', 0)}\n"
            f"计数文件：{usage_file()}"
        )
        logger.info("meme统计已重置: %s", prev)
        yield event.plain_result(msg)

    @filter.command("meme检查", alias={"meme环境", "meme状态", "meme依赖"})
    async def meme_check(self, event: AstrMessageEvent):
        """检查系统依赖与资源状态"""
        self._env_report = detect_environment()
        self._env_ok = self._env_report.ok
        parts = [self._env_report.format_message(), "", self.resources.status_text()]
        if self._env_ok:
            parts.append("")
            parts.append("资源命令：")
            parts.append("- /meme列表  （按名称排序列表图）")
            parts.append("- /meme排行  （全局热门 TOP20）")
            parts.append("- /meme表情修复  （增量安装；加“强制”可全量重装）")
            parts.append("- /meme字体修复  （默认内置源）")
            parts.append("- /meme重置  （管理员：清空全局计数/hot/new）")
        yield event.plain_result("\n".join(parts))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command(
        "meme表情修复",
        alias={
            "表情修复",
            "meme下载表情",
            "meme资源修复",
        },
    )
    async def meme_fix_images(self, event: AstrMessageEvent):
        """下载/解压表情资源包到 meme_generator/memes（默认增量）"""
        force = self._wants_force_fix(event)
        mode = "强制全量" if force else "增量"
        if not self._env_ok:
            yield event.plain_result(
                "警告：系统依赖检查未通过，先尝试修复表情资源。\n"
                + self._env_report.format_message()
                + f"\n\n开始表情修复（{mode}）..."
            )
        else:
            yield event.plain_result(f"开始表情修复（{mode}），请稍候...")

        # 运行时读取最新配置（额外链接 / 下载超时）
        self._sync_resource_settings()
        result = await self.resources.fix_memes(force=force)
        # 热重载 meme_generator 注册表，无需重启框架
        reload_msg = self.manager.reload_memes()
        logger.info(reload_msg)
        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("meme代理测速", alias={"github代理测速", "meme测速", "代理测速"})
    async def meme_proxy_probe(self, event: AstrMessageEvent):
        """测速 GitHub 代理；最低延迟≤3000ms 才自动选用。Gitee 始终直链。"""
        self._sync_resource_settings()
        yield event.plain_result("开始 GitHub 代理测速，请稍候...")

        def cb(msg: str) -> None:
            logger.info(msg)

        try:
            from .core.resources import (
                GITHUB_PROXY_MAX_AUTO_LATENCY_MS,
                get_selected_github_proxy,
                load_github_proxy_rank,
            )

            await self.resources.ensure_github_proxy_rank(progress_cb=cb, force=True)
            if not self.resources.use_github_proxy:
                yield event.plain_result(
                    "GitHub 代理已关闭（use_github_proxy=false）。\n"
                    "当前 GitHub / Gitee 均走直链。"
                )
                return

            selected = self.resources.github_proxy or get_selected_github_proxy()
            if self.resources.github_proxy:
                yield event.plain_result(
                    "已使用配置面板固定代理（优先生效）\n"
                    f"{self.resources.github_proxy}\n"
                    "GitHub 将优先走该代理；Gitee 始终直链。"
                )
                return

            cache = load_github_proxy_rank()
            available = [x for x in (cache.get("proxies") or []) if x.get("available")]
            if not available:
                yield event.plain_result(
                    "未测得可用代理。\n"
                    "GitHub 保持直链；也可在配置面板填写 github_proxy。"
                )
                return

            best = available[0]
            best_proxy = str(best.get("proxy") or "")
            best_lat = best.get("latency")
            lines_out = [
                "GitHub 代理测速完成",
                f"最低延迟: {best_proxy}  {best_lat} ms",
            ]
            if selected:
                lines_out.append(
                    f"已自动选用: {selected}（≤{GITHUB_PROXY_MAX_AUTO_LATENCY_MS} ms）"
                )
                lines_out.append("之后 GitHub 优先走代理，失败回退直链；Gitee 始终直链。")
            else:
                lines_out.append(
                    f"网络不佳：最低延迟 > {GITHUB_PROXY_MAX_AUTO_LATENCY_MS} ms，不自动切换代理"
                )
                lines_out.append("GitHub 保持直链；Gitee 始终直链。")
            lines_out.append(f"可用节点: {len(available)}")
            for idx, item in enumerate(available[:3], 1):
                mark = " <- 已选" if item.get("proxy") == selected else ""
                lines_out.append(
                    f"{idx}. {item.get('proxy')}  {item.get('latency')} ms{mark}"
                )
            yield event.plain_result("\n".join(lines_out))
        except Exception as exc:
            logger.exception("代理测速失败")
            yield event.plain_result(f"代理测速失败: {exc}")


    @filter.permission_type(PermissionType.ADMIN)
    @filter.command(
        "meme字体修复",
        alias={
            "字体修复",
            "meme下载字体",
            "meme安装字体",
        },
    )
    async def meme_fix_fonts(self, event: AstrMessageEvent):
        """下载/安装表情字体到用户字体目录（默认内置源）"""
        yield event.plain_result("开始字体修复，请稍候...")
        self._sync_resource_settings()
        result = await self.resources.fix_fonts()
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

        # 全局计数：任意会话触发即 +1（hot/new/排行共用）
        try:
            from .core.usage import record_trigger

            meme_obj = self.manager.find_meme(keyword)
            usage_key = str(getattr(meme_obj, "key", "") or keyword) if meme_obj else keyword
            record_trigger(usage_key)
        except Exception as exc:
            logger.debug("记录 meme 触发次数失败: %s", exc)

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

    def _wants_force_fix(self, event: AstrMessageEvent) -> bool:
        text = (event.message_str or "").strip().lower()
        tokens = {"强制", "force", "--force", "-f", "全量", "重装"}
        return any(tok in text for tok in tokens)

    def _sync_resource_settings(self) -> None:
        """Refresh runtime resource installer settings from latest config."""
        self.resources.extra_meme_resource_urls = self._parse_url_list(
            self.conf.get("meme_resource_urls")
            or self.conf.get("extra_meme_resource_urls")
            or []
        )
        try:
            from .core.resources import clamp_download_timeout, normalize_github_proxy
            self.resources.download_timeout = clamp_download_timeout(
                self.conf.get("download_timeout", 180)
            )
            self.resources.use_github_proxy = bool(self.conf.get("use_github_proxy", True))
            self.resources.github_proxy = normalize_github_proxy(
                self.conf.get("github_proxy") or ""
            )
            self.resources.proxy_probe_on_fix = bool(
                self.conf.get("proxy_probe_on_fix", True)
            )
        except Exception:
            pass

    def _pending_config_urls(self) -> list[str]:

        from .core.resources import load_resource_index

        configured = self._parse_url_list(
            self.conf.get("meme_resource_urls")
            or self.conf.get("extra_meme_resource_urls")
            or []
        )
        index = load_resource_index()
        known = {
            str((src or {}).get("url") or "")
            for src in (index.get("sources") or {}).values()
        }
        return [u for u in configured if u not in known]

    def _parse_url_list(self, value) -> list[str]:

        """Parse multi-line / comma-separated resource URLs."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            out: list[str] = []
            for item in value:
                out.extend(self._parse_url_list(item))
            return out
        text = str(value).replace("，", ",").replace("\r", "\n")
        parts: list[str] = []
        for line in text.split("\n"):
            for item in line.split(","):
                item = item.strip()
                if item:
                    parts.append(item)
        return parts

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
