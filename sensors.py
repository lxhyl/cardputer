"""Single-owner service for the Grove I2C bus + ENV III sensors.

Why this exists: the Grove SoftI2C bus is a single physical resource
(SDA=GPIO 2, SCL=GPIO 1). MicroPython's GIL releases between bytecodes
during bit-banged SoftI2C transactions, so two threads each holding
their own SoftI2C instance on the same pins will race mid-transaction
and both see CRC errors. The fix is structural: exactly one owner of
the bus, everyone else reads a cached snapshot.

This module IS that owner. The launcher boots it before env_daemon and
before any app runs. Consumers (env_daemon's HTTP poster, the env
foreground app, any future weather/dashboard widget) only ever call
`latest()` — they never touch SoftI2C / SHT30 / QMP6988 / SCD40 again.

Hot-plug: if the ENV III is unplugged the read raises and we drop the
sensor handle. The next loop iteration re-inits, which succeeds the
moment the unit is plugged back in — no launcher restart needed.

Partial population: SHT30, QMP6988, SCD40 init independently. If one is
absent (e.g. a unit without SCD40) the others still publish; the
missing one stays None in the snapshot.

Threading: a single background thread owns the sensor handles + writes
the snapshot under a `_thread.allocate_lock()`. `latest()` returns a
shallow copy so consumers can never observe a torn mid-update read.
"""

import time

try:
    import _thread
except ImportError:
    _thread = None


_READ_PERIOD_MS = 500    # 2 Hz — fine-grained enough for the env app's
                         # live UI, light enough on a battery-powered MCU.

# Lazy-imported in _init_sensors to avoid pulling SoftI2C / drivers
# into the import graph of any module that just wants to call state().

_state = "off"           # "off" | "init" | "ok" | "err"
_last_err = ""
_started = False
_lock = None
_snapshot = None         # dict | None — None until first successful read
                         # of ANY sensor


def _set_state(new_state, err=""):
    global _state, _last_err
    if _lock is not None:
        _lock.acquire()
    try:
        _state = new_state
        if err:
            _last_err = err[:80]
    finally:
        if _lock is not None:
            _lock.release()


def state():
    return _state


def last_error():
    return _last_err


def latest():
    """Return a shallow copy of the most recent snapshot, or None if no
    sensor has produced a reading yet. Safe to call from any thread."""
    if _lock is not None:
        _lock.acquire()
    try:
        if _snapshot is None:
            return None
        return dict(_snapshot)
    finally:
        if _lock is not None:
            _lock.release()


def _publish(snapshot):
    """Write a fresh snapshot under the lock so latest() can't tear."""
    global _snapshot
    if _lock is not None:
        _lock.acquire()
    try:
        _snapshot = snapshot
    finally:
        if _lock is not None:
            _lock.release()


def _make_bus():
    """Create a Grove SoftI2C handle. Caller owns it for the lifetime of
    the sensor handles initialized from it."""
    from machine import SoftI2C, Pin
    return SoftI2C(sda=Pin(2), scl=Pin(1), freq=100_000)


def _try_init_sht30(bus):
    """Init AND probe so a dead bus actually returns None.

    SHT30's class __init__ does no I/O — it just stashes the bus +
    address. Without a probe we'd return a "successfully initialized"
    handle that fails on every subsequent read, never letting the loop
    detect that the bus itself is wedged. Doing one real read at init
    time costs ~22 ms but means subsequent loop logic can trust that a
    non-None handle is actually alive."""
    try:
        from sht30 import SHT30
        s = SHT30(bus)
        s.read()
        return s
    except Exception:
        return None


def _try_init_qmp(bus):
    # QMP6988 already does a chip-id read in __init__, so a stuck bus
    # naturally produces an OSError there — no extra probe needed.
    try:
        from qmp6988 import QMP6988
        return QMP6988(bus)
    except Exception:
        return None


def _try_init_scd40(bus):
    """SCD40 ctor swallows OSError from STOP_PERIODIC — necessary for the
    common "already in periodic mode" case but it also masks a dead bus.
    Probe with data_ready() (cheap, doesn't consume a measurement) so we
    know the bus actually transports bytes before declaring success."""
    try:
        from scd40 import SCD40
        co2 = SCD40(bus)
        try:
            co2.start_periodic()
        except OSError:
            pass
        co2.data_ready()
        return co2
    except Exception:
        return None


def _bus_recovery():
    """Standard I2C bus recovery: toggle SCL up to 9 times with SDA
    released, then generate a STOP. Frees a slave that's holding SDA low
    waiting for the next clock pulse — happens when a previous I2C
    transaction was interrupted mid-byte (e.g., by a soft reset). Without
    this, a freshly-created SoftI2C would keep failing because the bus
    state on the wire is stuck even though the master Python object is
    new."""
    from machine import Pin
    try:
        sda = Pin(2, Pin.IN, Pin.PULL_UP)
        scl = Pin(1, Pin.OUT, value=1)
        for _ in range(9):
            scl.value(0)
            time.sleep_us(5)
            scl.value(1)
            time.sleep_us(5)
            if sda.value():
                break
        # Generate STOP: SDA low → high while SCL is high.
        sda = Pin(2, Pin.OUT, value=0)
        time.sleep_us(5)
        scl.value(1)
        time.sleep_us(5)
        sda.value(1)
        time.sleep_us(5)
    except Exception:
        pass


_CONSECUTIVE_READ_FAIL_LIMIT = 3
"""How many consecutive cycles of all-sensors-failed reads before we
forcibly drop and recreate the bus (with a recovery sequence). Belt and
suspenders: the per-sensor init probes already detect dead-bus on the
new-handle path, but if a previously-working bus goes south at runtime
(e.g., transient EMI, a stuck slave) we'd otherwise re-init each sensor
on the same broken bus forever."""


def _loop():
    """Single-owner read loop. Holds the I2C bus + sensor handles for
    the lifetime of the process; re-inits a handle on read failure, and
    fully recreates the bus (with recovery sequence) when reads have
    been failing for too long. Never returns."""
    _set_state("init")

    bus = None
    sht = None
    baro = None
    co2 = None
    last_co2 = None
    consecutive_fail_cycles = 0

    while True:
        if bus is None:
            # Run bus recovery before recreating SoftI2C — clocks out any
            # stuck slave from the previous bus state. Cheap (~100 µs).
            _bus_recovery()
            try:
                bus = _make_bus()
            except Exception as e:
                _set_state("err", "bus init: " + repr(e)[:60])
                time.sleep(2)
                continue

        if sht is None:
            sht = _try_init_sht30(bus)
        if baro is None:
            baro = _try_init_qmp(bus)
        if co2 is None:
            co2 = _try_init_scd40(bus)

        # Partial population is the steady state — a unit with only
        # SHT30+QMP6988 (no SCD40) should publish T/H/P with co2=None,
        # not stall. We always publish below; "all None" is detected by
        # the consecutive_fail_cycles failsafe further down, which
        # handles "bus actually wedged" without starving consumers.

        errors = []
        t = h = None
        if sht is not None:
            try:
                t, h = sht.read()
            except Exception as e:
                errors.append("sht30: " + repr(e)[:40])
                sht = None    # re-init next cycle (covers CRC + unplug)

        p_pa = None
        if baro is not None:
            try:
                _bt, p_pa = baro.read()
            except Exception as e:
                errors.append("qmp6988: " + repr(e)[:40])
                baro = None

        if co2 is not None:
            try:
                if co2.data_ready():
                    ppm, _tc, _rhc = co2.read()
                    last_co2 = ppm
            except Exception as e:
                errors.append("scd40: " + repr(e)[:40])
                co2 = None
                # Don't blank last_co2 — keep the most recent good value
                # visible while the sensor is being re-initialized.

        snapshot = {
            "ts_ms": time.ticks_ms(),
            "temp_c": t,
            "humidity": h,
            "pressure_pa": p_pa,
            "co2_ppm": last_co2,
            "errors": errors,
        }
        _publish(snapshot)

        # State: "ok" only if at least one live sensor produced fresh
        # data this cycle. Cached `last_co2` doesn't count — we want the
        # state to flip "err" if the bus is wedged even though stale CO2
        # is still being shown to consumers.
        live_data_this_cycle = (t is not None or h is not None
                                or p_pa is not None
                                or (co2 is not None and not any(
                                    e.startswith("scd40") for e in errors)))
        if live_data_this_cycle:
            _set_state("ok")
            consecutive_fail_cycles = 0
        else:
            _set_state("err", "; ".join(errors) if errors else "no data")
            consecutive_fail_cycles += 1

        # Failsafe: a long run of all-fail cycles means the bus itself
        # is wedged. Drop it (and all sensor handles) so the next
        # iteration recreates with a recovery sequence. Without this,
        # we'd keep re-initing handles against a dead bus forever
        # because SHT30/SCD40 init don't actually probe — see the
        # comments on _try_init_sht30 / _try_init_scd40.
        if consecutive_fail_cycles >= _CONSECUTIVE_READ_FAIL_LIMIT:
            bus = None
            sht = baro = co2 = None
            consecutive_fail_cycles = 0

        time.sleep_ms(_READ_PERIOD_MS)


def start():
    """Idempotent. Spawns the owner thread; later calls are no-ops."""
    global _started, _lock
    if _started:
        return
    if _thread is None:
        _set_state("off")
        return
    if _lock is None:
        try:
            _lock = _thread.allocate_lock()
        except Exception:
            _lock = None
    _started = True
    try:
        _thread.start_new_thread(_loop, ())
    except Exception as e:
        _started = False
        _set_state("err", "thread start: " + repr(e))
