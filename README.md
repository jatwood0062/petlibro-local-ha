# PetLibro Local

A Home Assistant custom integration (HACS) that controls PetLibro feeders and water fountains
**fully locally**. It runs a small MQTT broker inside Home Assistant; the devices connect to it instead
of the PetLibro cloud. No account, no cloud, no extra add-on. It uses its own domain (`petlibro_lan`),
so it can be installed next to the cloud `petlibro` integration.

> Unofficial and unaffiliated with PetLibro. The protocol was reverse engineered by others; see
> the credits. Feeding plans control a real animal's meals: read "Feeding plans" below.

## Supported devices

| Device | Wire model | What you get |
|---|---|---|
| Granary Smart Feeder | PLAF103 | feed now, plan, sensors, indicator light, button lock |
| One RFID Feeder | PLAF301 | feed now (1-5), plan, sensors, button lock |
| Polar wet-food feeder, Granary Camera Feeder | PLAF109, PLAF203 | sensors only |
| Dockstream 2 / Cordless / RFID fountains | PLWF106, PLWF116, PLWF305 | pump, light, water, filter |

All of them also appear in the **Pets** sidebar app (overview, feeding schedule editor, alerts, devices).

## Setup

1. Install through HACS (custom repository, category Integration) and restart Home Assistant.
2. **Point the devices at Home Assistant.** In your DNS server (AdGuard Home, Pi-hole, your router)
   add a rewrite so `mqtt.us.petlibro.com` resolves to your Home Assistant's LAN IP:
   - AdGuard Home: Filters > DNS rewrites > `mqtt.us.petlibro.com` -> `192.168.x.y`
   - Pi-hole: Local DNS > DNS Records, or `address=/mqtt.us.petlibro.com/192.168.x.y` in dnsmasq
3. Add the integration (Settings > Devices & services > Add integration > PetLibro Local). It checks
   that port 1883 is free; stop any other MQTT broker bound to 1883 on the same host first. If your
   Home Assistant runs in a container, publish/host-network port 1883.
4. Power-cycle each device (or remove its backup battery briefly) so it re-resolves DNS. It connects
   within a minute or two and its entities appear.

**Recommended: block the devices' internet access** (router firewall or a guest VLAN). The firmware
caches the cloud broker's IP and falls back to it directly when the local broker is unreachable,
even briefly, and some firmware never does a DNS lookup at all. If blocking is not possible, DNAT
the device's outbound TCP 1883 to Home Assistant. Restarting Home Assistant does not reboot the
devices, but they reconnect on their own.

## Entities

Feeder: Feed now (button), Portions for feed now (number), Food low, Dispenser blocked, Battery,
Power source, Wi-Fi signal, Last feed, Last feed portions, Feeds today, Feeding plan (state = next
feed, attributes = list), Indicator light and Button lock (switches). Fountain: Pump, Light,
Water weight, Water level, Water remaining, Water used today, Water days left, Water low, Filter remaining,
Filter due, Reset filter, Zero scale (disabled by default; remove the reservoir first). Water remaining, used and
days left need a water-level calibration (see "Water-level calibration"). Every device also has Connected and an IP address
(diagnostic). Devices become unavailable after 5 minutes without a message.

"Dispenser blocked" is inferred (fewer portions dispensed than requested, or error 2032), not a
firmware flag. "Reset filter" writes a due date 30 days ahead.

## Services

```yaml
action: petlibro_lan.feed
data:
  device_id: <feeder device>
  portions: 2

action: petlibro_lan.set_feeding_plan
data:
  device_id: <feeder device>
  plans:
    - { time: "07:30", portions: 2 }
    - { time: "18:00", portions: 2, days: [1, 2, 3, 4, 5] }   # 1 = Monday ... 7 = Sunday
```

## Pets app (sidebar)

The integration adds a **Pets** entry (paw icon) to the sidebar, at `/petlibro-local`. Everyone can look at it;
every change (feeding, switches, schedules, alert settings) needs an administrator. It is plain JavaScript served
by the integration (no build step, no extra card or resource), follows your theme, and works on a phone.

- **Overview**: one card per device with live status. Feeder: connected, food OK/low, last feed and portions,
  feeds today, next scheduled feed, blocked dispenser. Fountain: pump, light, battery and charging, water weight
  and low, filter days left (shown as "Overdue" in red once past due), when a pet was last seen drinking (radar).
  Actions: Feed now (portion picker and confirmation), pump on/off, light on/off, Reset filter (confirmation).
- **Schedule**: edit each feeder's meals (time, portions 1-12, weekdays, on/off switch, optional label such as
  "Breakfast"), see the next 3 feeds, and Save. Home Assistant **cannot read** the schedule you set in the PetLibro
  app, so the first Save replaces it completely; the app warns you and asks you to tick a box and confirm. Nothing is
  ever sent until you press Save. "Clear schedule" (typed `CLEAR`) is the only way an empty schedule is sent; saving a
  schedule with no enabled meal is refused. Times are Home Assistant's time zone (see "Feeding plans").
- **Devices** (administrators): see "Devices tab" below.
- **Alerts**: choose where alerts go (any `notify.*` service, including the Companion app `notify.mobile_app_*`, and
  any notify entity) and whether to also show a Home Assistant notification, then switch alerts on and tune them:
  food low, water low (percent for calibrated fountains, grams for the rest), filter due (N days before, default 3, and daily at 09:00 once overdue), device offline
  for more than N minutes (default 30), feed failed or dispenser blocked, fountain battery below N % (default 20),
  and an optional daily summary. Quiet hours hold back non-urgent pushes (filter, battery, offline, summary); feed
  failed, food low and water low always go through, and the Home Assistant notification is still created. "Send test
  notification" reports success or the error per target.

How alerts behave: they are checked on every device update and once a minute, and send **on a change** (a problem
starts, a filter phase changes) plus a re-reminder (food, water: every 12 h; battery, offline: every 24 h; filter
overdue: daily at 09:00; feed failed: none; each adjustable, 0 = never). When the problem has been gone for two
minutes a "resolved" message is sent and the Home Assistant notification is dismissed. Water and battery alerts have a
small hysteresis so a reading near the threshold does not flap. Mobile app notifications carry `data.tag` (a new
message for the same alert replaces the old one) and `url` / `clickAction` that open the Pets app. Offline devices do
not raise other alerts from their stale data. Settings and alert state are stored in
`.storage/petlibro_lan.alerts`.

Automations can listen for the event `petlibro_lan_alert`:

```yaml
trigger:
  - trigger: event
    event_type: petlibro_lan_alert
    event_data: { type: food_low, state: active }   # state: active | resolved | summary
# event data: type, device (name), serial, state, title, message, reminder
# types: food_low, water_low, filter_due, offline, feed_failed, battery_low, daily_summary
```

### Devices tab

Lists every device Home Assistant knows, connected or not: name, model, serial (only the last four characters),
IP address, firmware, last seen and connection state.

- **Rename** sets the device name in Home Assistant's device registry (clear it to return to the default name).
- **Remove** asks for confirmation, disconnects the device from the broker and deletes everything stored about it:
  its device and entities, calibration, water-use history, feeding schedule managed here and alert state. Nothing
  stored on the device itself changes. If the device is **still redirected** to Home Assistant it appears again
  the next time it connects, so the dialog offers **Block (ignore) this device**: the broker then answers its
  connection with CONNACK 5 (not authorized). Blocked devices are listed at the bottom of the tab with an
  **Unblock** button, which also shows when a blocked device last tried to connect. The device page's own
  "Delete" button in Home Assistant removes the stored data too.
- **Add a device** shows the steps (set the device up on Wi-Fi with the PetLibro app once; rewrite
  `mqtt.us.petlibro.com` to this host's IP in your DNS; pause the device on your router for about two minutes or
  power-cycle it), a **DNS check**, and a live "Waiting for new devices..." indicator that lists a device while it
  connects and announces it the moment it has connected. The DNS check asks Home Assistant's own resolver and
  compares the answer with this host's private IPv4 addresses (green: it points here; red: another host, an
  internet address, or no answer). Your devices use your router's DNS, so if that differs from Home Assistant's the
  real test is the device connecting.

### Water-level calibration

The fountain's own percentage is unreliable (the Dockstream 2 Cordless stays at one value all day), so the app
calibrates from the scale's weight. Open **Calibrate water level** on a fountain card (Overview or Devices):

1. Empty the reservoir (or lift it off the base) and press **Capture empty**.
2. Fill it to the MAX line, put it back and press **Capture full**.

Each capture waits for the fountain's next reading (about every 45 seconds, up to 60 s) and averages its samples.
Full must be at least 300 g heavier than empty. You can also type both weights by hand, **Reset calibration**, or
use the advanced **Zero scale** step (the scale then reads nothing for what is on it: lift the reservoir off first
and redo the calibration afterwards).

With a calibration, **Water level** is `(weight - empty) / (full - empty)`, clamped to 0-100 %; **Water remaining**
is the weight above empty in mL (1 g of water is about 1 mL). **Water used today** adds up the decreases between
readings (jitter under 10 g is ignored), skips refills (a rise of 150 g or more) and lifting the reservoir off (a
sudden drop of 300 g or more), and resets at midnight. **Water days left** is remaining water divided by the
average use of the last three complete days (it needs at least one). The Overview card shows a level bar with the
alert threshold marked. Without a calibration nothing changes: Water level keeps the device's own percentage and
the other three read unknown.

### Assumptions to know about

- **Food low** is the inverse of the feeder's `surplusGrain` flag (true means food present).
- **Water low** of a **calibrated** fountain is the level in percent below the threshold in the Alerts tab (default
  25 %; it clears 3 points above). For an uncalibrated one it needs a weight (grams, sent by the fountain every
  ~45 s) and is low when below, in order: the
  threshold you set in the Alerts tab; the fountain's own `lowWater` value, if it looks like grams (100-5000); else
  500 g. What unit `lowWater` really uses is not confirmed (one fountain reports 1649), so check the "low below"
  line on the card and set your own threshold if it looks wrong. The percent value is not used (unreliable on the
  cordless model). The Water low entity follows the same rule.
- **Filter due** compares `filterNextReplacementTimestamp` (epoch ms) with now. "Reset filter" writes a date 30 days
  ahead, because the firmware does not say its own filter life. "Filter due" (binary sensor) is on once overdue.
- **Charging**: `batteryState` "CHARGING" means charging and "CHARGED" means full on the charger; low-battery alerts
  are not sent while charging.
- "Pet last seen drinking" comes from the fountain's radar flag and exists only on models that report it.

## Feeding plans

- The device keeps its own schedule and runs it by itself. This integration **never overwrites it
  unless you call `set_feeding_plan`**. When a device asks for its plan at boot and you have not
  configured one here, it is answered with a plain acknowledgement (no `plans` key), which stops the
  retries and leaves the stored schedule alone. Passing an empty list to the service is an explicit
  request to clear the device's schedule. `plans` entries also accept `label` and `enabled: false`
  (a disabled meal is kept in Home Assistant but never sent to the device).
- Plans are entered in Home Assistant's time zone. The device only understands **UTC** times and
  weekdays and does not follow DST, so the plan is converted and re-sent whenever the conversion
  changes (DST) and at least daily. If a plan is not confirmed the integration retries every minute
  (up to 24 times) and logs a warning.
- Check the first plan against the real feeder (and the app, if you still use it) before relying on it.

## Sharing port 1883

PetLibro devices always dial port 1883. If another device that is also DNS-redirected to Home
Assistant needs that port and speaks TLS (for example an add-on that impersonates a pump's cloud),
move that add-on off host port 1883 and set **Relay TLS connections to** in this integration's
options to its address, e.g. `local-my-addon:1883`. TLS connections are passed through untouched;
plain MQTT stays with PetLibro Local.

## Security

The broker listens on 0.0.0.0:1883 and accepts only private / link-local IPv4 peers, at most 16
connections, 64 KiB packets, a CONNECT within 10 s, idle clients are dropped at 1.5x their
keepalive, messages are rate limited, client ids must look like PetLibro serials, and a device can
publish and subscribe only under its own `dl/<model>/<serial>/` topics. Device credentials are not
checked (they are static values shared by the product line). Nothing is retained.

## Troubleshooting

Enable debug logging for `custom_components.petlibro_lan` to see raw device messages (truncated);
please include them in issues, especially for fields this integration does not know yet. Diagnostics
redact serials, MAC addresses and IPs.

## Development

```
PYTHONPATH=<dir with pytest> python3 -m pytest tests -q
```
Tests need no Home Assistant installation.

Websocket API (all under `petlibro_lan/`, writes need an administrator): `state`, `subscribe`, `get_schedule`,
`set_schedule`, `clear_schedule`, `get_alerts`, `set_alerts`, `notify_targets`, `test_notification`, `command`,
`dns_check`, `rename_device`, `remove_device`, `block_device`, `unblock_device`, `calibration_capture`,
`calibration_set`, `calibration_reset`, `zero_scale`.

## Credits

Protocol facts come from two MIT-licensed projects, whose code was not copied:
- [j4n-e4t/petlibro_local](https://github.com/j4n-e4t/petlibro_local) (c) 2026 j4n-e4t
- [smcneece/petlibro-local](https://github.com/smcneece/petlibro-local) (c) 2026 Shawn McNeece

MIT licensed, see `LICENSE`.
