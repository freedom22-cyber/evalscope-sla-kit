#!/bin/bash
# Make amd64 containers run on this aarch64 host (SWE-bench_Pro sandbox images
# are amd64 only).
#
# Kernel 5.10 binfmt_misc facts this script is built on (fs/binfmt_misc.c):
#   * /proc/sys/fs/binfmt_misc/status  : "0" disable all, "1" enable all,
#     "-1" DELETE every registered handler.
#   * /proc/sys/fs/binfmt_misc/<entry> : same commands for a single entry.
#   * the magic/mask fields are escape-decoded with UNESCAPE_HEX, i.e. they
#     must be written as literal "\xHH" text.  Raw NUL bytes truncate the
#     field (the unescaper stops at the first NUL) and a truncated magic
#     matches EVERY ELF64 binary -- including the host's own aarch64 ones --
#     which breaks execve() system-wide with ENOEXEC.
#   * the mask must decode to the same length as the magic.
#   * flags: use OCF, NOT POCF.  With 'P' (preserve argv[0]) the kernel hands
#     the original argv[0] to the interpreter as an extra argument, so
#     qemu-user passes it on to the emulated program: bash then tries to run
#     "/bin/bash" as a script file -> "cannot execute binary file".
set -u
INTERP=/usr/local/bin/qemu-x86_64-static
NAME=qemu-x86_64
D=/proc/sys/fs/binfmt_misc

if [ ! -x "$INTERP" ]; then
    echo "MISSING interpreter $INTERP" >&2
    exit 1
fi
[ -d "$D" ] || { modprobe binfmt_misc 2>/dev/null || true; sleep 1; }

python3 - "$INTERP" "$NAME" <<'PY'
import binascii
import os
import sys

interp, name = sys.argv[1], sys.argv[2]
D = '/proc/sys/fs/binfmt_misc'

# canonical qemu-user x86-64 entry (qemu scripts/qemu-binfmt-conf.sh):
#   e_machine is compared in full -> x86-64 only, never aarch64
MAGIC = r'\x7f\x45\x4c\x46\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00'
MASK = r'\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff'

def decode_escapes(text):
    """Decode the \\xHH form the kernel unescapes (UNESCAPE_HEX)."""
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 3 < len(text) and text[i + 1] == 'x':
            out.append(int(text[i + 2:i + 4], 16))
            i += 4
        else:
            out.append(ord(text[i]))
            i += 1
    return bytes(out)


EXPECT_MAGIC = decode_escapes(MAGIC).hex()
EXPECT_MASK = decode_escapes(MASK).hex()


def write(path, data):
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def fields(path):
    out = {}
    for line in open(path):
        line = line.rstrip('\n')
        if ' ' in line:
            k, v = line.split(' ', 1)
            out[k] = v
        else:
            out[line] = True
    return out


# 1) wipe every handler (this also removes half-registered broken entries)
#    and re-enable the subsystem, which is off while a broken entry exists
write(os.path.join(D, 'status'), b'-1\n')
write(os.path.join(D, 'status'), b'1\n')

# 2) register the good one
line = f':{name}:M::{MAGIC}:{MASK}:{interp}:OCF\n'.encode()
write(os.path.join(D, 'register'), line)

path = os.path.join(D, name)
f = fields(path)
if f.get('magic') != EXPECT_MAGIC or f.get('mask') != EXPECT_MASK:
    raise SystemExit(f'registration mismatch: {f}')

# 3) enable
write(path, b'1\n')
write(os.path.join(D, 'status'), b'1\n')

f = fields(path)
print('status:', open(os.path.join(D, 'status')).read().strip())
print(f'{name}: enabled={f.get("enabled")} flags={f.get("flags")} '
      f'magic={f.get("magic")} mask={f.get("mask")} interp={f.get("interpreter")}')
PY
