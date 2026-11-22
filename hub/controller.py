# Copyright (C) 2026 Keith Sprochi
# SPDX-License-Identifier: GPL-2.0-or-later
"""Owns the serial connection to the antenna Nano and drives the blinds.

Opens the serial port once at process start (opening it again resets the Nano), so only
one program may use the Nano at a time: stop the hub before running tools/radio.py.

The rolling counters (rc1/rc2) belong to the remote id, so all rooms with the same source
share them; they persist to rooms.json (on every such room) before each send, so they
survive a hub restart.

Position uses the blind's own protocol, never timing (motor speed falls as the battery
drains, and any single send can be missed):
- set_position sends a SET of the position field, as the hub (id 0000) unicast to the
  blind's own id; the blind drives itself there.
- read_position sends a FETCH query and parses the blind's reply (checked by CRC and by
  the blind's radio id). Position is 0-100 % of the blind's raw 0x0000-0xFFFF scale
  (0xFFFF = fully open). The scale isn't necessarily linear with visible height.
Nothing here assumes a command took effect; only a read reports where a blind is.
A room can have a bottom no-go line (min_raw) and a top line (max_raw), each set from a
real reading: set_position refuses anything outside them, and plain down/up are refused
while the matching line exists, so the hub never drives a blind past either one.
"""
import json
import os
import threading
import time

import serial

from hd_packet import build_groups, crc16

ROOMS_PATH = os.path.join(os.path.dirname(__file__), "rooms.json")
BAUD = 115200
BURST_MS = 1500
BURSTS_PER_COMMAND = 1  # one burst was always enough in testing; a second burst only delays Stop
HUB_ID = "0000"          # the PowerView hub's logical id; blinds act on set-position only when it comes from the hub
QUERY_LISTEN_MS = 1500  # the blind's reply comes up to ~1 s after our burst ends (500 ms missed some)
QUERY_TRIES = 3         # one query in testing got no reply; retry with fresh counters
FAKE_ADDR = "aaaaaaaaaa"  # 5-byte fake address acts as extra preamble (the framing that works)
MAX_PAYLOAD = 32


def _load_rooms():
    if not os.path.exists(ROOMS_PATH):
        raise SystemExit(f"{ROOMS_PATH} not found: copy hub/rooms.example.json to hub/rooms.json "
                         "and fill it in (see README, 'Set up your rooms')")
    with open(ROOMS_PATH) as f:
        return json.load(f)


def _save_rooms(rooms):
    tmp = ROOMS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rooms, f, indent=2)
    os.replace(tmp, ROOMS_PATH)


def pct_to_raw(pct):
    return round(max(0.0, min(100.0, float(pct))) / 100.0 * 0xFFFF)


def raw_to_pct(raw):
    return round(raw / 0xFFFF * 100.0, 1)


def parse_any_position_reply(dest_hex, rx_hex):
    """Parse one `RX` payload (the bytes after the 3-byte dest, e.g. 1234c0) as a position
    report from any blind. Returns (blind_id_hex, raw 16-bit position), or None if it isn't
    a valid report. Layout: 15 <sender> 05 rc1 ffff <blind> 86|85 05 rc2 <to> <blind>
    21 5a 04 21 50 <lo> <hi> <crc16>."""
    try:
        b = bytes.fromhex(rx_hex)
    except ValueError:
        return None
    if len(b) < 24 or b[0] != 0x15:
        return None
    pkt = b[:22]
    if crc16(bytes.fromhex(dest_hex) + pkt) != int.from_bytes(b[22:24], "big"):
        return None
    if pkt[6:8] != pkt[13:15] or pkt[9] != 0x05:
        return None
    if pkt[15:20] != bytes.fromhex("215a042150"):
        return None
    return pkt[6:8].hex(), pkt[20] | (pkt[21] << 8)


def parse_position_reply(dest_hex, blind_id_hex, rx_hex):
    """Like parse_any_position_reply, but only accepts a report from blind_id. Returns the
    raw 16-bit position or None."""
    got = parse_any_position_reply(dest_hex, rx_hex)
    if got is None or got[0] != blind_id_hex.lower():
        return None
    return got[1]


class BlindsController:
    def __init__(self, port):
        self.port = port
        self.ser = serial.Serial(port, BAUD, timeout=0.05)
        self.lock = threading.Lock()          # serial port
        self.counter_lock = threading.Lock()  # rolling counters + rooms.json
        self.rooms = _load_rooms()
        # opening the port resets the Nano; wait out the boot and drop its banner
        time.sleep(2.0)
        self.ser.reset_input_buffer()

    # ---- low-level ----

    def _serial_cmd(self, line, until_marker, timeout_s):
        """Send one sketch command and return its output, stopping at until_marker."""
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((line + "\r\n").encode())
            out = b""
            end = time.time() + timeout_s
            while time.time() < end:
                out += self.ser.read(4096)
                if until_marker.encode() in out and out.endswith(b"\n"):
                    break
        return out.decode(errors="replace")

    def _next_counters(self, room):
        """rc1/rc2 belong to the sending remote (source id), not the room: every room that
        uses the same source shares one counter pair, so take the next value after the
        latest one used by any of them, and record it on all of them before sending."""
        r = self.rooms[room]
        with self.counter_lock:
            peers = [p for p in self.rooms.values() if p.get("source") == r["source"]]
            # rooms are normally identical; if not, pick the one furthest ahead of this room
            # by rc2, treating a lead of under 128 as "ahead" so 0xff -> 0x00 wraps correctly
            last = max(peers, key=lambda p: d if (d := (p["rc2"] - r["rc2"]) & 0xFF) < 128 else -1)
            rc1 = (last["rc1"] + 1) & 0xFF
            rc2 = (last["rc2"] + 1) & 0xFF
            for p in peers:
                p["rc1"], p["rc2"] = rc1, rc2
            _save_rooms(self.rooms)
        return rc1, rc2

    def _payload(self, room, command_hex):
        """Fresh counters + groups packet + CRC, framed as dest + packet + aa filler."""
        r = self.rooms[room]
        if not r.get("captured"):
            raise ValueError(f"{room} has no captured protocol data yet")
        rc1, rc2 = self._next_counters(room)
        shades_hex = "".join(f"{s:02x}" for s in r["shades"])
        pkt = build_groups(r["dest"], r["source"], f"{rc1:02x}", f"{rc2:02x}", shades_hex, command_hex)
        frame = bytes.fromhex(r["dest"]) + pkt
        frame += b"\xaa" * min(6, MAX_PAYLOAD - len(frame))  # 6 normally, 5 for set-position
        return frame.hex(), {"rc1": rc1, "rc2": rc2}

    def _next_hub_counter(self):
        """Counter for packets sent as the hub (HUB_ID); separate from the remote's pair.
        Kept in rooms.json as hub_rc on every room."""
        with self.counter_lock:
            rc = (max(p.get("hub_rc", 0x15) for p in self.rooms.values()) + 1) & 0xFF
            for p in self.rooms.values():
                p["hub_rc"] = rc
            _save_rooms(self.rooms)
        return rc

    def _hub_unicast_payload(self, room, command_hex):
        """Unicast packet from the hub id straight to this room's blind id."""
        r = self.rooms[room]
        if not r.get("blind_id"):
            raise ValueError(f"{room} has no blind_id in rooms.json")
        rc = self._next_hub_counter()
        body = bytes.fromhex(f"0005{rc:02x}ffff{HUB_ID}8605{rc:02x}{r['blind_id']}{HUB_ID}{command_hex}")
        frame = bytes.fromhex(r["dest"]) + bytes([len(body)]) + body
        frame += crc16(frame).to_bytes(2, "big")
        frame += b"\xaa" * min(6, MAX_PAYLOAD - len(frame))
        return frame.hex(), {"hub_rc": rc}

    def _send(self, room, command_hex):
        payload_hex, counters = self._payload(room, command_hex)
        for _ in range(BURSTS_PER_COMMAND):
            self._serial_cmd(f"txr {FAKE_ADDR} {payload_hex} {BURST_MS}", "txr bytes", BURST_MS / 1000 + 2)
        return counters

    # ---- commands ----

    def up(self, room):
        if self.rooms[room].get("max_raw") is not None:
            raise ValueError(f"{room} has a top line; plain up would pass it. Set a position instead.")
        return self._send(room, "525500")

    def down(self, room):
        if self.rooms[room].get("min_raw") is not None:
            raise ValueError(f"{room} has a no-go zone; plain down would enter it. Set a position instead.")
        return self._send(room, "524400")

    def stop(self, room):
        return self._send(room, "525300")

    def set_position(self, room, pct):
        """Tell the blind to drive itself to pct (0 = closed, 100 = open) of its raw scale.
        Doesn't wait for or assume arrival; read_position reports where it actually is."""
        raw = pct_to_raw(pct)
        min_raw = self.rooms[room].get("min_raw")
        if min_raw is not None and raw < min_raw:
            raise ValueError(f"{pct}% is inside {room}'s no-go zone (limit {raw_to_pct(min_raw)}%)")
        max_raw = self.rooms[room].get("max_raw")
        if max_raw is not None and raw > max_raw:
            raise ValueError(f"{pct}% is above {room}'s top line ({raw_to_pct(max_raw)}%)")
        # Sent as the hub, unicast to the blind: from the remote's id in a groups packet the
        # blind ignored set-position almost every time (2 of ~12); as the hub it took 4 of 4.
        payload_hex, result = self._hub_unicast_payload(room, f"3f5a044050{raw & 0xFF:02x}{raw >> 8:02x}")
        for _ in range(BURSTS_PER_COMMAND):
            self._serial_cmd(f"txr {FAKE_ADDR} {payload_hex} {BURST_MS}", "txr bytes", BURST_MS / 1000 + 2)
        result.update({"requested_pct": float(pct), "requested_raw": f"{raw:04x}"})
        return result

    def read_position(self, room):
        """Ask the blind for its position and return what it reports. Raises if no valid
        reply arrives after QUERY_TRIES queries."""
        r = self.rooms[room]
        if not r.get("blind_id"):
            raise ValueError(f"{room} has no blind_id in rooms.json")
        for attempt in range(1, QUERY_TRIES + 1):
            payload_hex, counters = self._payload(room, "3f5a023f50")
            out = self._serial_cmd(f"txq {FAKE_ADDR} {payload_hex} {QUERY_LISTEN_MS}", "txq sent",
                                   (BURST_MS + QUERY_LISTEN_MS) / 1000 + 2)
            values = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[0] == "RX":
                    v = parse_position_reply(r["dest"], r["blind_id"], parts[2])
                    if v is not None:
                        values.append(v)
            if values:
                raw = max(set(values), key=values.count)
                reading = {"pct": raw_to_pct(raw), "raw": f"{raw:04x}", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                with self.counter_lock:
                    r["last_read"] = reading
                    _save_rooms(self.rooms)
                return dict(reading, attempts=attempt, **counters)
        raise RuntimeError(f"no position reply from {room} after {QUERY_TRIES} queries")

    def find_blind_ids(self, room, tries=3):
        """Send this room's group a position query and return {blind_id: raw position} for
        every blind that answers. Used once during setup to fill in blind_id."""
        r = self.rooms[room]
        found = {}
        for _ in range(tries):
            payload_hex, _counters = self._payload(room, "3f5a023f50")
            out = self._serial_cmd(f"txq {FAKE_ADDR} {payload_hex} {QUERY_LISTEN_MS}", "txq sent",
                                   (BURST_MS + QUERY_LISTEN_MS) / 1000 + 2)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[0] == "RX":
                    got = parse_any_position_reply(r["dest"], parts[2])
                    if got:
                        found[got[0]] = got[1]
            if found:
                break
        return found

    def set_blind_id(self, room, blind_id):
        with self.counter_lock:
            self.rooms[room]["blind_id"] = blind_id
            _save_rooms(self.rooms)

    LIMIT_KEYS = {"bottom": "min_raw", "top": "max_raw"}

    def set_limit_here(self, room, kind):
        """Read the blind's current position and make it the bottom no-go line or the top
        line: no command from the hub may send the blind past it."""
        key = self.LIMIT_KEYS[kind]
        reading = self.read_position(room)
        raw = int(reading["raw"], 16)
        r = self.rooms[room]
        other = r.get(self.LIMIT_KEYS["top" if kind == "bottom" else "bottom"])
        if other is not None and (raw >= other if kind == "bottom" else raw <= other):
            raise ValueError(f"the blind is at {reading['pct']}%, which is on the wrong side of the other line")
        with self.counter_lock:
            r[key] = raw
            _save_rooms(self.rooms)
        return reading

    def clear_limit(self, room, kind):
        with self.counter_lock:
            self.rooms[room].pop(self.LIMIT_KEYS[kind], None)
            _save_rooms(self.rooms)

    def get_rooms(self):
        return self.rooms
