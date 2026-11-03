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
Water weight, Water level, Water low, Filter remaining, Reset filter, Zero scale (disabled by
default; remove the reservoir first). Every device also has Connected and an IP address
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

## Feeding plans

- The device keeps its own schedule and runs it by itself. This integration **never overwrites it
  unless you call `set_feeding_plan`**. When a device asks for its plan at boot and you have not
  configured one here, it is answered with a plain acknowledgement (no `plans` key), which stops the
  retries and leaves the stored schedule alone. Passing an empty list to the service is an explicit
  request to clear the device's schedule.
- Plans are entered in Home Assistant's time zone. The device only understands **UTC** times and
  weekdays and does not follow DST, so the plan is converted and re-sent whenever the conversion
  changes (DST) and at least daily. If a plan is not confirmed the integration retries every minute
  (up to 24 times) and logs a warning.
- Check the first plan against the real feeder (and the app, if you still use it) before relying on it.

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

## Credits

Protocol facts come from two MIT-licensed projects, whose code was not copied:
- [j4n-e4t/petlibro_local](https://github.com/j4n-e4t/petlibro_local) (c) 2026 j4n-e4t
- [smcneece/petlibro-local](https://github.com/smcneece/petlibro-local) (c) 2026 Shawn McNeece

MIT licensed, see `LICENSE`.
