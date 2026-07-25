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
# Format: {proxy}/{https://github.com/...}
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
GITHUB_PROXY_CACHE_TTL = 12 * 3600
GITHUB_PROXY_PROBE_TIMEOUT = 8
GITHUB_PROXY_MAX_AUTO_LATENCY_MS = 3000

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
    return host in {
        "github.com",
        "www.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
    }


def normalize_github_proxy(proxy: str | None) -> str:
    proxy = (proxy or "").strip().rstrip("/")
    if not proxy:
        return ""
    if not proxy.startswith("http://") and not proxy.startswith("https://"):
        proxy = "https://" + proxy
    return proxy.rstrip("/")


def apply_github_proxy(url: str, proxy: str | None) -> str:
    proxy = normalize_github_proxy(proxy)
    if not proxy or not url:
        return url
    if url.startswith(proxy + "/"):
        return url
    return f"{proxy}/{url}"


def get_github_proxy_cache_path() -> Path:
    return get_plugin_data_dir() / "index" / "github_proxy_rank.json"


def load_github_proxy_rank() -> dict[str, Any]:
    path = get_github_proxy_cache_path()
    if not path.is_file():
        return {"updated_at": 0, "proxies": [], "selected": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"updated_at": 0, "proxies": [], "selected": ""}
        data.setdefault("updated_at", 0)
        data.setdefault("proxies", [])
        data.setdefault("selected", "")
        return data
    except Exception:
        return {"updated_at": 0, "proxies": [], "selected": ""}


def save_github_proxy_rank(rank: dict[str, Any]) -> None:
    path = get_github_proxy_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rank, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_selected_github_proxy() -> str:
    """Return explicitly selected proxy only (no silent fallback to any available)."""
    cache = load_github_proxy_rank()
    selected = normalize_github_proxy(str(cache.get("selected") or ""))
    if not selected:
        return ""
    # validate against threshold when latency is known
    for item in cache.get("proxies") or []:
        if str(item.get("proxy") or "") != selected:
            continue
        try:
            lat = float(item.get("latency"))
        except Exception:
            return selected
        if lat > GITHUB_PROXY_MAX_AUTO_LATENCY_MS:
            return ""
        return selected
    return selected


def set_selected_github_proxy(proxy: str) -> None:
    cache = load_github_proxy_rank()
    cache["selected"] = normalize_github_proxy(proxy)
    cache.setdefault("proxies", cache.get("proxies") or [])
    cache["updated_at"] = int(cache.get("updated_at") or time.time())
    save_github_proxy_rank(cache)


async def probe_github_proxy_latency(
    session: aiohttp.ClientSession,
    proxy: str,
    timeout_sec: float = GITHUB_PROXY_PROBE_TIMEOUT,
) -> float | None:
    """Same probe target as AstrBot /stats/ghproxy/test."""
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
    proxies = [
        normalize_github_proxy(p)
        for p in (proxies or (ASTRBOT_GITHUB_PROXIES + LEGACY_GITHUB_PROXIES))
    ]
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
            "latency": latency,
        }

    results = list(await asyncio.gather(*[one(p) for p in uniq]))
    ranked = sorted(
        results,
        key=lambda x: (
            0 if x.get("available") else 1,
            x.get("latency") if x.get("latency") is not None else 1e12,
        ),
    )
    if progress_cb:
        for item in ranked:
            if item.get("available"):
                progress_cb(f"代理可用: {item['proxy']}  延迟 {item['latency']} ms")
            else:
                progress_cb(f"代理不可用: {item['proxy']}")
    selected = ""
    best = next((x for x in ranked if x.get("available")), None)
    if best is not None:
        latency = best.get("latency")
        try:
            latency_f = float(latency)
        except Exception:
            latency_f = None
        if latency_f is not None and latency_f <= GITHUB_PROXY_MAX_AUTO_LATENCY_MS:
            selected = str(best.get("proxy") or "")
            if progress_cb:
                progress_cb(
                    f"已选最低延迟代理: {selected}（{latency} ms ≤ {GITHUB_PROXY_MAX_AUTO_LATENCY_MS} ms）"
                )
        elif progress_cb:
            progress_cb(
                f"网络不佳：最低延迟 {latency} ms > {GITHUB_PROXY_MAX_AUTO_LATENCY_MS} ms，"
                "不自动切换代理，GitHub 仍走直链"
            )
    save_github_proxy_rank(
        {
            "updated_at": int(time.time()),
            "proxies": ranked,
            "selected": selected,
        }
    )
    return ranked


def expand_urls_with_github_proxies(
    urls: list[str],
    *,
    ranked_proxies: list[str] | None = None,
    fixed_proxy: str = "",
    use_proxy: bool = True,
    force_proxy: bool = False,
) -> list[str]:
    """Expand download candidates.

    - Gitee / non-GitHub: always direct only.
    - GitHub default: direct only.
    - When panel github_proxy is set, or probe selected a proxy (latency <= 3000ms):
      try proxy first, then fall back to direct (unless force_proxy with fixed proxy).
    - Never inject untested proxy list blindly.
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

        preferred: list[str] = []
        if fixed_proxy:
            preferred.append(fixed_proxy)
        if selected and selected not in preferred:
            preferred.append(selected)
        for p in ranked_proxies:
            if p and p not in preferred:
                preferred.append(p)

        if preferred:
            for proxy in preferred:
                add(apply_github_proxy(url, proxy))
            # fallback direct unless forcing a fixed panel proxy only
            if not (force_proxy and fixed_proxy):
                add(url)
        else:
            add(url)
    return out


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
        return Path(local) / "Microsoft" / "Windows" / "Fonts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Fonts"
    return Path.home() / ".local" / "share" / "fonts" / "meme-generator"


def count_meme_image_files(memes_dir: Path | None) -> int:
    if not memes_dir or not memes_dir.is_dir():
        return 0
    return sum(
        1
        for p in memes_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def count_installed_meme_fonts(fonts_dir: Path | None = None) -> int:
    fonts_dir = fonts_dir or get_user_fonts_dir()
    if not fonts_dir.is_dir():
        return 0
    return sum(
        1 for p in fonts_dir.iterdir() if p.is_file() and p.suffix.lower() in FONT_EXTS
    )


def _github_urls(filename: str) -> list[str]:
    """Raw GitHub release URL; proxies applied via expand_urls_with_github_proxies."""
    return [
        f"https://github.com/{DEFAULT_GITHUB_REPO}/releases/download/"
        f"{DEFAULT_RELEASE_TAG}/{filename}"
    ]


def _gitee_urls(filename: str) -> list[str]:
    return [
        f"https://gitee.com/{DEFAULT_GITEE_REPO}/releases/download/"
        f"{DEFAULT_RELEASE_TAG}/{filename}"
    ]


def default_memes_urls() -> list[str]:
    return _gitee_urls(MEMES_ASSET_NAME) + _github_urls(MEMES_ASSET_NAME)


def default_fonts_urls() -> list[str]:
    return _gitee_urls(FONTS_ASSET_NAME) + _github_urls(FONTS_ASSET_NAME)


def default_parts_manifest_urls() -> list[str]:
    return _gitee_urls(MEMES_PARTS_MANIFEST) + _github_urls(MEMES_PARTS_MANIFEST)


def default_part_urls(part_name: str) -> list[str]:
    return _gitee_urls(part_name) + _github_urls(part_name)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_id_from_url(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    name = Path(urlparse(url).path).name or "pack"
    name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return f"extra_{name}_{digest}"


def _safe_unlink(path: Path | None) -> None:
    if not path:
        return
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def detect_archive_format(path: Path) -> str:
    """Return archive kind: zip / tar.gz / tar / rar / 7z / unknown / empty."""
    try:
        if not path or not path.is_file() or path.stat().st_size <= 0:
            return "empty"
        with path.open("rb") as f:
            head = f.read(16)
    except Exception:
        return "unknown"

    name = path.name.lower()
    if head[:2] == b"PK" or zipfile.is_zipfile(path):
        return "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:2] == b"7z":
        return "7z"
    # gzip header -> usually .tar.gz / .tgz
    if len(head) >= 2 and head[0] == 0x1F and head[1] == 0x8B:
        if name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar.gzip"):
            return "tar.gz"
        try:
            with tarfile.open(path, "r:gz") as tf:
                if tf.getmembers():
                    return "tar.gz"
        except Exception:
            return "gzip"
        return "tar.gz"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar.gz"
    if name.endswith(".tar") or tarfile.is_tarfile(path):
        return "tar"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".rar"):
        return "rar"
    if name.endswith(".7z"):
        return "7z"
    return "unknown"


def describe_unsupported_archive(path: Path) -> str:
    kind = detect_archive_format(path)
    if kind in {"zip", "tar.gz", "tar", "tgz"}:
        return ""
    if kind == "rar":
        return (
            f"检测到 RAR 资源包: {path.name}。"
            "当前支持 ZIP / tar.gz（需要内部是 Python meme 模块：表情名/__init__.py+图片）。"
            "请将 .rar 转为 .zip 或 .tar.gz 后重新配置直链。"
        )
    if kind == "7z":
        return (
            f"检测到 7z 资源包: {path.name}。"
            "当前支持 ZIP / tar.gz，请转换后重试。"
        )
    if kind == "gzip":
        return (
            f"检测到 gzip 文件但无法按 tar.gz 打开: {path.name}。"
            "请使用 .tar.gz（而不是单独的 .gz）。"
        )
    if kind == "empty":
        return f"资源包为空或下载不完整: {path}"
    return (
        f"不是有效的资源包: {path}（识别格式: {kind}）。"
        "请使用 ZIP 或 tar.gz 直链；RAR/7z 需先转换。"
    )


def _join_part_files(part_paths: list[Path], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    tmp = dest.with_suffix(dest.suffix + ".joining")
    with tmp.open("wb") as out:
        for part in part_paths:
            data = part.read_bytes()
            out.write(data)
            total += len(data)
    tmp.replace(dest)
    return total


def _looks_like_meme_module(path: Path) -> bool:
    """Python meme module: requires .py (usually __init__.py) + assets/config."""
    if not path.is_dir():
        return False
    name = path.name.lower()
    if name in WRAPPER_DIR_NAMES or name.startswith("__"):
        return False
    # reject common non-python package names / rust repos
    if name.endswith(("-rs", "_rs")) or "contrib-rs" in name:
        return False
    if (path / "Cargo.toml").exists() or (path / "Cargo.lock").exists():
        return False

    has_py = False
    has_asset = False
    has_meme_meta = False
    try:
        children = list(path.iterdir())
    except OSError:
        return False

    for child in children:
        cname = child.name.lower()
        if child.is_file():
            if child.suffix.lower() == ".py":
                has_py = True
            if cname in {"config.json", "meme.toml", "meme.json", "meme.yaml", "meme.yml"}:
                has_meme_meta = True
            if child.suffix.lower() in IMAGE_EXTS:
                has_asset = True
        elif child.is_dir() and cname in {
            "images",
            "image",
            "img",
            "assets",
            "static",
            "resource",
            "resources",
            "gif",
            "png",
        }:
            if any(p.is_file() for p in child.rglob("*")):
                has_asset = True
    # Python meme modules always ship with .py entry
    return has_py and (has_asset or has_meme_meta or (path / "__init__.py").exists())


def _score_meme_root(path: Path) -> int:
    if not path.is_dir():
        return -1
    try:
        children = [c for c in path.iterdir() if c.is_dir() and not c.name.startswith(".")]
    except OSError:
        return -1
    module_count = sum(1 for c in children if _looks_like_meme_module(c))
    score = module_count * 10
    if module_count >= 3:
        score += 20
    if path.name.lower() in {"memes", "meme"}:
        score += 5
    return score


def find_memes_content_root(extract_dir: Path) -> Path:
    best: Path | None = None
    best_score = -1
    candidates = [extract_dir]
    try:
        for p in extract_dir.rglob("*"):
            if not p.is_dir():
                continue
            try:
                rel = p.relative_to(extract_dir)
            except ValueError:
                continue
            if len(rel.parts) > 6:
                continue
            candidates.append(p)
    except OSError:
        pass

    for cand in candidates:
        score = _score_meme_root(cand)
        if score > best_score:
            best_score = score
            best = cand

    if best is not None and best_score > 0:
        return best

    cur = extract_dir
    for _ in range(5):
        try:
            kids = [c for c in cur.iterdir() if not c.name.startswith(".")]
        except OSError:
            break
        dirs = [c for c in kids if c.is_dir()]
        files = [c for c in kids if c.is_file()]
        if len(dirs) == 1 and not files:
            cur = dirs[0]
            continue
        if len(dirs) == 1 and all(
            f.name.lower() in {"readme.md", "license", "license.txt"} for f in files
        ):
            cur = dirs[0]
            continue
        break
    return cur


def _safe_relpath(name: str) -> str | None:
    name = name.replace("\\", "/")
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _extract_zip_raw(zip_path: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            rel = _safe_relpath(info.filename)
            if not rel:
                continue
            out_path = target_dir / rel
            if info.is_dir() or info.filename.endswith("/"):
                out_path.mkdir(parents=True, exist_ok=True)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _extract_tar_raw(tar_path: Path, target_dir: Path) -> int:
    """Extract tar / tar.gz safely (no path traversal)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    # r:* auto-detects gz/bz2/xz when possible
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            rel = _safe_relpath(member.name)
            if not rel:
                continue
            out_path = target_dir / rel
            if member.isdir():
                out_path.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                # skip links/devices
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _extract_archive_raw(archive_path: Path, target_dir: Path) -> int:
    kind = detect_archive_format(archive_path)
    if kind == "zip":
        return _extract_zip_raw(archive_path, target_dir)
    if kind in {"tar.gz", "tar", "tgz"}:
        return _extract_tar_raw(archive_path, target_dir)
    msg = describe_unsupported_archive(archive_path) or f"不支持的压缩格式: {archive_path}"
    raise RuntimeError(msg)


def list_meme_modules(content_root: Path) -> list[Path]:
    if _looks_like_meme_module(content_root):
        return [content_root]
    modules = [
        c
        for c in content_root.iterdir()
        if c.is_dir() and _looks_like_meme_module(c)
    ]
    return sorted(modules, key=lambda p: p.name.lower())


def _copy_meme_modules(src_root: Path, dest: Path) -> tuple[int, list[str], str]:
    dest.mkdir(parents=True, exist_ok=True)
    content_root = find_memes_content_root(src_root)
    modules = list_meme_modules(content_root)
    if modules:
        count = 0
        installed: list[str] = []
        for mod in modules:
            out = dest / mod.name
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            shutil.copytree(mod, out)
            count += sum(1 for _ in out.rglob("*") if _.is_file())
            installed.append(mod.name)
        note = (
            f"检测到资源根目录: "
            f"{content_root.relative_to(src_root) if content_root != src_root else '.'}"
        )
        return count, sorted(set(installed), key=str.lower), note
    return (
        0,
        [],
        "未识别到可用的 meme 模块（需要 <表情名>/逻辑文件+图片）。"
        "源码仓/纯图片包不能直接作为 Python meme 资源安装。",
    )


def install_memes_from_zip(zip_path: Path, target_dir: Path) -> tuple[int, str, list[str]]:
    """Install memes from zip/tar.gz archive. Name kept for compatibility."""
    return install_memes_from_archive(zip_path, target_dir)


def install_memes_from_archive(
    archive_path: Path, target_dir: Path
) -> tuple[int, str, list[str]]:
    work = Path(tempfile.mkdtemp(prefix="meme_extract_", dir=str(get_temp_dir())))
    try:
        raw = work / "raw"
        n = _extract_archive_raw(archive_path, raw)
        if n <= 0:
            raise RuntimeError(f"压缩包为空: {archive_path}")
        written, memes, note = _copy_meme_modules(raw, target_dir)
        if written <= 0 or not memes:
            raise RuntimeError(note or "压缩包中没有可安装的 meme 模块")
        kind = detect_archive_format(archive_path)
        if note:
            note = f"[{kind}] {note}"
        return written, note, memes
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_fonts_from_zip(zip_path: Path, target_dir: Path) -> int:
    """Install fonts from zip/tar.gz archive."""
    target_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="font_extract_", dir=str(get_temp_dir())))
    try:
        raw = work / "raw"
        _extract_archive_raw(zip_path, raw)
        count = 0
        for path in raw.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in FONT_EXTS:
                continue
            shutil.copy2(path, target_dir / path.name)
            count += 1
        return count
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _refresh_font_cache() -> str | None:
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        fc = shutil.which("fc-cache")
        if not fc:
            return "未找到 fc-cache，请安装 fontconfig 后手动执行: fc-cache -fv"
        try:
            subprocess.run(
                [fc, "-fv"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return None
        except Exception as exc:
            return f"fc-cache 执行失败: {exc}"
    return None


def _normalize_url_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.replace("，", ",").replace("\r", "\n")
        parts: list[str] = []
        for line in text.split("\n"):
            for item in line.split(","):
                item = item.strip()
                if item:
                    parts.append(item)
        return parts
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_normalize_url_list(item))
        return out
    text = str(value).strip()
    return [text] if text else []


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "updated_at": 0, "sources": {}}


def load_resource_index() -> dict[str, Any]:
    path = get_resource_index_path()
    legacy = get_plugin_root() / "data" / "resource_index.json"
    read_path = path if path.is_file() else legacy
    if not read_path.is_file():
        return _empty_index()
    try:
        data = json.loads(read_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_index()
        data.setdefault("version", INDEX_VERSION)
        data.setdefault("sources", {})
        if not isinstance(data["sources"], dict):
            data["sources"] = {}
        return data
    except Exception as exc:
        logger.warning("读取 resource_index.json 失败: %s", exc)
        return _empty_index()


def save_resource_index(index: dict[str, Any]) -> None:
    index["version"] = INDEX_VERSION
    index["updated_at"] = int(time.time())
    path = get_resource_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)



def list_pack_footer_text() -> str:
    """Footer for list image: 默认+x组额外表情包，最近更新：Y.M.D"""
    from datetime import datetime

    index = load_resource_index()
    sources = index.get("sources") if isinstance(index.get("sources"), dict) else {}
    extra_ok = 0
    latest_ts = int(index.get("updated_at") or 0)
    for sid, src in sources.items():
        if not isinstance(src, dict):
            continue
        installed = int(src.get("installed_at") or 0)
        if installed > latest_ts:
            latest_ts = installed
        if sid == DEFAULT_SOURCE_ID:
            continue
        if str(src.get("status") or "").lower() == "ok":
            extra_ok += 1

    if latest_ts > 0:
        try:
            dt = datetime.fromtimestamp(latest_ts)
            date_s = f"{dt.year}.{dt.month}.{dt.day}"
        except Exception:
            date_s = "—"
    else:
        date_s = "—"
    return f"默认+{extra_ok}组额外表情包，最近更新：{date_s}"



def meme_module_exists(target_dir: Path | None, name: str) -> bool:
    if not target_dir:
        return False
    path = target_dir / name
    return _looks_like_meme_module(path)


def source_memes_present(target_dir: Path | None, memes: list[str]) -> bool:
    if not memes:
        return False
    return all(meme_module_exists(target_dir, name) for name in memes)


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

    def _timeout_hint(url: str) -> None:
        looks_github = is_github_url(url) or "github.com" in url
        if not looks_github:
            return
        if proxy_enabled is False:
            logger.warning(
                "GitHub 下载超时且未启用代理(use_github_proxy=false): %s。"
                "可开启 use_github_proxy 并执行 /meme代理测速，或填写 github_proxy。",
                url,
            )
            return
        if proxy_enabled and not (selected_proxy or get_selected_github_proxy()):
            logger.warning(
                "GitHub 直链下载超时且无已选代理: %s。"
                "可执行 /meme代理测速（最低延迟≤%sms 才会自动选用），或填写 github_proxy。",
                url,
                GITHUB_PROXY_MAX_AUTO_LATENCY_MS,
            )

    for url in urls:
        tmp = dest.with_suffix(dest.suffix + ".part")
        _safe_unlink(tmp)
        try:
            if progress_cb:
                progress_cb(f"尝试下载: {url}（超时 {timeout_sec}s）")
            connect_budget = min(30, max(10, timeout_sec // 6))
            client_timeout = aiohttp.ClientTimeout(
                total=timeout_sec,
                connect=connect_budget,
                sock_connect=connect_budget,
                sock_read=min(timeout_sec, max(30, timeout_sec // 2)),
            )
            started = time.monotonic()
            async with session.get(url, timeout=client_timeout, allow_redirects=True) as resp:
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
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                if downloaded <= 0:
                    raise RuntimeError(f"空响应: {url}")
                if total > 0 and downloaded < total:
                    raise RuntimeError(
                        f"下载不完整: {downloaded}/{total} bytes from {url}"
                    )
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
            if dest.is_file() and dest.stat().st_size <= 0:
                _safe_unlink(dest)
            logger.warning("下载超时 %s: %s", url, exc)
            _timeout_hint(url)
            if progress_cb:
                progress_cb(str(last_error) + "，已清除临时缓存")
        except asyncio.CancelledError:
            _safe_unlink(tmp)
            raise
        except Exception as exc:
            last_error = exc
            _safe_unlink(tmp)
            logger.warning("下载失败 %s: %s", url, exc)
            if progress_cb:
                progress_cb(f"下载失败: {url} -> {exc}")
    _safe_unlink(dest.with_suffix(dest.suffix + ".part"))
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
        self._selected_proxy: str = self.github_proxy or get_selected_github_proxy()
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

    async def ensure_github_proxy_rank(
        self,
        progress_cb: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> list[str]:
        """Probe proxies; auto-select only if best latency <= 3000ms. Panel proxy overrides."""
        if not self.use_github_proxy:
            self._ranked_proxies = []
            self._selected_proxy = ""
            if progress_cb:
                progress_cb("GitHub 代理已关闭（use_github_proxy=false），仅直链")
            return []

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
        selected = normalize_github_proxy(str(cache.get("selected") or ""))
        # Drop stale auto-selection if recorded latency is above threshold.
        if selected:
            for item in cache.get("proxies") or []:
                if str(item.get("proxy") or "") != selected:
                    continue
                try:
                    lat = float(item.get("latency"))
                except Exception:
                    lat = None
                if lat is not None and lat > GITHUB_PROXY_MAX_AUTO_LATENCY_MS:
                    selected = ""
                break
        if not selected and False:
            # do not auto-pick first cached proxy without threshold check
            pass

        if not force and cached and (
            age <= GITHUB_PROXY_CACHE_TTL or not self.proxy_probe_on_fix
        ):
            self._ranked_proxies = cached
            self._selected_proxy = selected
            if progress_cb:
                if selected:
                    progress_cb(f"使用已选 GitHub 代理: {selected}（缓存 {age}s）")
                else:
                    progress_cb(f"使用 GitHub 直链（缓存 {age}s，无合格代理）")
            return cached

        session = await self._get_session()
        ranked = await rank_github_proxies(session, progress_cb=progress_cb)
        available_items = [x for x in ranked if x.get("available")]
        available = [str(x["proxy"]) for x in available_items]
        cache = load_github_proxy_rank()
        selected = normalize_github_proxy(str(cache.get("selected") or ""))
        self._ranked_proxies = available
        self._selected_proxy = selected
        if selected:
            if progress_cb:
                best_lat = None
                for x in available_items:
                    if str(x.get("proxy")) == selected:
                        best_lat = x.get("latency")
                        break
                progress_cb(
                    f"已自动选择最低延迟代理: {selected}"
                    + (f"（{best_lat} ms）" if best_lat is not None else "")
                )
            return available

        if available_items:
            latency = available_items[0].get("latency")
            msg = (
                f"网络不佳：最低延迟 {latency} ms > {GITHUB_PROXY_MAX_AUTO_LATENCY_MS} ms，"
                "不自动切换代理，GitHub 保持直链"
            )
            logger.warning(msg)
            if progress_cb:
                progress_cb(msg)
            return available

        self._ranked_proxies = []
        self._selected_proxy = ""
        logger.warning(
            "GitHub 代理测速无可用节点。将使用直链；也可在配置面板填写 github_proxy。"
        )
        if progress_cb:
            progress_cb("未测得可用代理；GitHub 将走直链，也可填写自定义代理")
        return []

    def prepare_urls(self, urls: list[str]) -> list[str]:
        """Gitee direct; GitHub direct unless proxy selected/configured."""
        force = bool(self.github_proxy)
        return expand_urls_with_github_proxies(
            urls,
            ranked_proxies=self._ranked_proxies,
            fixed_proxy=self.github_proxy,
            use_proxy=self.use_github_proxy,
            force_proxy=force,
        )

    async def _download_urls(
        self,
        session: aiohttp.ClientSession,
        urls: list[str],
        dest: Path,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        selected = (
            self.github_proxy
            or getattr(self, "_selected_proxy", "")
            or get_selected_github_proxy()
        )
        await _download_to_path(
            session,
            self.prepare_urls(urls),
            dest,
            progress_cb=progress_cb,
            timeout_sec=self.download_timeout,
            proxy_enabled=self.use_github_proxy,
            selected_proxy=selected,
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
            lines.append(f"- 失败资源源: {error_sources}（见 /meme列表）")
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

        try:
            await self.ensure_github_proxy_rank(progress_cb=log, force=False)
        except Exception as exc:
            logger.warning("GitHub 代理测速失败，将按已有排序/直连策略尝试: %s", exc)

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
