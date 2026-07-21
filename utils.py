import io

from PIL import Image


def compress_image(image: bytes, max_size: int = 512) -> bytes | None:
    """压缩静态图片到 max_size；GIF 跳过压缩并返回 None。"""
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
        raise ValueError(f"图片压缩失败: {e}")
