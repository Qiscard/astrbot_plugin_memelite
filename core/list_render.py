# -*- coding: utf-8 -*-
"""Custom meme list image renderer (standard 3-col / compact 4-col)."""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

StyleName = Literal["standard", "compact"]

# emoji / symbol ranges commonly used in labels
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2300-\u23FF"
    "\u2B50"  # star
    "\U0001F525"  # fire
    "]+",
    flags=re.UNICODE,
)


@dataclass
class ListItem:
    key: str
    keywords: list[str] = field(default_factory=list)
    kind: str = "image"  # image | text
    labels: list[str] = field(default_factory=list)  # new / hot
    count: int = 0
    example: str = ""  # command example for standard mode


def _unique_existing(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        if Path(p).is_file():
            out.append(p)
    return out


def _plugin_font_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        from .resources import get_user_fonts_dir

        dirs.append(get_user_fonts_dir())
    except Exception:
        pass

    windir = os.environ.get("WINDIR", r"C:\Windows")
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    dirs.extend(
        [
            Path(local) / "Microsoft" / "Windows" / "Fonts",
            Path.home() / "Library" / "Fonts",
            Path.home() / ".local" / "share" / "fonts" / "meme-generator",
            Path.home() / ".local" / "share" / "fonts",
            Path(windir) / "Fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
        ]
    )
    return dirs


def _find_named_fonts(names: list[str]) -> list[str]:
    found: list[str] = []
    dirs = _plugin_font_dirs()
    for name in names:
        for d in dirs:
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            # direct file
            direct = d / name
            if direct.is_file():
                found.append(str(direct))
                continue
            # recursive shallow search by filename
            try:
                for p in d.rglob(name):
                    if p.is_file():
                        found.append(str(p))
                        break
            except OSError:
                continue
    return _unique_existing(found)


@lru_cache(maxsize=1)
def _resolve_fonts() -> tuple[str, str, str | None]:
    """Return (regular, bold, emoji) font paths."""
    # Prefer meme-generator / plugin-installed CJK fonts (clearer on list cards)
    preferred_reg = [
        "NotoSansSC-Regular.ttf",
        "NotoSansSC-Regular.otf",
        "NotoSerifSC-Regular.otf",
        "MiSans-Semibold.ttf",
        "DroidSansFallback.ttf",
        "GlowSansSC-Normal-Heavy.otf",
        "msyh.ttc",
        "msyh.ttf",
        "simhei.ttf",
        "NotoSansCJK-Regular.ttc",
        "wqy-microhei.ttc",
    ]
    preferred_bold = [
        "NotoSansSC-Bold.ttf",
        "NotoSansSC-Bold.otf",
        "NotoSerifSC-Bold.otf",
        "MiSans-Semibold.ttf",
        "msyhbd.ttc",
        "msyhbd.ttf",
        "NotoSansCJK-Bold.ttc",
        "msyh.ttc",
    ]
    # Prefer scalable emoji fonts (COLR/SVG/monochrome). Bitmap-strike color
    # fonts (NotoColorEmoji CBDT, Apple Color Emoji sbix) only carry a large
    # fixed strike (~109px) and degrade to boxes at the ~17px label size, so
    # they are listed last as a fallback only.
    emoji_names = [
        "seguiemj.ttf",  # Segoe UI Emoji (Windows, COLR)
        "SegoeUIEmoji.ttf",
        "TwitterColorEmoji-SVGinOT.ttf",  # SVG, scalable
        "NotoEmoji-Regular.ttf",  # monochrome, scalable
        "NotoColorEmoji.ttf",  # CBDT bitmap strike, does not scale small
        "Apple Color Emoji.ttc",  # sbix bitmap strike
    ]

    reg_list = _find_named_fonts(preferred_reg)
    bold_list = _find_named_fonts(preferred_bold)
    emoji_list = _find_named_fonts(emoji_names)

    # hard fallbacks
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in preferred_reg:
        p = windir / name
        if p.is_file():
            reg_list.append(str(p))
    for name in preferred_bold:
        p = windir / name
        if p.is_file():
            bold_list.append(str(p))
    for name in emoji_names:
        p = windir / name
        if p.is_file():
            emoji_list.append(str(p))

    reg = reg_list[0] if reg_list else str(windir / "arial.ttf")
    bold = bold_list[0] if bold_list else reg
    emoji = emoji_list[0] if emoji_list else None
    return reg, bold, emoji


def _load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size=size, index=0)
    except Exception:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            return ImageFont.load_default()


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    reg, bld, _ = _resolve_fonts()
    return _load_font(bld if bold else reg, size)


def _emoji_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
    _, _, emoji = _resolve_fonts()
    if not emoji:
        return None
    return _load_font(emoji, size)


def _text_size(draw: ImageDraw.ImageDraw, text: str, fnt) -> tuple[int, int]:
    if not text:
        return 0, 0
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _measure_mixed(
    draw: ImageDraw.ImageDraw,
    text: str,
    base_font,
    emoji_font,
    emoji_scale: float = 1.0,
) -> tuple[int, int]:
    if not text:
        return 0, 0
    if emoji_font is None:
        return _text_size(draw, text, base_font)

    width = 0
    height = 0
    pos = 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos : m.start()]
            w, h = _text_size(draw, chunk, base_font)
            width += w
            height = max(height, h)
        emo = m.group(0)
        # emoji font size may differ; measure with emoji font
        w, h = _text_size(draw, emo, emoji_font)
        width += max(1, int(w * emoji_scale))
        height = max(height, int(h * emoji_scale))
        pos = m.end()
    if pos < len(text):
        chunk = text[pos:]
        w, h = _text_size(draw, chunk, base_font)
        width += w
        height = max(height, h)
    return width, height


def _draw_text_lm(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill,
    *,
    embedded_color: bool = False,
) -> None:
    """Draw with left-middle anchor for stable vertical centering in chips."""
    if not text:
        return
    x, y = xy
    try:
        if embedded_color:
            draw.text((x, y), text, font=font, fill=fill, anchor="lm", embedded_color=True)
        else:
            draw.text((x, y), text, font=font, fill=fill, anchor="lm")
        return
    except TypeError:
        pass
    # fallback: manual optical center using ascent/descent
    try:
        ascent, descent = font.getmetrics()
        top = y - (ascent - descent) / 2 - descent * 0.15
    except Exception:
        w, h = _text_size(draw, text, font)
        top = y - h / 2
    if embedded_color:
        try:
            draw.text((x, top), text, font=font, fill=fill, embedded_color=True)
            return
        except TypeError:
            pass
    draw.text((x, top), text, font=font, fill=fill)


def _draw_mixed_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    base_font,
    fill,
    emoji_font=None,
    emoji_fill=None,
    anchor_y_center: float | None = None,
) -> int:
    """Draw text with optional emoji font fallback. Returns total width.

    When anchor_y_center is set, use left-middle anchoring so CJK digits and
    emoji share the same visual midline (avoids text-low / emoji-high).
    """
    x, y = xy
    if not text:
        return 0

    mid_y = anchor_y_center if anchor_y_center is not None else None

    if emoji_font is None:
        if mid_y is not None:
            _draw_text_lm(draw, (x, mid_y), text, base_font, fill)
        else:
            draw.text((x, y), text, font=base_font, fill=fill)
        return _text_size(draw, text, base_font)[0]

    pos = 0
    cursor = x
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos : m.start()]
            w, _ = _text_size(draw, chunk, base_font)
            if mid_y is not None:
                _draw_text_lm(draw, (cursor, mid_y), chunk, base_font, fill)
            else:
                draw.text((cursor, y), chunk, font=base_font, fill=fill)
            cursor += w
        emo = m.group(0)
        w, _ = _text_size(draw, emo, emoji_font)
        if mid_y is not None:
            # emoji fonts often sit high; nudge slightly downward for optical center
            try:
                _draw_text_lm(
                    draw,
                    (cursor, mid_y + 1.0),
                    emo,
                    emoji_font,
                    emoji_fill or fill,
                    embedded_color=True,
                )
            except Exception:
                _draw_text_lm(draw, (cursor, mid_y + 1.0), emo, emoji_font, emoji_fill or fill)
        else:
            try:
                draw.text((cursor, y), emo, font=emoji_font, fill=emoji_fill or fill, embedded_color=True)
            except TypeError:
                draw.text((cursor, y), emo, font=emoji_font, fill=emoji_fill or fill)
        cursor += w
        pos = m.end()
    if pos < len(text):
        chunk = text[pos:]
        w, _ = _text_size(draw, chunk, base_font)
        if mid_y is not None:
            _draw_text_lm(draw, (cursor, mid_y), chunk, base_font, fill)
        else:
            draw.text((cursor, y), chunk, font=base_font, fill=fill)
        cursor += w
    return int(cursor - x)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_w: int,
    max_lines: int,
    emoji_font=None,
) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if max_w <= 0:
        return [text[:1]]

    # prefer wrapping on separators
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch in {"/", " ", "，", ",", "、", "|"}:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        else:
            buf += ch
    if buf:
        tokens.append(buf)

    lines: list[str] = []
    cur = ""

    def wider(s: str) -> int:
        return _measure_mixed(draw, s, font, emoji_font)[0]

    def push_line(s: str) -> None:
        if s:
            lines.append(s)

    for tok in tokens:
        trial = cur + tok
        if wider(trial) <= max_w or not cur:
            # if single token too long, hard-split chars
            if wider(trial) > max_w and not cur:
                piece = ""
                for ch in tok:
                    t2 = piece + ch
                    if wider(t2) <= max_w or not piece:
                        piece = t2
                    else:
                        push_line(piece)
                        if len(lines) >= max_lines:
                            return lines
                        piece = ch
                cur = piece
            else:
                cur = trial
            continue
        push_line(cur)
        if len(lines) >= max_lines:
            return lines
        # start new line with tok (strip leading spaces)
        cur = tok.lstrip(" ")
        if wider(cur) > max_w:
            piece = ""
            for ch in cur:
                t2 = piece + ch
                if wider(t2) <= max_w or not piece:
                    piece = t2
                else:
                    push_line(piece)
                    if len(lines) >= max_lines:
                        return lines
                    piece = ch
            cur = piece

    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur and lines:
        # ellipsis on last line
        last = lines[-1]
        while last and wider(last + "…") > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"

    # if overflow remaining content, ellipsize last
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines


def _style_params(style: StyleName) -> dict:
    if style == "compact":
        return {
            "width": 1280,
            "cols": 4,
            "card_h_min": 44,
            "line_h": 16,
            "header_h": 104,
            "footer_h": 44,
            "margin": 22,
            "gap_x": 10,
            "gap_y": 8,
            "title_size": 30,
            "sub_size": 14,
            "item_size": 15,
            "key_size": 12,
            "show_example": False,
            "max_title_lines": 3,
            "max_example_lines": 0,
            "default_rows": 40,
            "default_page_size": 160,  # 4 * 40
        }
    return {
        "width": 1280,
        "cols": 3,
        "card_h_min": 64,
        "line_h": 19,
        "header_h": 112,
        "footer_h": 48,
        "margin": 28,
        "gap_x": 12,
        "gap_y": 10,
        "title_size": 34,
        "sub_size": 16,
        "item_size": 17,
        "key_size": 13,
        "show_example": True,
        "max_title_lines": 4,
        "max_example_lines": 3,
        "default_rows": 30,
        "default_page_size": 90,  # 3 * 30
    }


def paginate_items(items: list[ListItem], page_size: int) -> list[list[ListItem]]:
    if page_size <= 0:
        page_size = max(1, len(items) or 1)
    if not items:
        return [[]]
    return [items[i : i + page_size] for i in range(0, len(items), page_size)]


def _draw_chip(
    draw: ImageDraw.ImageDraw,
    left: float,
    center_y: float,
    text: str,
    bg,
    fg,
    font,
    emoji_font=None,
    pad_x: int = 7,
    pad_y: int = 4,
) -> tuple[float, float]:
    """Draw a vertically centered chip. Returns (width, height)."""
    tw, th = _measure_mixed(draw, text, font, emoji_font)
    # fixed visual height reduces jitter between digit/emoji chips
    chip_h = max(22, int(th + pad_y * 2))
    chip_w = max(tw + pad_x * 2, chip_h)
    x0 = left
    y0 = center_y - chip_h / 2
    draw.rounded_rectangle((x0, y0, x0 + chip_w, y0 + chip_h), radius=999, fill=bg)
    # left-middle anchor on the chip's true center line
    _draw_mixed_text(
        draw,
        (x0 + (chip_w - tw) / 2, center_y),
        text,
        font,
        fill=fg,
        emoji_font=emoji_font,
        emoji_fill=fg,
        anchor_y_center=center_y,
    )
    return float(chip_w), float(chip_h)


def _estimate_card_height(
    draw,
    item: ListItem,
    cfg: dict,
    text_max_w: int,
    item_font,
    example_font,
    emoji_font,
) -> int:
    title = " / ".join(item.keywords[:4]) if item.keywords else item.key
    lines = _wrap_text(
        draw,
        title,
        item_font,
        text_max_w,
        int(cfg["max_title_lines"]),
        emoji_font=emoji_font,
    )
    line_h = int(cfg["line_h"])
    content_h = max(1, len(lines)) * line_h
    if cfg["show_example"] and int(cfg.get("max_example_lines") or 0) > 0:
        example = item.example or ""
        if example:
            ex_lines = _wrap_text(
                draw,
                example,
                example_font,
                text_max_w,
                int(cfg["max_example_lines"]),
                emoji_font=emoji_font,
            )
            content_h += 4 + max(1, len(ex_lines)) * (line_h - 2)
    # paddings
    h = content_h + 18
    return max(int(cfg["card_h_min"]), h)


def render_list_page(
    items: list[ListItem],
    *,
    style: StyleName = "standard",
    page_index: int = 1,
    page_total: int = 1,
    total_count: int | None = None,
    title: str = "meme表情列表",
    start_index: int = 1,
    footer_text: str = "",
) -> bytes:
    cfg = _style_params(style)
    W = int(cfg["width"])
    COLS = int(cfg["cols"])
    HEADER_H = int(cfg["header_h"])
    FOOTER_H = int(cfg["footer_h"])
    MARGIN = int(cfg["margin"])
    GAP_X = int(cfg["gap_x"])
    GAP_Y = int(cfg["gap_y"])
    show_example = bool(cfg["show_example"])

    total = total_count if total_count is not None else len(items)

    # theme
    BG = (244, 247, 251)
    CARD = (255, 255, 255)
    BORDER = (226, 232, 240)
    TEXT = (15, 23, 42)
    MUTED = (100, 116, 139)
    ACCENT = (13, 148, 136)
    CHIP_IMG = (219, 234, 254)
    CHIP_IMG_T = (30, 64, 175)
    CHIP_TXT = (254, 243, 199)
    CHIP_TXT_T = (146, 64, 14)
    NEW_BG = (254, 243, 199)
    HOT_BG = (255, 237, 213)
    COUNT_BG = (224, 242, 254)
    COUNT_FG = (3, 105, 161)
    H1 = (15, 118, 110)
    H2 = (20, 184, 166)
    IDX = (148, 163, 184)

    # fonts
    title_font = _font(int(cfg["title_size"]), bold=True)
    sub_font = _font(int(cfg["sub_size"]))
    item_font = _font(int(cfg["item_size"]), bold=True)
    example_font = _font(int(cfg["key_size"]))
    badge_font = _font(13, bold=True)
    idx_font = _font(12)
    emoji_font = _emoji_font(17)
    emoji_badge = _emoji_font(17)

    # probe draw for measuring
    probe = Image.new("RGB", (10, 10), BG)
    pdraw = ImageDraw.Draw(probe)

    content_w = W - MARGIN * 2
    col_w = (content_w - GAP_X * (COLS - 1)) // COLS

    # precompute per-item card heights and row heights
    # reserved right chips approx
    def right_reserve(it: ListItem) -> int:
        n = 0
        if it.count > 0:
            n += 34
        for lab in it.labels or []:
            if lab in {"new", "hot"}:
                n += 30
        return n + 8

    # text area starts after index + kind chip
    text_left_pad = 46 + 28  # idx area + kind chip approx
    heights: list[int] = []
    for it in items:
        max_text_w = col_w - text_left_pad - right_reserve(it) - 12
        heights.append(
            _estimate_card_height(
                pdraw, it, cfg, max(40, max_text_w), item_font, example_font, emoji_font
            )
        )

    # row heights = max card in row
    row_heights: list[int] = []
    if not items:
        row_heights = [int(cfg["card_h_min"])]
    else:
        rows = (len(items) + COLS - 1) // COLS
        for r in range(rows):
            chunk = heights[r * COLS : (r + 1) * COLS]
            row_heights.append(max(chunk) if chunk else int(cfg["card_h_min"]))

    body_h = sum(row_heights) + GAP_Y * max(len(row_heights) - 1, 0) + 12
    H = HEADER_H + body_h + FOOTER_H + 10

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # header gradient
    for y in range(HEADER_H):
        t = y / max(HEADER_H - 1, 1)
        r = int(H1[0] * (1 - t) + H2[0] * t)
        g = int(H1[1] * (1 - t) + H2[1] * t)
        b = int(H1[2] * (1 - t) + H2[2] * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    overlay = Image.new("RGBA", (W, HEADER_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((W - 240, -90, W + 20, 150), fill=(255, 255, 255, 30))
    od.ellipse((W - 380, 10, W - 140, 230), fill=(255, 255, 255, 16))
    base = img.crop((0, 0, W, HEADER_H)).convert("RGBA")
    img.paste(Image.alpha_composite(base, overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)

    # Header: large title + small total only
    draw.text((MARGIN, 24), title or "meme表情列表", font=title_font, fill=(255, 255, 255))
    sub = f"共{total}个"
    draw.text((MARGIN, 70 if style == "standard" else 66), sub, font=sub_font, fill=(236, 253, 245))

    # legend
    legends = [
        ("图片", CHIP_IMG, CHIP_IMG_T, False),
        ("文本", CHIP_TXT, CHIP_TXT_T, False),
        ("⭐", NEW_BG, (180, 83, 9), True),
        ("🔥", HOT_BG, (194, 65, 12), True),
    ]
    lx = W - MARGIN
    for text, bg, fg, is_emoji in reversed(legends):
        fnt = badge_font
        ef = emoji_badge if is_emoji else None
        tw, th = _measure_mixed(draw, text, fnt, ef)
        chip_w, chip_h = tw + 18, max(th + 10, 24)
        lx -= chip_w
        cy = 34 + chip_h / 2
        draw.rounded_rectangle((lx, 34, lx + chip_w, 34 + chip_h), radius=chip_h // 2, fill=bg)
        _draw_mixed_text(
            draw,
            (lx + 9, 34 + 5),
            text,
            fnt,
            fill=fg,
            emoji_font=ef,
            emoji_fill=fg,
            anchor_y_center=cy,
        )
        lx -= 8

    # body cards
    y0 = HEADER_H + 12
    row_y = y0
    for i, it in enumerate(items):
        ci = i % COLS
        ri = i // COLS
        if ci == 0 and i > 0:
            row_y += row_heights[ri - 1] + GAP_Y
        x = MARGIN + ci * (col_w + GAP_X)
        card_h = row_heights[ri]
        y = row_y

        draw.rounded_rectangle(
            (x, y, x + col_w, y + card_h),
            radius=12 if style == "standard" else 10,
            fill=CARD,
            outline=BORDER,
            width=1,
        )
        accent = ACCENT if it.kind == "image" else (245, 158, 11)
        draw.rounded_rectangle((x, y + 8, x + 4, y + card_h - 8), radius=2, fill=accent)

        # index
        idx_txt = f"{start_index + i:03d}"
        iw, ih = _text_size(draw, idx_txt, idx_font)
        _draw_text_lm(draw, (x + 10, y + card_h / 2), idx_txt, idx_font, IDX)

        # kind chip
        kind_label = "图" if it.kind == "image" else "文"
        kind_bg = CHIP_IMG if it.kind == "image" else CHIP_TXT
        kind_fg = CHIP_IMG_T if it.kind == "image" else CHIP_TXT_T
        kind_w, _ = _draw_chip(
            draw,
            x + 44,
            y + card_h / 2,
            kind_label,
            kind_bg,
            kind_fg,
            badge_font,
            emoji_font=None,
            pad_x=7,
            pad_y=3,
        )

        # right chips
        chips: list[tuple[str, tuple, tuple, bool]] = []
        if it.count > 0:
            chips.append((str(it.count), COUNT_BG, COUNT_FG, False))
        for tag in it.labels or []:
            if tag == "new":
                chips.append(("⭐", NEW_BG, (180, 83, 9), True))
            elif tag == "hot":
                chips.append(("🔥", HOT_BG, (194, 65, 12), True))

        tx = x + col_w - 8
        for text, bg, fg, is_emoji in reversed(chips):
            tw, th = _measure_mixed(draw, text, badge_font, emoji_badge if is_emoji else None)
            chip_w = tw + 14
            tx -= chip_w
            _draw_chip(
                draw,
                tx,
                y + card_h / 2,
                text,
                bg,
                fg,
                badge_font,
                emoji_font=emoji_badge if is_emoji else None,
                pad_x=7,
                pad_y=3,
            )
            tx -= 6

        # text block
        text_x = x + 44 + kind_w + 10
        max_text_w = int(tx - text_x - 6)
        if max_text_w < 40:
            max_text_w = 40

        title_text = " / ".join(item.keywords[:4]) if (item := it).keywords else it.key
        title_lines = _wrap_text(
            draw,
            title_text,
            item_font,
            max_text_w,
            int(cfg["max_title_lines"]),
            emoji_font=emoji_font,
        )
        if not title_lines:
            title_lines = [it.key]

        ex_lines: list[str] = []
        max_ex = int(cfg.get("max_example_lines") or 0)
        if show_example and max_ex > 0:
            example = (it.example or "").strip()
            if example:
                ex_lines = _wrap_text(
                    draw, example, example_font, max_text_w, max_ex, emoji_font=emoji_font
                )

        line_h = int(cfg["line_h"])
        block_h = len(title_lines) * line_h
        if ex_lines:
            block_h += 4 + len(ex_lines) * (line_h - 2)
        ty = y + (card_h - block_h) / 2

        for li, line in enumerate(title_lines):
            draw.text((text_x, ty + li * line_h), line, font=item_font, fill=TEXT)
        if ex_lines:
            ey = ty + len(title_lines) * line_h + 3
            for li, line in enumerate(ex_lines):
                draw.text((text_x, ey + li * (line_h - 2)), line, font=example_font, fill=MUTED)

    # footer
    footer_y = H - FOOTER_H + 14
    draw.line([(MARGIN, H - FOOTER_H + 2), (W - MARGIN, H - FOOTER_H + 2)], fill=BORDER, width=1)
    left = f"第{page_index}页/共{page_total}页" if page_total > 0 else "第1页/共1页"
    draw.text((MARGIN, footer_y), left, font=_font(13), fill=MUTED)
    right = footer_text or "默认+0组额外表情包，最近更新：—"
    rf = _font(12)
    max_right_w = max(80, W - MARGIN * 2 - 140)
    orig_right = right
    while len(right) > 1 and _text_size(draw, right, rf)[0] > max_right_w:
        right = right[:-1]
    if right != orig_right:
        right = (right[:-1] + "…") if len(right) > 1 else "…"
    bw2, _ = _text_size(draw, right, rf)
    draw.text((W - MARGIN - bw2, footer_y), right, font=rf, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_meme_list_images(
    items: list[ListItem],
    *,
    style: StyleName = "standard",
    page_size: int | None = None,
    title: str = "meme表情列表",
    footer_text: str = "",
) -> list[bytes]:
    cfg = _style_params(style)
    size = int(page_size) if page_size and page_size > 0 else int(cfg["default_page_size"])
    pages = paginate_items(items, size)
    total = len(items)
    out: list[bytes] = []
    cursor = 1
    for idx, page_items in enumerate(pages, start=1):
        out.append(
            render_list_page(
                page_items,
                style=style,
                page_index=idx,
                page_total=len(pages),
                total_count=total,
                title=title,
                start_index=cursor,
                footer_text=footer_text,
            )
        )
        cursor += len(page_items)
    return out
