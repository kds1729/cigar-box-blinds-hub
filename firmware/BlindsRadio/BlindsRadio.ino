// BlindsRadio: USB-serial to 2.4 GHz radio bridge for Hunter Douglas PowerView Gen 2 blinds.
//
// Hardware: Arduino Nano (ATmega328P) + nRF24L01+ module (with a 3.3 V socket adapter).
// Library:  RF24 by TMRh20 (Arduino Library Manager: "RF24").
//
// The Nano does no protocol work of its own. The host (the Python hub) builds every packet,
// including its checksum, and uses three serial commands (115200 baud, one command per line):
//
//   listen [seconds]
//       Sniff the PowerView channel and print every valid packet heard (one line per distinct
//       packet), with its fields decoded. Press buttons on your Pebble remote to learn your
//       network ID, remote ID and group numbers. Stops after <seconds> (default 30) or on any
//       serial input.
//
//   txr <addr hex> <payload hex> [ms]
//       Transmit <payload> back to back for <ms> milliseconds (default 1500).
//       Prints "txr copies: N in MS ms" then "txr bytes: <payload>".
//
//   txq <addr hex> <payload hex> [listen_ms]
//       Like txr (fixed 1500 ms burst), then listen for <listen_ms> (default 1500) for replies
//       addressed to the network ID in the first 3 payload bytes. Prints "RX <millis> <32 bytes hex>"
//       per packet, then "txq sent N writes; heard M packets".
//
// <addr> is the nRF24 address to send to (3 or 5 bytes). The hub uses aaaaaaaaaa: on air it acts
// as extra preamble in front of the real PowerView frame, which is carried in the payload.
//
// SPDX-License-Identifier: MIT

#include <SPI.h>
#include <RF24.h>

// ---- configuration ----------------------------------------------------------------------
#define CE_PIN        10           // nRF24 CE
#define CSN_PIN       9            // nRF24 CSN
#define IRQ_PIN       2            // nRF24 IRQ (must be an interrupt pin: D2 or D3 on a Nano)
#define RF_CHANNEL    7            // PowerView Gen 2: 2.407 GHz
#define PA_LEVEL      RF24_PA_MAX  // try RF24_PA_LOW if an amplified (PA+LNA) module misbehaves on USB power
#define BAUD          115200
#define QUERY_BURST_MS 1500UL      // txq transmit burst length
#define RX_SLOTS      8            // received-packet ring buffer (32 bytes each)

RF24 radio(CE_PIN, CSN_PIN);

// ---- received-packet ring buffer, filled from the radio's IRQ ----------------------------
static uint8_t rxBuf[RX_SLOTS][32];
static volatile uint8_t rxHead = 0, rxTail = 0;  // head: next write, tail: next read

static void onRadioIrq() {
  static uint8_t scratch[32];
  while (radio.available()) {
    uint8_t next = (rxHead + 1) % RX_SLOTS;
    if (next == rxTail) {            // full: drain the radio anyway so the IRQ clears
      radio.read(scratch, 32);
    } else {
      radio.read(rxBuf[rxHead], 32);
      rxHead = next;
    }
  }
}

// Copy the oldest buffered packet into out; false if the buffer is empty.
static bool popPacket(uint8_t *out) {
  bool got = false;
  noInterrupts();
  if (rxTail != rxHead) {
    memcpy(out, rxBuf[rxTail], 32);
    rxTail = (rxTail + 1) % RX_SLOTS;
    got = true;
  }
  interrupts();
  return got;
}

// ---- small helpers -----------------------------------------------------------------------
static void printHex(const uint8_t *p, uint8_t len) {
  for (uint8_t i = 0; i < len; i++) {
    if (p[i] < 0x10) Serial.print('0');
    Serial.print(p[i], HEX);
  }
}

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse a hex string into out (at most maxLen bytes). Returns the byte count, or -1 if invalid.
static int parseHex(const char *s, uint8_t *out, uint8_t maxLen) {
  size_t n = strlen(s);
  if (n == 0 || n % 2 || n / 2 > maxLen) return -1;
  for (size_t i = 0; i < n / 2; i++) {
    int hi = hexNibble(s[2 * i]), lo = hexNibble(s[2 * i + 1]);
    if (hi < 0 || lo < 0) return -1;
    out[i] = (uint8_t)(hi << 4 | lo);
  }
  return (int)(n / 2);
}

// PowerView packet checksum: CRC-16, polynomial 0x755B, initial value 0xFFFF, not reflected.
static uint16_t pvCrc(const uint8_t *p, uint8_t len) {
  uint16_t crc = 0xFFFF;
  while (len--) {
    crc ^= (uint16_t)(*p++) << 8;
    for (uint8_t b = 0; b < 8; b++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x755B : crc << 1;
  }
  return crc;
}

// ---- radio setup -------------------------------------------------------------------------
static void radioReset() {
  detachInterrupt(digitalPinToInterrupt(IRQ_PIN));
  radio.stopListening();
  radio.powerDown();
  radio.begin();
  delay(5);
  radio.setChannel(RF_CHANNEL);
  radio.setDataRate(RF24_1MBPS);
  radio.setPALevel(PA_LEVEL);
  radio.disableCRC();            // PowerView packets carry their own checksum
  radio.setAutoAck(false);
  radio.setRetries(0, 0);
}

static void startReceiving(uint64_t syncAddr) {
  radio.setAddressWidth(3);
  radio.setPayloadSize(32);
  radio.openReadingPipe(0, syncAddr);
  radio.maskIRQ(true, true, false);  // interrupt on "data ready" only
  noInterrupts();
  rxHead = rxTail = 0;
  interrupts();
  radio.startListening();
  attachInterrupt(digitalPinToInterrupt(IRQ_PIN), onRadioIrq, FALLING);
}

// Fast CSN control for the burst loop (digitalWrite is too slow and jittery).
static volatile uint8_t *csnPort;
static uint8_t csnMask;

// Transmit pl back to back for durationMs. CE stays high for the whole burst, and each copy is a
// single SPI transaction (W_TX_PAYLOAD_NOACK + payload). The radio ignores writes while its 3-deep
// TX FIFO is full, so no status polling is needed; with CE high it sends the FIFO continuously.
// This keeps the gaps between copies short and even, which the blinds turned out to need:
// RF24's writeFast() adds a status read per copy, and sends with it worked only ~15% of the time.
static uint32_t burst(const uint8_t *addr, uint8_t addrLen, const uint8_t *pl, uint8_t len,
                      unsigned long durationMs) {
  uint8_t lsbFirst[5];
  for (uint8_t i = 0; i < addrLen; i++) lsbFirst[i] = addr[addrLen - 1 - i];  // register is LSB first
  radioReset();
  radio.setAddressWidth(addrLen);
  radio.setPayloadSize(len);
  radio.openWritingPipe(lsbFirst);
  radio.powerUp();
  delay(2);
  radio.flush_tx();

  uint32_t n = 0;
  unsigned long until = micros() + durationMs * 1000UL;
  digitalWrite(CE_PIN, HIGH);
  while ((long)(until - micros()) > 0) {
    *csnPort &= ~csnMask;
    SPI.transfer(0xB0);  // W_TX_PAYLOAD_NOACK
    for (uint8_t i = 0; i < len; i++) SPI.transfer(pl[i]);
    *csnPort |= csnMask;
    n++;
  }
  delayMicroseconds(500);  // let the last queued copies go out
  digitalWrite(CE_PIN, LOW);
  radio.flush_tx();
  return n;
}

// ---- commands ----------------------------------------------------------------------------
static bool parseAddrPayload(char *a, char *p, uint8_t *addr, uint8_t *addrLen, uint8_t *pl, uint8_t *len) {
  if (!a || !p) return false;
  int al = parseHex(a, addr, 5), pn = parseHex(p, pl, 32);
  if ((al != 3 && al != 5) || pn < 1) return false;
  *addrLen = al;
  *len = pn;
  return true;
}

static void cmdTxr(char *a, char *p, char *d) {
  uint8_t addr[5], addrLen, pl[32], len;
  if (!parseAddrPayload(a, p, addr, &addrLen, pl, &len)) {
    Serial.println(F("usage: txr <addr 3|5 bytes hex> <payload hex, max 32 bytes> [ms]"));
    return;
  }
  unsigned long ms = d ? strtoul(d, NULL, 10) : 1500UL;
  uint32_t n = burst(addr, addrLen, pl, len, ms);
  Serial.print(F("txr copies: ")); Serial.print(n); Serial.print(F(" in ")); Serial.print(ms); Serial.println(F(" ms"));
  Serial.print(F("txr bytes: ")); printHex(pl, len); Serial.println();
}

static void cmdTxq(char *a, char *p, char *d) {
  uint8_t addr[5], addrLen, pl[32], len;
  if (!parseAddrPayload(a, p, addr, &addrLen, pl, &len) || len < 3) {
    Serial.println(F("usage: txq <addr hex> <payload hex starting with the 3-byte network id + header> [listen_ms]"));
    return;
  }
  unsigned long listenMs = d ? strtoul(d, NULL, 10) : 1500UL;
  uint32_t n = burst(addr, addrLen, pl, len, QUERY_BURST_MS);

  // Replies start with the same network ID + header bytes as our frame (for example 12 34 c0).
  // Syncing on them puts the rest of the reply in the 32-byte payload.
  uint64_t sync = ((uint64_t)pl[0] << 16) | ((uint64_t)pl[1] << 8) | pl[2];
  startReceiving(sync);
  uint8_t pkt[32];
  uint16_t heard = 0;
  unsigned long until = millis() + listenMs;
  while ((long)(until - millis()) > 0) {
    while (popPacket(pkt)) {
      heard++;
      Serial.print(F("RX ")); Serial.print(millis()); Serial.print(' '); printHex(pkt, 32); Serial.println();
    }
  }
  radioReset();
  Serial.print(F("txq sent ")); Serial.print(n); Serial.print(F(" writes; heard ")); Serial.print(heard);
  Serial.println(F(" packets"));
}

static void shiftLeft1(uint8_t *p, uint8_t len) {
  for (uint8_t i = 0; i < len; i++) p[i] = (p[i] << 1) | (i + 1 < len ? p[i + 1] >> 7 : 0);
}

// Look for a valid PowerView frame anywhere in buf: [net id 2][header 1][len 1][body len][crc 2],
// where the body starts with: sender, 05, rc1, ff, ff. Returns the frame's offset or -1.
static int findFrame(const uint8_t *buf, uint8_t bufLen, uint8_t *frameLen) {
  for (uint8_t i = 0; i + 4 <= bufLen; i++) {
    uint8_t len = buf[i + 3];
    uint8_t total = 4 + len + 2;
    if (len < 10 || i + total > bufLen) continue;
    if (buf[i + 5] != 0x05 || buf[i + 7] != 0xFF || buf[i + 8] != 0xFF) continue;
    uint16_t crc = (uint16_t)buf[i + 4 + len] << 8 | buf[i + 5 + len];
    if (pvCrc(buf + i, 4 + len) == crc) {
      *frameLen = total;
      return i;
    }
  }
  return -1;
}

static void printCommandName(const uint8_t *c, uint8_t n) {
  if (n >= 2 && c[0] == 0x52) {
    if (c[1] == 0x55) Serial.print(F(" (up)"));
    else if (c[1] == 0x44) Serial.print(F(" (down)"));
    else if (c[1] == 0x53) Serial.print(F(" (stop)"));
  } else if (n >= 5 && c[0] == 0x3F && c[1] == 0x5A && c[3] == 0x40 && c[4] == 0x50) {
    Serial.print(F(" (set position)"));
  } else if (n >= 5 && c[0] == 0x21 && c[1] == 0x5A && c[4] == 0x50) {
    Serial.print(F(" (position report)"));
  }
}

// Print a frame (offsets as in the README's packet layout, counted after the header byte).
static void printFrame(const uint8_t *f, uint8_t total) {
  const uint8_t *b = f + 3;          // b[0] = length byte
  uint8_t end = 1 + b[0];            // index just past the body
  Serial.print(F("PKT ")); printHex(f, total); Serial.println();
  Serial.print(F("    network ")); printHex(f, 2);
  Serial.print(F("  header ")); printHex(f + 2, 1);
  Serial.print(F("  from ")); printHex(b + 6, 2);
  Serial.print(F("  rc1 ")); printHex(b + 3, 1);
  Serial.print(F("  rc2 ")); printHex(b + 10, 1);
  uint8_t i = 11;
  if (b[9] == 0x06) {                // groups: list, 00, logical source, command
    Serial.print(F("  groups"));
    while (i < end && b[i] != 0x00) { Serial.print(' '); printHex(b + i, 1); i++; }
    i += 3;                          // 00 terminator + 2-byte logical source
  } else if (b[9] == 0x05) {         // unicast: destination, source, command
    Serial.print(F("  unicast to ")); printHex(b + 11, 2);
    i += 4;
  } else if (b[9] == 0x04) {
    Serial.print(F("  broadcast"));
  }
  if (i < end) {
    Serial.print(F("  command ")); printHex(b + i, end - i);
    printCommandName(b + i, end - i);
  }
  Serial.println();
}

static void cmdListen(char *d) {
  unsigned long secs = d ? strtoul(d, NULL, 10) : 30UL;
  radioReset();
  startReceiving(0x555555ULL);  // sync on the preamble so every copy of every packet is caught
  Serial.print(F("listening for ")); Serial.print(secs); Serial.println(F(" s; press buttons on the remote (any key stops)"));

  uint8_t pkt[32], last[32], lastLen = 0;
  uint16_t found = 0;
  unsigned long until = millis() + secs * 1000UL;
  while ((long)(until - millis()) > 0 && !Serial.available()) {
    while (popPacket(pkt)) {
      // The radio can sync a few bits early or late on the preamble: try each bit alignment.
      for (uint8_t shift = 0; shift < 8; shift++) {
        uint8_t len;
        int at = findFrame(pkt, 32, &len);
        if (at >= 0) {
          if (len != lastLen || memcmp(last, pkt + at, len) != 0) {  // print each distinct packet once
            memcpy(last, pkt + at, len);
            lastLen = len;
            printFrame(pkt + at, len);
            found++;
          }
          break;
        }
        shiftLeft1(pkt, 32);
      }
    }
  }
  while (Serial.available()) Serial.read();
  radioReset();
  Serial.print(F("listen done; ")); Serial.print(found); Serial.println(F(" distinct packets"));
}

static void dispatch(char *line) {
  char *cmd = strtok(line, " \t");
  if (!cmd) return;
  char *a = strtok(NULL, " \t"), *b = strtok(NULL, " \t"), *c = strtok(NULL, " \t");
  if (!strcmp(cmd, "txr")) cmdTxr(a, b, c);
  else if (!strcmp(cmd, "txq")) cmdTxq(a, b, c);
  else if (!strcmp(cmd, "listen")) cmdListen(a);
  else Serial.println(F("commands: listen [seconds] | txr <addr> <payload> [ms] | txq <addr> <payload> [listen_ms]"));
}

// ---- main --------------------------------------------------------------------------------
void setup() {
  Serial.begin(BAUD);
  pinMode(IRQ_PIN, INPUT);
  csnPort = portOutputRegister(digitalPinToPort(CSN_PIN));
  csnMask = digitalPinToBitMask(CSN_PIN);
  if (!radio.begin()) Serial.println(F("radio not responding: check wiring and power"));
  radioReset();
  Serial.println(F("ready"));
}

void loop() {
  static char line[128];
  static uint8_t n = 0;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\r' || ch == '\n') {
      if (n) { line[n] = 0; dispatch(line); n = 0; }
    } else if (n < sizeof(line) - 1) {
      line[n++] = ch;
    }
  }
}
