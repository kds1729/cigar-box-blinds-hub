# PowerView Gen 2 radio protocol

What this project relies on, worked out from captures of a Pebble remote and checked against [mattyway/RFPowerView](https://github.com/mattyway/RFPowerView). All IDs below are made-up examples: run `tools/radio.py listen` to see your own.

## Radio

- Channel 7 (2.407 GHz), 1 Mbps, nRF24 "ShockBurst"-compatible framing.
- No nRF24 auto-ack and no nRF24 CRC: the packet carries its own checksum.
- No encryption.

## Frame

On air, every frame starts with a **2-byte network ID** (shared by all remotes and blinds in a home, e.g. `12 34`) and a **header byte** (`c0`). The hub treats these 3 bytes as the frame's destination (`dest` in `rooms.json`).

Then comes the packet. Byte index after the header byte:

| Bytes | Meaning |
|---|---|
| 0 | length: bytes after this one, not counting the checksum |
| 1 | sender: `00` standard, `10` blind replying |
| 2 | `05` constant |
| 3 | rolling code 1 |
| 4–5 | `ff ff` constant |
| 6–7 | physical source ID (the remote, the hub `0000`, or a blind) |
| 8 | `86` direct, `85` repeated |
| 9 | address type: `04` broadcast, `05` unicast, `06` groups |
| 10 | rolling code 2 |
| then | **groups:** group numbers, `00`, logical source (2 bytes). **unicast:** destination ID (2), source ID (2) |
| then | command |
| last 2 | checksum, big-endian |

**Checksum:** CRC-16, polynomial `0x755B`, initial value `0xFFFF`, not reflected. It's computed over the network ID, the header byte, and the packet from the length byte to the end of the command. See `crc16()` in `hub/hd_packet.py`.

## Commands

| Command | Bytes |
|---|---|
| Up / down / stop | `52 55 00` / `52 44 00` / `52 53 00` |
| Set position | `3f 5a 04 40 50 <lo> <hi>`: a 16-bit position, low byte first; `ffff` = fully open |
| Ask for position | `3f 5a 02 3f 50` |
| Blind's position report | `21 5a 04 21 50 <lo> <hi>` |

**Set-position must come from the hub's ID (`0000`), sent unicast to the blind's own ID.** Sent from the remote's ID in a groups packet, the blinds ignored it almost every time. Up, down, stop and the position query work as groups packets from the remote's ID.

## Rolling codes

Rolling code 1 counts the sending device's transmissions, and rolling code 2 identifies the logical command (repeaters keep it, so receivers can drop duplicate copies). Blinds ignore a packet whose counter is well below the last one they saw from that sender. The hub sends each command with both counters one above the last ones it used, and saves them to `rooms.json` before transmitting. Packets sent as the hub use a separate counter (`hub_rc`).

## Examples

Network `1234`, remote `abcd`, group `01`, blind `5678`. Each is shown as dest + packet + checksum:

- Down, group 1: `1234c0 11 00 05 33 ff ff ab cd 86 06 39 01 00 ab cd 52 44 00 ce 29`
- Position query, group 1: `1234c0 13 00 05 49 ff ff ab cd 86 06 4f 01 00 ab cd 3f 5a 02 3f 50 94 f1`
- Set position `0x7fff` (half), as the hub, to blind `5678`: `1234c0 15 00 05 48 ff ff 00 00 86 05 48 56 78 00 00 3f 5a 04 40 50 ff 7f 34 7c`
- Blind `5678` reporting `0x7fda` to the remote: `1234c0 15 10 05 50 ff ff 56 78 86 05 62 ab cd 56 78 21 5a 04 21 50 da 7f c8 99`

The position scale is the blind's own 0x0000–0xFFFF. It isn't necessarily linear with visible height: on one blind, `0x7fff` looked about 60% open.

## Transmitting reliably

The real remote sends each command as a long, tight burst of identical copies. Two details turned out to matter:

1. **Framing.** The sketch sends to a fake 5-byte nRF24 address `aaaaaaaaaa`, which works as extra preamble. The real frame (`dest` + packet + checksum) goes in the payload, padded with `aa` bytes. Sending straight to the 3-byte address without the extra preamble failed.
2. **Even, back-to-back copies for 1.5 s.** RF24's `writeFast()` reads the status register before every copy, which leaves irregular gaps, and sends made with it worked only about 15% of the time. The sketch instead holds CE high for the whole burst and writes each copy as a single SPI transaction (`W_TX_PAYLOAD_NOACK` plus the payload). The radio ignores writes while its 3-deep queue is full and keeps sending continuously. Shorter bursts (300 ms) failed.

## Listening

To capture a remote, the sketch syncs on the `55 55 55` preamble, so the payload holds the rest of the preamble and then the frame, often a few bits out of alignment. It tries each bit shift and keeps any frame whose checksum is valid.

To catch a blind's reply after a query, it syncs on the 3-byte `dest` instead, so the payload starts with the reply's length byte. Replies arrive up to about 1 s after the query burst ends.
