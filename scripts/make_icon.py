"""生成 Paper Radar 的图标文件（纯标准库，不需要 Pillow）。

产物：
    paper_radar/web/favicon.ico   多尺寸 ICO（16/32/48 用 DIB 保证兼容，256 用 PNG 省体积）
    <输出目录>/icon-preview.png   仅供人眼检查的预览图

为什么用 Python 而不是 PowerShell：这台机器上的 Windows PowerShell 5.1
会在**解析期**解析 `[System.Drawing.*]` 这类类型字面量，`Add-Type` 加载完也来不及；
Python 标准库直接算像素更省事，也不受脚本编码问题影响。

用法：python scripts/make_icon.py [预览图输出目录]
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = ROOT / "paper_radar" / "web" / "favicon.ico"

DIB_SIZES = (16, 32, 48)
PNG_SIZE = 256
SUPERSAMPLE = 4

# 与 web/favicon.svg 保持同一套视觉语言
GRAD_FROM = (0x1E, 0x3A, 0x8A)
GRAD_TO = (0x25, 0x63, 0xEB)
RINGS = (
    # (半径, 线宽, 白色透明度 0..1)
    (0.300, 0.045, 0.55),
    (0.175, 0.045, 0.85),
)
DOT_RADIUS = 0.055
NEEDLE_FROM = (0.50, 0.50)
NEEDLE_TO = (0.78, 0.22)
NEEDLE_WIDTH = 0.05
CORNER_RADIUS = 0.22


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def _in_rounded_rect(u: float, v: float, radius: float = CORNER_RADIUS) -> bool:
    if u < 0 or u > 1 or v < 0 or v > 1:
        return False
    cx = min(max(u, radius), 1 - radius)
    cy = min(max(v, radius), 1 - radius)
    return (u - cx) ** 2 + (v - cy) ** 2 <= radius**2 or (radius <= u <= 1 - radius) or (radius <= v <= 1 - radius)


def _ring_hit(u: float, v: float, radius: float, width: float) -> bool:
    d = ((u - 0.5) ** 2 + (v - 0.5) ** 2) ** 0.5
    return abs(d - radius) <= width / 2


def _dot_hit(u: float, v: float) -> bool:
    return ((u - 0.5) ** 2 + (v - 0.5) ** 2) ** 0.5 <= DOT_RADIUS


def _needle_hit(u: float, v: float) -> bool:
    ax, ay = NEEDLE_FROM
    bx, by = NEEDLE_TO
    px, py = u - ax, v - ay
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, (px * dx + py * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return ((u - cx) ** 2 + (v - cy) ** 2) ** 0.5 <= NEEDLE_WIDTH / 2


def _gradient(u: float, v: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, (u + v) / 2))  # 135° 方向
    return tuple(round(a + (b - a) * t) for a, b in zip(GRAD_FROM, GRAD_TO))  # type: ignore[return-value]


def _sample(u: float, v: float) -> tuple[float, float, float, float]:
    """返回一个子采样点的**预乘 alpha** RGBA（0..255 归一化到 0..1）。"""
    if not _in_rounded_rect(u, v):
        return (0.0, 0.0, 0.0, 0.0)
    r, g, b = _gradient(u, v)
    pr, pg, pb, pa = r / 255, g / 255, b / 255, 1.0

    for radius, width, alpha in RINGS:
        if _ring_hit(u, v, radius, width):
            pr, pg, pb, pa = _over_white(pr, pg, pb, pa, alpha)
    if _dot_hit(u, v):
        pr, pg, pb, pa = _over_white(pr, pg, pb, pa, 1.0)
    if _needle_hit(u, v):
        pr, pg, pb, pa = _over_white(pr, pg, pb, pa, 1.0)
    return (pr, pg, pb, pa)


def _over_white(r: float, g: float, b: float, a: float, src_alpha: float):
    """source-over 合成一层白色（预乘 alpha）。"""
    sr = sg = sb = src_alpha
    out_a = src_alpha + a * (1 - src_alpha)
    out_r = sr + r * (1 - src_alpha)
    out_g = sg + g * (1 - src_alpha)
    out_b = sb + b * (1 - src_alpha)
    return (out_r, out_g, out_b, out_a)


def render_rgba(size: int) -> bytes:
    """渲染成 size×size 的直通（非预乘）RGBA 字节，行序从上到下。"""
    step = 1.0 / (size * SUPERSAMPLE)
    out = bytearray()
    samples = SUPERSAMPLE * SUPERSAMPLE
    for y in range(size):
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    u = (x * SUPERSAMPLE + sx + 0.5) * step
                    v = (y * SUPERSAMPLE + sy + 0.5) * step
                    sr, sg, sb, sa = _sample(u, v)
                    acc_r += sr
                    acc_g += sg
                    acc_b += sb
                    acc_a += sa
            r, g, b, a = acc_r / samples, acc_g / samples, acc_b / samples, acc_a / samples
            if a <= 0.0001:
                out += bytes((0, 0, 0, 0))
                continue
            out += bytes(
                (
                    max(0, min(255, round(r / a * 255))),
                    max(0, min(255, round(g / a * 255))),
                    max(0, min(255, round(b / a * 255))),
                    max(0, min(255, round(a * 255))),
                )
            )
    return bytes(out)


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def encode_png(rgba: bytes, size: int) -> bytes:
    """最小 PNG 编码器：8 位 RGBA、无滤波。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def encode_dib(rgba: bytes, size: int) -> bytes:
    """ICO 里的经典 DIB：BITMAPINFOHEADER + 自下而上的 BGRA + 1bpp AND 掩码。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for y in range(size - 1, -1, -1):  # 自下而上
        row = rgba[y * size * 4 : (y + 1) * size * 4]
        for x in range(size):
            r, g, b, a = row[x * 4 : x * 4 + 4]
            body += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    body += bytes(mask_stride * size)  # 全 0 = 掩码不裁剪（透明度由 alpha 通道决定）
    return header + bytes(body)


def build_ico(entries: list[tuple[int, bytes]]) -> bytes:
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = bytearray()
    payload = bytearray()
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    return out + bytes(directory) + bytes(payload)


def main() -> int:
    preview_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    dib_entries = [(size, encode_dib(render_rgba(size), size)) for size in DIB_SIZES]
    png_entries = [(PNG_SIZE, encode_png(render_rgba(PNG_SIZE), PNG_SIZE))]
    ico = build_ico(dib_entries + png_entries)
    ICO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICO_PATH.write_bytes(ico)
    print(f"[ico] {ICO_PATH} ({len(ico)} bytes, sizes={[s for s, _ in dib_entries + png_entries]})")

    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)
        target = preview_dir / "icon-preview.png"
        target.write_bytes(encode_png(render_rgba(256), 256))
        print(f"[png] {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
