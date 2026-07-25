# -*- coding: utf-8 -*-
"""Meme usage counters for hot/new labels."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

try:
    from astrbot import logger
except Exception:  # standalone / unit test
    import logging
    logger = logging.getLogger("astrbot_plugin_memelite.usage")

from .resources import get_plugin_data_dir

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float | None = None


def usage_file() -> Path:
    path = get_plugin_data_dir() / "index" / "meme_usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _empty() -> dict[str, Any]:
    return {"version": 1, "updated_at": int(time.time()), "memes": {}}


def _load() -> dict[str, Any]:
    global _CACHE, _CACHE_MTIME
    path = usage_file()
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None

    if _CACHE is not None and _CACHE_MTIME == mtime:
        return _CACHE

    data = _empty()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                memes = raw.get("memes")
                if not isinstance(memes, dict):
                    memes = {}
                cleaned: dict[str, dict[str, Any]] = {}
                for key, val in memes.items():
                    if not isinstance(key, str) or not key:
                        continue
                    if not isinstance(val, dict):
                        continue
                    count = int(val.get("count") or 0)
                    first_seen = int(val.get("first_seen") or 0)
                    last_used = int(val.get("last_used") or 0)
                    cleaned[key] = {
                        "count": max(0, count),
                        "first_seen": max(0, first_seen),
                        "last_used": max(0, last_used),
                    }
                data = {
                    "version": int(raw.get("version") or 1),
                    "updated_at": int(raw.get("updated_at") or int(time.time())),
                    "memes": cleaned,
                }
        except Exception as exc:
            logger.warning("读取 meme 使用计数失败，将重建: %s", exc)
            data = _empty()

    _CACHE = data
    _CACHE_MTIME = mtime
    return data


def _save(data: dict[str, Any]) -> None:
    global _CACHE, _CACHE_MTIME
    path = usage_file()
    data["updated_at"] = int(time.time())
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        _CACHE_MTIME = path.stat().st_mtime
    except OSError:
        _CACHE_MTIME = None
    _CACHE = data


def ensure_keys(keys: list[str] | set[str]) -> None:
    """Register meme keys.

    First bulk scan (empty store) records keys without first_seen,
    so the whole library is not marked new. Later newly appeared keys
    get first_seen=now and can show the new label.
    """
    now = int(time.time())
    with _LOCK:
        data = _load()
        memes = data.setdefault("memes", {})
        changed = False
        is_initial = len(memes) == 0
        for key in keys:
            if not key:
                continue
            entry = memes.get(key)
            if entry is None:
                memes[key] = {
                    "count": 0,
                    # initial inventory: not new; subsequent new keys: new window starts now
                    "first_seen": 0 if is_initial else now,
                    "last_used": 0,
                }
                changed = True
        if changed:
            _save(data)


def record_use(key: str, amount: int = 1) -> int:
    """Global usage counter across all sessions."""
    if not key:
        return 0
    now = int(time.time())
    with _LOCK:
        data = _load()
        memes = data.setdefault("memes", {})
        entry = memes.get(key)
        if entry is None:
            entry = {"count": 0, "first_seen": now, "last_used": 0}
            memes[key] = entry
        entry["count"] = int(entry.get("count") or 0) + max(1, int(amount))
        # first real use also opens the new window when inventory had first_seen=0
        if not entry.get("first_seen"):
            entry["first_seen"] = now
        entry["last_used"] = now
        _save(data)
        return int(entry["count"])


def get_counts() -> dict[str, int]:
    with _LOCK:
        data = _load()
        return {
            k: int(v.get("count") or 0)
            for k, v in data.get("memes", {}).items()
            if isinstance(v, dict)
        }


def get_entries() -> dict[str, dict[str, int]]:
    with _LOCK:
        data = _load()
        out: dict[str, dict[str, int]] = {}
        for k, v in data.get("memes", {}).items():
            if not isinstance(v, dict):
                continue
            out[k] = {
                "count": int(v.get("count") or 0),
                "first_seen": int(v.get("first_seen") or 0),
                "last_used": int(v.get("last_used") or 0),
            }
        return out


def compute_labels(
    keys: list[str],
    *,
    new_days: int = 3,
    hot_min_count: int = 3,
    hot_top_n: int = 0,
    now: int | None = None,
) -> dict[str, list[str]]:
    """Return {key: ["new"/"hot", ...]} based on usage file.

    new_days=0 disables new; hot_min_count=0 disables hot.
    hot_top_n=0 means no top-N limit (all above threshold are hot).
    """
    ensure_keys(keys)
    entries = get_entries()
    ts = int(now if now is not None else time.time())

    enable_new = int(new_days) > 0
    enable_hot = int(hot_min_count) > 0
    new_window = max(0, int(new_days)) * 86400
    hot_min = max(0, int(hot_min_count))
    top_n = int(hot_top_n)

    hot_keys: set[str] = set()
    if enable_hot:
        ranked = sorted(
            ((k, entries.get(k, {}).get("count", 0)) for k in keys),
            key=lambda x: (-x[1], x[0]),
        )
        candidates = [(k, c) for k, c in ranked if c >= hot_min]
        if top_n > 0:
            candidates = candidates[:top_n]
        hot_keys = {k for k, _ in candidates}

    labels: dict[str, list[str]] = {}
    for key in keys:
        entry = entries.get(key) or {}
        labs: list[str] = []
        first_seen = int(entry.get("first_seen") or 0)
        # first_seen==0 means inventory-only registration, not a true "new" meme
        if enable_new and first_seen > 0 and (ts - first_seen) <= new_window:
            labs.append("new")
        if enable_hot and key in hot_keys:
            labs.append("hot")
        if labs:
            labels[key] = labs
    return labels


def usage_summary_text() -> str:
    entries = get_entries()
    total_uses = sum(v.get("count", 0) for v in entries.values())
    used = sum(1 for v in entries.values() if v.get("count", 0) > 0)
    top = sorted(entries.items(), key=lambda kv: (-kv[1].get("count", 0), kv[0]))[:10]
    lines = [
        f"计数文件: {usage_file()}",
        f"登记表情: {len(entries)}  有使用: {used}  总次数: {total_uses}",
    ]
    if top:
        lines.append("TOP:")
        for key, val in top:
            if val.get("count", 0) <= 0:
                break
            lines.append(f"  - {key}: {val.get('count', 0)}")
    return "\n".join(lines)


def reset_usage() -> dict[str, int]:
    """Reset global usage / hot / new counters.

    Returns summary of previous state.
    """
    with _LOCK:
        data = _load()
        memes = data.get("memes") if isinstance(data.get("memes"), dict) else {}
        prev_keys = len(memes)
        prev_uses = 0
        for v in memes.values():
            if isinstance(v, dict):
                prev_uses += int(v.get("count") or 0)
        data = _empty()
        _save(data)
        return {"keys": prev_keys, "uses": prev_uses}


def top_usage(limit: int = 20) -> list[tuple[str, int, int, int]]:
    """Return [(key, count, first_seen, last_used), ...] sorted by count desc."""
    limit = max(1, int(limit))
    entries = get_entries()
    ranked = sorted(
        entries.items(),
        key=lambda kv: (-int(kv[1].get("count") or 0), kv[0]),
    )
    out: list[tuple[str, int, int, int]] = []
    for key, val in ranked:
        count = int(val.get("count") or 0)
        if count <= 0:
            continue
        out.append(
            (
                key,
                count,
                int(val.get("first_seen") or 0),
                int(val.get("last_used") or 0),
            )
        )
        if len(out) >= limit:
            break
    return out


def record_trigger(key: str) -> int:
    """Global counter: +1 once per successful trigger (all sessions)."""
    return record_use(key, 1)
