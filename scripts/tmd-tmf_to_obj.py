#!/usr/bin/env python3
"""Experimental NASCAR TMD/TMF to OBJ converter, version 0.5.
Python 3.9+, standard library only. See README for tested scope and limitations.
Optional --body-variant 1 or 2 pairs alternate Cup body shapes with their UVs.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile

VERSION = '0.5'
PS2_STATIC_FITTED_SCALE = 0.0328
PS2_STATIC_ROW_BIAS = 0x3FC00000
PS2_FITTED_SCALE = 0.00030030611648629253
PS2_SCALE_HEADER_BITS = 0x3B5A740E
KNOWN = {
 '9e3ae76bd5a778105e10bcd6619fcd3b65a35dd8314d5eec80203ec9e8ab8d84': 'NASCAR06 rcar100 PS2',
 '7c5eb4f47a964ce621485e900fff3f412cd6774d78ae66a93e76fa39356c6065': 'NASCAR06 rcar1600 PS2',
 '759241b90341c04b9d2491bbbe09995b72287b41bfaac62f02f663acebe1ae5b': 'NASCAR06 rcar250 PS2',
 '0d9dfd7036fa3f0ecedba36cf7456f1e0bb7aac7ba36487fb48f273eae4429ac': 'NASCAR06 rcarInterface PS2',
 'accbd4c3e1139369fd478afa9a85b1eafaf323f6fd72c8d58594ba5e493dd24b': 'NASCAR06 rcar100 Xbox',
 '85892a974b09c2475b1c3444816aed794b3d82d7d16a72086c7ffc95933b0ca0': 'NASCAR06 rcar1600 Xbox',
 '27009d4915a614c9532bcc14e5bdcc31d93c2c98fa0b8c82cdbd45e12dab633d': 'NASCAR06 rcar250 Xbox',
 'c11374bee6ec12194355772d143bd53c23d2406cb406ce6ad69b4f9dccd3318b': 'NASCAR06 rcarInterface Xbox',
 '2d447418cbd162fb58c37110c92eb8016b6f0328ef683496b1bf5fbd6f273a40': 'NASCAR06 rcar750 Xbox',
 'ac573263abd6f151824722d629514e4b283c18d717e0e5a091b59086b1227f6e': 'NASCAR06 rcar750 PS2',
 '1ffc9bf65e8544fac67a19cb3ed0505582345980163acf6903e1b7f5d8a4210d': 'NASCAR06 ChevInterior_NonDynamic Xbox',
 '14e09b50119f98d6e1c8ef6f3b5f6e14ac7ee4606ebcd6a05f32cc6406d82fba': 'NASCAR06 ChevInterior_No PS2',

 'd4a9ffad1129a390c90a53e721b694e57db9e6de11159145da186c68b55f10da': 'NASCAR06 rcar2850 Xbox',
 '199c6dbe6db9d63464a3846f567dfd0d674efc5dd5010cffa786dce4f3a78cd9': 'NASCAR06 rcar2850 PS2',
 '77f480ec71942095faf37d06ba48bf47aae2c1009f2d5f94b2e30a48d618e084': 'NASCAR06 rtruck_LOD1 Xbox',
 '0186a5175c15d10426ef0dee01767801edf0b4bc0cc4339ced77f6687c79e620': 'NASCAR06 rtruck_LOD1 PS2',
 '24dafcf6374d917cfeb663d28549b744e238c8acbfdd1440c856ec5f67c18377': 'NASCAR06 rtruck_LOD5 Xbox',
 '4787ebc1befb5dd057c34c51ac6d8f171112a000625dbc7be63ed9ea505af955': 'NASCAR06 rtruck_LOD5 PS2',
 '5c359639b432a340a0d089fa3eeea6dff9e32ee03715d3ec22773635198b1f86': 'Earlier NASCAR Thunder 2002 Xbox-labelled car',
}

class FormatError(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise FormatError(message)

class Reader:
    def __init__(self, data):
        self.data = data
    def span(self, offset, size, label='data'):
        require(offset >= 0 and size >= 0 and offset <= len(self.data) - size,
                f'{label} outside file: offset=0x{offset:X}, size={size}')
    def read(self, fmt, offset):
        self.span(offset, struct.calcsize('<' + fmt))
        return struct.unpack_from('<' + fmt, self.data, offset)
    def u32(self, offset):
        return self.read('I', offset)[0]


def strip_faces(indices):
    faces = []
    for k in range(len(indices) - 2):
        a, b, c = indices[k:k + 3]
        if k & 1:
            a, b = b, a
        if len({a, b, c}) == 3:
            faces.append((a, b, c))
    return faces


def decode_tmd(r):
    require(r.u32(4) == len(r.data), 'TMdl declared length differs from actual file')
    count, directory = r.read('HH', 8)
    require(directory >= 16 and count > 0, 'Unsupported chunk directory')
    r.span(directory, count * 16, 'chunk directory')
    chunks = []
    for i in range(count):
        tag, start, size, extra = r.read('4sIII', directory + i * 16)
        r.span(start, size, 'chunk')
        chunks.append(dict(tag=tag.decode('ascii', errors='replace'), offset=start, size=size, metadata=extra))
    gs = [c for c in chunks if c['tag'] == 'Geom']
    require(len(gs) == 1 and gs[0]['metadata'] == 2, 'Requires one unrelocated Geom version-2 chunk')
    geom = gs[0]
    def geom_span(a, n):
        require(a >= geom['offset'] and a + n <= geom['offset'] + geom['size'], 'Geometry reference outside Geom chunk')
    dt, nr, st, ns = r.read('4I', geom['offset'])
    geom_span(dt, nr * 20); geom_span(st, ns * 12)
    streams = []
    for i in range(ns):
        a, n, low, high, kind = r.read('IHBBI', st + i * 12)
        geom_span(a, 12)
        require(r.data[a:a + 12] == bytes.fromhex('010000010000000000000000'), 'Unsupported resource prefix (possibly relocated data)')
        streams.append(dict(index=i, offset=a, count=n, layout_low=low, layout_high=high, kind=kind))
    objects, draws, skipped, extra_streams = [], [], [], []
    for ri in range(nr):
        off = dt + ri * 20
        inner, outer, material, unknown_a, unknown_b, binding, stream_count, flags, unknown_c, followers = r.read('IIHHHHBBBB', off)
        if not flags & 2:
            require(not inner and not outer, f'Draw {ri}: non-indexed flags with index pointers')
            skipped.append(dict(row=ri, stream_binding=binding, stream_count=stream_count, flags=flags))
            continue
        require(inner and outer, f'Draw {ri}: indexed flag without both wrapper pointers')
        require(stream_count >= 1 and binding + stream_count <= ns,
                f'Draw {ri}: stream binding out of range (binding={binding}, count={stream_count}, table_count={ns})')
        pos = streams[binding]
        interleaved = (stream_count == 1 and (pos['kind'], pos['layout_low'], pos['layout_high']) == (0x152, 40, 0))
        channel_report = {}
        if interleaved:
            n = pos['count']
            require(n > 0, f'Draw {ri}: empty interleaved stream')
            geom_span(pos['offset'] + 12, n * 40)
            records = [r.read('6fI2fI', pos['offset'] + 12 + j * 40) for j in range(n)]
            require(all(v[9] == 0 for v in records), f'Draw {ri}: unsupported interleaved trailing word')
            attrs = [v[:6] + v[7:9] for v in records]
            uv_offset = pos['offset'] + 12 + 28
            channel_report = dict(layout='interleaved_0x152_stride40', stride=40,
                                  position_in_record=0, normal_in_record=12, colour_in_record=24,
                                  uv_in_record=28, trailing_word_in_record=36,
                                  colour_words_raw=[v[6] for v in records], trailing_word=0)
        else:
            require(stream_count >= 2, f'Draw {ri}: unsupported single stream kind=0x{pos["kind"]:X}, stride={pos["layout_low"]}')
            uv = streams[binding + 1]
            require((pos['kind'], pos['layout_low'], pos['layout_high']) == (0x12, 24, 0), f'Draw {ri}: unsupported position/normal stream')
            require((uv['kind'], uv['layout_low'], uv['layout_high']) == (0x100, 8, 0), f'Draw {ri}: unsupported first UV stream')
            n = pos['count']
            require(n > 0 and n == uv['count'], f'Draw {ri}: empty/mismatched attribute counts')
            geom_span(pos['offset'] + 12, n * 24); geom_span(uv['offset'] + 12, n * 8)
            attrs = [r.read('6f', pos['offset'] + 12 + j * 24) + r.read('2f', uv['offset'] + 12 + j * 8) for j in range(n)]
            uv_offset = uv['offset'] + 12
        require(all(math.isfinite(v) for a in attrs for v in a), f'Draw {ri}: nonfinite attributes')
        extras = streams[binding + 2:binding + stream_count]
        for ex in extras:
            require((ex['kind'], ex['layout_low'], ex['layout_high'], ex['count']) == (0x100, 8, 0, n), f'Draw {ri}: unsupported additional attribute stream')
            geom_span(ex['offset'] + 12, n * 8)
            extra_streams.append(dict(row=ri, **ex))
        # These are pointer lists, not a universal outer+12 shortcut.
        geom_span(inner, 4); geom_span(outer, 4)
        ic, oc = r.u32(inner), r.u32(outer)
        require(ic == 1 and oc == 1, f'Draw {ri}: multiple/empty index-list entries are not supported in v0.2')
        geom_span(inner, 4 + 4 * ic); geom_span(outer, 4 + 4 * oc)
        terminal = r.u32(inner + 4)
        geom_span(terminal, 16)
        require(r.data[terminal + 2:terminal + 16] == bytes(14), f'Draw {ri}: unsupported terminal prefix')
        index_count = r.read('H', terminal)[0] + 2
        geom_span(terminal + 16, index_count * 2)
        ids = r.read(str(index_count) + 'H', terminal + 16)
        require(max(ids) < n, f'Draw {ri}: index out of vertex range')
        faces = strip_faces(ids)
        require(faces, f'Draw {ri}: no supported triangles')
        objects.append(dict(name=f'draw_{ri}', row=ri, attrs=attrs, triangles=faces))
        draws.append(dict(row=ri, row_offset=off, following_records=followers, stream_binding=binding, stream_count=stream_count,
                          material_raw=material, position_offset=pos['offset'] + 12, uv_offset=uv_offset, **channel_report,
                          vertex_count=n, terminal_offset=terminal, index_offset=terminal + 16, index_count=index_count, triangles=len(faces)))
    require(objects, 'No supported indexed draws')
    warnings = []
    if any(d.get('layout') == 'interleaved_0x152_stride40' for d in draws):
        warnings.append('Interleaved colour words are retained raw in the report, not exported to OBJ. Channel order and runtime use remain unresolved.')
    if skipped:
        warnings.append(f'{len(skipped)} non-indexed records were retained in the report, not exported as independent meshes.')
    if extra_streams:
        warnings.append(f'{len(extra_streams)} additional two-float/UV-like streams are not exported; OBJ uses the first UV stream only. Their material/runtime roles are unresolved.')
    return objects, dict(platform='Xbox', chunks=chunks, draw_table=dt, draw_count=nr, stream_table=st, stream_count=ns,
                         streams=streams, selected_draws=draws, skipped_draws=skipped, additional_attribute_streams=extra_streams,
                         position_scale=1.0, warnings=warnings)


def decode_tmf(r, scale_override, body_variant=0):
    require(r.read('BB', 4) == (4, 6), 'TMF v0.2 supports only the tested header bytes +4=4,+5=6 (NASCAR06 layout)')
    require(r.u32(8) == 0, 'Unsupported TMF flags or relocated pointers')
    header_scale_bits = r.u32(0x1c)
    static_profile = header_scale_bits == 0x3F800000
    if scale_override is None and static_profile:
        scale = PS2_STATIC_FITTED_SCALE
    elif scale_override is None:
        require(header_scale_bits == PS2_SCALE_HEADER_BITS, 'Uncalibrated PS2 scale header; specify --ps2-scale only if you know the conversion')
        scale = PS2_FITTED_SCALE
    else:
        scale = scale_override
    require(math.isfinite(scale) and scale > 0, 'PS2 scale must be finite and positive')
    nr, dt, npk, pt = r.read('4I', 0x28)
    r.span(dt, nr * 20, 'PS2 draw table'); r.span(pt, npk * 8, 'PS2 descriptor table')
    draws = [r.read('10H', dt + i * 20) for i in range(nr)]
    desc = [r.read('IHH', pt + i * 8) for i in range(npk)]
    used_desc = bytearray(npk)
    for ri, d in enumerate(draws):
        start, groups, per = d[4:7]
        require(per in (1, 2), f'Draw {ri}: unsupported descriptors-per-group {per}')
        require(start + groups * per <= npk, f'Draw {ri}: descriptor range overflow')
        for j in range(start, start + groups * per):
            require(not used_desc[j], 'Overlapping draw descriptor ranges')
            used_desc[j] = 1
    require(all(used_desc), 'Descriptor table is not fully accounted for by draw records')
    for a, q, f in desc:
        r.span(a, 16 * q, 'PS2 descriptor payload')
    roots, skipped, ri = [], [], 0
    variant_roots = {}
    while ri < nr:
        d = draws[ri]; following = d[2]
        require(ri + following < nr, f'Draw {ri}: following-record range overflow')
        require(d[0] in (0, 1), f'Draw {ri}: unsupported primary draw type {d[0]}')
        require((d[0], d[6]) in ((0, 1), (1, 2)), f'Draw {ri}: unsupported primary layout')
        selected_row = ri
        if body_variant and following:
            selected_row = ri + body_variant
            target = draws[selected_row]
            require(following >= body_variant and d[0] == target[0] == 1 and d[6] == target[6] == 2,
                    f'Draw {ri}: unsupported full-shape body variant')
            require(target[2] == 0 and target[3] == d[3] and target[5:9] == d[5:9],
                    f'Draw {ri}: variant material/group/topology counts differ')
            variant_roots[selected_row] = ri
        roots.append(selected_row)
        for j in range(ri + 1, ri + following + 1):
            require(draws[j][2] == 0 and draws[j][0] in (1, 2, 4), 'Unsupported nested or unknown modifier record')
            if j != selected_row:
                skipped.append(dict(row=j, base_row=ri, type=draws[j][0], descriptor_start=draws[j][4], groups=draws[j][5]))
        ri += following + 1

    def unpacks(index):
        a, q, f = desc[index]; cur, end, us = a, a + 16 * q, []
        cycle, mode = None, None
        while cur < end:
            w = r.u32(cur); cur += 4
            cmd = (w >> 24) & 127; n = (w >> 16 & 255) or 256
            if cmd in (0x68, 0x69, 0x6d, 0x6e):
                width = {0x68:12, 0x69:6, 0x6d:8, 0x6e:4}[cmd]
                require(cur + n * width <= end, f'Descriptor {index}: truncated unpack')
                us.append(dict(cmd=cmd, n=n, offset=cur, immediate=w & 65535, cycle=cycle, mode=mode))
                cur = (cur + n * width + 3) & ~3
            else:
                require(cmd in (0, 1, 5), f'Descriptor {index}: unsupported VIF command 0x{cmd:02X}')
                if cmd == 1:
                    cycle = w & 65535
                    require(w & 65535 in (0x103, 0x101), 'Unsupported VIF cycle layout')
                if cmd == 5:
                    mode = w & 65535
                    require(w & 65535 in (0, 1), 'Unsupported VIF mode')
        require(cur == end, f'Descriptor {index}: alignment exceeds payload')
        return us

    objects, selected = [], []
    for ri in roots:
        d = draws[ri]; group_reports = []; total_faces = 0
        for g in range(d[5]):
            di = d[4] + g * d[6]
            require([desc[di + k][2] for k in range(d[6])] == ([2] if d[6] == 1 else [2, 16]), f'Draw {ri}: unsupported descriptor flag sequence')
            us = [u for k in range(d[6]) for u in unpacks(di + k)]
            if static_profile:
                require(d[0] == 0 and d[6] == 1, f'Draw {ri}: unsupported static draw layout')
                require(all(u['cycle'] == 0x103 and u['mode'] == 1 for u in us), 'Unsupported static VIF cycle/mode')
                require(len(us) == 4 and [u['cmd'] for u in us] == [0x6e, 0x68, 0x6e, 0x69], f'Draw {ri} group {g}: unsupported static unpack layout')
                require([u['immediate'] for u in us] == [0xc000, 0x8001, 0xc002, 0x8003] and us[0]['n'] == 1, 'Unsupported static VIF destinations')
                meta = r.read('4B', us[0]['offset']); n = us[1]['n']
                require(meta == (n, 0, 0, n) and n == us[2]['n'] == us[3]['n'], 'Unsupported static metadata/attribute counts')
                attrs, controls, colours = [], [], []
                for j in range(n):
                    packed = r.read('3I', us[1]['offset'] + 12 * j)
                    # Empirical decoding, supported by a 0x3FC00000 STROW template in the PS2 executable.
                    native = tuple(struct.unpack('<f', struct.pack('<I', (v + PS2_STATIC_ROW_BIAS) & 0xffffffff))[0] for v in packed)
                    uu, vv, ctrl = r.read('2hH', us[3]['offset'] + 6 * j)
                    require((ctrl & 32767) == j * 3 + 1, 'Unsupported static slot permutation')
                    attrs.append(tuple(v * scale for v in native) + (0.0, 0.0, 0.0) + (uu / 4096, vv / 4096))
                    controls.append(bool(ctrl & 32768)); colours.append(r.read('4B', us[2]['offset'] + 4 * j))
                require(all(math.isfinite(v) for a in attrs for v in a), 'Nonfinite static attributes')
                require(n >= 3 and controls[:2] == [True, True], 'Unsupported static strip start')
                faces, origin = [], 0
                for k in range(2, n):
                    if controls[k-2] and controls[k-1]: origin = k-2
                    if controls[k]: continue
                    face = (k-2, k-1, k) if not ((k-origin) & 1) else (k-1, k-2, k)
                    faces.append(face)
                require(faces, f'Draw {ri} group {g}: empty static geometry')
                objects.append(dict(name=f'draw_{ri}_group_{g}', row=ri, group=g, attrs=attrs, triangles=faces, normals_present=False))
                total_faces += len(faces)
                group_reports.append(dict(group=g, descriptor_start=di, descriptor_count=1,
                    descriptors=[dict(index=di, offset=desc[di][0], qwords=desc[di][1], flag=desc[di][2])],
                    unpacks=us, main_records=n, auxiliary_records=0, slots=n, triangles=len(faces),
                    layout='static_biased_float_colour_uv', normals_present=False,
                    position_row_bias=PS2_STATIC_ROW_BIAS, colour_bytes_raw=colours))
                continue
            require(len(us) >= 4 and [u['cmd'] for u in us[:4]] == [0x6e, 0x69, 0x69, 0x6d], f'Draw {ri} group {g}: unsupported unpack layout')
            require(us[0]['n'] == 1, 'Unsupported metadata count')
            require([u['immediate'] for u in us[:4]] == [0xc000, 0x8001, 0x8002, 0x8003], 'Unsupported VIF destination/address flags')
            meta = r.read('4B', us[0]['offset']); n = us[1]['n']
            require(n == us[2]['n'] == us[3]['n'] == meta[3] and meta[1] == 1, 'Metadata/attribute count mismatch')
            require(all(u['cmd'] == 0x6d for u in us[4:]) and sum(u['n'] for u in us[4:]) == meta[2], 'Unsupported auxiliary layout')
            attrs, slots = [], {}
            def slot(word):
                addr = word & 32767
                require(addr >= 1 and (addr - 1) % 3 == 0, 'Invalid encoded slot address')
                dst = (addr - 1) // 3
                require(dst < meta[0], 'Slot exceeds declared expansion')
                return dst
            for j in range(n):
                pos = r.read('3h', us[1]['offset'] + 6 * j)
                norm = r.read('3h', us[2]['offset'] + 6 * j)
                uv = r.read('2h', us[3]['offset'] + 8 * j)
                attrs.append(tuple(v * scale for v in pos) + tuple(v / 32767 for v in norm) + tuple(v / 4096 for v in uv))
                for w in set(r.read('2H', us[3]['offset'] + 8 * j + 4)):
                    dst = slot(w)
                    require(dst not in slots, 'Duplicate primary slot')
                    slots[dst] = (j, bool(w & 32768))
            for u in us[4:]:
                for j in range(u['n']):
                    uu, vv, a, b = r.read('2h2H', u['offset'] + 8 * j)
                    src, dst = slot(a), slot(b)
                    require(src in slots, 'Auxiliary source is not initialized')
                    av = attrs[slots[src][0]][:6] + (uu / 4096, vv / 4096)
                    flag = bool(b & 32768)
                    require(dst not in slots or (attrs[slots[dst][0]] == av and slots[dst][1] == flag), 'Conflicting auxiliary destination')
                    attrs.append(av); slots[dst] = (len(attrs) - 1, flag)
            require(set(slots) == set(range(meta[0])), 'Expanded slot table incomplete')
            faces, origin = [], 0
            for k in range(2, len(slots)):
                if slots[k - 2][1] and slots[k - 1][1]:
                    origin = k - 2
                if slots[k][1]:
                    continue
                a, b, c = (slots[t][0] for t in (k - 2, k - 1, k))
                if (k - origin) & 1:
                    a, b = b, a
                faces.append((a, b, c))
            require(faces, f'Draw {ri} group {g}: empty primary geometry')
            used = sorted({v for f in faces for v in f}); remap = {v:i for i,v in enumerate(used)}
            objects.append(dict(name=f'draw_{ri}_group_{g}', row=ri, group=g, attrs=[attrs[v] for v in used], triangles=[tuple(remap[v] for v in f) for f in faces]))
            total_faces += len(faces)
            group_reports.append(dict(group=g, descriptor_start=di, descriptor_count=d[6], descriptors=[dict(index=di+k, offset=desc[di+k][0], qwords=desc[di+k][1], flag=desc[di+k][2]) for k in range(d[6])], unpacks=us, main_records=n, auxiliary_records=meta[2], slots=meta[0], triangles=len(faces)))
        require(total_faces == d[7], f'Draw {ri}: decoded triangles {total_faces} != header {d[7]}')
        row_report = dict(row=ri, row_offset=dt + ri * 20, type=d[0], following_records=d[2], material_raw=d[3], triangles=total_faces, groups=group_reports)
        if ri in variant_roots:
            row_report.update(base_row=variant_roots[ri], body_variant=body_variant,
                              variant_source='complete PS2 position/normal/UV/topology packets')
        selected.append(row_report)
    require(objects, 'No supported PS2 primary draws')
    return objects, dict(platform='PS2', draw_count=nr, draw_table=dt, descriptor_count=npk, descriptor_table=pt,
                         selected_draws=selected, skipped_draws=skipped, position_scale=scale,
                         scale_status='USER OVERRIDE' if scale_override is not None else ('ASSUMPTION: 0.0328 fitted against paired ChevInterior; native unit/engine conversion not fully traced' if static_profile else 'ASSUMPTION: multiplier fitted on truck LOD5; native scale unresolved'),
                         layout_profile='static_biased_float_colour_uv' if static_profile else 'quantized_position_normal_uv',
                         header_scale=r.read('f', 0x1c)[0],
                         warnings=(['PS2 static packets have no identified normal stream. OBJ omits normals; colour bytes are retained raw in the report, not exported.'] if static_profile else []) + ['PS2 scale is a fitted conversion, not a derived native unit.', f'{len(skipped)} following/modifier records are not exported. Primary-row selection uses +4 following counts, validated against paired samples.'])


def apply_xbox_body_variant(r, objects, report, variant):
    """Verified Cup pairing: additive follower N plus UV stream N (N=1,2)."""
    geom = next(c for c in report['chunks'] if c['tag'] == 'Geom')
    changed = 0
    for ob, draw in zip(objects, report['selected_draws']):
        if not draw['following_records']:
            continue
        require(draw['following_records'] >= variant and draw['stream_count'] == 4,
                f"Draw {draw['row']}: expected position stream and three UV sets")
        row = draw['row'] + variant
        require(row < report['draw_count'], 'Variant row outside draw table')
        rr = r.read('IIHHHHBBBB', report['draw_table'] + row * 20)
        require(rr[0] == rr[1] == 0 and rr[6] == 1 and not rr[7] & 2 and rr[9] == 0,
                f'Draw {row}: unsupported additive follower binding')
        require(rr[5] < len(report['streams']), 'Variant stream index outside table')
        st = report['streams'][rr[5]]
        require(st['kind'] == 0xffffffff and st['layout_low'] in (0, 12),
                f'Draw {row}: expected sparse additive stream')
        end = st['offset'] + 12 + st['count'] * 12
        next_start = min([s['offset'] for s in report['streams'] if s['offset'] > st['offset']]
                         + [geom['offset'] + geom['size']])
        require(st['offset'] >= geom['offset'] and end <= next_start, 'Variant payload exceeds stream range')
        uvst = report['streams'][draw['stream_binding'] + 1 + variant]
        n = len(ob['attrs'])
        require((uvst['kind'], uvst['layout_low'], uvst['layout_high'], uvst['count']) == (0x100, 8, 0, n),
                f'Draw {row}: invalid alternate UV stream')
        uvs = [r.read('2f', uvst['offset'] + 12 + j * 8) for j in range(n)]
        require(all(math.isfinite(v) for uv in uvs for v in uv), 'Nonfinite alternate UV')
        attrs = [a[:6] + uv for a, uv in zip(ob['attrs'], uvs)]
        seen = set()
        for j in range(st['count']):
            entry = r.read('4h4b', st['offset'] + 12 + j * 12)
            index = entry[0]
            require(0 <= index < n and index not in seen, 'Invalid or duplicate sparse vertex index')
            seen.add(index)
            a = attrs[index]
            delta = tuple(entry[k + 1] / (2 ** (st['layout_high'] or 12)) for k in range(3))
            attrs[index] = tuple(a[k] + delta[k] for k in range(3)) + a[3:]
        draw.update(body_variant=variant, modifier_row=row, modifier_stream=st['index'],
                    modifier_weight=4096, selected_uv_set=variant, selected_uv_stream=uvst['index'],
                    base_uv_offset=draw['uv_offset'], uv_offset=uvst['offset'] + 12,
                    modified_position_records=st['count'], normals_exported=False)
        ob['attrs'] = attrs
        # Do not attach unmodified base normals to a changed shape. Xbox sparse
        # normal reconstruction still differs from the native PS2 shape normals.
        ob['normals_present'] = False
        changed += 1
    require(changed > 0, 'No supported Cup body groups found')
    report['warnings'].append('Variant body normals omitted on Xbox; recalculate them in your DCC. Base-only parts retain source normals.')
    report['warnings'].append('Selected Cup body shape and UV set are paired; other modifiers and damage controls remain unexported.')
    report['warnings'] = [w for w in report['warnings'] if 'OBJ uses the first UV stream only' not in w]


def decode(data, ps2_scale=None, body_variant=0):
    r = Reader(data); r.span(0, 16, 'header')
    digest = hashlib.sha256(data).hexdigest()
    require(body_variant in (0, 1, 2), 'Body variant must be 0, 1 or 2')
    if body_variant:
        # Scope guard, not an offset/layout table: only these Cup pairs have
        # independent position+UV+triangle correspondence for both variants.
        sample = KNOWN.get(digest, '')
        require(sample.startswith(tuple('NASCAR06 ' + name + ' ' for name in ('rcar100', 'rcar250', 'rcar750', 'rcar2850', 'rcarInterface'))),
                'Body variants require a validated NASCAR06 rcar100, rcar250, rcar750, rcar2850 or rcarInterface source; rcar1600 supports base export only')
    if data[:4] == b'TMdl':
        objects, report = decode_tmd(r)
        if body_variant:
            apply_xbox_body_variant(r, objects, report, body_variant)
    elif data[:4] == b'TMF\0':
        objects, report = decode_tmf(r, ps2_scale, body_variant)
    else:
        raise FormatError('Unsupported magic; expected extracted TMdl or TMF model, not CARDATA/TERF')
    report.update(converter_version=VERSION, sha256=digest, size_bytes=len(data), known_sample=KNOWN.get(digest),
                  exported_draws=len(report['selected_draws']), objects=len(objects),
                  vertices=sum(len(o['attrs']) for o in objects), triangles=sum(len(o['triangles']) for o in objects),
                  scope='Supported base geometry only; no materials, textures, modifiers, hierarchy or scene transforms; unchanged decoded axes/UVs; see uv_export for the OBJ orientation.')
    if digest not in KNOWN:
        report['warnings'].insert(0, 'Untested file hash: structural checks passed, but visual correctness and game/version support are not established.')
    if body_variant:
        report['body_variant'] = dict(index=body_variant, manufacturer='UNKNOWN; pending textured review',
                                     pairing='PS2 full shape; Xbox additive follower N plus UV set N',
                                     validation='paired position/UV triangles; five supplied Cup model pairs')
        report['scope'] = 'Selected supported Cup body variant plus unchanged base parts; no material assignment or other damage/modifier controls.'
    return objects, report


def decode_all_variants(data, ps2_scale=None):
    """Export shared draws once and each manufacturer-specific body separately."""
    base, report = decode(data, ps2_scale, 0)
    objects = [dict(ob) for ob in base]
    variants = []
    for variant in (1, 2):
        alt, alt_report = decode(data, ps2_scale, variant)
        if report['platform'] == 'Xbox':
            # Xbox follower records contain sparse position deltas and matching
            # UV streams, not full independent indexed draws like PS2.
            changed = {d['row']: d['modifier_row']
                       for d in alt_report['selected_draws']
                       if d.get('body_variant') == variant}
            if not changed:
                raise FormatError(f'Xbox manufacturer body {variant} has no modified draws')
            for ob in alt:
                if ob['row'] in changed:
                    copy = dict(ob)
                    copy['row'] = changed[ob['row']]
                    copy['name'] = f"draw_{copy['row']}"
                    objects.append(copy)
            variants.append(dict(index=variant, changed_draws=len(changed),
                                 modifier_rows=sorted(changed.values())))
        else:
            changed = {d['row']: d['base_row']
                       for d in alt_report['selected_draws'] if 'base_row' in d}
            if not changed:
                raise FormatError(f'PS2 manufacturer body {variant} has no alternate draws')
            for ob in alt:
                if ob['row'] in changed:
                    copy = dict(ob)
                    copy['name'] = f"draw_{copy['row']}_group_{copy.get('group', 0)}"
                    objects.append(copy)
            variants.append(dict(index=variant, changed_draws=len(changed),
                                 replaced_base_draws=sorted(set(changed.values()))))
    report['all_body_variants'] = variants
    report['objects'] = len(objects)
    report['triangles'] = sum(len(ob['triangles']) for ob in objects)
    report['scope'] = 'Shared geometry once plus all three manufacturer body shapes; manufacturer names not established.'
    return objects, report


def merge_draw_sections(objects):
    """Combine geometry sections sharing a source draw, without welding vertices."""
    merged = {}
    for ob in objects:
        row = ob['row']
        if row not in merged:
            merged[row] = dict(name=f'draw_{row}', row=row, attrs=[], triangles=[],
                               normals_present=ob.get('normals_present', True))
        dest = merged[row]
        if dest['normals_present'] != ob.get('normals_present', True):
            raise FormatError(f'Draw {row} mixes normal formats')
        offset = len(dest['attrs'])
        dest['attrs'].extend(ob['attrs'])
        dest['triangles'].extend(tuple(i + offset for i in face) for face in ob['triangles'])
    return list(merged.values())


def obj_text(objects, flip_v=False):
    lines = ['# NASCAR experimental base mesh export v' + VERSION, '# OBJ objects are grouped by source draw record.']
    if flip_v:
        lines.append('# Export UV transform: U=u, V=1-v; raw decoded UVs are unchanged.')
    base = 1
    normal_base = 1
    for ob in objects:
        lines.append('g ' + ob['name'])
        lines.append('o ' + ob['name'])
        has_normals = ob.get('normals_present', True)
        channels = [('v',0,3), ('vt',6,8)] + ([('vn',3,6)] if has_normals else [])
        for tag, a, b in channels:
            for rec in ob['attrs']:
                values = (rec[6], 1.0-rec[7]) if tag == 'vt' and flip_v else rec[a:b]
                lines.append(tag + ' ' + ' '.join(format(v, '.9g') for v in values))
        if has_normals:
            lines.extend('f ' + ' '.join(f'{base+v}/{base+v}/{normal_base+v}' for v in face) for face in ob['triangles'])
            normal_base += len(ob['attrs'])
        else:
            lines.extend('f ' + ' '.join(f'{base+v}/{base+v}' for v in face) for face in ob['triangles'])
        base += len(ob['attrs'])
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--output-dir', type=Path, help='Default: beside each input; output is <source stem>.obj')
    parser.add_argument('--ps2-scale', type=float, help='Explicit position multiplier override; recorded in report')
    parser.add_argument('--flip-v', action='store_true', help='Export V=1-V for the tested texture workflow; raw decoded UVs stay unchanged')
    parser.add_argument('--body-variant', type=int, choices=(0, 1, 2), default=0,
                        help='0: base; 1/2: verified alternate shape with matching UVs (validated rcar100/250/750/2850/Interface sources only)')
    parser.add_argument('--all-body-variants', action='store_true', help='One OBJ containing base and both alternate Cup body shapes; no duplicated shared parts')
    parser.add_argument('--overwrite', action='store_true', help='Replace existing OBJ/report outputs (never input files)')
    args = parser.parse_args(argv)
    failed = False
    for src in args.files:
        try:
            out = args.output_dir if args.output_dir is not None else src.parent
            require(not (args.all_body_variants and args.body_variant), 'Use either --all-body-variants or --body-variant, not both')
            op = out / (src.stem + '.obj')
            require(args.overwrite or not op.exists(), f'Output already exists for {src.name}; use --overwrite deliberately')
            require(src.resolve() != op.resolve(), 'Output would replace input')
            data = src.read_bytes()
            objects, report = decode_all_variants(data, args.ps2_scale) if args.all_body_variants else decode(data, args.ps2_scale, args.body_variant)
            report['source_filename'] = src.name
            report['uv_export'] = dict(mode='flip_v' if args.flip_v else 'raw', formula='U=u, V=1-v' if args.flip_v else 'U=u, V=v', body_scale_applied=False, decoded_uvs='raw; transform applied only while writing OBJ')
            payloads = [(op, obj_text(merge_draw_sections(objects), flip_v=args.flip_v))]
            out.mkdir(parents=True, exist_ok=True)
            temp = []
            try:
                for dest, text in payloads:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=out, prefix='nascar_', suffix='.tmp', delete=False) as f:
                        temp.append((Path(f.name), dest)); f.write(text)
                for source, dest in temp:
                    os.replace(source, dest)
            finally:
                for source, dest in temp:
                    if source.exists(): source.unlink()
            print(f'{src.name}: {report["exported_draws"]} draws, {report["triangles"]} triangles -> {op}')
            if args.flip_v:
                print('  Note: OBJ UVs use U=u, V=1-v. No body-specific scaling applied.')
            for warning in report['warnings']:
                print('  Note: ' + warning)
        except (OSError, FormatError, struct.error) as exc:
            failed = True
            print(f'{src}: ERROR: {exc}', file=sys.stderr)
    return 1 if failed else 0

if __name__ == '__main__':
    sys.exit(main())
