#!/usr/bin/env python3
"""NASCAR 06 PS2 / Xbox MMAP texture converter, experimental v0.5.2.

Requires numpy and Pillow. Reads only the input MMAP bytes; no reference PNGs.

Supported:
  - Xbox format 6 / mode 0, palette-free BGRA32 Morton texels
    (verified on NASCAR 06 Dodge Charger road-car texture and its mip chain)
  - Xbox format 11 / mode 0, BGRA palette kind 1 / mode 0
    (empirical image alignment / palette prefix rules; auto alpha preserves raw bytes)
  - serialized MMAP v2
  - format 12 / mode 0 + palette kind 1 / mode 3 / 1024 bytes
  - experimental format 1 / mode 0 + palette kind 1 / mode 3 / 1024 bytes
  - experimental format 0 / mode 0 + palette kind 0 / mode 3 / 64 bytes
  - format 1 / mode 0 + 16-colour palettes: exports low/high nibble planes
  - compound resources using one image with multiple palettes

Output:
  - decoded PNG textures
  - one conversion_manifest.json for the whole batch

Format 1 and 0 support is best-effort and uses small heuristics to choose
between linear and GS-swizzled indexed layouts.
"""

import argparse
import re
import glob
import hashlib
import json
from pathlib import Path
import struct
import sys

import numpy as np
from PIL import Image


class Unsupported(ValueError):
    pass


def unpack(data, fmt, offset):
    if offset < 0 or offset + struct.calcsize(fmt) > len(data):
        raise ValueError(f'truncated structure at {offset:#x}')
    return struct.unpack_from(fmt, data, offset)


def span(data, offset, size):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError(
            f'truncated payload at {offset:#x}, size {size:#x}'
        )
    return data[offset:offset + size]


def align(value, multiple):
    return ((value + multiple - 1) // multiple) * multiple


def next_power_two(value):
    return 1 if value <= 1 else 1 << (value - 1).bit_length()


def read_mappings(data):
    if data[:4] != b'MMAP':
        raise Unsupported('not an MMAP resource')
    version, flags = unpack(data, '<HH', 4)
    if version != 2 or flags & 1:
        raise Unsupported('requires serialized, unrelocated MMAP version 2')
    nm, ni, npal = unpack(data, '<3H', 12)
    mt, it, pt = unpack(data, '<3I', 20)
    if not 0 < nm <= 4096 or not 0 < ni <= 4096 or not 0 <= npal <= 4096:
        raise Unsupported('unsupported empty or excessive table counts')
    span(data, mt, nm * 12)
    span(data, it, ni * 16)
    span(data, pt, npal * 12)
    mappings = []
    for n in range(nm):
        pc, ic, image_index, palette_index = unpack(data, '<HHII', mt + n * 12)
        if ic < 1:
            raise Unsupported('mapping without images')
        if pc == 0 and (npal != 0 or palette_index != 0):
            raise Unsupported('unrecognized palette-free mapping profile')
        if image_index + ic > ni or palette_index + pc > npal:
            raise ValueError('mapping range outside descriptor table')
        for islot in range(ic):
            ii = image_index + islot
            w, h, fmt, mode, size, offset = unpack(data, '<4H2I', it + ii * 16)
            if ic != 1 and fmt not in (6, 11):
                raise Unsupported('multiple images per mapping supported only for Xbox formats 6 and 11')
            if not 0 < w <= 1024 or not 0 < h <= 1024:
                raise Unsupported('dimensions outside supported 1..1024 range')
            span(data, offset, size)
            if pc == 0:
                if (fmt, mode) != (6, 0):
                    raise Unsupported('palette-free mapping requires Xbox format 6 / mode 0')
                mappings.append(dict(mapping=n, image_index=ii, image_slot=islot,
                    palette_index=None, palette_slot=None, palette_count=0, image_count=ic,
                    width=w, height=h, format=fmt, mode=mode, image_offset=offset,
                    image_bytes=size, palette_kind=None, palette_mode=None,
                    palette_offset=None, palette_bytes=0))
                continue
            for pslot in range(pc):
                pi = palette_index + pslot
                kind, pmode, psize, poffset = unpack(data, '<HHII', pt + pi * 12)
                span(data, poffset, psize)
                mappings.append(dict(mapping=n, image_index=ii, image_slot=islot,
                    palette_index=pi, palette_slot=pslot, palette_count=pc, image_count=ic,
                    width=w, height=h, format=fmt, mode=mode, image_offset=offset,
                    image_bytes=size, palette_kind=kind, palette_mode=pmode,
                    palette_offset=poffset, palette_bytes=psize))
    return mappings


def xbox_payload(data, stored, size, label):
    # Empirical profile, not a proven universal MMAP rule. Aligned mip offsets
    # stay unchanged; do NOT add 128 unconditionally.
    effective = align(stored, 256)
    if any(span(data, stored, effective - stored)):
        raise Unsupported(f'Xbox {label}: nonzero alignment padding; unknown layout')
    return span(data, effective, size), effective


def decode_xbox_format11(data, mapping):
    w, h = mapping['width'], mapping['height']
    if w & (w-1) or h & (h-1):
        raise Unsupported('Xbox indexed layout requires power-of-two dimensions')
    if mapping['image_bytes'] != w*h:
        raise Unsupported('Xbox indexed payload size does not match dimensions')
    raw, image_offset = xbox_payload(data, mapping['image_offset'], w*h, 'image')
    # Palette records have a 128-byte zero prefix, including records whose
    # stored offsets are already 256-byte aligned. Rounding those offsets
    # instead would read 32 zero colours and omit the final 32 real entries.
    stored_palette = mapping['palette_offset']
    if any(span(data, stored_palette, 128)):
        raise Unsupported('Xbox palette: nonzero 128-byte prefix; unknown layout')
    palette_offset = stored_palette + 128
    pal = span(data, palette_offset, 1024)
    if max(image_offset, palette_offset) < min(image_offset+w*h, palette_offset+1024):
        raise ValueError('Xbox image and palette payloads overlap')
    y, x = np.indices((h, w), dtype=np.int64)
    address = np.zeros((h, w), dtype=np.int64)
    output_bit = 0
    for bit in range(max(w, h).bit_length()-1):
        if 1 << bit < w:
            address |= ((x >> bit) & 1) << output_bit
            output_bit += 1
        if 1 << bit < h:
            address |= ((y >> bit) & 1) << output_bit
            output_bit += 1
    indices = np.frombuffer(raw, dtype=np.uint8)[address]
    palette = np.frombuffer(pal, dtype=np.uint8).reshape(256,4)
    rgba = palette[indices][:, :, [2,1,0,3]].copy()
    return rgba, indices, dict(platform='Xbox', layout='indexed8_morton_xy',
        palette='BGRA8 to RGBA8; no PS2 CLUT shuffle',
        stored_image_offset=mapping['image_offset'], effective_image_offset=image_offset,
        stored_palette_offset=mapping['palette_offset'], effective_palette_offset=palette_offset,
        image_offset_rule='empirical align-up-to-256, zero padding required',
        palette_offset_rule='empirical stored offset + 128, zero prefix required',
        evidence='visually checked samples; runtime alignment rule and alpha semantics unresolved')


def decode_xbox_format6(data, mapping):
    """Observed NASCAR 06 direct-colour profile; no palette/index stream."""
    w, h = mapping['width'], mapping['height']
    if w <= 0 or h <= 0 or w & (w - 1) or h & (h - 1):
        raise Unsupported('Xbox direct-colour layout requires power-of-two dimensions')
    if mapping['image_bytes'] != w * h * 4:
        raise Unsupported('Xbox direct-colour payload size does not match dimensions')
    raw, image_offset = xbox_payload(data, mapping['image_offset'], w * h * 4, 'image')
    y, x = np.indices((h, w), dtype=np.int64)
    address = np.zeros((h, w), dtype=np.int64)
    output_bit = 0
    for bit in range(max(w, h).bit_length() - 1):
        if 1 << bit < w:
            address |= ((x >> bit) & 1) << output_bit
            output_bit += 1
        if 1 << bit < h:
            address |= ((y >> bit) & 1) << output_bit
            output_bit += 1
    texels = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4)
    rgba = texels[address][:, :, [2, 1, 0, 3]].copy()
    return rgba, None, dict(platform='Xbox', layout='direct32_morton_xy',
        channels='BGRA8 to RGBA8; fourth byte preserved', palette=None,
        stored_image_offset=mapping['image_offset'], effective_image_offset=image_offset,
        image_offset_rule='empirical align-up-to-256, zero padding required',
        evidence='verified Charger road-car texture and three image levels; broader format and alpha semantics unresolved')


def gs_byte_addresses(width, height, pitch, depth):
    """GS PSMCT32/PSMT8 byte addresses.

    pitch is pixels, not the GS TBW register.
    """

    y, x = np.indices(
        (height, width),
        dtype=np.int64
    )

    if depth == 32:

        if pitch % 64:
            raise ValueError(
                '32-bit pitch must be a multiple of 64'
            )

        px, py = x // 64, y // 32
        bx, by = (x % 64) // 8, (y % 32) // 8
        xx, yy = x % 8, y % 8

        col = (
            (xx & 1)
            | ((yy & 1) << 1)
            | ((xx & 6) << 1)
            | ((yy & 6) << 3)
        ) * 4

        pages = pitch // 64

    elif depth == 8:

        if pitch % 128:
            raise ValueError(
                '8-bit pitch must be a multiple of 128'
            )

        px, py = x // 128, y // 64

        bx, by = (
            (x % 128) // 16,
            (y % 64) // 16
        )

        xx, yy = x % 16, y % 16

        col = (
            ((yy >> 1) & 1)
            | ((xx >> 2) & 2)
            | ((xx & 1) << 2)
            | ((yy & 1) << 3)
            | ((xx & 2) << 3)
            | (
                (
                    (xx >> 2)
                    ^ (yy >> 1)
                    ^ (yy >> 2)
                ) & 1
            ) << 5
            | ((yy & 4) << 4)
            | ((yy & 8) << 4)
        )

        pages = pitch // 128

    else:
        raise ValueError(
            'unsupported address depth'
        )

    block = (
        (bx & 1)
        | ((by & 1) << 1)
        | ((bx & 2) << 1)
        | ((by & 2) << 2)
        | ((bx & 4) << 2)
    )

    return (
        (px + py * pages) * 8192
        + block * 256
        + col
    )


# ------------------------------------------------------------
# PSMT4 lookup tables
# ------------------------------------------------------------

PSMT4_BLOCK_LAYOUT = np.array([
     0,  2,  8, 10,
     1,  3,  9, 11,
     4,  6, 12, 14,
     5,  7, 13, 15,
    16, 18, 24, 26,
    17, 19, 25, 27,
    20, 22, 28, 30,
    21, 23, 29, 31,
], dtype=np.int16)


PSMT4_COLUMN_WORD = np.array([
    [
         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,

         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,

         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,

        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,
    ],

    [
         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,

        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,

         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,

         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,
    ],
], dtype=np.int16)


PSMT4_COLUMN_BYTE = np.array([
    0, 0, 0, 0, 0, 0, 0, 0,
    2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4,
    6, 6, 6, 6, 6, 6, 6, 6,

    0, 0, 0, 0, 0, 0, 0, 0,
    2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4,
    6, 6, 6, 6, 6, 6, 6, 6,

    1, 1, 1, 1, 1, 1, 1, 1,
    3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5,
    7, 7, 7, 7, 7, 7, 7, 7,

    1, 1, 1, 1, 1, 1, 1, 1,
    3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5,
    7, 7, 7, 7, 7, 7, 7, 7,
], dtype=np.int16)


def read_psmt8_from_memory(
    memory,
    width,
    height,
    pitch
):

    if pitch % 128:
        raise ValueError(
            'PSMT8 pitch must be a multiple of 128 pixels'
        )

    addresses = gs_byte_addresses(
        width,
        height,
        pitch,
        8
    )

    return memory[addresses].copy()


def read_psmt4_from_memory(
    memory,
    width,
    height,
    pitch
):

    if pitch % 128:
        raise ValueError(
            'PSMT4 pitch must be a multiple of 128 pixels'
        )

    out = np.empty(
        (height, width),
        dtype=np.uint8
    )

    pages = pitch // 128

    for y in range(height):

        page_y = y // 128
        py = y - page_y * 128

        block_y = py // 16
        by = py - block_y * 16

        column = by // 4
        cy = by - column * 4

        for x in range(width):

            page_x = x // 128
            px = x - page_x * 128

            page = page_x + page_y * pages

            block_x = px // 32

            block = int(
                PSMT4_BLOCK_LAYOUT[
                    block_x + block_y * 4
                ]
            )

            bx = px - block_x * 32
            cx = bx

            idx = cx + cy * 32

            cw = int(
                PSMT4_COLUMN_WORD[
                    column & 1,
                    idx
                ]
            )

            cb = int(
                PSMT4_COLUMN_BYTE[idx]
            )

            addr = (
                page * 2048
                + block * 64
                + column * 16
                + cw
                + (cb >> 1)
            )

            packed = int(memory[addr])

            odd = (
                y * width + x
            ) & 1

            if cb & 1:

                nibble = (
                    (packed >> 4) & 0xF
                    if odd
                    else packed & 0xF
                )

            else:

                nibble = (
                    packed & 0xF
                    if odd
                    else (packed >> 4) & 0xF
                )

            out[y, x] = nibble

    return out


def clut_shuffle_8(indices):

    return (
        (indices & 0xE7)
        | ((indices & 0x08) << 1)
        | ((indices & 0x10) >> 1)
    )


def apply_palette(
    indices,
    palette,
    palette_kind
):

    if (
        palette_kind == 1
        and len(palette) == 256
    ):

        rgba = palette[
            clut_shuffle_8(indices)
        ]

    elif (
        palette_kind == 0
        and len(palette) == 16
    ):

        rgba = palette[
            indices & 0x0F
        ]

    else:

        raise Unsupported(
            f'palette kind/length '
            f'{palette_kind}/{len(palette)} unsupported'
        )

    return rgba.copy()


def continuity_score(rgba):

    rgb = rgba[..., :3].astype(
        np.int16
    )

    score = 0.0

    if rgb.shape[1] > 1:

        score += float(
            np.abs(
                rgb[:, 1:]
                - rgb[:, :-1]
            ).mean()
        )

    if rgb.shape[0] > 1:

        score += float(
            np.abs(
                rgb[1:]
                - rgb[:-1]
            ).mean()
        )

    return score


def candidate_pitches_8(
    width,
    height,
    image_bytes
):

    candidates = set()

    aligned_w = align(
        width,
        16
    )

    for ph in {
        height,
        align(height, 16),
        align(height, 64)
    }:

        if (
            ph > 0
            and image_bytes % ph == 0
        ):

            pitch = (
                image_bytes // ph
            )

            if (
                pitch >= aligned_w
                and pitch % 128 == 0
            ):

                candidates.add(
                    int(pitch)
                )

    default_pitch = max(
        128,
        next_power_two(aligned_w)
    )

    for pitch in {
        default_pitch,
        align(aligned_w, 128)
    }:

        if (
            pitch > 0
            and image_bytes in {
                pitch * height,
                pitch * align(height, 16),
                pitch * align(height, 64)
            }
            and pitch % 128 == 0
        ):

            candidates.add(
                int(pitch)
            )

    return sorted(candidates)


def candidate_shapes_linear_8(
    width,
    height,
    image_bytes
):

    candidates = []

    if image_bytes == width * height:

        candidates.append(
            (height, width)
        )

    for ph in sorted({
        height,
        align(height, 16),
        align(height, 64)
    }):

        if (
            ph > 0
            and image_bytes % ph == 0
        ):

            pitch = (
                image_bytes // ph
            )

            if pitch >= width:

                candidates.append(
                    (ph, pitch)
                )

    seen = set()
    result = []

    for item in candidates:

        if item not in seen:

            seen.add(item)
            result.append(item)

    return result


def candidate_pitches_4(
    width,
    height,
    image_bytes
):

    candidates = set()

    aligned_w = align(
        width,
        32
    )

    for ph in {
        height,
        align(height, 16),
        align(height, 128)
    }:

        pixels = image_bytes * 2

        if (
            ph > 0
            and pixels % ph == 0
        ):

            pitch = pixels // ph

            if (
                pitch >= aligned_w
                and pitch % 128 == 0
            ):

                candidates.add(
                    int(pitch)
                )

    default_pitch = max(
        128,
        next_power_two(aligned_w)
    )

    for pitch in {
        default_pitch,
        align(aligned_w, 128)
    }:

        if (
            pitch > 0
            and (
                pitch * height + 1
            ) // 2 <= image_bytes
            <= (
                pitch * align(
                    height,
                    128
                ) + 1
            ) // 2
            and pitch % 128 == 0
        ):

            candidates.add(
                int(pitch)
            )

    return sorted(candidates)


def candidate_shapes_linear_4(
    width,
    height,
    image_bytes
):

    candidates = []

    pixels = image_bytes * 2

    if width * height == pixels:

        candidates.append(
            (height, width)
        )

    for ph in sorted({
        height,
        align(height, 16),
        align(height, 128)
    }):

        if (
            ph > 0
            and pixels % ph == 0
        ):

            pitch = pixels // ph

            if pitch >= width:

                candidates.append(
                    (ph, pitch)
                )

    seen = set()
    result = []

    for item in candidates:

        if item not in seen:

            seen.add(item)
            result.append(item)

    return result


def unpack_nibbles(payload):

    b = np.frombuffer(
        payload,
        dtype=np.uint8
    )

    out = np.empty(
        len(b) * 2,
        dtype=np.uint8
    )

    out[0::2] = b & 0x0F
    out[1::2] = (
        b >> 4
    ) & 0x0F

    return out


# ============================================================
# FORMAT 12
# ============================================================

def decode_format12(
    data,
    mapping
):

    w = mapping['width']
    h = mapping['height']

    pw = align(w, 32)
    ph = align(h, 16)

    sw = pw // 4
    sh = ph // 4

    plane_size = (
        sw * sh * 4
    )

    if (
        mapping['image_bytes']
        != plane_size * 2
    ):

        raise ValueError(
            'format-12 two-plane size '
            'does not match descriptor'
        )

    payload = span(
        data,
        mapping['image_offset'],
        plane_size * 2
    )

    base_plane = np.frombuffer(
        payload[:plane_size],
        dtype=np.uint8
    ).reshape(
        sh,
        sw,
        4
    )

    delta_plane = np.frombuffer(
        payload[plane_size:],
        dtype=np.uint8
    ).reshape(
        sh,
        sw,
        4
    )

    half_pitch = max(
        128,
        next_power_two(pw) // 2
    )

    full_pitch = (
        half_pitch * 2
    )

    memory = np.zeros(
        4 * 1024 * 1024,
        dtype=np.uint8
    )

    write = gs_byte_addresses(
        sw,
        sh,
        align(sw, 64),
        32
    )

    for c in range(4):
        memory[
            write + c
        ] = delta_plane[:, :, c]

    read = gs_byte_addresses(
        pw // 2,
        ph // 2,
        half_pitch,
        8
    )

    packed = memory[read]

    delta = np.stack(
        [
            (packed >> shift) & 3
            for shift in (
                0,
                2,
                4,
                6
            )
        ],
        axis=2
    )

    base = np.repeat(
        np.repeat(
            base_plane,
            2,
            axis=0
        ),
        2,
        axis=1
    )

    reconstructed = np.minimum(
        base.astype(np.uint16)
        + delta,
        255
    ).astype(np.uint8)

    reconstructed[:, :, 3] = (
        (base[:, :, 3] & 252)
        | (delta[:, :, 3] & 3)
    )

    memory.fill(0)

    write = gs_byte_addresses(
        pw // 2,
        ph // 2,
        half_pitch,
        32
    )

    for c in range(4):
        memory[
            write + c
        ] = reconstructed[:, :, c]

    read = gs_byte_addresses(
        pw,
        ph,
        full_pitch,
        8
    )

    indices = memory[read][
        :h,
        :w
    ]

    palette = np.frombuffer(
        span(
            data,
            mapping['palette_offset'],
            1024
        ),
        dtype=np.uint8
    ).reshape(
        256,
        4
    )

    rgba = apply_palette(
        indices,
        palette,
        mapping['palette_kind']
    )

    return (
        rgba,
        indices,
        {
            'decode_family':
                'format12',

            'decode_variant':
                'reconstructed-psmt8'
        }
    )


def choose_best_candidate(
    candidates
):

    if not candidates:

        raise Unsupported(
            'no viable decode candidate '
            'for indexed texture'
        )

    candidates.sort(
        key=lambda item: (
            item['score'],
            item['name']
        )
    )

    return candidates[0]


# ============================================================
# FORMAT 1
# ============================================================

def decode_format1(
    data,
    mapping
):

    if (
        mapping['palette_kind'],
        mapping['palette_mode'],
        mapping['palette_bytes']
    ) != (
        1,
        3,
        1024
    ):

        raise Unsupported(
            f"image format/mode "
            f"{mapping['format']}/"
            f"{mapping['mode']}, "
            f"palette "
            f"{mapping['palette_kind']}/"
            f"{mapping['palette_mode']}/"
            f"{mapping['palette_bytes']} "
            f"unsupported"
        )

    w = mapping['width']
    h = mapping['height']

    payload = span(
        data,
        mapping['image_offset'],
        mapping['image_bytes']
    )

    palette = np.frombuffer(
        span(
            data,
            mapping['palette_offset'],
            1024
        ),
        dtype=np.uint8
    ).reshape(
        256,
        4
    )

    payload_arr = np.frombuffer(
        payload,
        dtype=np.uint8
    )

    candidates = []

    for ph, pitch in candidate_shapes_linear_8(
        w,
        h,
        len(payload)
    ):

        try:

            indices = (
                payload_arr
                .reshape(
                    ph,
                    pitch
                )[:h, :w]
                .copy()
            )

        except ValueError:
            continue

        rgba = apply_palette(
            indices,
            palette,
            mapping['palette_kind']
        )

        candidates.append(
            dict(
                name=
                    f'linear-{pitch}x{ph}',

                indices=indices,

                rgba=rgba,

                score=
                    continuity_score(
                        rgba
                    )
            )
        )

    memory = np.zeros(
        max(
            4 * 1024 * 1024,
            len(payload) + 1
        ),
        dtype=np.uint8
    )

    memory[
        :len(payload)
    ] = payload_arr

    for pitch in candidate_pitches_8(
        w,
        h,
        len(payload)
    ):

        try:

            indices = (
                read_psmt8_from_memory(
                    memory,
                    w,
                    h,
                    pitch
                )
            )

        except Exception:
            continue

        rgba = apply_palette(
            indices,
            palette,
            mapping['palette_kind']
        )

        candidates.append(
            dict(
                name=
                    f'psmt8-{pitch}',

                indices=indices,

                rgba=rgba,

                score=
                    continuity_score(
                        rgba
                    )
            )
        )

    best = choose_best_candidate(
        candidates
    )

    return (
        best['rgba'],
        best['indices'],
        {
            'decode_family':
                'format1',

            'decode_variant':
                best['name'],

            'candidate_scores': {
                c['name']:
                    round(
                        c['score'],
                        4
                    )
                for c in candidates
            }
        }
    )


# ============================================================
# FORMAT 0 / 4-BIT
# ============================================================

def decode_format0(
    data,
    mapping
):

    if (
        mapping['palette_kind'],
        mapping['palette_mode'],
        mapping['palette_bytes']
    ) != (
        0,
        3,
        64
    ):

        raise Unsupported(
            f"image format/mode "
            f"{mapping['format']}/"
            f"{mapping['mode']}, "
            f"palette "
            f"{mapping['palette_kind']}/"
            f"{mapping['palette_mode']}/"
            f"{mapping['palette_bytes']} "
            f"unsupported"
        )

    w = mapping['width']
    h = mapping['height']

    payload = span(
        data,
        mapping['image_offset'],
        mapping['image_bytes']
    )

    palette = np.frombuffer(
        span(
            data,
            mapping['palette_offset'],
            64
        ),
        dtype=np.uint8
    ).reshape(
        16,
        4
    )

    linear_pixels = unpack_nibbles(
        payload
    )

    candidates = []

    for ph, pitch in candidate_shapes_linear_4(
        w,
        h,
        len(payload)
    ):

        try:

            indices = (
                linear_pixels
                .reshape(
                    ph,
                    pitch
                )[:h, :w]
                .copy()
            )

        except ValueError:
            continue

        rgba = apply_palette(
            indices,
            palette,
            mapping['palette_kind']
        )

        candidates.append(
            dict(
                name=
                    f'linear4-{pitch}x{ph}',

                indices=indices,

                rgba=rgba,

                score=
                    continuity_score(
                        rgba
                    )
            )
        )

    memory = np.zeros(
        max(
            4 * 1024 * 1024,
            len(payload) + 1
        ),
        dtype=np.uint8
    )

    memory[
        :len(payload)
    ] = np.frombuffer(
        payload,
        dtype=np.uint8
    )

    for pitch in candidate_pitches_4(
        w,
        h,
        len(payload)
    ):

        try:

            indices = (
                read_psmt4_from_memory(
                    memory,
                    w,
                    h,
                    pitch
                )
            )

        except Exception:
            continue

        rgba = apply_palette(
            indices,
            palette,
            mapping['palette_kind']
        )

        candidates.append(
            dict(
                name=
                    f'psmt4-{pitch}',

                indices=indices,

                rgba=rgba,

                score=
                    continuity_score(
                        rgba
                    )
            )
        )

    best = choose_best_candidate(
        candidates
    )

    return (
        best['rgba'],
        best['indices'],
        {
            'decode_family':
                'format0',

            'decode_variant':
                best['name'],

            'candidate_scores': {
                c['name']:
                    round(
                        c['score'],
                        4
                    )
                for c in candidates
            }
        }
    )


# ============================================================
# SPECIAL PACKED-NIBBLE FORMAT
# ============================================================

def decode_format1_nibble_planes(
    data,
    mapping
):
    """Decode format-1 image as two packed 4-bit index planes."""

    if (
        mapping['palette_kind'],
        mapping['palette_mode'],
        mapping['palette_bytes']
    ) != (
        0,
        3,
        64
    ):

        raise Unsupported(
            'format-1 nibble decoder '
            'requires a 16-entry palette'
        )

    w = mapping['width']
    h = mapping['height']

    payload = span(
        data,
        mapping['image_offset'],
        mapping['image_bytes']
    )

    payload_arr = np.frombuffer(
        payload,
        dtype=np.uint8
    )

    palette = np.frombuffer(
        span(
            data,
            mapping['palette_offset'],
            64
        ),
        dtype=np.uint8
    ).reshape(
        16,
        4
    )

    candidates = []

    for ph, pitch in candidate_shapes_linear_8(
        w,
        h,
        len(payload)
    ):

        try:

            packed = (
                payload_arr
                .reshape(
                    ph,
                    pitch
                )[:h, :w]
                .copy()
            )

        except ValueError:
            continue

        low = (
            packed & 0x0F
        )

        high = (
            packed >> 4
        )

        low_rgba = (
            palette[low]
            .copy()
        )

        high_rgba = (
            palette[high]
            .copy()
        )

        score = (
            continuity_score(
                low_rgba
            )
            + continuity_score(
                high_rgba
            )
        )

        candidates.append(
            dict(
                name=
                    f'linear-packed4-'
                    f'{pitch}x{ph}',

                packed=packed,

                low=low,

                high=high,

                low_rgba=
                    low_rgba,

                high_rgba=
                    high_rgba,

                score=score
            )
        )

    memory = np.zeros(
        max(
            4 * 1024 * 1024,
            len(payload) + 1
        ),
        dtype=np.uint8
    )

    memory[
        :len(payload)
    ] = payload_arr

    for pitch in candidate_pitches_8(
        w,
        h,
        len(payload)
    ):

        try:

            packed = (
                read_psmt8_from_memory(
                    memory,
                    w,
                    h,
                    pitch
                )
            )

        except Exception:
            continue

        low = (
            packed & 0x0F
        )

        high = (
            packed >> 4
        )

        low_rgba = (
            palette[low]
            .copy()
        )

        high_rgba = (
            palette[high]
            .copy()
        )

        score = (
            continuity_score(
                low_rgba
            )
            + continuity_score(
                high_rgba
            )
        )

        candidates.append(
            dict(
                name=
                    f'psmt8-packed4-'
                    f'{pitch}',

                packed=packed,

                low=low,

                high=high,

                low_rgba=
                    low_rgba,

                high_rgba=
                    high_rgba,

                score=score
            )
        )

    if not candidates:

        raise Unsupported(
            'no viable packed-nibble '
            'decode candidate'
        )

    candidates.sort(
        key=lambda x: (
            x['score'],
            x['name']
        )
    )

    best = candidates[0]

    meta = {
        'decode_family':
            'format1-packed4',

        'decode_variant':
            best['name'],

        'interpretation':
            'two independent 4-bit index '
            'planes packed into each 8-bit texel',

        'candidate_scores': {
            c['name']:
                round(
                    c['score'],
                    4
                )
            for c in candidates
        }
    }

    return best, meta


# ============================================================
# FORMAT DISPATCH
# ============================================================

def decode_mapping(
    data,
    mapping
):

    if (mapping['format'], mapping['mode'], mapping['palette_count']) == (6, 0, 0):
        return decode_xbox_format6(data, mapping)

    signature = (
        mapping['format'],
        mapping['mode'],
        mapping['palette_kind'],
        mapping['palette_mode'],
        mapping['palette_bytes']
    )

    if signature == (11, 0, 1, 0, 1024):
        return decode_xbox_format11(data, mapping)

    if signature == (
        12,
        0,
        1,
        3,
        1024
    ):

        return decode_format12(
            data,
            mapping
        )

    if signature == (
        1,
        0,
        1,
        3,
        1024
    ):

        return decode_format1(
            data,
            mapping
        )

    if signature == (
        0,
        0,
        0,
        3,
        64
    ):

        return decode_format0(
            data,
            mapping
        )

    raise Unsupported(
        f"image format/mode "
        f"{mapping['format']}/"
        f"{mapping['mode']}, "
        f"palette "
        f"{mapping['palette_kind']}/"
        f"{mapping['palette_mode']}/"
        f"{mapping['palette_bytes']} "
        f"unsupported"
    )


# ============================================================
# EXPORT ONE MMAP RESOURCE
# ============================================================

def export(
    source,
    output,
    manifest_records,
    all_images=False,
    alpha='auto',
    overwrite=False
):

    data = source.read_bytes()

    mappings = read_mappings(
        data
    )

    # Xbox image_count describes a mipmap chain, not distinct textures.
    # PS2 multiple palettes/images can represent separate artwork.
    xbox_resource = all(m['format'] in (6, 11) for m in mappings)
    compound = any(m['palette_count'] > 1 for m in mappings) or (
        not xbox_resource and any(m['image_count'] > 1 for m in mappings)
    )

    if all_images:
        selected = mappings
    elif xbox_resource:
        first_image = min(m['image_index'] for m in mappings)
        selected = [m for m in mappings if m['image_index'] == first_image]
    else:
        selected = mappings if compound else mappings[:1]

    # Keep multi-image resources together; separate unnamed single images.
    # Preserve original resource hashes for grouping and collision safety.
    stem = source.stem
    hash_match = re.fullmatch(r'[0-9a-fA-F]{8}', stem)
    named_match = re.fullmatch(r'(.+)__([0-9a-fA-F]{8})', stem)
    clean_name = named_match.group(1) if named_match else stem
    # Folder only when the selected output really contains multiple images.
    multi_image = len(selected) > 1 or (compound and not xbox_resource)
    if multi_image:
        resource_output = output / stem
        image_prefix = ''
    elif hash_match:
        resource_output = output / 'unnamed'
        image_prefix = stem
    else:
        resource_output = output
        image_prefix = clean_name

    outputs = []
    skipped_variants = []
    seen_16pal = {}

    source_sha256 = hashlib.sha256(
        data
    ).hexdigest()


    def save_one(
        dest,
        rgba,
        indices,
        mapping,
        decode_meta,
        extra_record=None
    ):

        if (
            not overwrite
            and dest.exists()
        ):

            raise FileExistsError(
                f'output exists: {dest}; '
                f'use --overwrite to replace'
            )

        raw_alpha = (
            rgba[:, :, 3]
            .copy()
        )

        rgba = rgba.copy()

        effective_alpha = ('raw' if mapping['format'] in (6, 11) else 'ps2') if alpha == 'auto' else alpha

        if effective_alpha == 'ps2':

            rgba[:, :, 3] = np.minimum(
                raw_alpha.astype(
                    np.uint16
                ) * 2,
                255
            ).astype(
                np.uint8
            )

        elif effective_alpha == 'opaque':

            rgba[:, :, 3] = 255

        elif effective_alpha != 'raw':

            raise ValueError(
                'invalid alpha mode'
            )

        dest.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        Image.fromarray(
            rgba
        ).save(
            dest
        )

        record = {
            'source_name':
                source.name,

            'source_sha256':
                source_sha256,

            'output_name':
                str(dest.relative_to(output)),

            'output_sha256':
                hashlib.sha256(
                    dest.read_bytes()
                ).hexdigest(),

            'descriptor':
                mapping,

            'index_sha256':
                None if indices is None else hashlib.sha256(
                    indices.tobytes()
                ).hexdigest(),

            'raw_alpha_sha256':
                hashlib.sha256(
                    raw_alpha.tobytes()
                ).hexdigest(),

            'alpha_mode':
                effective_alpha,
            'alpha_requested': alpha,
            'platform': 'Xbox' if mapping['format'] in (6, 11) else 'PS2',

            'decode':
                decode_meta,
        }

        if extra_record:

            record.update(
                extra_record
            )

        manifest_records.append(
            record
        )

        outputs.append(
            str(dest)
        )

        return (
            len(manifest_records) - 1
        )


    for mapping in selected:

        use_suffix = (
            all_images
            or compound
        )

        signature = (
            mapping['format'],
            mapping['mode'],
            mapping['palette_kind'],
            mapping['palette_mode'],
            mapping['palette_bytes']
        )


        # ----------------------------------------------------
        # Special NASCAR 06 compound packed-nibble resource
        # ----------------------------------------------------

        if signature == (
            1,
            0,
            0,
            3,
            64
        ):

            pal_bytes = span(
                data,
                mapping[
                    'palette_offset'
                ],
                mapping[
                    'palette_bytes'
                ]
            )

            pal_hash = (
                hashlib.sha256(
                    pal_bytes
                ).hexdigest()
            )

            key = (
                mapping['mapping'],
                mapping['image_index'],
                pal_hash
            )


            if key in seen_16pal:

                seen_16pal[
                    key
                ][
                    'duplicates'
                ].append(
                    mapping[
                        'palette_index'
                    ]
                )

                continue


            best, decode_meta = (
                decode_format1_nibble_planes(
                    data,
                    mapping
                )
            )

            seen_16pal[key] = {
                'first':
                    mapping[
                        'palette_index'
                    ],

                'duplicates':
                    [],

                'record_indices':
                    []
            }


            base = (
                f"_image"
                f"{mapping['image_index']}"
                f"_pal"
                f"{mapping['palette_index']:02d}"
            )


            low_dest = resource_output / (
                image_prefix + base.lstrip('_') + '_lo4.png'
            )

            high_dest = resource_output / (
                image_prefix + base.lstrip('_') + '_hi4.png'
            )


            extra = {
                'palette_sha256':
                    pal_hash,

                'palette_deduplication':
                    'identical 16-entry palette '
                    'descriptors are exported once'
            }


            low_record = save_one(
                low_dest,

                best[
                    'low_rgba'
                ],

                best[
                    'low'
                ],

                mapping,

                dict(
                    decode_meta,
                    nibble_plane='low'
                ),

                extra
            )


            high_record = save_one(
                high_dest,

                best[
                    'high_rgba'
                ],

                best[
                    'high'
                ],

                mapping,

                dict(
                    decode_meta,
                    nibble_plane='high'
                ),

                extra
            )


            seen_16pal[
                key
            ][
                'record_indices'
            ] = [
                low_record,
                high_record
            ]

            continue


        # ----------------------------------------------------
        # Standard texture
        # ----------------------------------------------------

        if mapping['palette_count'] == 0:
            suffix = f"_image{mapping['image_index']}" if all_images else ''

        elif use_suffix:

            suffix = (
                f"_image"
                f"{mapping['image_index']}"
                f"_pal"
                f"{mapping['palette_index']:02d}"
            )

        else:

            suffix = ''


        dest = resource_output / (
            (image_prefix + (suffix.lstrip('_') or 'image00')) if multi_image else image_prefix
        )
        dest = dest.with_suffix('.png')


        try:

            rgba, indices, decode_meta = (
                decode_mapping(
                    data,
                    mapping
                )
            )

        except Unsupported as e:

            skipped_variants.append(
                (
                    mapping[
                        'palette_index'
                    ],
                    str(e)
                )
            )

            continue


        save_one(
            dest,
            rgba,
            indices,
            mapping,
            decode_meta
        )


    # --------------------------------------------------------
    # Add duplicate-palette information to manifest
    # --------------------------------------------------------

    for key, info in seen_16pal.items():

        if not info[
            'duplicates'
        ]:

            continue


        for record_index in info[
            'record_indices'
        ]:

            manifest_records[
                record_index
            ][
                'duplicate_palette_indices'
            ] = info[
                'duplicates'
            ]

            manifest_records[
                record_index
            ][
                'duplicate_palette_count'
            ] = len(
                info[
                    'duplicates'
                ]
            )


        print(
            f"DEDUP {source.name}: "
            f"palette {info['first']} "
            f"represents "
            f"{1 + len(info['duplicates'])} "
            f"identical 16-colour descriptors",
            file=sys.stderr
        )


    if skipped_variants:

        preview = ', '.join(
            f'pal{p}: {why}'
            for p, why
            in skipped_variants[:4]
        )

        more = (
            ''
            if len(
                skipped_variants
            ) <= 4
            else
            f' (+'
            f'{len(skipped_variants)-4}'
            f' more)'
        )

        print(
            f'PARTIAL {source.name}: '
            f'skipped '
            f'{len(skipped_variants)} '
            f'palette variant(s): '
            f'{preview}{more}',
            file=sys.stderr
        )


    if not outputs:

        raise Unsupported(
            f'no supported variants '
            f'in mapping '
            f'({len(skipped_variants)} skipped)'
        )


    return outputs


# ============================================================
# MAIN
# ============================================================

def main():

    ap = argparse.ArgumentParser(
        description=__doc__
    )

    ap.add_argument(
        'inputs',
        nargs='+',
        help=
            'MMAP file paths '
            'or quoted glob patterns'
    )

    ap.add_argument(
        '-o',
        '--output',
        type=Path,
        default=
            Path(
                'decoded_textures'
            )
    )

    ap.add_argument(
        '--all-images',
        action='store_true',
        help=
            'export each supported '
            'image/palette mapping'
    )

    ap.add_argument(
        '--alpha',
        choices=[
            'auto',
            'ps2',
            'raw',
            'opaque'
        ],
        default='auto',
        help='auto: PS2 alpha doubling, Xbox source alpha; opaque forces alpha 255'
    )

    ap.add_argument(
        '--skip-unsupported',
        action='store_true',
        help=
            'report unsupported '
            'formats without '
            'failing the batch'
    )

    ap.add_argument(
        '--overwrite',
        action='store_true'
    )

    args = ap.parse_args()


    sources = []

    for pattern in args.inputs:

        if glob.has_magic(
            pattern
        ):

            matches = sorted(
                glob.glob(
                    pattern
                )
            )

        else:

            matches = [
                pattern
            ]


        if not matches:

            ap.error(
                f'no files match '
                f'{pattern}'
            )


        sources.extend(
            Path(x)
            for x in matches
        )


    failures = 0
    skipped = 0
    success = 0

    seen = set()

    manifest_records = []


    for source in sources:

        resolved = (
            source.resolve()
        )


        if resolved in seen:
            continue


        seen.add(
            resolved
        )


        try:

            outputs = export(
                source,
                args.output,
                manifest_records,
                args.all_images,
                args.alpha,
                args.overwrite
            )

            success += 1

            print(
                f'OK '
                f'{source.name}: '
                f'{len(outputs)} '
                f'PNG(s)'
            )


        except Unsupported as e:

            skipped += 1

            print(
                f'UNSUPPORTED '
                f'{source.name}: '
                f'{e}',
                file=sys.stderr
            )

            if not (
                args.skip_unsupported
            ):

                failures += 1


        except (
            OSError,
            ValueError,
            struct.error
        ) as e:

            failures += 1

            print(
                f'ERROR '
                f'{source.name}: '
                f'{e}',
                file=sys.stderr
            )


    # ========================================================
    # WRITE ONE MANIFEST
    # ========================================================

    args.output.mkdir(
        parents=True,
        exist_ok=True
    )


    manifest_path = (
        args.output
        / 'conversion_manifest.json'
    )


    manifest = {
        'converter':
            'NASCAR 06 PS2 / Xbox MMAP Converter v0.5.2',

        'alpha_mode':
            args.alpha,

        'source_files_converted':
            success,

        'source_files_unsupported':
            skipped,

        'failures':
            failures,

        'output_images':
            len(
                manifest_records
            ),

        'records':
            manifest_records
    }


    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2
        ) + '\n',
        encoding='utf-8'
    )


    print()

    print(
        f'{success} converted; '
        f'{skipped} unsupported; '
        f'{failures} failures'
    )

    print(
        f'Manifest: '
        f'{manifest_path}'
    )


    return (
        1
        if failures
        else 0
    )


if __name__ == '__main__':
    raise SystemExit(
        main()
    )
