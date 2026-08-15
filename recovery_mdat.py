#!/usr/bin/env python3
"""
Recover H.264 from an MP4 that has no moov atom. Constant memory.

Walks the mdat payload as AVCC-style 4-byte-length-prefixed NAL units and
writes an Annex B elementary stream. Resyncs byte-by-byte on garbage.

Library usage:
  from recover_mdat import recover_mdat, extract_h264

  recover_mdat('broken.mp4')                  # remux in place via ffmpeg
  recover_mdat('broken.mp4', 'fixed.mp4')      # remux to a chosen path
  extract_h264('broken.mp4', 'out.h264')       # just dump the elementary stream

CLI usage:
  recover_mdat.py broken.mp4              # remux in place via ffmpeg
  recover_mdat.py broken.mp4 out.h264
  recover_mdat.py broken.mp4 -            # stream elementary stream to stdout
"""
import mmap
import os
import struct
import subprocess
import sys

MAX_NAL = 8 * 1024 * 1024
START = b'\x00\x00\x00\x01'


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def find_mdat_payload(mm, size):
    pos = 0
    while pos + 8 <= size:
        box = struct.unpack('>I', mm[pos:pos + 4])[0]
        btype = mm[pos + 4:pos + 8]
        if btype == b'mdat':
            return pos + 16 if box == 1 else pos + 8
        if box < 8:
            break
        pos += box
    idx = mm.find(b'mdat', 0, min(size, 1 << 20))
    if idx < 0:
        raise ValueError('no mdat box found in first 1 MiB')
    return idx + 4


def plausible(b):
    if b & 0x80:
        return False
    t = b & 0x1F
    return 1 <= t <= 12


def scan_nals(mm, size, pos, out):
    """Walk mdat payload from `pos`, writing Annex B NALs to `out`. Returns stats dict."""
    kept = resync = 0
    types = {}
    next_tick = pos + 50_000_000

    while pos + 5 <= size:
        ln = struct.unpack('>I', mm[pos:pos + 4])[0]
        avail = size - (pos + 4)

        if ln == 0 or ln > MAX_NAL or not plausible(mm[pos + 4]):
            pos += 1
            resync += 1
            continue

        if ln > avail:
            log(f'truncated tail: wanted {ln}, have {avail} -- writing partial')
            out.write(START)
            out.write(mm[pos + 4:size])
            kept += 1
            break

        t = mm[pos + 4] & 0x1F
        types[t] = types.get(t, 0) + 1
        out.write(START)
        out.write(mm[pos + 4:pos + 4 + ln])
        kept += 1
        pos += 4 + ln

        if pos > next_tick:
            log(f'  {100.0 * pos / size:5.1f}%  {kept} NALs, {resync} resync bytes')
            next_tick = pos + 50_000_000

    log(f'done: {kept} NALs, {resync} bytes skipped resyncing')
    names = {1: 'P/B slice', 5: 'IDR', 6: 'SEI', 7: 'SPS', 8: 'PPS', 9: 'AUD'}
    for t in sorted(types):
        log(f'  type {t:2d} {names.get(t, ""):10s} x{types[t]}')

    return {'kept': kept, 'resync': resync, 'types': types}


def extract_h264(src, out):
    """
    Extract the Annex B elementary stream from `src` into `out`.

    `out` may be a path or a writable binary file-like object (e.g. a
    subprocess's stdin, or sys.stdout.buffer). Returns the scan_nals() stats.
    """
    owns_out = isinstance(out, (str, os.PathLike))
    size = os.path.getsize(src)
    with open(src, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pos = find_mdat_payload(mm, size)
        log(f'mdat payload at 0x{pos:x}, {(size - pos) / 1e6:.1f} MB to scan')

        stream = open(out, 'wb') if owns_out else out
        try:
            return scan_nals(mm, size, pos, stream)
        finally:
            stream.flush()
            if owns_out:
                stream.close()


def recover_mdat(src, dst=None, fps=25, ffmpeg='ffmpeg'):
    """
    Recover a moov-less MP4 by re-extracting its H.264 stream and remuxing
    it with ffmpeg. If `dst` is omitted, the recovered file replaces `src`
    in place (via a temporary "fixed.mp4" alongside it). Returns the output
    path. Raises RuntimeError if ffmpeg fails; `src` is left untouched.
    """
    in_place = dst is None
    out_path = dst or os.path.join(os.path.dirname(os.path.abspath(src)), 'fixed.mp4')

    proc = subprocess.Popen(
        ['ffmpeg', '-y', '-r', str(fps), '-f', 'h264', '-i', 'pipe:0', '-c', 'copy', out_path],
        stdin=subprocess.PIPE,
    )
    try:
        extract_h264(src, proc.stdin)
    finally:
        proc.stdin.close()
    ret = proc.wait()

    if ret != 0:
        raise RuntimeError(f'ffmpeg exited with status {ret}; {src} left untouched')

    if in_place:
        os.replace(out_path, src)
        log(f'replaced {src} with remuxed output')
        return src

    log(f'wrote {out_path}')
    return out_path


def main():
    if len(sys.argv) not in (2, 3):
        log(f'usage: {sys.argv[0]} src.mp4 [dst.mp4|out.h264|-]')
        return 1

    src = sys.argv[1]
    try:
        if len(sys.argv) == 2:
            recover_mdat(src)
        else:
            dst = sys.argv[2]
            out = sys.stdout.buffer if dst == '-' else dst
            extract_h264(src, out)
    except (ValueError, RuntimeError) as e:
        log(f'error: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())