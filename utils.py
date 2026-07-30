import io
import re

from PIL import Image

# QQ official / botpy style: <@OPENID> or <@!OPENID>
_MENTION_TAG_RE = re.compile(r"<@!?([0-9A-Za-z_\-]+)>")


def extract_mention_ids(text: str | None) -> list[str]:
    """Extract user ids from <@id> / <@!id> tags in order (deduped)."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for mid in _MENTION_TAG_RE.findall(str(text)):
        mid = str(mid).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def strip_mention_tags(text: str | None) -> str:
    """Replace mention tags with spaces so keyword splitting works."""
    if not text:
        return ""
    cleaned = _MENTION_TAG_RE.sub(" ", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def split_meme_command(text: str | None) -> tuple[str, str]:
    """Split raw message into (first_token, rest) after stripping mention tags."""
    cleaned = strip_mention_tags(text)
    if not cleaned:
        return "", ""
    parts = cleaned.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def compress_image(image: bytes, max_size: int = 512) -> bytes | None:
    """Compress static image to max_size; skip GIF and return None."""
    try:
        img = Image.open(io.BytesIO(image))
        if img.format == "GIF":
            return None

        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        fmt = img.format or "PNG"
        if fmt.upper() == "JPEG" and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        output = io.BytesIO()
        img.save(output, format=fmt)
        return output.getvalue()
    except Exception as e:
        raise ValueError(f"image compress failed: {e}")
