# -*- coding: utf-8 -*-
"""컴비서 아이콘 생성기 (외부 라이브러리 없이 PNG + ICO 작성).

디자인: 파랑→보라 그라데이션 라운드 사각형 + 흰색 'C' 링(오른쪽 열림).
"""
import math
import os
import struct
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "combiyso.ico")
SUPER = 1024

C1 = (91, 140, 255)      # 파랑
C2 = (124, 92, 255)      # 보라
CORNER = 0.225           # 라운드 비율
OUTER = 0.315            # C 바깥 반지름
INNER = 0.192            # C 안쪽 반지름
GAP_DEG = 40.0           # 오른쪽 열린 각도


def render_master(n):
    """n x n RGBA(프리멀티플라이드 아님) 마스터 이미지."""
    buf = bytearray(n * n * 4)
    cx = cy = n / 2.0
    rad = n * CORNER
    for y in range(n):
        for x in range(n):
            # 라운드 사각형 내부 판정
            px = min(x, n - 1 - x)
            py = min(y, n - 1 - y)
            inside = True
            if px < rad and py < rad:
                dx, dy = rad - px, rad - py
                if dx * dx + dy * dy > rad * rad:
                    inside = False
            if not inside:
                continue
            # 그라데이션
            t = (x + y) / (2.0 * (n - 1))
            r = int(C1[0] + (C2[0] - C1[0]) * t)
            g = int(C1[1] + (C2[1] - C1[1]) * t)
            b = int(C1[2] + (C2[2] - C1[2]) * t)

            # C 링
            dxn = (x - cx) / n
            dyn = (y - cy) / n
            dist = math.hypot(dxn, dyn)
            if INNER <= dist <= OUTER:
                ang = math.degrees(math.atan2(dyn, dxn))
                if abs(ang) > GAP_DEG:
                    r = g = b = 255

            i = (y * n + x) * 4
            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b
            buf[i + 3] = 255
    return buf


def downsample(master, n, size):
    """프리멀티플라이드 박스 필터로 size x size 로 축소."""
    f = n // size
    out = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            pr = pg = pb = pa = 0.0
            for dy in range(f):
                base = ((oy * f + dy) * n + ox * f) * 4
                for dx in range(f):
                    i = base + dx * 4
                    a = master[i + 3]
                    pr += master[i] * a
                    pg += master[i + 1] * a
                    pb += master[i + 2] * a
                    pa += a
            cnt = float(f * f)
            o = (oy * size + ox) * 4
            if pa > 0:
                out[o] = int(pr / pa + 0.5)
                out[o + 1] = int(pg / pa + 0.5)
                out[o + 2] = int(pb / pa + 0.5)
                out[o + 3] = int(pa / cnt + 0.5)
    return bytes(out)


def png_bytes(rgba, w, h):
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib_bytes(rgba, w, h):
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            i = (y * w + x) * 4
            row += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))
        rows.append(bytes(row))
    mask_row = ((w + 31) // 32) * 4
    return hdr + b"".join(rows) + b"\x00" * (mask_row * h)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print("마스터 렌더링…", SUPER)
    master = render_master(SUPER)

    entries = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = downsample(master, SUPER, size)
        data = png_bytes(img, size, size) if size >= 128 else dib_bytes(img, size, size)
        entries.append((size, data))
        print(f"  {size}x{size} -> {len(data)} bytes")

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    dirs, blobs = b"", b""
    for size, data in entries:
        w = 0 if size >= 256 else size
        dirs += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(OUT, "wb") as f:
        f.write(header + dirs + blobs)
    print("저장:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
