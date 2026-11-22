# Copyright (C) 2026 Keith Sprochi
# SPDX-License-Identifier: GPL-2.0-or-later
"""Setup helper: talk to the BlindsRadio Nano directly. Stop the hub first, because only
one program can have the Nano's serial port open.

    python tools/radio.py --port /dev/ttyUSB0 listen [seconds]
        Print every packet your remote sends while you press its buttons. Gives you the
        network ID, the remote's ID, each room's group number, and the remote's counters.

    python tools/radio.py --port /dev/ttyUSB0 find-blind <room> [--save]
        Ask the blinds in <room>'s group(s) for their position and print the radio ID of
        each one that answers. --save writes it into hub/rooms.json as that room's blind_id.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hub"))

import serial  # noqa: E402

from controller import BlindsController, raw_to_pct  # noqa: E402


def listen(port, seconds):
    s = serial.Serial(port, 115200, timeout=0.1)
    time.sleep(2.5)  # opening the port resets the Nano
    s.reset_input_buffer()
    s.write(f"listen {seconds}\n".encode())
    end = time.time() + seconds + 3
    buf = b""
    while time.time() < end:
        buf += s.read(4096)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace").rstrip()
            print(text, flush=True)
            if text.startswith("listen done"):
                return


def find_blind(port, room, save):
    ctrl = BlindsController(port)
    if room not in ctrl.rooms:
        sys.exit(f"no room '{room}' in hub/rooms.json (rooms: {', '.join(ctrl.rooms)})")
    found = ctrl.find_blind_ids(room)
    if not found:
        sys.exit("no blind answered; check the room's dest, source, shades and counters, and move closer")
    for blind_id, raw in found.items():
        print(f"blind {blind_id} answered: position {raw_to_pct(raw)}% (raw {raw:04x})")
    if save:
        if len(found) != 1:
            sys.exit("more than one blind answered; set blind_id in hub/rooms.json by hand")
        ctrl.set_blind_id(room, next(iter(found)))
        print(f"saved blind_id for {room}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listen")
    p.add_argument("seconds", nargs="?", type=int, default=30)
    p = sub.add_parser("find-blind")
    p.add_argument("room")
    p.add_argument("--save", action="store_true")
    args = ap.parse_args()
    if args.cmd == "listen":
        listen(args.port, args.seconds)
    else:
        find_blind(args.port, args.room, args.save)


if __name__ == "__main__":
    main()
