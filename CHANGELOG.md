# Changelog

## [0.1.1]

- Share port 1883 with another device: connections that start with a TLS handshake are relayed
  unchanged to a host:port set in the options ("Relay TLS connections to"), so a TLS MQTT add-on
  can keep working beside the broker. Without the option they are refused, as before.

## [0.1.0]

First release.

- Built-in MQTT broker (3.1 and 3.1.1, QoS 0/1, pure asyncio) that PetLibro feeders and water fountains
  connect to once `mqtt.us.petlibro.com` is rewritten to Home Assistant in your DNS.
- Devices appear automatically when they first connect and are remembered across restarts.
- Feeders (Granary PLAF103, One RFID PLAF301; Polar PLAF109 and Camera PLAF203 read-only): feed now,
  portions, food low, dispenser blocked, battery, power source, Wi-Fi signal, last feed, feeds today,
  feeding plan, indicator light and button lock.
- Fountains (PLWF106, PLWF116, PLWF305): pump and light switches, water weight/level/low, filter
  remaining, reset filter, zero scale.
- Services `petlibro_lan.feed` and `petlibro_lan.set_feeding_plan` (local time, converted to UTC and
  re-sent on DST changes and daily). A device's own schedule is never cleared unless you ask.
- Security: LAN-only peers, connection/packet/rate limits, per-device topic isolation.
