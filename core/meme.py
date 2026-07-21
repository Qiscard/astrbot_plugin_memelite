import asyncio
import io
import re
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as get_package_version
from typing import Any, Literal

from astrbot import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .param import ParamsCollector


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", version)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _resolve_version(module: Any) -> str:
    try:
        from meme_generator.version import __version__ as legacy_version

        return str(legacy_version)
    except Exception:
        pass

    get_version = getattr(module, "get_version", None)
    if callable(get_version):
        try:
            return str(get_version())
        except Exception:
            pass

    try:
        return get_package_version("meme_generator")
    except PackageNotFoundError:
        return "0.0.0"


IMPORT_ERROR: str | None = None
MEME_GENERATOR_AVAILABLE = False
__version__ = "0.0.0"
Meme = Any  # type: ignore[misc,assignment]


def get_memes(*args: Any, **kwargs: Any) -> list[Any]:
    return []


try:
    import meme_generator as meme_generator_module
    from meme_generator import Meme as _Meme
    from meme_generator import get_memes as _get_memes

    Meme = _Meme  # type: ignore[misc,assignment]
    get_memes = _get_memes  # type: ignore[assignment]
    __version__ = _resolve_version(meme_generator_module)
    MEME_GENERATOR_AVAILABLE = True
except ImportError as exc:
    IMPORT_ERROR = str(exc)
    logger.error(
        "meme-generator 未安装或导入失败: %s。请安装依赖后重载插件。",
        IMPORT_ERROR,
    )


@dataclass
class LegacyMemeProperties:
    disabled: bool = False
    labels: list[Literal["new", "hot"]] = field(default_factory=list)


class MemeManager:
    is_py_version = _parse_version(__version__) < (0, 2, 0)

    def __init__(self, config: AstrBotConfig, collect: ParamsCollector):
        self.conf = config
        self.collect = collect
        self.memes: list[Any] = []
        self.meme_keywords: set[str] = set()

        self.render_meme_list_func: Any = None
        self.check_resources_func: Any = None
        self.run_sync: Any = None
        self.MemeImage: Any = None
        self.MemePropertiesType: Any = LegacyMemeProperties
        self.MemeSortBy: Any = None

        if not MEME_GENERATOR_AVAILABLE:
            return

        if self.is_py_version:
            from meme_generator.download import check_resources
            from meme_generator.utils import render_meme_list, run_sync

            self.render_meme_list_func = render_meme_list
            self.check_resources_func = check_resources
            self.run_sync = run_sync
        else:
            from meme_generator import Image as MemeImage
            from meme_generator.tools import (
                MemeProperties as RustMemeProperties,
                MemeSortBy,
                render_meme_list,
            )

            try:
                from meme_generator.resources import check_resources
            except ImportError:
                from meme_generator.resources import (
                    check_resources_in_background as check_resources,
                )

            self.render_meme_list_func = render_meme_list
            self.check_resources_func = check_resources
            self.MemeImage = MemeImage
            self.MemePropertiesType = RustMemeProperties
            self.MemeSortBy = MemeSortBy

    def _load_memes(self) -> None:
        if not MEME_GENERATOR_AVAILABLE:
            self.memes = []
            self.meme_keywords = set()
            return
        try:
            self.memes = list(get_memes())
        except Exception as exc:
            logger.error(f"加载 meme 列表失败: {exc}")
            self.memes = []
            self.meme_keywords = set()
            return

        keywords: set[str] = set()
        for meme in self.memes:
            keywords.update(self._get_keywords(meme))
            if getattr(meme, "key", None):
                keywords.add(meme.key)
        self.meme_keywords = keywords

    def _ensure_memes_loaded(self) -> bool:
        if not self.memes:
            self._load_memes()
        return bool(self.memes)

    @staticmethod
    def _get_info(meme: Any) -> Any | None:
        return getattr(meme, "info", None)

    def _get_keywords(self, meme: Any) -> list[str]:
        info = self._get_info(meme)
        if info is not None and hasattr(info, "keywords"):
            return list(info.keywords)
        return list(getattr(meme, "keywords", []) or [])

    def _get_params(self, meme: Any) -> Any:
        info = self._get_info(meme)
        if info is not None and hasattr(info, "params"):
            return info.params
        return meme.params_type

    def _get_tags(self, meme: Any) -> list[str]:
        info = self._get_info(meme)
        if info is not None and hasattr(info, "tags"):
            return list(info.tags)
        return list(getattr(meme, "tags", []) or [])

    @staticmethod
    def _unwrap_bytes(result: Any, action: str) -> bytes:
        if isinstance(result, io.BytesIO):
            return result.getvalue()
        if isinstance(result, (bytes, bytearray, memoryview)):
            return bytes(result)

        detail = None
        for attr in ("feedback", "error", "path"):
            value = getattr(result, attr, None)
            if value:
                detail = str(value)
                break
        raise RuntimeError(f"{action} failed: {detail or repr(result)}")

    async def check_resources(self):
        if not MEME_GENERATOR_AVAILABLE:
            logger.error(
                "meme-generator 不可用，跳过资源检查。请安装: pip install 'meme_generator>=0.1.14,<0.2.0'"
            )
            return

        if not self.conf.get("is_check_resources", True):
            self._load_memes()
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
        self._load_memes()

    def find_meme(self, keyword: str) -> Any | None:
        if not self._ensure_memes_loaded():
            return None
        for meme in self.memes:
            keywords = self._get_keywords(meme)
            if keyword == getattr(meme, "key", None) or keyword in keywords:
                return meme
        return None

    def is_meme_keyword(self, meme_name: str) -> bool:
        if not self._ensure_memes_loaded():
            return False
        return meme_name in self.meme_keywords

    def match_meme_keyword(self, text: str) -> str | None:
        """精确匹配消息首词是否为 meme 关键词"""
        if not self._ensure_memes_loaded():
            return None
        if not text.strip():
            return None
        first_word = text.split()[0] if text.split() else ""
        if first_word in self.meme_keywords:
            return first_word
        return None

    async def render_meme_list_image(self) -> bytes | None:
        if not self._ensure_memes_loaded() or not self.render_meme_list_func:
            return None
        try:
            if self.is_py_version:
                meme_list = [(m, LegacyMemeProperties(labels=[])) for m in self.memes]
                rendered = self.render_meme_list_func(
                    meme_list=meme_list,  # type: ignore[arg-type]
                    text_template="{index}.{keywords}",
                    add_category_icon=True,
                )
                return self._unwrap_bytes(rendered, "render meme list")

            meme_props = {m.key: self.MemePropertiesType() for m in self.memes}
            rendered = await asyncio.to_thread(
                self.render_meme_list_func,
                meme_properties=meme_props,
                exclude_memes=[],
                sort_by=self.MemeSortBy.KeywordsPinyin,
                sort_reverse=False,
                text_template="{index}. {keywords}",
                add_category_icon=True,
            )
            return self._unwrap_bytes(rendered, "render meme list")
        except Exception as e:
            logger.error(f"生成meme列表图片失败: {e}")
            return None

    def get_meme_info(self, keyword: str) -> tuple[str, bytes] | None:
        meme = self.find_meme(keyword)
        if not meme:
            return None

        params = self._get_params(meme)
        keywords = self._get_keywords(meme)
        tags = self._get_tags(meme)

        lines: list[str] = []
        if getattr(meme, "key", None):
            lines.append(f"名称：{meme.key}")
        if keywords:
            lines.append(f"别名：{', '.join(keywords)}")
        if params.max_images > 0:
            if params.min_images == params.max_images:
                lines.append(f"所需图片：{params.min_images}张")
            else:
                lines.append(f"所需图片：{params.min_images}~{params.max_images}张")
        if params.max_texts > 0:
            if params.min_texts == params.max_texts:
                lines.append(f"所需文本：{params.min_texts}段")
            else:
                lines.append(f"所需文本：{params.min_texts}~{params.max_texts}段")
        if params.default_texts:
            lines.append(f"默认文本：{', '.join(params.default_texts)}")
        if tags:
            lines.append(f"标签：{', '.join(tags)}")

        try:
            preview = self._unwrap_bytes(
                meme.generate_preview(), f"generate preview for {meme.key}"
            )
        except Exception as e:
            logger.warning(f"meme {keyword} 预览图生成失败: {e}")
            preview = b""
        return "\n".join(lines), preview

    async def generate_meme(
        self,
        event: AstrMessageEvent,
        keyword: str,
        force_sender_as_target: bool = False,
        protected_user_id: str | None = None,
    ) -> bytes | None:
        meme = self.find_meme(keyword)
        if not meme:
            logger.warning(f"未找到meme关键词: {keyword}")
            return None

        params = self._get_params(meme)
        images, texts, options = await self.collect.collect_params(
            event, params, force_sender_as_target, protected_user_id
        )

        try:
            if self.is_py_version:
                meme_images = [data for _, data in images]
                result = await self.run_sync(meme)(
                    images=meme_images, texts=texts, args=options
                )
                return self._unwrap_bytes(result, f"generate meme {meme.key}")

            meme_images = [
                self.MemeImage(name=str(name), data=data) for name, data in images
            ]
            result = await asyncio.to_thread(meme.generate, meme_images, texts, options)
            return self._unwrap_bytes(result, f"generate meme {meme.key}")
        except Exception as e:
            logger.error(f"生成meme {keyword} 失败: {e}")
            return None
