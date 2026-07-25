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

    @staticmethod
    def _meme_sort_name(meme: Any) -> str:
        """Name used for list sorting: first keyword (display), else key."""
        info = getattr(meme, "info", None)
        if info is not None and hasattr(info, "keywords"):
            kws = [str(x).strip() for x in list(info.keywords or []) if str(x).strip()]
        else:
            kws = [str(x).strip() for x in list(getattr(meme, "keywords", []) or []) if str(x).strip()]
        if kws:
            return kws[0]
        return str(getattr(meme, "key", "") or "").strip()

    @classmethod
    def _meme_sort_key(cls, meme: Any) -> tuple[int, str, str]:
        """Sort by first char category: 0-9 → a-z → symbols/other.

        Only ordering changes; pagination / labels / layout stay the same.
        """
        raw = cls._meme_sort_name(meme)
        name = raw.casefold() if hasattr(raw, "casefold") else raw.lower()
        key = str(getattr(meme, "key", "") or "").casefold()
        if not name:
            return (3, "", key)
        ch = name[0]
        if ch.isdigit():
            cat = 0
        elif "a" <= ch <= "z":
            cat = 1
        else:
            # Chinese, punctuation, emoji, fullwidth, etc.
            cat = 2
        return (cat, name, key)

    def sorted_memes(self) -> list[Any]:
        if not self._ensure_memes_loaded():
            return []
        return sorted(self.memes, key=self._meme_sort_key)

    def reload_memes(self) -> str:
        """Hot-reload meme modules from disk without restarting the framework.

        meme_generator keeps an in-process registry (_memes). New folders under
        site-packages/meme_generator/memes are not visible until reloaded.
        """
        if not MEME_GENERATOR_AVAILABLE:
            return "meme_generator 不可用，无法热重载"

        try:
            import importlib
            import sys
            from pathlib import Path

            import meme_generator
            from meme_generator import load_meme, load_memes
            from meme_generator.config import meme_config
            import meme_generator.manager as meme_manager

            before = {getattr(m, "key", None) for m in list(get_memes())}
            before.discard(None)

            importlib.invalidate_caches()

            stale = [
                name
                for name in list(sys.modules)
                if name == "meme_generator.memes"
                or name.startswith("meme_generator.memes.")
            ]
            for name in stale:
                sys.modules.pop(name, None)

            if hasattr(meme_manager, "_memes") and isinstance(meme_manager._memes, dict):
                meme_manager._memes.clear()

            memes_root = Path(meme_generator.__file__).resolve().parent / "memes"
            loaded = 0
            if getattr(getattr(meme_config, "meme", None), "load_builtin_memes", True):
                if memes_root.is_dir():
                    for path in sorted(memes_root.iterdir(), key=lambda p: p.name.lower()):
                        if not path.is_dir() or path.name.startswith("_"):
                            continue
                        try:
                            load_meme(f"meme_generator.memes.{path.name}")
                            loaded += 1
                        except Exception as exc:
                            logger.warning("热重载表情失败 %s: %s", path.name, exc)

            extra_dirs = list(getattr(getattr(meme_config, "meme", None), "meme_dirs", []) or [])
            for meme_dir in extra_dirs:
                try:
                    load_memes(meme_dir)
                except Exception as exc:
                    logger.warning("热重载扩展目录失败 %s: %s", meme_dir, exc)

            self._load_memes()
            after = {getattr(m, "key", None) for m in self.memes}
            after.discard(None)
            added = sorted(after - before)
            removed = sorted(before - after)
            msg = (
                f"表情热重载完成：扫描 {loaded} 个目录，当前 {len(after)} 个"
                f"（新增 {len(added)}，移除 {len(removed)}）"
            )
            if added:
                preview = ", ".join(added[:50]) + ("..." if len(added) > 50 else "")
                logger.info("热重载新增表情: %s", preview)
            if removed:
                preview = ", ".join(removed[:50]) + ("..." if len(removed) > 50 else "")
                logger.info("热重载移除表情: %s", preview)
            logger.info(msg)
            return msg
        except Exception as exc:
            logger.exception("表情热重载失败")
            self._load_memes()
            return f"表情热重载失败，已回退普通加载: {exc}"


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
        """Optional legacy online resource check from upstream meme-generator.

        Preferred path is plugin commands:
        - /meme表情修复
        - /meme字体修复
        """
        if not MEME_GENERATOR_AVAILABLE:
            logger.error(
                "meme-generator 不可用，跳过资源检查。"
                "请先修复环境依赖（/meme检查），并安装: "
                "pip install 'meme_generator>=0.1.14,<0.2.0'"
            )
            return

        if not self.conf.get("is_check_resources", False):
            self._load_memes()
            return

        if not self.check_resources_func:
            self._load_memes()
            return

        logger.info("开始检查 memes 官方在线资源(is_check_resources=true)...")
        try:
            if self.is_py_version:
                await self.check_resources_func()
            else:
                await asyncio.to_thread(self.check_resources_func)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                f"检查memes资源失败: {e}。也可改用管理员命令 /meme表情修复 安装本仓库资源包"
            )
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

    def _meme_kind(self, meme: Any) -> str:
        """image if needs images, else text."""
        try:
            params = self._get_params(meme)
            max_images = int(getattr(params, "max_images", 0) or 0)
            if max_images > 0:
                return "image"
        except Exception:
            pass
        return "text"

    def _list_style(self) -> str:
        style = str(self.conf.get("meme_list_style") or "standard").strip().lower()
        if style in {"compact", "dense", "4", "四列", "紧凑"}:
            return "compact"
        return "standard"

    def _list_page_size(self, style: str) -> int:
        raw = self.conf.get("meme_list_page_size")
        try:
            value = int(raw)
        except Exception:
            value = 0
        if value > 0:
            return max(12, min(1000, value))
        # standard: 3x30=90; compact: 4x40=160
        return 160 if style == "compact" else 90

    def _build_example(self, meme, keywords: list[str]) -> str:
        """Standard-mode secondary line.

        Format: 拍 @[图x2] <字>
        - [] optional params
        - <> required params
        """
        kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        if not kws:
            key = str(getattr(meme, "key", "") or "").strip()
            kws = [key] if key else ["?"]

        extra = str(self.conf.get("extra_prefix") or "").strip()
        trigger = f"{extra}{kws[0]}" if extra else kws[0]
        parts: list[str] = [trigger]

        try:
            params = self._get_params(meme)
            min_images = int(getattr(params, "min_images", 0) or 0)
            max_images = int(getattr(params, "max_images", 0) or 0)
            min_texts = int(getattr(params, "min_texts", 0) or 0)
            max_texts = int(getattr(params, "max_texts", 0) or 0)

            if max_images > 0:
                if min_images <= 0:
                    # optional images
                    parts.append(f"@[图x{max_images}]" if max_images > 1 else "@[图]")
                elif min_images == max_images:
                    parts.append(f"<@图x{min_images}>" if min_images > 1 else "<@图>")
                else:
                    parts.append(f"<@图{min_images}-{max_images}>")

            if max_texts > 0:
                if min_texts <= 0:
                    parts.append(f"[字x{max_texts}]" if max_texts > 1 else "[字]")
                elif min_texts == max_texts:
                    parts.append(f"<字x{min_texts}>" if min_texts > 1 else "<字>")
                else:
                    parts.append(f"<字{min_texts}-{max_texts}>")
        except Exception:
            pass

        return " ".join(parts)

    def build_list_items(self) -> list[Any]:
        """Build sorted list items with usage labels for custom renderer."""
        from .list_render import ListItem
        from .usage import compute_labels, ensure_keys, get_counts

        memes = self.sorted_memes()
        if not memes:
            return []

        keys = [str(getattr(m, "key", "") or "") for m in memes]
        ensure_keys([k for k in keys if k])
        counts = get_counts()

        try:
            new_days = int(self.conf.get("meme_new_days", 3) or 0)
        except Exception:
            new_days = 3
        try:
            hot_min = int(self.conf.get("meme_hot_min_count", 3) or 0)
        except Exception:
            hot_min = 3
        # 0 = disable corresponding label
        labels_map = compute_labels(
            [k for k in keys if k],
            new_days=max(0, new_days),
            hot_min_count=max(0, hot_min),
            hot_top_n=0,  # 0 means no top-N limit; pure threshold
        )

        items: list[ListItem] = []
        for meme in memes:
            key = str(getattr(meme, "key", "") or "")
            kws = self._get_keywords(meme)
            items.append(
                ListItem(
                    key=key or (kws[0] if kws else "unknown"),
                    keywords=list(kws),
                    kind=self._meme_kind(meme),
                    labels=list(labels_map.get(key, [])),
                    count=int(counts.get(key, 0) or 0),
                    example=self._build_example(meme, list(kws)),
                )
            )
        return items

    async def render_meme_list_images(self) -> list[bytes]:
        """Render one or more list images: name-sorted, paginated, with new/hot."""
        items = self.build_list_items()
        if not items:
            return []

        style = self._list_style()
        page_size = self._list_page_size(style)
        try:
            from .list_render import render_meme_list_images
            from .usage import usage_summary_text

            try:
                from .resources import list_pack_footer_text

                footer_text = list_pack_footer_text()
            except Exception:
                footer_text = ""
            images = await asyncio.to_thread(
                render_meme_list_images,
                items,
                style=style,
                page_size=page_size,
                title="meme表情列表",
                footer_text=footer_text,
            )
            logger.info(
                "自定义表情列表渲染完成: style=%s page_size=%s pages=%s items=%s\n%s",
                style,
                page_size,
                len(images),
                len(items),
                usage_summary_text(),
            )
            return images
        except Exception as e:
            logger.error("自定义表情列表渲染失败，尝试官方渲染: %s", e)

        # fallback: official single image, still sorted
        one = await self.render_meme_list_image_official()
        return [one] if one else []

    async def render_meme_list_image(self) -> bytes | None:
        """Backward-compatible single image API (first page)."""
        images = await self.render_meme_list_images()
        return images[0] if images else None

    async def render_meme_list_image_official(self) -> bytes | None:
        """Official meme-generator list renderer fallback."""
        memes = self.sorted_memes()
        if not memes or not self.render_meme_list_func:
            return None
        try:
            from .usage import compute_labels

            keys = [str(getattr(m, "key", "") or "") for m in memes]
            labels_map = compute_labels(
                [k for k in keys if k],
                new_days=int(self.conf.get("meme_new_days", 3) or 0),
                hot_min_count=int(self.conf.get("meme_hot_min_count", 3) or 0),
                hot_top_n=0,
            )

            if self.is_py_version:
                meme_list = []
                for m in memes:
                    key = str(getattr(m, "key", "") or "")
                    labs = labels_map.get(key, [])
                    # official only accepts new/hot
                    labs2 = [x for x in labs if x in ("new", "hot")]
                    meme_list.append((m, LegacyMemeProperties(labels=labs2)))  # type: ignore[arg-type]
                rendered = self.render_meme_list_func(
                    meme_list=meme_list,  # type: ignore[arg-type]
                    text_template="{index}.{keywords}",
                    add_category_icon=True,
                )
                return self._unwrap_bytes(rendered, "render meme list")

            meme_props = {}
            for m in memes:
                key = str(getattr(m, "key", "") or "")
                labs = [x for x in labels_map.get(key, []) if x in ("new", "hot")]
                try:
                    meme_props[m.key] = self.MemePropertiesType(labels=labs)
                except TypeError:
                    prop = self.MemePropertiesType()
                    if hasattr(prop, "labels"):
                        try:
                            prop.labels = labs
                        except Exception:
                            pass
                    meme_props[m.key] = prop
            kwargs = dict(
                meme_properties=meme_props,
                exclude_memes=[],
                sort_reverse=False,
                text_template="{index}. {keywords}",
                add_category_icon=True,
            )
            if self.MemeSortBy is not None:
                for attr in ("Key", "Keywords", "KeywordsPinyin", "Default"):
                    if hasattr(self.MemeSortBy, attr):
                        kwargs["sort_by"] = getattr(self.MemeSortBy, attr)
                        break
            rendered = await asyncio.to_thread(self.render_meme_list_func, **kwargs)
            return self._unwrap_bytes(rendered, "render meme list")
        except Exception as e:
            logger.error(f"渲染meme列表图片失败: {e}")
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
                data = self._unwrap_bytes(result, f"generate meme {meme.key}")
            else:
                meme_images = [
                    self.MemeImage(name=str(name), data=data) for name, data in images
                ]
                result = await asyncio.to_thread(meme.generate, meme_images, texts, options)
                data = self._unwrap_bytes(result, f"generate meme {meme.key}")

            # usage counted at trigger time in main.meme_handle (global)
            return data
        except Exception as e:
            logger.error(f"生成meme {keyword} 失败: {e}")
            return None
