# Changelog

## [0.2.0]

- New **Pets** sidebar app (`/petlibro-local`, icon `mdi:paw`; registered when the entry loads, removed when it
  unloads; changes need an admin): Overview with live device cards and actions (feed now, pump, light, reset
  filter), Schedule editor for feeder meals (labels, weekdays, on/off, next 3 feeds, first-save warning, typed
  confirmation to clear) and Alerts.
- Alert engine with notify targets (notify services and entities), Home Assistant persistent notifications, quiet
  hours, re-reminders, resolved messages, a daily summary and the `petlibro_lan_alert` event. Alerts: food low,
  water low, filter due/overdue, device offline, feed failed, fountain battery low.
- Websocket API under `petlibro_lan/` (state, subscribe, schedule, alerts, notify targets, test, command); changes
  are admin only.
- Water low now follows a configurable threshold (app setting, else the fountain's own plausible `lowWater`, else
  500 g). New binary sensor "Filter due" for fountains. The fountain radar flag is parsed (last pet seen drinking).
- Feeding plan entries (service and app) accept `label` and `enabled`.
- The manifest now depends on frontend, http, panel_custom, persistent_notification and websocket_api.

## [0.1.2]

- Fix a reply loop with the real Granary (PLAF103 fw 1.1.1): it answers ATTR_GET_SERVICE on
  `event/post`, and acknowledging that answer made it reply again several times a second. Replies
  to our commands (`*_SERVICE`) are now never acknowledged, whatever channel they arrive on.

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
