from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp

try:
    from astrbot import logger
except Exception:  # pragma: no cover - offline tooling
    import logging

    logger = logging.getLogger("astrbot_plugin_memelite")

# Built-in defaults (no user-facing repo/tag config)
DEFAULT_RELEASE_TAG = "assets-v1"
DEFAULT_GITHUB_REPO = "Qiscard/astrbot_plugin_memelite"
DEFAULT_GITEE_REPO = "qiscard/astrbot_plugin_memelite"

MEMES_ASSET_NAME = "memes.zip"
FONTS_ASSET_NAME = "fonts.zip"
MEMES_PARTS_MANIFEST = "memes.parts.txt"
DEFAULT_SOURCE_ID = "default"
INDEX_VERSION = 2
PLUGIN_DATA_NAME = "astrbot_plugin_memelite"
DEFAULT_DOWNLOAD_TIMEOUT = 180

# Align with AstrBot dashboard ProxySelector + common fallbacks.
# Format used by AstrBot: {proxy}/{https://github.com/...}
ASTRBOT_GITHUB_PROXIES = [
    "https://edgeone.gh-proxy.com",
    "https://hk.gh-proxy.com",
    "https://gh-proxy.com",
    "https://gh.dpik.top",
]
LEGACY_GITHUB_PROXIES = [
    "https://mirror.ghproxy.com",
    "https://ghproxy.net",
    "https://gitdl.cn",
]
GITHUB_PROXY_PROBE_PATH = (
    "https://github.com/AstrBotDevs/AstrBot/raw/refs/heads/master/.python-version"
)
GITHUB_PROXY_CACHE_TTL = 12 * 3600  # seconds
GITHUB_PROXY_PROBE_TIMEOUT = 8

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
FONT_EXTS = {".ttf", ".otf", ".ttc"}
LOGIC_EXTS = {".py", ".json", ".toml", ".yaml", ".yml"}
WRAPPER_DIR_NAMES = {
    "memes",
    "fonts",
    "resources",
    "resource",
    "assets",
    "data",
    "dist",
    "output",
    "package",
    "meme",
    "meme-generator",
    "meme_generator",
    "images",
    "img",
    "static",
}


def get_plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_framework_plugin_data_dir() -> Path:
    """AstrBot: <root>/data/plugin_data/astrbot_plugin_memelite"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        base = Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_NAME
    except Exception:
        plugin = get_plugin_root()
        if plugin.parent.name == "plugins" and plugin.parent.parent.name == "data":
            base = plugin.parent.parent / "plugin_data" / PLUGIN_DATA_NAME
        else:
            base = plugin / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_data_layout(root: Path) -> None:
    for name in ("packages", "cache", "temp", "index"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _migrate_legacy_data(new_root: Path) -> None:
    legacy = get_plugin_root() / "data"
    if not legacy.is_dir() or legacy.resolve() == new_root.resolve():
        return
    try:
        old_index = legacy / "resource_index.json"
        new_index = new_root / "index" / "resource_index.json"
        if old_index.is_file() and not new_index.is_file():
            shutil.copy2(old_index, new_index)
        old_cache = legacy / "cache"
        if old_cache.is_dir():
            for f in old_cache.glob("*.zip"):
                dest = new_root / "packages" / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
        marker = legacy / ".migrated_to_plugin_data"
        if not marker.exists():
            marker.write_text(str(new_root), encoding="utf-8")
    except Exception as exc:
        logger.warning("迁移旧 data 目录失败: %s", exc)


def get_plugin_data_dir() -> Path:
    path = get_framework_plugin_data_dir()
    _ensure_data_layout(path)
    _migrate_legacy_data(path)
    return path


def get_resource_index_path() -> Path:
    return get_plugin_data_dir() / "index" / "resource_index.json"


def get_packages_dir() -> Path:
    path = get_plugin_data_dir() / "packages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    path = get_plugin_data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_temp_dir() -> Path:
    path = get_plugin_data_dir() / "temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_package_zip_path(source_id: str) -> Path:
    """Backward-compatible default package path (.zip)."""
    return get_package_path(source_id, ".zip")


def get_package_path(source_id: str, ext: str = ".zip") -> Path:
    if not ext.startswith("."):
        ext = "." + ext
    return get_packages_dir() / f"{source_id}{ext}"


def package_ext_from_url(url: str) -> str:
    path = (urlparse(url).path or "").lower()
    if path.endswith(".tar.gz") or path.endswith(".tgz"):
        return ".tar.gz"
    if path.endswith(".tar"):
        return ".tar"
    if path.endswith(".zip"):
        return ".zip"
    return ".zip"


def package_candidates(source_id: str) -> list[Path]:
    d = get_packages_dir()
    return [
        d / f"{source_id}.zip",
        d / f"{source_id}.tar.gz",
        d / f"{source_id}.tgz",
        d / f"{source_id}.tar",
    ]


def is_supported_archive(path: Path) -> bool:
    return detect_archive_format(path) in {"zip", "tar.gz", "tar", "tgz"}


def clamp_download_timeout(value, default: int = DEFAULT_DOWNLOAD_TIMEOUT) -> int:
    try:
        n = int(value)
    except Exception:
        n = default
    return max(30, min(300, n))


def is_github_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host in {"github.com", "www.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com"}


def normalize_github_proxy(proxy: str | None) -> str:
    proxy = (proxy or "").strip().rstrip("/")
    if not proxy:
        return ""
    if not proxy.startswith("http://") and not proxy.startswith("https://"):
        proxy = "https://" + proxy
    return proxy.rstrip("/")


def apply_github_proxy(url: str, proxy: str | None) -> str:
    """Wrap a GitHub URL with AstrBot-style proxy prefix."""
    proxy = normalize_github_proxy(proxy)
    if not proxy or not url:
        return url
    if url.startswith(proxy + "/"):
        return url
    # already proxied by another known prefix
    return f"{proxy}/{url}"


def get_github_proxy_cache_path() -> Path:
    return get_plugin_data_dir() / "index" / "github_proxy_rank.json"


def load_github_proxy_rank() -> dict[str, Any]:
    path = get_github_proxy_cache_path()
    if not path.is_file():
        return {"updated_at": 0, "proxies": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"updated_at": 0, "proxies": []}
        data.setdefault("updated_at", 0)
        data.setdefault("proxies", [])
        return data
    except Exception:
        return {"updated_at": 0, "proxies": []}


def save_github_proxy_rank(rank: dict[str, Any]) -> None:
    path = get_github_proxy_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rank, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def probe_github_proxy_latency(
    session: aiohttp.ClientSession,
    proxy: str,
    timeout_sec: float = GITHUB_PROXY_PROBE_TIMEOUT,
) -> float | None:
    """Return latency ms if proxy can fetch AstrBot probe file, else None.

    Uses the same probe target as AstrBot dashboard /stats/ghproxy/test.
    """
    proxy = normalize_github_proxy(proxy)
    if not proxy:
        return None
    test_url = f"{proxy}/{GITHUB_PROXY_PROBE_PATH}"
    started = time.monotonic()
    try:
        async with session.get(
            test_url,
            timeout=aiohttp.ClientTimeout(total=timeout_sec, connect=min(5, timeout_sec)),
        ) as resp:
            if resp.status != 200:
                return None
            await resp.read()
            return round((time.monotonic() - started) * 1000, 2)
    except Exception:
        return None


async def rank_github_proxies(
    session: aiohttp.ClientSession,
    proxies: list[str] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    proxies = [normalize_github_proxy(p) for p in (proxies or (ASTRBOT_GITHUB_PROXIES + LEGACY_GITHUB_PROXIES))]
    # de-dup keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for p in proxies:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)

    if progress_cb:
        progress_cb(f"开始 GitHub 代理测速，共 {len(uniq)} 个源 ...")

    async def one(proxy: str) -> dict[str, Any]:
        latency = await probe_github_proxy_latency(session, proxy)
        return {
            "proxy": proxy,
            "available": latency is not None,
            "latency": latency if latency is not None else None,
        }

    results = await asyncio.gather(*[one(p) for p in uniq])
    ranked = sorted(
        results,
        key=lambda x: (0 if x.get("available") else 1, x.get("latency") if x.get("latency") is not None else 1e12),
    )
    if progress_cb:
        for item in ranked:
            if item.get("available"):
                progress_cb(f"代理可用: {item['proxy']}  延迟 {item['latency']} ms")
            else:
                progress_cb(f"代理不可用: {item['proxy']}")
    save_github_proxy_rank({"updated_at": int(time.time()), "proxies": ranked})
    return ranked


def get_selected_github_proxy() -> str:
    """Auto-selected best proxy from last probe (not panel custom)."""
    cache = load_github_proxy_rank()
    selected = normalize_github_proxy(str(cache.get("selected") or ""))
    if selected:
        return selected
    for item in cache.get("proxies") or []:
        if item.get("available") and item.get("proxy"):
            return normalize_github_proxy(str(item.get("proxy")))
    return ""


def set_selected_github_proxy(proxy: str) -> None:
    cache = load_github_proxy_rank()
    cache["selected"] = normalize_github_proxy(proxy)
    cache["updated_at"] = int(cache.get("updated_at") or time.time())
    # keep proxies list if present
    cache.setdefault("proxies", cache.get("proxies") or [])
    save_github_proxy_rank(cache)


def expand_urls_with_github_proxies(
    urls: list[str],
    *,
    ranked_proxies: list[str] | None = None,
    fixed_proxy: str = "",
    use_proxy: bool = True,
    force_proxy: bool = True,
) -> list[str]:
    """Expand download candidates.

    - Non-GitHub (e.g. Gitee): unchanged, never proxied.
    - GitHub + use_proxy: go through proxy only when force_proxy (default).
      Order: fixed_proxy (config) > selected best > ranked list.
      Direct github.com is omitted while force_proxy is on.
    - GitHub + use_proxy off: direct only.
    """
    if not urls:
        return []
    fixed_proxy = normalize_github_proxy(fixed_proxy)
    selected = get_selected_github_proxy()

    if ranked_proxies is None:
        cache = load_github_proxy_rank()
        ranked_proxies = [
            str(x.get("proxy"))
            for x in (cache.get("proxies") or [])
            if x.get("available") and x.get("proxy")
        ]
    ranked_proxies = [normalize_github_proxy(p) for p in (ranked_proxies or []) if p]
    if not ranked_proxies:
        ranked_proxies = list(ASTRBOT_GITHUB_PROXIES) + list(LEGACY_GITHUB_PROXIES)

    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    for url in urls:
        if not is_github_url(url):
            add(url)
            continue
        if not use_proxy:
            add(url)
            continue

        # force GitHub via proxy
        preferred: list[str] = []
        if fixed_proxy:
            preferred.append(fixed_proxy)
        if selected and selected not in preferred:
            preferred.append(selected)
        for p in ranked_proxies:
            if p and p not in preferred:
                preferred.append(p)

        for proxy in preferred:
            add(apply_github_proxy(url, proxy))

        # only allow direct GitHub when force_proxy is disabled
        if not force_proxy:
            add(url)
        elif not preferred:
            # no proxy available at all: last resort direct + caller should log
            add(url)
    return out


async def _download_to_path(
    session: aiohttp.ClientSession,
    urls: list[str],
    dest: Path,
    progress_cb: Callable[[str], None] | None = None,
    timeout_sec: int = DEFAULT_DOWNLOAD_TIMEOUT,
    *,
    proxy_enabled: bool | None = None,
    selected_proxy: str = "",
) -> None:
    """Download first successful URL into dest.

    timeout_sec is hard-capped to 30-300. On timeout/failure the .part cache is removed.
    """
    timeout_sec = clamp_download_timeout(timeout_sec)
    last_error: Exception | None = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    any_github = any(is_github_url(u) or "github.com" in u for u in urls)
    used_proxy = any(
        normalize_github_proxy(p) and normalize_github_proxy(p) in u
        for u in urls
        for p in (ASTRBOT_GITHUB_PROXIES + LEGACY_GITHUB_PROXIES + [selected_proxy])
        if p
    ) or any("/https://github.com/" in u or "/http://github.com/" in u for u in urls)

    def _timeout_hint(url: str) -> None:
        # No usable proxy configured/selected and GitHub download timed out
        no_proxy = not (proxy_enabled and (selected_proxy or get_selected_github_proxy() or used_proxy))
        if not no_proxy and proxy_enabled:
            # still hint when this particular attempt looks like direct github
            if is_github_url(url) and proxy_enabled:
                logger.warning(
                    "GitHub 直连下载超时: %s。当前强制代理已开启，将继续尝试其他代理源。",
                    url,
                )
                return
        if is_github_url(url) or "github.com" in url:
            if not proxy_enabled:
                logger.warning(
                    "GitHub 下载超时且未启用强制代理(use_github_proxy=false): %s。"
                    "可在配置面板开启「强制 GitHub 走代理」，或执行 /meme代理测速 后重试。",
                    url,
                )
            elif not (selected_proxy or get_selected_github_proxy()):
                logger.warning(
                    "GitHub 下载超时且尚未选择可用代理: %s。"
                    "请执行 /meme代理测速 自动选择最低延迟代理，或在配置面板填写 github_proxy。",
                    url,
                )

    for url in urls:
        tmp = dest.with_suffix(dest.suffix + ".part")
        _safe_unlink(tmp)
        try:
            if progress_cb:
                progress_cb(f"尝试下载: {url}（超时 {timeout_sec}s）")
            client_timeout = aiohttp.ClientTimeout(
                total=timeout_sec,
                connect=min(30, timeout_sec),
                sock_connect=min(30, timeout_sec),
                sock_read=timeout_sec,
            )
            started = time.monotonic()
            async with session.get(url, timeout=client_timeout) as resp:
                if resp.status != 200:
                    last_error = RuntimeError(f"HTTP {resp.status} for {url}")
                    continue
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with tmp.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        if time.monotonic() - started > timeout_sec:
                            raise asyncio.TimeoutError(
                                f"下载超过 {timeout_sec} 秒"
                            )
                        f.write(chunk)
                        downloaded += len(chunk)
                if downloaded <= 0:
                    raise RuntimeError(f"空响应: {url}")
                tmp.replace(dest)
                if progress_cb:
                    mb = downloaded / 1024 / 1024
                    msg = f"下载完成: {mb:.1f} MB"
                    if total:
                        msg += f" / {total / 1024 / 1024:.1f} MB"
                    progress_cb(msg)
                return
        except asyncio.TimeoutError as exc:
            last_error = RuntimeError(f"下载超时({timeout_sec}s): {url}")
            _safe_unlink(tmp)
            # also clear incomplete final dest if partially replaced somehow
            if dest.is_file() and dest.stat().st_size <= 0:
                _safe_unlink(dest)
            logger.warning("下载超时 %s: %s", url, exc)
            _timeout_hint(url)
            if progress_cb:
                progress_cb(str(last_error) + "，已清除缓存")
        except Exception as exc:
            last_error = exc
            _safe_unlink(tmp)
            logger.warning(f"下载失败 {url}: {exc}")
            if progress_cb:
                progress_cb(f"下载失败: {url} -> {exc}")
    raise RuntimeError(f"所有下载源失败: {last_error}")


class ResourceInstaller:
    def __init__(
        self,
        extra_meme_resource_urls=None,
        download_timeout=DEFAULT_DOWNLOAD_TIMEOUT,
        use_github_proxy: bool = True,
        github_proxy: str = "",
        proxy_probe_on_fix: bool = True,
    ):
        self.extra_meme_resource_urls = _normalize_url_list(extra_meme_resource_urls)
        self.download_timeout = clamp_download_timeout(download_timeout)
        self.use_github_proxy = bool(use_github_proxy)
        self.github_proxy = normalize_github_proxy(github_proxy)
        self.proxy_probe_on_fix = bool(proxy_probe_on_fix)
        self._ranked_proxies: list[str] | None = None
        self._selected_proxy: str = normalize_github_proxy(github_proxy) or get_selected_github_proxy()
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

    def _load_ranked_proxies_from_cache(self) -> list[str]:
        cache = load_github_proxy_rank()
        ranked = [
            str(x.get("proxy"))
            for x in (cache.get("proxies") or [])
            if x.get("available") and x.get("proxy")
        ]
        if ranked:
            self._ranked_proxies = ranked
        return ranked

    async def ensure_github_proxy_rank(
        self,
        progress_cb: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> list[str]:
        """Speed-test GitHub proxies and auto-select the lowest latency one.

        Config `github_proxy` (panel) overrides auto selection.
        Gitee links are never proxied.
        """
        if not self.use_github_proxy:
            self._ranked_proxies = []
            self._selected_proxy = ""
            if progress_cb:
                progress_cb("强制 GitHub 代理已关闭（配置 use_github_proxy=false）")
            return []

        # Panel custom proxy wins
        if self.github_proxy:
            self._ranked_proxies = [self.github_proxy]
            self._selected_proxy = self.github_proxy
            set_selected_github_proxy(self.github_proxy)
            if progress_cb:
                progress_cb(f"使用配置面板固定代理: {self.github_proxy}")
            return self._ranked_proxies

        cache = load_github_proxy_rank()
        age = int(time.time()) - int(cache.get("updated_at") or 0)
        cached = [
            str(x.get("proxy"))
            for x in (cache.get("proxies") or [])
            if x.get("available") and x.get("proxy")
        ]
        selected = normalize_github_proxy(str(cache.get("selected") or "")) or (cached[0] if cached else "")

        if not force and cached and selected and (age <= GITHUB_PROXY_CACHE_TTL or not self.proxy_probe_on_fix):
            self._ranked_proxies = cached
            self._selected_proxy = selected
            if progress_cb:
                progress_cb(f"使用已选 GitHub 代理: {selected}（缓存 {age}s）")
            return cached

        if not force and not self.proxy_probe_on_fix and cached:
            self._ranked_proxies = cached
            self._selected_proxy = selected or cached[0]
            if progress_cb:
                progress_cb(f"跳过测速，使用代理: {self._selected_proxy}")
            return cached

        session = await self._get_session()
        ranked = await rank_github_proxies(session, progress_cb=progress_cb)
        available = [str(x["proxy"]) for x in ranked if x.get("available")]
        if available:
            best = available[0]
            set_selected_github_proxy(best)
            # re-save full rank with selected
            cache = load_github_proxy_rank()
            cache["selected"] = best
            cache["proxies"] = ranked
            cache["updated_at"] = int(time.time())
            save_github_proxy_rank(cache)
            self._selected_proxy = best
            self._ranked_proxies = available
            if progress_cb:
                progress_cb(f"已自动选择最低延迟代理: {best}")
            return available

        self._ranked_proxies = list(ASTRBOT_GITHUB_PROXIES)
        self._selected_proxy = ""
        if progress_cb:
            progress_cb("未测得可用代理；GitHub 下载可能失败，请检查网络或在面板填写 github_proxy")
        logger.warning(
            "GitHub 代理测速无可用节点。未配置自定义代理时，强制代理模式下 GitHub 资源可能无法下载。"
            "请执行 /meme代理测速 重试，或在配置面板填写 github_proxy。"
        )
        return self._ranked_proxies

    def prepare_urls(self, urls: list[str]) -> list[str]:
        return expand_urls_with_github_proxies(
            urls,
            ranked_proxies=self._ranked_proxies,
            fixed_proxy=self.github_proxy,
            use_proxy=self.use_github_proxy,
            force_proxy=self.use_github_proxy,
        )

    async def _download_urls(
        self,
        session: aiohttp.ClientSession,
        urls: list[str],
        dest: Path,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        selected = self.github_proxy or getattr(self, "_selected_proxy", "") or get_selected_github_proxy()
        await _download_to_path(
            session,
            self.prepare_urls(urls),
            dest,
            progress_cb=progress_cb,
            timeout_sec=self.download_timeout,
            proxy_enabled=self.use_github_proxy,
            selected_proxy=selected,
        )

        return expand_urls_with_github_proxies(
            urls,
            ranked_proxies=self._ranked_proxies,
            fixed_proxy=self.github_proxy,
            use_proxy=self.use_github_proxy,
        )

    def _upsert_source(
        self,
        index: dict[str, Any],
        *,
        source_id: str,
        name: str,
        url: str,
        sha256: str,
        memes: list[str],
        status: str,
        note: str = "",
        file_count: int = 0,
        package_path: str = "",
    ) -> None:
        sources = index.setdefault("sources", {})
        pkg = package_path or str(get_package_zip_path(source_id))
        sources[source_id] = {
            "id": source_id,
            "name": name,
            "url": url,
            "sha256": sha256,
            "memes": sorted(set(memes), key=str.lower),
            "status": status,
            "note": note,
            "file_count": file_count,
            "package_path": pkg,
            "installed_at": int(time.time()),
        }

    def status_text(self) -> str:
        memes_dir = get_memes_target_dir()
        fonts_dir = get_user_fonts_dir()
        img_count = count_meme_image_files(memes_dir)
        font_count = count_installed_meme_fonts(fonts_dir)
        index = load_resource_index()
        sources = index.get("sources") or {}
        lines = [
            "资源状态：",
            f"- meme_generator: {'已安装' if get_meme_package_dir() else '未安装'}",
            f"- 表情图片目录: {memes_dir or '不可用'}",
            f"- 表情图片文件数: {img_count}",
            f"- 字体安装目录: {fonts_dir}",
            f"- 字体文件数: {font_count}",
            f"- 资源源数量: {len(sources)}",
            f"- 额外配置链接: {len(self.extra_meme_resource_urls)} 条",
            f"- 下载超时: {self.download_timeout}s（30-300）",
            f"- 强制 GitHub 代理: {'开' if self.use_github_proxy else '关'}",
            f"- 当前代理: {self.github_proxy or getattr(self, '_selected_proxy', '') or get_selected_github_proxy() or '未选择'}",
            f"- 插件数据目录: {get_plugin_data_dir()}",
            "- 安装策略: 本地包优先 + 增量（hash 未变且表情在位则跳过）",
        ]
        missing_sources = 0
        error_sources = 0
        for src in sources.values():
            if str(src.get("status") or "") == "error":
                error_sources += 1
                continue
            memes = list(src.get("memes") or [])
            if memes and not source_memes_present(memes_dir, memes):
                missing_sources += 1
        if missing_sources:
            lines.append(f"- 不完整资源源: {missing_sources}")
        if error_sources:
            lines.append(f"- 失败资源源: {error_sources}（见 /meme资源列表）")
        if img_count < 50 and not sources:
            lines.append("- 表情资源可能未完整安装，请执行 /meme表情修复")
        if font_count < 3:
            lines.append("- 字体可能未安装，请执行 /meme字体修复")
        return "\n".join(lines)

    def resource_list_text(self) -> str:
        """Text list sorted by source name, then meme names."""
        index = load_resource_index()
        sources = list((index.get("sources") or {}).values())
        target = get_memes_target_dir()

        if not sources:
            return (
                "资源列表为空。\n"
                "请先执行 /meme表情修复 安装默认资源，"
                "或在配置 meme_resource_urls 中添加额外链接后再修复。"
            )

        sources_sorted = sorted(
            sources,
            key=lambda s: str(s.get("name") or s.get("id") or "").lower(),
        )
        lines: list[str] = ["meme 资源列表（按名称排序）", ""]
        all_memes: set[str] = set()

        for src in sources_sorted:
            name = str(src.get("name") or src.get("id") or "unknown")
            sid = str(src.get("id") or "")
            url = str(src.get("url") or "")
            status = str(src.get("status") or "unknown")
            memes = sorted(set(src.get("memes") or []), key=str.lower)
            present = [m for m in memes if meme_module_exists(target, m)]
            missing = [m for m in memes if m not in present]
            all_memes.update(memes)

            sha = str(src.get("sha256") or "")
            sha_short = (sha[:10] + "...") if len(sha) > 10 else (sha or "-")
            lines.append(f"[{name}]")
            lines.append(f"  id: {sid}")
            lines.append(f"  状态: {status}")
            lines.append(f"  hash: {sha_short}")
            if url:
                lines.append(f"  链接: {url}")
            lines.append(f"  表情数: {len(memes)} (在位 {len(present)} / 缺失 {len(missing)})")
            if memes:
                # sorted meme names, plain text for now
                lines.append("  表情: " + ", ".join(memes))
            if missing:
                lines.append("  缺失: " + ", ".join(missing))
            lines.append("")

        lines.append(f"合计资源源: {len(sources_sorted)}")
        lines.append(f"合计表情名: {len(all_memes)}")
        return "\n".join(lines).rstrip()

    async def fix_memes(
        self,
        progress_cb: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> str:
        async with self._memes_lock:
            return await self._fix_memes_unlocked(progress_cb, force=force)

    async def fix_fonts(self, progress_cb: Callable[[str], None] | None = None) -> str:
        async with self._fonts_lock:
            return await self._fix_fonts_unlocked(progress_cb)

    async def fix_memes_gitee(
        self, progress_cb: Callable[[str], None] | None = None
    ) -> str:
        return await self.fix_memes(progress_cb)

    async def fix_fonts_gitee(
        self, progress_cb: Callable[[str], None] | None = None
    ) -> str:
        return await self.fix_fonts(progress_cb)

    def _should_skip_source(
        self,
        index: dict[str, Any],
        source_id: str,
        url: str,
        target: Path | None,
        force: bool,
    ) -> tuple[bool, str]:
        if force:
            return False, "强制重装"
        src = (index.get("sources") or {}).get(source_id)
        if not src:
            return False, "新资源源"
        if str(src.get("status") or "") == "error":
            note = str(src.get("note") or "")
            local = self._resolve_local_package(source_id)
            # permanently unusable package with unchanged local hash: don't loop forever
            if local and local.is_file() and ("未识别到可用的 meme 模块" in note or "没有可安装" in note):
                try:
                    sha = _sha256_file(local)
                    if sha and sha == str(src.get("sha256") or ""):
                        # keep error status but drop fake meme names
                        if src.get("memes"):
                            src["memes"] = []
                            src["file_count"] = 0
                        return True, "不可用资源包（格式不支持，已记录）"
                except Exception:
                    pass
            return False, "上次安装失败，重试"
        if str(src.get("url") or "") != url:
            return False, "链接已变更"
        memes = list(src.get("memes") or [])
        if not memes:
            return False, "无表情清单"
        if not source_memes_present(target, memes):
            return False, "本地表情缺失"
        if not src.get("sha256"):
            return False, "无 hash 记录"
        # if local package exists, require hash match for confidence
        local = self._resolve_local_package(source_id)
        if local and local.is_file():
            try:
                sha = _sha256_file(local)
                if sha and sha != str(src.get("sha256") or ""):
                    return False, "本地包 hash 变化"
            except Exception:
                return False, "本地包校验失败"
        return True, "hash 有效且表情在位"

    def _resolve_local_package(self, source_id: str) -> Path | None:
        """Prefer framework plugin_data packages/, then legacy cache paths."""
        candidates: list[Path] = []
        candidates.extend(package_candidates(source_id))
        candidates.extend(
            [
                get_cache_dir() / f"{source_id}.zip",
                get_cache_dir() / f"{source_id}.tar.gz",
                get_cache_dir() / f"{source_id}.tgz",
                get_plugin_root() / "data" / "cache" / f"{source_id}.zip",
                get_plugin_root() / "data" / "cache" / f"{source_id}.tar.gz",
            ]
        )
        if source_id == DEFAULT_SOURCE_ID:
            candidates.extend(
                [
                    get_plugin_root() / "data" / "cache" / "default.zip",
                    get_plugin_root() / "assets" / MEMES_ASSET_NAME,
                ]
            )
        if source_id == "fonts":
            candidates.extend(
                [
                    get_plugin_root() / "data" / "cache" / "fonts.zip",
                    get_plugin_root() / "assets" / FONTS_ASSET_NAME,
                ]
            )

        seen: set[str] = set()
        for cand in candidates:
            try:
                if not cand or not cand.is_file():
                    continue
                key = str(cand.resolve())
                if key in seen:
                    continue
                seen.add(key)
                if cand.stat().st_size <= 1024:
                    continue
                if not is_supported_archive(cand):
                    continue
                return cand
            except Exception:
                continue
        return None

    async def _download_default_memes_zip(
        self,
        session: aiohttp.ClientSession,
        tmp_dir: Path,
        zip_path: Path,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        bundled = get_plugin_root() / "assets" / "memes.zip"
        if bundled.is_file():
            shutil.copy2(bundled, zip_path)
            log(f"使用插件内置压缩包: {bundled}")
            return

        try:
            await self._download_urls(
                session, default_memes_urls(), zip_path, progress_cb=progress_cb
            )
            if zip_path.is_file() and zip_path.stat().st_size > 1024:
                log("已下载完整 memes.zip")
                return
        except Exception as exc:
            log(f"完整 memes.zip 不可用，尝试分包下载: {exc}")

        manifest_path = tmp_dir / MEMES_PARTS_MANIFEST
        await self._download_urls(
            session, default_parts_manifest_urls(), manifest_path, progress_cb=progress_cb
        )
        part_names = [
            line.strip()
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not part_names:
            raise RuntimeError("memes.parts.txt 为空，无法组装表情资源包")

        part_paths: list[Path] = []
        for name in part_names:
            part_path = tmp_dir / name
            log(f"下载分包: {name}")
            await self._download_urls(
                session, default_part_urls(name), part_path, progress_cb=progress_cb
            )
            part_paths.append(part_path)

        total = await asyncio.to_thread(_join_part_files, part_paths, zip_path)
        log(f"分包合并完成: {total / 1024 / 1024:.1f} MB")

    async def _acquire_package(
        self,
        *,
        session: aiohttp.ClientSession,
        source_id: str,
        urls: list[str],
        progress_cb: Callable[[str], None] | None = None,
        force_download: bool = False,
        downloader: Callable | None = None,
    ) -> Path:
        """Return local package path, downloading only when necessary."""

        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # choose package extension from first url (zip default for builtin downloader)
        ext = ".zip"
        if urls:
            ext = package_ext_from_url(urls[0])
        dest = get_package_path(source_id, ext)

        if not force_download:
            local = self._resolve_local_package(source_id)
            if local and local.is_file():
                log(f"使用本地资源包: {local}")
                return local

        # download / rebuild into packages/
        dest.parent.mkdir(parents=True, exist_ok=True)
        if downloader is not None:
            tmp_dir = Path(
                tempfile.mkdtemp(prefix=f"pkg_{source_id}_", dir=str(get_temp_dir()))
            )
            try:
                tmp_zip = tmp_dir / f"{source_id}.zip"
                await downloader(session, tmp_dir, tmp_zip, progress_cb)
                if not tmp_zip.is_file() or tmp_zip.stat().st_size <= 1024:
                    raise RuntimeError("资源包下载结果无效")
                dest = get_package_path(source_id, ".zip")
                shutil.copy2(tmp_zip, dest)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            if not urls:
                raise RuntimeError(f"资源源 {source_id} 无可用下载地址")
            await self._download_urls(
                session, urls, dest, progress_cb=progress_cb
            )
            # if server returned different format, rename by magic
            if dest.is_file():
                kind = detect_archive_format(dest)
                if kind in {"tar.gz", "tgz"} and not str(dest).endswith((".tar.gz", ".tgz")):
                    new_dest = get_package_path(source_id, ".tar.gz")
                    try:
                        if new_dest.exists():
                            _safe_unlink(new_dest)
                        dest.replace(new_dest)
                        dest = new_dest
                    except Exception:
                        pass
                elif kind == "zip" and dest.suffix.lower() != ".zip":
                    new_dest = get_package_path(source_id, ".zip")
                    try:
                        if new_dest.exists():
                            _safe_unlink(new_dest)
                        dest.replace(new_dest)
                        dest = new_dest
                    except Exception:
                        pass
        if not dest.is_file():
            raise RuntimeError(f"资源包不存在: {dest}")
        if not is_supported_archive(dest):
            msg = describe_unsupported_archive(dest) or f"不是有效的资源包: {dest}"
            raise RuntimeError(msg)
        log(f"资源包已就绪: {dest}（{detect_archive_format(dest)}）")
        return dest

    async def _install_source_from_zip(
        self,
        *,
        index: dict[str, Any],
        source_id: str,
        name: str,
        url: str,
        zip_path: Path,
        target: Path,
        progress_cb: Callable[[str], None] | None = None,
    ) -> str:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # normalize into packages/{source_id}.{zip|tar.gz}
        src_path = Path(zip_path)
        kind = detect_archive_format(src_path)
        ext = ".tar.gz" if kind in {"tar.gz", "tgz"} else (".tar" if kind == "tar" else ".zip")
        package_path = get_package_path(source_id, ext)
        try:
            if src_path.resolve() != package_path.resolve():
                package_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, package_path)
                zip_path = package_path
            else:
                zip_path = package_path
        except Exception:
            package_path = src_path
            zip_path = src_path

        sha = await asyncio.to_thread(_sha256_file, zip_path)
        n, note, memes = await asyncio.to_thread(
            install_memes_from_archive, zip_path, target
        )
        self._upsert_source(
            index,
            source_id=source_id,
            name=name,
            url=url,
            sha256=sha,
            memes=memes,
            status="ok",
            note=note,
            file_count=n,
            package_path=str(package_path),
        )
        log(
            f"{name}: 写入 {n} 文件，表情 {len(memes)} 个；{note}；"
            f"hash={sha[:12]}...；包={package_path}"
        )
        return f"{name}: 安装完成，表情 {len(memes)} 个，文件 {n}；"

    async def _fix_memes_unlocked(
        self,
        progress_cb: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> str:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # ensure data dirs / migration
        data_dir = get_plugin_data_dir()
        target = get_memes_target_dir()
        if not target:
            return (
                "未找到 meme_generator 安装路径。请先安装依赖：\n"
                'pip install "meme_generator>=0.1.14,<0.2.0"'
            )

        index = load_resource_index()
        session = await self._get_session()
        reports: list[str] = []
        details: list[str] = []
        installed = 0
        skipped = 0
        failed = 0

        # GitHub mirror speed-test (AstrBot-compatible), used when falling back to GitHub
        try:
            await self.ensure_github_proxy_rank(progress_cb=log, force=False)
        except Exception as exc:
            logger.warning("GitHub 代理测速失败，将按默认顺序尝试: %s", exc)

        log(
            f"数据目录: {data_dir}；下载超时: {self.download_timeout}s；"
            f"模式: {'强制全量' if force else '增量'}"
        )

        # 1) default source
        default_url = f"builtin://{DEFAULT_SOURCE_ID}"
        skip, reason = self._should_skip_source(
            index, DEFAULT_SOURCE_ID, default_url, target, force
        )
        if skip:
            skipped += 1
            log(f"默认资源: 跳过（{reason}）")
            reports.append("默认资源: 跳过")
            details.append(f"默认资源: 跳过（{reason}）")
            log(reports[-1])
            # backfill package path / ensure index path migrated
            src = (index.get("sources") or {}).get(DEFAULT_SOURCE_ID) or {}
            local = self._resolve_local_package(DEFAULT_SOURCE_ID)
            if local and not src.get("package_path"):
                self._upsert_source(
                    index,
                    source_id=DEFAULT_SOURCE_ID,
                    name=str(src.get("name") or "默认资源"),
                    url=default_url,
                    sha256=str(src.get("sha256") or ""),
                    memes=list(src.get("memes") or []),
                    status="ok",
                    note=str(src.get("note") or reason),
                    file_count=int(src.get("file_count") or 0),
                    package_path=str(local),
                )
        else:
            try:
                log("处理默认表情资源包 ...")
                zip_path = await self._acquire_package(
                    session=session,
                    source_id=DEFAULT_SOURCE_ID,
                    urls=default_memes_urls(),
                    progress_cb=log,
                    force_download=force,
                    downloader=self._download_default_memes_zip,
                )
                msg = await self._install_source_from_zip(
                    index=index,
                    source_id=DEFAULT_SOURCE_ID,
                    name="默认资源",
                    url=default_url,
                    zip_path=zip_path,
                    target=target,
                    progress_cb=log,
                )
                reports.append(msg)
                installed += 1
            except Exception as exc:
                failed += 1
                logger.exception("默认资源安装失败")
                log(f"默认资源: 失败: {exc}")
                reports.append("默认资源: 失败")
                details.append(f"默认资源: 失败: {exc}")
                src = (index.get("sources") or {}).get(DEFAULT_SOURCE_ID) or {}
                self._upsert_source(
                    index,
                    source_id=DEFAULT_SOURCE_ID,
                    name="默认资源",
                    url=default_url,
                    sha256=str(src.get("sha256") or ""),
                    memes=list(src.get("memes") or []),
                    status="error",
                    note=str(exc),
                    package_path=str(get_package_zip_path(DEFAULT_SOURCE_ID)),
                )

        # 2) extra urls from config
        for idx, url in enumerate(self.extra_meme_resource_urls, 1):
            sid = _source_id_from_url(url)
            name = f"额外资源{idx}"
            skip, reason = self._should_skip_source(index, sid, url, target, force)
            if skip:
                skipped += 1
                log(f"{name}: 跳过（{reason}）")
                reports.append(f"{name}: 跳过")
                details.append(f"{name}: 跳过（{reason}）")
                log(reports[-1])
                src = (index.get("sources") or {}).get(sid) or {}
                local = self._resolve_local_package(sid)
                if local and not src.get("package_path"):
                    self._upsert_source(
                        index,
                        source_id=sid,
                        name=str(src.get("name") or name),
                        url=url,
                        sha256=str(src.get("sha256") or ""),
                        memes=list(src.get("memes") or []),
                        status="ok",
                        note=str(src.get("note") or reason),
                        file_count=int(src.get("file_count") or 0),
                        package_path=str(local),
                    )
                continue
            try:
                log(f"处理 {name}: {url}")
                zip_path = await self._acquire_package(
                    session=session,
                    source_id=sid,
                    urls=[url],
                    progress_cb=log,
                    force_download=force,
                )
                msg = await self._install_source_from_zip(
                    index=index,
                    source_id=sid,
                    name=name,
                    url=url,
                    zip_path=zip_path,
                    target=target,
                    progress_cb=log,
                )
                reports.append(msg)
                details.append(msg)
                installed += 1
            except Exception as exc:
                failed += 1
                logger.exception("额外资源安装失败: %s", url)
                log(f"{name}: 失败: {exc}")
                reports.append(f"{name}: 失败")
                details.append(f"{name}: 失败: {exc}")
                src = (index.get("sources") or {}).get(sid) or {}
                pkg = self._resolve_local_package(sid)
                sha = str(src.get("sha256") or "")
                if pkg and pkg.is_file():
                    try:
                        sha = _sha256_file(pkg)
                    except Exception:
                        pass
                note = str(exc)
                # non-installable package: do not keep fake meme names
                keep_memes = [] if ("未识别到可用的 meme 模块" in note or "没有可安装" in note) else list(src.get("memes") or [])
                self._upsert_source(
                    index,
                    source_id=sid,
                    name=name,
                    url=url,
                    sha256=sha,
                    memes=keep_memes,
                    status="error",
                    note=note,
                    package_path=str(pkg or get_package_zip_path(sid)),
                )

        save_resource_index(index)
        img_count = count_meme_image_files(target)
        total_memes = sorted(
            {
                m
                for s in (index.get("sources") or {}).values()
                if str(s.get("status") or "") == "ok"
                for m in (s.get("memes") or [])
            },
            key=str.lower,
        )
        mode = "强制全量" if force else "增量"
        user_msg = (
            f"表情修复完成（{mode}）\n"
            f"安装/更新: {installed}  跳过: {skipped}  失败: {failed}\n"
            + "\n".join(reports)
            + f"\n清单有效表情数: {len(total_memes)}\n"
            f"图片文件数: {img_count}"
        )
        logger.info(
            "表情修复详情（%s） 安装/更新=%s 跳过=%s 失败=%s 有效表情=%s 图片=%s\n"
            "目标=%s\n数据目录=%s\n资源包目录=%s\n清单=%s\n明细:\n%s",
            mode,
            installed,
            skipped,
            failed,
            len(total_memes),
            img_count,
            target,
            data_dir,
            get_packages_dir(),
            get_resource_index_path(),
            "\n".join(details) if details else ("\n".join(reports) if reports else "(无)"),
        )
        return user_msg

    async def _fix_fonts_unlocked(
        self, progress_cb: Callable[[str], None] | None = None
    ) -> str:
        def log(msg: str) -> None:
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        target = get_user_fonts_dir()
        target.mkdir(parents=True, exist_ok=True)

        try:
            session = await self._get_session()
            try:
                await self.ensure_github_proxy_rank(progress_cb=log, force=False)
            except Exception as exc:
                logger.warning("GitHub 代理测速失败: %s", exc)
            log("处理默认字体资源包 ...")
            zip_path = await self._acquire_package(
                session=session,
                source_id="fonts",
                urls=default_fonts_urls(),
                progress_cb=log,
                force_download=False,
            )
            n = await asyncio.to_thread(install_fonts_from_zip, zip_path, target)

            cache_msg = await asyncio.to_thread(_refresh_font_cache)
            font_count = count_installed_meme_fonts(target)
            extra = f"\n{cache_msg}" if cache_msg else "\n已刷新字体缓存（如适用）"
            if sys.platform.startswith("win"):
                extra += (
                    "\nWindows 提示: 若部分字体仍未生效，可打开字体目录手动安装，"
                    f"或重启 AstrBot。目录: {target}"
                )
            return (
                f"字体修复完成\n"
                f"安装字体文件: {n}\n"
                f"字体文件数: {font_count}\n"
                f"资源包: {zip_path}\n"
                f"目标: {target}{extra}"
            )
        except Exception as exc:
            logger.exception("字体修复失败")
            return f"字体修复失败: {exc}"
