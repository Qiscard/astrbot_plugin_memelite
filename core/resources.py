from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import aiohttp

try:
    from astrbot import logger
except Exception:  # pragma: no cover - offline tooling
    import logging
    logger = logging.getLogger('astrbot_plugin_memelite')

DEFAULT_RELEASE_TAG = "assets-v1"
DEFAULT_REPO = "Qiscard/astrbot_plugin_memelite"
DEFAULT_GITEE_REPO = "qiscard/astrbot_plugin_memelite"

# GitHub release asset names
MEMES_ASSET_NAME = "memes.zip"
FONTS_ASSET_NAME = "fonts.zip"


def get_plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_meme_package_dir() -> Path | None:
    try:
        import meme_generator

        return Path(meme_generator.__file__).resolve().parent
    except Exception:
        return None


def get_memes_target_dir() -> Path | None:
    pkg = get_meme_package_dir()
    return (pkg / "memes") if pkg else None


def get_user_fonts_dir() -> Path:
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        # Per-user fonts (Windows 10+)
        return Path(local) / "Microsoft" / "Windows" / "Fonts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Fonts"
    # Linux / Docker
    return Path.home() / ".local" / "share" / "fonts" / "meme-generator"


def count_meme_image_files(memes_dir: Path | None) -> int:
    if not memes_dir or not memes_dir.is_dir():
        return 0
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    return sum(1 for p in memes_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def count_installed_meme_fonts(fonts_dir: Path | None = None) -> int:
    fonts_dir = fonts_dir or get_user_fonts_dir()
    if not fonts_dir.is_dir():
        return 0
    exts = {".ttf", ".otf", ".ttc"}
    return sum(1 for p in fonts_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)


def _release_asset_urls(repo: str, tag: str, filename: str) -> list[str]:
    """Prefer multiple mirrors for GitHub release assets."""
    base = f"https://github.com/{repo}/releases/download/{tag}/{filename}"
    return [
        base,
        f"https://mirror.ghproxy.com/{base}",
        f"https://ghproxy.net/{base}",
        f"https://gitdl.cn/{base}",
    ]


def _gitee_release_asset_urls(repo: str, tag: str, filename: str) -> list[str]:
    """Gitee release asset download URLs for domestic mirror channel."""
    return [
        f"https://gitee.com/{repo}/releases/download/{tag}/{filename}",
    ]


async def _download_to_path(
    session: aiohttp.ClientSession,
    urls: list[str],
    dest: Path,
    progress_cb: Callable[[str], None] | None = None,
) -> None:
    last_error: Exception | None = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            if progress_cb:
                progress_cb(f"尝试下载: {url}")
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    last_error = RuntimeError(f"HTTP {resp.status} for {url}")
                    continue
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        f.write(chunk)
                        downloaded += len(chunk)
                tmp.replace(dest)
                if progress_cb:
                    mb = downloaded / 1024 / 1024
                    msg = f"下载完成: {mb:.1f} MB"
                    if total:
                        msg += f" / {total/1024/1024:.1f} MB"
                    progress_cb(msg)
                return
        except Exception as exc:
            last_error = exc
            logger.warning(f"下载失败 {url}: {exc}")
    raise RuntimeError(f"所有镜像下载失败: {last_error}")


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> int:
    """Extract zip into target_dir. Returns number of files written."""
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            # zip-slip guard
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            # strip a single top-level memes/ or fonts/ prefix if present
            parts = name.split("/")
            if parts and parts[0].lower() in {"memes", "fonts", "resources"}:
                # keep nested structure under that prefix stripped once
                if parts[0].lower() == "resources" and len(parts) > 1 and parts[1].lower() == "fonts":
                    rel = "/".join(parts[2:])
                else:
                    rel = "/".join(parts[1:])
            else:
                rel = name
            if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                continue
            out_path = target_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _copy_tree_files(src: Path, dest: Path) -> int:
    if not src.is_dir():
        raise FileNotFoundError(f"源目录不存在: {src}")
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() == "memes.rar":
            continue
        rel = path.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        count += 1
    return count


def _refresh_font_cache() -> str | None:
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        fc = shutil.which("fc-cache")
        if not fc:
            return "未找到 fc-cache，请安装 fontconfig 后手动执行: fc-cache -fv"
        try:
            subprocess_run = __import__("subprocess").run
            subprocess_run([fc, "-fv"], capture_output=True, text=True, timeout=120, check=False)
            return None
        except Exception as exc:
            return f"fc-cache 执行失败: {exc}"
    return None


class ResourceInstaller:
    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        release_tag: str = DEFAULT_RELEASE_TAG,
        gitee_repo: str = DEFAULT_GITEE_REPO,
        gitee_release_tag: str = DEFAULT_RELEASE_TAG,
        gitee_memes_url: str = "",
        gitee_fonts_url: str = "",
        memes_url: str = "",
        fonts_url: str = "",
        local_memes_dir: str = "",
        local_fonts_dir: str = "",
        local_memes_zip: str = "",
        local_fonts_zip: str = "",
    ):
        self.repo = repo or DEFAULT_REPO
        self.release_tag = release_tag or DEFAULT_RELEASE_TAG
        self.gitee_repo = gitee_repo or DEFAULT_GITEE_REPO
        self.gitee_release_tag = gitee_release_tag or DEFAULT_RELEASE_TAG
        self.gitee_memes_url = (gitee_memes_url or "").strip()
        self.gitee_fonts_url = (gitee_fonts_url or "").strip()
        self.memes_url = (memes_url or "").strip()
        self.fonts_url = (fonts_url or "").strip()
        self.local_memes_dir = (local_memes_dir or "").strip()
        self.local_fonts_dir = (local_fonts_dir or "").strip()
        self.local_memes_zip = (local_memes_zip or "").strip()
        self.local_fonts_zip = (local_fonts_zip or "").strip()
        self._session: aiohttp.ClientSession | None = None
        self._memes_lock = asyncio.Lock()
        self._fonts_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "astrbot_plugin_memelite/resource-installer"}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def status_text(self) -> str:
        memes_dir = get_memes_target_dir()
        fonts_dir = get_user_fonts_dir()
        img_count = count_meme_image_files(memes_dir)
        font_count = count_installed_meme_fonts(fonts_dir)
        lines = [
            "资源状态：",
            f"- meme_generator: {'已安装' if get_meme_package_dir() else '未安装'}",
            f"- 表情图片目录: {memes_dir or '不可用'}",
            f"- 表情图片文件数: {img_count}",
            f"- 字体安装目录: {fonts_dir}",
            f"- 字体文件数: {font_count}",
            f"- 资源版本标签: {self.release_tag}",
            f"- 资源仓库(GitHub): {self.repo}",
            f"- Gitee 资源仓库: {self.gitee_repo}",
            f"- Gitee 资源标签: {self.gitee_release_tag}",
        ]
        if img_count < 50:
            lines.append("- 表情资源可能未完整安装，请执行 /meme表情修复 或 /meme表情修复2(Gitee)")
        if font_count < 3:
            lines.append("- 字体可能未安装，请执行 /meme字体修复 或 /meme字体修复2(Gitee)")
        return "\n".join(lines)

    async def fix_memes(self, progress_cb: Callable[[str], None] | None = None) -> str:
        async with self._memes_lock:
            return await self._fix_memes_unlocked(progress_cb, source="github")

    async def fix_fonts(self, progress_cb: Callable[[str], None] | None = None) -> str:
        async with self._fonts_lock:
            return await self._fix_fonts_unlocked(progress_cb, source="github")

    async def fix_memes_gitee(self, progress_cb: Callable[[str], None] | None = None) -> str:
        """Install meme images from Gitee Release (domestic channel)."""
        async with self._memes_lock:
            return await self._fix_memes_unlocked(progress_cb, source="gitee")

    async def fix_fonts_gitee(self, progress_cb: Callable[[str], None] | None = None) -> str:
        """Install fonts from Gitee Release (domestic channel)."""
        async with self._fonts_lock:
            return await self._fix_fonts_unlocked(progress_cb, source="gitee")

    async def _fix_memes_unlocked(
        self, progress_cb: Callable[[str], None] | None = None, source: str = "github"
    ) -> str:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        target = get_memes_target_dir()
        if not target:
            return (
                "未找到 meme_generator 安装路径。请先安装依赖：\n"
                'pip install "meme_generator>=0.1.14,<0.2.0"'
            )

        # 1) local dir
        if self.local_memes_dir:
            src = Path(self.local_memes_dir)
            if src.is_dir():
                log(f"从本地目录同步表情: {src}")
                n = await asyncio.to_thread(_copy_tree_files, src, target)
                return f"表情修复完成（本地目录）\n写入文件: {n}\n目标: {target}"

        # 2) local zip
        zip_path: Path | None = None
        cleanup = False
        try:
            if self.local_memes_zip and Path(self.local_memes_zip).is_file():
                zip_path = Path(self.local_memes_zip)
                log(f"使用本地压缩包: {zip_path}")
            else:
                # bundled optional
                bundled = get_plugin_root() / "assets" / "memes.zip"
                if bundled.is_file():
                    zip_path = bundled
                    log(f"使用插件内置压缩包: {zip_path}")
                else:
                    channel = "Gitee" if source == "gitee" else "GitHub"
                    log(f"开始从 {channel} 下载表情资源包 memes.zip ...")
                    tmp_dir = Path(tempfile.mkdtemp(prefix="meme_assets_"))
                    zip_path = tmp_dir / MEMES_ASSET_NAME
                    cleanup = True
                    if source == "gitee":
                        urls = (
                            [self.gitee_memes_url]
                            if self.gitee_memes_url
                            else _gitee_release_asset_urls(
                                self.gitee_repo, self.gitee_release_tag, MEMES_ASSET_NAME
                            )
                        )
                    else:
                        urls = (
                            [self.memes_url]
                            if self.memes_url
                            else _release_asset_urls(
                                self.repo, self.release_tag, MEMES_ASSET_NAME
                            )
                        )
                    session = await self._get_session()
                    await _download_to_path(session, urls, zip_path, progress_cb=log)

            channel_note = "（Gitee）" if source == "gitee" else ""
            log(f"解压表情资源到: {target}")
            n = await asyncio.to_thread(_safe_extract_zip, zip_path, target)
            img_count = count_meme_image_files(target)
            return (
                f"表情修复完成{channel_note}\n"
                f"解压文件: {n}\n"
                f"图片文件数: {img_count}\n"
                f"目标: {target}"
            )
        except Exception as exc:
            logger.exception("表情修复失败")
            return f"表情修复失败: {exc}"
        finally:
            if cleanup and zip_path:
                try:
                    shutil.rmtree(zip_path.parent, ignore_errors=True)
                except Exception:
                    pass

    async def _fix_fonts_unlocked(
        self, progress_cb: Callable[[str], None] | None = None, source: str = "github"
    ) -> str:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        target = get_user_fonts_dir()
        target.mkdir(parents=True, exist_ok=True)

        if self.local_fonts_dir:
            src = Path(self.local_fonts_dir)
            if src.is_dir():
                log(f"从本地目录同步字体: {src}")
                n = await asyncio.to_thread(_copy_tree_files, src, target)
                cache_msg = await asyncio.to_thread(_refresh_font_cache)
                extra = f"\n{cache_msg}" if cache_msg else "\n已刷新字体缓存"
                return f"字体修复完成（本地目录）\n写入文件: {n}\n目标: {target}{extra}"

        zip_path: Path | None = None
        cleanup = False
        try:
            if self.local_fonts_zip and Path(self.local_fonts_zip).is_file():
                zip_path = Path(self.local_fonts_zip)
                log(f"使用本地字体包: {zip_path}")
            else:
                bundled = get_plugin_root() / "assets" / "fonts.zip"
                if bundled.is_file():
                    zip_path = bundled
                    log(f"使用插件内置字体包: {zip_path}")
                else:
                    channel = "Gitee" if source == "gitee" else "GitHub"
                    log(f"开始从 {channel} 下载字体资源包 fonts.zip ...")
                    tmp_dir = Path(tempfile.mkdtemp(prefix="meme_fonts_"))
                    zip_path = tmp_dir / FONTS_ASSET_NAME
                    cleanup = True
                    if source == "gitee":
                        urls = (
                            [self.gitee_fonts_url]
                            if self.gitee_fonts_url
                            else _gitee_release_asset_urls(
                                self.gitee_repo, self.gitee_release_tag, FONTS_ASSET_NAME
                            )
                        )
                    else:
                        urls = (
                            [self.fonts_url]
                            if self.fonts_url
                            else _release_asset_urls(
                                self.repo, self.release_tag, FONTS_ASSET_NAME
                            )
                        )
                    session = await self._get_session()
                    await _download_to_path(session, urls, zip_path, progress_cb=log)

            channel_note = "（Gitee）" if source == "gitee" else ""
            log(f"安装字体到: {target}")
            n = await asyncio.to_thread(_safe_extract_zip, zip_path, target)
            cache_msg = await asyncio.to_thread(_refresh_font_cache)
            font_count = count_installed_meme_fonts(target)
            extra = f"\n{cache_msg}" if cache_msg else "\n已刷新字体缓存（如适用）"
            if sys.platform.startswith("win"):
                extra += (
                    "\nWindows 提示: 若部分字体仍未生效，可打开字体目录手动安装，"
                    f"或重启 AstrBot。目录: {target}"
                )
            return (
                f"字体修复完成{channel_note}\n"
                f"解压文件: {n}\n"
                f"字体文件数: {font_count}\n"
                f"目标: {target}{extra}"
            )
        except Exception as exc:
            logger.exception("字体修复失败")
            return f"字体修复失败: {exc}"
        finally:
            if cleanup and zip_path:
                try:
                    shutil.rmtree(zip_path.parent, ignore_errors=True)
                except Exception:
                    pass
