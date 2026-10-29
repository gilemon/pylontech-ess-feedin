# pylontech-ess-feedin

Keep DC-coupled solar exporting to the grid on a Victron ESS when a Pylontech battery is full.

A small Python script for a Victron GX device (Venus OS). When a full Pylontech blocks charging, it lowers the ESS grid setpoint step by step until the MPPT runs at full power again, then hands back to normal ESS control at night or when the battery has room.

> Unofficial community script. Not affiliated with or supported by Victron Energy or Pylontech. Use at your own risk.

## The problem

On an ESS with DC-coupled PV (SmartSolar MPPT) and **DC-coupled PV – feed in excess** enabled, the MPPT should run at full power and the Multi/Quattro should export whatever the house and battery don't use.

With some Pylontech batteries this stops working once the battery reaches 100%:

- The BMS reports a charge current limit (CCL) of **0 A** and holds it there.
- The MPPT is held back (`MppOperationMode = 1`) to the AC loads only.
- The battery sits around 51.7–51.9 V, below the 52.5 V charge voltage the ESS needs to see before it exports DC solar.
- Result: no charging, no export, solar wasted. Grid stays at about the ESS setpoint.

Everything on the Victron side can look correct: feed-in enabled, MPPT in *Ext. control*, the +0.4 V solar offset applied, full current allowed to the MPPT, Multi set to feed in overvoltage. The only blocker is the battery's CCL = 0.

The well-known manual workaround is to set **ESS → Grid setpoint** to a negative value. That releases the solar, but a fixed value either wastes solar or drains the battery when the sun drops, and keeps exporting from the battery at night. This script automates the workaround.

### How to check whether you have this problem

On a sunny day with the battery at 100%, over SSH:

```sh
dbus -y com.victronenergy.battery.socketcan_can0 /Info/MaxChargeCurrent GetValue   # 0.0
dbus -y com.victronenergy.solarcharger.ttyO0 /MppOperationMode GetValue           # 1
dbus -y com.victronenergy.solarcharger.ttyO0 /Link/ChargeVoltage GetValue         # CVL + 0.4 V
```

If the battery reports 0 A, the MPPT is in mode 1 and grid is not going negative, this script is for you. If the MPPT is in mode 1 while the battery reports a normal CCL, the cause is something else.

## How it works

The script can't know how much solar is available: a held-back MPPT only produces what is used. So it searches, every 15 seconds, using two signals:

- **MPPT mode** (`/MppOperationMode`): 1 = held back, 2 = full power.
- **Battery current** (`/Dc/0/Current`): negative = the export is bigger than the solar and the battery covers the difference.

| Situation | Action |
|---|---|
| Battery discharging more than 3 A | Raise setpoint by the power coming out of the battery (at least 50 W) |
| MPPT held back (mode 1) | Lower setpoint by 100 W (export more) |
| Battery discharging 1–3 A | Raise setpoint by 50 W |
| Otherwise | No change |

It only takes over while the battery reports CCL < 1 A. It returns the setpoint to normal (50 W) when:

- there is no sun (PV below 20 W or MPPT off),
- the battery accepts charge again and SOC is below 95%,
- any value can't be read,
- the script is paused, stopped or hits an error.

Export is capped at 3000 W. The setpoint is only written when it changes.

## Requirements

- Victron GX device with root/SSH access ([how to enable](https://www.victronenergy.com/live/ccgx:root_access))
- ESS with a Multi/Quattro, DC-coupled MPPT, and **DC-coupled PV – feed in excess** on
- Pylontech battery connected by CAN-bus
- Single-phase system (the script reads `/Ac/Grid/L1` and writes the one ESS grid setpoint)

Tested on: Color Control GX (Venus OS v3.55), Quattro 48/8000/110-2x100 (VE.Bus v558), SmartSolar MPPT 150/85 rev2, 2 × Pylontech modules (200 Ah). Other setups may need the settings adjusted.

## Configuration

Edit the settings block at the top of `pv-unblock.py`:

| Setting | Default | Meaning |
|---|---|---|
| `BATTERY` | `com.victronenergy.battery.socketcan_can0` | Battery D-Bus service |
| `SOLAR` | `com.victronenergy.solarcharger.ttyO0` | MPPT D-Bus service |
| `NORMAL_SETPOINT` | `50` | Your usual ESS grid setpoint (W) |
| `MAX_EXPORT` | `3000` | Never export more than this (W) |
| `STEP_DOWN` | `100` | Export increase per step while the MPPT is held back (W) |
| `LEAVE_SOC` | `95` | Below this SOC, with charging allowed, return to normal (%) |
| `INTERVAL` | `15` | Seconds between checks |

Find your service names with:

```sh
dbus -y | grep -E 'battery|solarcharger'
```

Ramp-up from 50 W to full export takes a few minutes at the defaults. A larger `STEP_DOWN` or shorter `INTERVAL` is faster but overshoots more.

## Install

1. Copy the script to the GX device:

   ```sh
   scp pv-unblock.py root@<gx-ip>:/data/
   ```

2. Try it in the foreground first, on a sunny day with the battery full. Ctrl+C stops it and resets the setpoint.

   ```sh
   python3 /data/pv-unblock.py --dry-run   # log decisions, write nothing
   python3 /data/pv-unblock.py
   ```

3. Start it at every boot, and start it now:

   ```sh
   [ -f /data/rc.local ] || echo '#!/bin/sh' > /data/rc.local
   echo 'nohup python3 /data/pv-unblock.py >/dev/null 2>&1 &' >> /data/rc.local
   chmod +x /data/rc.local
   nohup python3 /data/pv-unblock.py >/dev/null 2>&1 &
   ```

Files in `/data` survive Venus OS firmware updates.

While the script runs it controls the ESS grid setpoint. Leave the setpoint in the ESS menu at `NORMAL_SETPOINT`.

## Usage

| Task | Command |
|---|---|
| Follow the log | `tail -f /tmp/pv-unblock.log` |
| Pause | `touch /data/pv-unblock.disable` |
| Resume | `rm /data/pv-unblock.disable` |
| Stop (resets the setpoint) | `pkill -f pv-unblock.py` |

## Uninstall

1. Delete the `pv-unblock.py` line from `/data/rc.local`.
2. `pkill -f pv-unblock.py`
3. `rm /data/pv-unblock.py`

## Notes and caveats

- The script never pushes charge into a battery that refuses it: it only changes how much is exported. Battery voltage and cell protection remain with the BMS and Venus OS DVCC.
- If you also set the grid setpoint from somewhere else (Node-RED, Modbus, VRM, Dynamic ESS), the two will fight. Use one or the other.
- Grid feed-in limits from your network operator still apply: keep **Limit system feed-in** set if you need it, and `MAX_EXPORT` below that limit.
- A better long-term fix would be on the battery side (a BMS that lowers its charge voltage limit instead of sending CCL = 0, as Victron's CAN-bus BMS spec expects) or in Venus OS itself.

## Background

- [MPPT charger in limiting mode / DVCC – Victron Community](https://community.victronenergy.com/t/mppt-charger-in-limiting-mode-dvcc-external-control/398) – Victron staff on BMSs sending CCL = 0
- [Smart Solar MPPT not feeding in when battery is full – Victron Community](https://community.victronenergy.com/t/smart-solar-mppt-rs-450-100-not-feed-in-grid-when-battery-is-full/5266)
- [Pylontech (15 cells) SoC behaviour – Victron Community archive](https://communityarchive.victronenergy.com/questions/309805/pylontech-15-cells-soc-behavior-and-overcharging-e.html)

## License

MIT
