import asyncio
import io
import importlib.metadata
from dataclasses import dataclass, field
from typing import Literal

from meme_generator import Meme, get_memes

try:
    __version__ = importlib.metadata.version("meme_generator")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.12"

from astrbot import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .param import ParamsCollector


@dataclass
class MemeProperties:
    disabled: bool = False
    labels: list[Literal["new", "hot"]] = field(default_factory=list)


class MemeManager:
    # 0.1.x 为 Python 版，0.2.x 为 Rust 版
    is_py_version = tuple(map(int, __version__.split(".")[:3])) < (0, 2, 0)

    def __init__(self, config: AstrBotConfig, collect: ParamsCollector):
        self.conf = config
        self.collect = collect

        if self.is_py_version:
            from meme_generator.download import check_resources
            from meme_generator.utils import render_meme_list, run_sync

            self.render_meme_list = render_meme_list
            self.check_resources_func = check_resources
            self.run_sync = run_sync
        else:
            from meme_generator import Image as MemeImage
            from meme_generator.resources import check_resources_in_background
            from meme_generator.tools import (
                MemeProperties as MemeProps,
                MemeSortBy,
                render_meme_list,
            )

            self.render_meme_list = render_meme_list
            self.check_resources_func = check_resources_in_background
            self.MemeImage = MemeImage
            self.MemeProperties = MemeProps
            self.MemeSortBy = MemeSortBy

        self.memes: list[Meme] = get_memes()
        self.meme_keywords: set[str] = set()
        for m in self.memes:
            keywords = m.keywords if self.is_py_version else m.info.keywords
            self.meme_keywords.update(keywords)
            if m.key:
                self.meme_keywords.add(m.key)

    async def check_resources(self):
        """检查 meme 资源"""
        if not self.conf.get("is_check_resources", True):
            return
        logger.info("开始检查memes资源...")
        try:
            if self.is_py_version:
                await self.check_resources_func()
            else:
                await asyncio.to_thread(self.check_resources_func)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"检查memes资源失败: {e}")

    def find_meme(self, keyword: str) -> Meme | None:
        """根据关键词或 key 查找 meme"""
        for meme in self.memes:
            keywords = meme.keywords if self.is_py_version else meme.info.keywords
            if keyword == meme.key or keyword in keywords:
                return meme
        return None

    def is_meme_keyword(self, meme_name: str) -> bool:
        """判断是否为有效 meme 关键词或 key"""
        return meme_name in self.meme_keywords

    def match_meme_keyword(self, text: str) -> str | None:
        """精确匹配消息首词是否为 meme 关键词"""
        if not text.strip():
            return None
        words = text.split()
        first_word = words[0] if words else ""
        if first_word in self.meme_keywords:
            return first_word
        return None

    async def render_meme_list_image(self) -> bytes | None:
        """生成 meme 列表图片"""
        try:
            if self.is_py_version:
                meme_list = [(m, MemeProperties(labels=[])) for m in self.memes]
                img_io = self.render_meme_list(
                    meme_list=meme_list,
                    text_template="{index}.{keywords}",
                    add_category_icon=True,
                )
                return img_io.getvalue()
            img_bytes = await asyncio.to_thread(
                self.render_meme_list,
                meme_properties={m.key: self.MemeProperties() for m in self.memes},
                exclude_memes=[],
                sort_by=self.MemeSortBy.KeywordsPinyin,
                sort_reverse=False,
                text_template="{index}. {keywords}",
                add_category_icon=True,
            )
            return img_bytes
        except Exception as e:
            logger.error(f"生成meme列表图片失败: {e}")
            return None

    def get_meme_info(self, keyword: str) -> tuple[str, bytes] | None:
        """获取 meme 详情（描述 + 预览图）"""
        meme = self.find_meme(keyword)
        if not meme:
            return None

        if self.is_py_version:
            p = meme.params_type
            keywords = meme.keywords
            tags = meme.tags
        else:
            p = meme.info.params
            keywords = meme.info.keywords
            tags = meme.info.tags

        meme_info: list[str] = []
        if meme.key:
            meme_info.append(f"名称：{meme.key}")
        if keywords:
            meme_info.append(f"别名：{', '.join(keywords)}")
        if p.max_images > 0:
            if p.min_images == p.max_images:
                meme_info.append(f"所需图片：{p.min_images}张")
            else:
                meme_info.append(f"所需图片：{p.min_images}~{p.max_images}张")
        if p.max_texts > 0:
            if p.min_texts == p.max_texts:
                meme_info.append(f"所需文本：{p.min_texts}段")
            else:
                meme_info.append(f"所需文本：{p.min_texts}~{p.max_texts}段")
        if p.default_texts:
            meme_info.append(f"默认文本：{', '.join(p.default_texts)}")
        if tags:
            meme_info.append(f"标签：{', '.join(list(tags))}")
        meme_info_str = "\n".join(meme_info)

        previewed = meme.generate_preview()
        if isinstance(previewed, io.BytesIO):
            image = previewed.getvalue()
        elif isinstance(previewed, bytes):
            image = previewed
        else:
            logger.warning(f"meme {keyword} 预览图格式异常: {type(previewed)}")
            image = b""
        return meme_info_str, image

    async def generate_meme(
        self,
        event: AstrMessageEvent,
        keyword: str,
        force_sender_as_target: bool = False,
        protected_user_id: str | None = None,
    ) -> bytes | None:
        """生成指定 meme 图片"""
        meme = self.find_meme(keyword)
        if not meme:
            logger.warning(f"未找到meme关键词: {keyword}")
            return None

        params = meme.params_type if self.is_py_version else meme.info.params
        images, texts, options = await self.collect.collect_params(
            event, params, force_sender_as_target, protected_user_id
        )

        try:
            if self.is_py_version:
                meme_images = [i[1] for i in images]
                result = await self.run_sync(meme)(
                    images=meme_images, texts=texts, args=options
                )
                return result.getvalue()

            meme_images = [
                self.MemeImage(name=str(i[0]), data=i[1]) for i in images
            ]
            result = await asyncio.to_thread(meme.generate, meme_images, texts, options)
            return result
        except Exception as e:
            logger.error(f"生成meme {keyword} 失败: {e}")
            return None
