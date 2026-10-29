#!/usr/bin/env python3
"""
pv-unblock.py - keep DC-coupled solar exporting when the Pylontech is full.

When the Pylontech reaches 100% it reports a charge current limit of 0 A.
The ESS then holds the MPPT to the house load and exports nothing.
Lowering the ESS grid setpoint releases the solar; this script does that
automatically and puts the setpoint back when it is no longer needed.

  Run in foreground:    python3 /data/pv-unblock.py              (Ctrl+C stops)
  Test without acting:  python3 /data/pv-unblock.py --dry-run
  Pause:                touch /data/pv-unblock.disable           (rm it to resume)
  Log:                  /tmp/pv-unblock.log      (tail -f to follow it)
  Stop:                 pkill -f pv-unblock.py   (sets the setpoint back)

Start at every boot, and start it now:
  [ -f /data/rc.local ] || echo '#!/bin/sh' > /data/rc.local
  echo 'nohup python3 /data/pv-unblock.py >/dev/null 2>&1 &' >> /data/rc.local
  chmod +x /data/rc.local
  nohup python3 /data/pv-unblock.py >/dev/null 2>&1 &

  To remove it: delete that line from /data/rc.local, then pkill -f pv-unblock.py

While the script runs it owns the ESS grid setpoint: leave it at
NORMAL_SETPOINT in the ESS menu.
"""

import logging
import logging.handlers
import os
import signal
import sys
import time

import dbus

# ---------------------------------------------------------------- settings
BATTERY = 'com.victronenergy.battery.socketcan_can0'
SOLAR = 'com.victronenergy.solarcharger.ttyO0'
SETTINGS = 'com.victronenergy.settings'
SETPOINT_PATH = '/Settings/CGwacs/AcPowerSetPoint'

NORMAL_SETPOINT = 50    # W  your usual ESS grid setpoint
MAX_EXPORT = 3000       # W  never request more export than this
STEP_DOWN = 100         # W  export this much more while the MPPT is held back
TRIM_UP = 50            # W  small correction when the battery discharges a little
DISCHARGE_SMALL = 1.0   # A  battery discharge that triggers the small correction
DISCHARGE_TOL = 3.0     # A  above this, correct at once by the full amount
LEAVE_SOC = 95          # %  below this (and battery accepting charge) -> normal
MIN_PV = 20             # W  below this it is considered night
INTERVAL = 15           # s  between checks

DISABLE_FILE = '/data/pv-unblock.disable'
LOG_FILE = '/tmp/pv-unblock.log'
# --------------------------------------------------------------------------

DRY_RUN = '--dry-run' in sys.argv
BUSITEM = 'com.victronenergy.BusItem'

log = logging.getLogger('pv-unblock')
log.setLevel(logging.INFO)
_fmt = logging.Formatter('%(asctime)s %(message)s', '%Y-%m-%d %H:%M:%S')
for _h in (logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=200000, backupCount=1),
           logging.StreamHandler(sys.stdout)):
    _h.setFormatter(_fmt)
    log.addHandler(_h)

bus = dbus.SystemBus()
setpoint = None  # last value this script wrote


def read(service, path):
    """Return a D-Bus value as float, or None if missing or invalid."""
    try:
        value = bus.get_object(service, path, introspect=False).GetValue(
            dbus_interface=BUSITEM)
        return float(value)
    except Exception:
        return None


def write_setpoint(watts, reason):
    """Write the ESS grid setpoint, only when it actually changes."""
    global setpoint
    watts = int(max(-MAX_EXPORT, min(NORMAL_SETPOINT, watts)))
    if watts == setpoint:
        return
    log.info('grid setpoint %s -> %d W (%s)%s', setpoint, watts, reason,
             ' [dry run, not written]' if DRY_RUN else '')
    if not DRY_RUN:
        bus.get_object(SETTINGS, SETPOINT_PATH, introspect=False).SetValue(
            dbus.Int32(watts), dbus_interface=BUSITEM)
    setpoint = watts


def check(active):
    """One control step. Returns whether export mode is active."""
    if os.path.exists(DISABLE_FILE):
        write_setpoint(NORMAL_SETPOINT, 'paused')
        return False

    ccl = read(BATTERY, '/Info/MaxChargeCurrent')
    soc = read(BATTERY, '/Soc')
    ibat = read(BATTERY, '/Dc/0/Current')      # + charging, - discharging
    vbat = read(BATTERY, '/Dc/0/Voltage')
    pv = read(SOLAR, '/Yield/Power')
    mode = read(SOLAR, '/MppOperationMode')    # 0 off, 1 held back, 2 full power

    if None in (ccl, soc, ibat, vbat, pv, mode):
        write_setpoint(NORMAL_SETPOINT, 'missing data')
        return False

    if mode == 0 or pv < MIN_PV:
        write_setpoint(NORMAL_SETPOINT, 'no sun')
        return False

    battery_blocks = ccl < 1
    if not active:
        if not battery_blocks:
            return False
        log.info('battery reports %.0f A (SOC %.0f%%), PV %.0f W: start exporting',
                 ccl, soc, pv)
        active = True
    elif not battery_blocks and soc < LEAVE_SOC:
        write_setpoint(NORMAL_SETPOINT, 'battery has room again (SOC %.0f%%)' % soc)
        return False

    if ibat < -DISCHARGE_TOL:
        # Exporting more than the panels give: give it back at once.
        write_setpoint(setpoint + max(TRIM_UP, int(-ibat * vbat)),
                       'battery %.1f A' % ibat)
    elif mode == 1:
        # MPPT still held back: export more.
        write_setpoint(setpoint - STEP_DOWN, 'PV held back at %.0f W' % pv)
    elif ibat < -DISCHARGE_SMALL:
        # Slight drain on the battery: trim the export a little.
        write_setpoint(setpoint + TRIM_UP, 'battery %.1f A' % ibat)
    return True


def stop(*_):
    try:
        write_setpoint(NORMAL_SETPOINT, 'script stopped')
    finally:
        sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info('pv-unblock started%s', ' (dry run)' if DRY_RUN else '')
    write_setpoint(NORMAL_SETPOINT, 'start')
    active = False
    while True:
        time.sleep(INTERVAL)
        try:
            active = check(active)
        except Exception as e:
            log.info('error: %s', e)
            active = False
            try:
                write_setpoint(NORMAL_SETPOINT, 'error')
            except Exception:
                pass


if __name__ == '__main__':
    main()
