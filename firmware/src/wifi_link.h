#pragma once
#include <Arduino.h>
#include <Print.h>
#include <ArduinoJson.h>

// House Wi-Fi session (wifi-idle). Same newline JSON as USB.
// Credentials: firmware/wifi.json (gitignored; copy firmware/wifi.example.json). Not stored in git.
// One session: USB wins while the USB host is connected (native CDC) or has spoken recently.

void linkBegin();                         // after boardBeginSerial()
void linkOnWifiSession(void (*fn)());    // called once when the TCP session opens
void linkPoll();                          // from loop(); non-blocking
Print &linkOut();                         // replies and events (USB or the Wi-Fi session, not both)
int linkAvailable();
int linkRead();                           // -1 if none
bool linkSessionChanged();                // true once after USB <-> Wi-Fi; caller drops a partial line

// fw 1.6.0: staying connected without anyone unplugging the board
//  * TCP keepalive on the session socket: a cabinet that vanished (power cut, Wi-Fi blip that ate the
//    close) is noticed in about 25 s instead of never, and the board dials again.
//  * Heartbeat: a cabinet that sends {"cmd":"hb"} (cyd_daemon, every 30 s) arms a silence timer for
//    that session; nothing received for WIFI_SILENCE_DROP_MS (90 s) -> drop and dial again. Hosts that
//    never send hb (older daemons) are not affected.
//  * Wi-Fi lost for WIFI_REJOIN_MS -> join again; still down after WIFI_REBOOT_MS -> restart the
//    board (not while a USB host is driving it).
void linkHeartbeat();                     // a host heartbeat arrived on the current session
void linkStatus(JsonDocument &r);         // "hb" (silence timeout s) + link counters for hello / hb acks
void linkDropForTest();                   // selftest: close the Wi-Fi session as if the link broke
