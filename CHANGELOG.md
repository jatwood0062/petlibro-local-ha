# Changelog

## [0.3.0]

- **Devices tab** in the Pets app (administrators): every known device, connected or not, with name, model, masked
  serial (last 4 shown), IP, firmware, last seen and connection state. **Rename** (sets the device registry
  `name_by_user`), **Remove** (confirmation; disconnects it from the broker and deletes its stored state, schedule,
  calibration, water history, alert state, device and entities) and **Block (ignore)**: a blocked serial gets
  CONNACK 5 (not authorized) from the broker until it is unblocked in the "Blocked devices" list. The block list is
  stored with the other data. Home Assistant's own device page "Delete" now works too
  (`async_remove_config_entry_device`). A removed device that is still redirected to Home Assistant reappears when
  it next connects; the app says so.
- **Add a device** guide: set up on Wi-Fi with the PetLibro app once, DNS rewrite, pause or power-cycle. A live
  "Waiting for new devices..." indicator shows devices that are connecting and the moment one has connected, and a
  **DNS check** resolves `mqtt.us.petlibro.com` (off the event loop) and compares it with this host's LAN addresses
  (green/red, with the address it resolves to). New dependency: `network`.
- **Fountain water-level calibration**: a wizard (Overview card or Devices tab) captures the empty and full weights
  (the average of the next water reading, waits up to 60 s) with validation (full at least 300 g above empty), or
  takes them by hand; Reset calibration; an advanced, warned "Zero scale" step. Stored per device.
- With a calibration: **Water level** is the position between empty and full (0-100 %); new sensors **Water
  remaining** (mL), **Water used today** (mL; decreases are summed, refills of 150 g or more and lifting the
  reservoir off are ignored) and **Water days left** (3-day average). Without one, Water level keeps the device's
  own percent as before. Overview cards show a level bar.
- **Water low alert** is a percentage (default 25 %, 3-point hysteresis) for calibrated fountains; the grams rules
  are unchanged for the others. Old alert settings load unchanged (`threshold_pct` defaults to 25).
- Websocket commands: `dns_check`, `rename_device`, `remove_device`, `block_device`, `unblock_device`,
  `calibration_capture`, `calibration_set`, `calibration_reset`, `zero_scale` (all admin only). The `state` and
  `subscribe` results carry `blocked`, `connecting` and `port` for administrators.
- Tests for calibration math, consumption and refill detection, the broker block list and disconnect, settings
  and stored-data migration, DNS classification.

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
