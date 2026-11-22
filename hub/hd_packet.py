"""Build PowerView Gen 2 radio packets with a valid checksum.

Usage (prints a ready-to-paste `txr` line for the BlindsRadio sketch):
    python hub/hd_packet.py <dest 6 hex> <remote id 4 hex> <rc1 hex> <rc2 hex> <groups e.g. 01 or 0102> <cmd 55|44|53>
    e.g. python hub/hd_packet.py 1234c0 abcd 44 e6 01 53     (stop, group 1)
"""
import sys


def crc16(data, poly=0x755B, init=0xFFFF):
    """PowerView checksum: CRC-16, polynomial 0x755B, initial 0xFFFF, not reflected."""
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build(dest, source, rc1, rc2, shades, cmd):
    """Simple move command to groups: cmd is 55 (up), 44 (down) or 53 (stop)."""
    return build_groups(dest, source, rc1, rc2, shades, "52" + cmd + "00")


def build_groups(dest, source, rc1, rc2, shades, command_hex):
    """Groups packet from `source` with an arbitrary command tail, e.g. 524400 (down),
    3f5a044050<lo><hi> (set position), 3f5a023f50 (position query)."""
    body = bytes.fromhex("0005" + rc1 + "ffff" + source + "8606" + rc2 + shades + "00" + source + command_hex)
    pkt = bytes([len(body)]) + body  # the length byte counts the body only (not itself or the CRC)
    crc = crc16(bytes.fromhex(dest) + pkt)
    return pkt + crc.to_bytes(2, "big")


if __name__ == "__main__":
    dest, source, rc1, rc2, shades, cmd = sys.argv[1:7]
    pkt = build(dest, source, rc1, rc2, shades, cmd)
    frame = bytes.fromhex(dest) + pkt
    frame += b"\xaa" * min(6, 32 - len(frame))
    print("packet:", pkt.hex())
    print("txr aaaaaaaaaa " + frame.hex())
