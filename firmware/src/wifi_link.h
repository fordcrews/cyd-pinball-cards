#pragma once
#include <Arduino.h>
#include <Print.h>

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