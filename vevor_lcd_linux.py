#!/usr/bin/env python3
"""
vevor_lcd_linux.py
==================
Native Linux driver for the "HT / PC Monitor All V3" cooler LCD
(Vevor AIO with built-in display), reverse-engineered from the Windows
.NET app `PC Monitoring.exe` (CyUSB HID).

Device:   USB HID  VID 0x5131  PID 0x2007
Frame:    HID output report, 64 data bytes, sent every 200 ms.
          report = [reportID=0x00, 0x00, 0x01, 0x02, <33 value bytes>, 0x00...]
          shutdown frame = [reportID=0x00, 0x00, 0x0F, 0x00 ...]

The screen firmware does the rendering. We only push 33 decimal-split values.
See PROTOCOL.md for the full byte map.

Deps:
    pip install hidapi psutil pynvml
Run (needs HID access — see udev rule in PROTOCOL.md, or run with sudo):
    python3 vevor_lcd_linux.py
"""

import sys
import time
import glob
import signal
import warnings
from datetime import datetime

import hid
import psutil

# ---- optional NVIDIA GPU (lazy init — survives boot ordering) --------------
warnings.filterwarnings("ignore", category=FutureWarning)  # pynvml rename notice
try:
    import pynvml
    _HAS_PYNVML = True
except Exception:
    _HAS_PYNVML = False

VID = 0x5131
PID = 0x2007
REPORT_LEN = 64           # data bytes (excludes the leading report-ID byte)
REPORT_ID = 0x00
REFRESH_S = 0.2           # 200 ms, same as the Windows Thread_Send loop
USE_FAHRENHEIT = False    # array[2]/[12]: 0 = Celsius, 1 = Fahrenheit

# Optional: hwmon paths you can pin for pump RPM / CPU voltage if your board
# exposes them (find with: `sensors` and `ls /sys/class/hwmon/*/`).
PUMP_RPM_HWMON = None     # e.g. "/sys/class/hwmon/hwmon2/fan2_input"
CPU_VOLT_HWMON = None     # e.g. "/sys/class/hwmon/hwmon2/in0_input"  (millivolts)


# ---------------------------------------------------------------------------
# Sensor reading (Linux equivalents of the SendValue_* fields)
# ---------------------------------------------------------------------------
def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError, TypeError):
        return None


class Sensors:
    def __init__(self):
        self._rapl = "/sys/class/powercap/intel-rapl:0/energy_uj"
        self._e_prev = _read_int(self._rapl)
        self._t_prev = time.monotonic()
        self._gpu = None
        self._gpu_last_try = 0.0
        self._ensure_gpu()
        psutil.cpu_percent(interval=None)  # prime

    def _ensure_gpu(self):
        """(Re)initialize NVML if not ready. Retries every 5s so the GPU is
        picked up even when the driver loads after this service at boot."""
        if self._gpu is not None or not _HAS_PYNVML:
            return
        now = time.monotonic()
        if now - self._gpu_last_try < 5.0:
            return
        self._gpu_last_try = now
        try:
            pynvml.nvmlInit()
            self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self._gpu = None

    def cpu_temp(self):
        t = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower"):
            if t.get(key):
                pkg = next((s for s in t[key] if "Package" in (s.label or "")), None)
                return float((pkg or t[key][0]).current)
        return 0.0

    def cpu_usage(self):
        return psutil.cpu_percent(interval=None)

    def cpu_power(self):
        """Intel RAPL package power in watts (energy-counter delta)."""
        e_now = _read_int(self._rapl)
        t_now = time.monotonic()
        if e_now is None or self._e_prev is None:
            return 0.0
        dt = max(t_now - self._t_prev, 1e-3)
        de = e_now - self._e_prev          # microjoules
        if de < 0:                          # counter wraparound
            de = 0
        self._e_prev, self._t_prev = e_now, t_now
        return de / 1_000_000.0 / dt        # uJ -> J / s = W

    def cpu_freq(self):
        f = psutil.cpu_freq()
        return float(f.current) if f else 0.0

    def cpu_volt(self):
        mv = _read_int(CPU_VOLT_HWMON) if CPU_VOLT_HWMON else None
        return (mv / 1000.0) if mv else 0.0

    def gpu_temp(self):
        self._ensure_gpu()
        if not self._gpu:
            return 0.0
        try:
            return float(pynvml.nvmlDeviceGetTemperature(self._gpu, pynvml.NVML_TEMPERATURE_GPU))
        except Exception:
            self._gpu = None       # lost device -> retry next cycle
            return 0.0

    def gpu_usage(self):
        self._ensure_gpu()
        if not self._gpu:
            return 0.0
        try:
            return float(pynvml.nvmlDeviceGetUtilizationRates(self._gpu).gpu)
        except Exception:
            self._gpu = None
            return 0.0

    def gpu_power(self):
        self._ensure_gpu()
        if not self._gpu:
            return 0.0
        try:
            return pynvml.nvmlDeviceGetPowerUsage(self._gpu) / 1000.0
        except Exception:
            self._gpu = None
            return 0.0

    def gpu_freq(self):
        self._ensure_gpu()
        if not self._gpu:
            return 0.0
        try:
            return float(pynvml.nvmlDeviceGetClockInfo(self._gpu, pynvml.NVML_CLOCK_GRAPHICS))
        except Exception:
            self._gpu = None
            return 0.0

    def fan_rpm(self):
        fans = psutil.sensors_fans()
        for chip in fans.values():
            for f in chip:
                if f.current:
                    return float(f.current)
        return 0.0

    def pump_rpm(self):
        v = _read_int(PUMP_RPM_HWMON) if PUMP_RPM_HWMON else None
        return float(v) if v else 0.0

    def ram_usage(self):
        return int(psutil.virtual_memory().percent)


# ---------------------------------------------------------------------------
# Frame encoder — 1:1 with SendData2() / SendValueArray in the .NET app
# ---------------------------------------------------------------------------
def build_value_bytes(s: Sensors):
    v = [0] * 33

    def b(x):
        return max(0, min(255, int(x)))   # mirror the (byte) cast clamp

    cpu_t = s.cpu_temp()
    cpu_p = s.cpu_power()
    cpu_f = s.cpu_freq()
    cpu_v = s.cpu_volt()
    gpu_t = s.gpu_temp()
    gpu_p = s.gpu_power()
    gpu_f = s.gpu_freq()
    fan   = s.fan_rpm()
    pump  = s.pump_rpm()
    unit  = 1 if USE_FAHRENHEIT else 0

    # CPU temp (int + decimal*100 + unit flag)
    v[0] = b(cpu_t // 1)
    v[1] = b((cpu_t - (cpu_t // 1)) * 100)
    v[2] = unit
    # CPU usage
    v[3] = b(s.cpu_usage())
    # CPU power: [31]=hundreds, [4]=mod100, [5]=decimal*100
    cpu_p_i = cpu_p // 1
    v[4]  = b(cpu_p_i % 100)
    v[5]  = b((cpu_p - cpu_p_i) * 100)
    v[31] = b(cpu_p_i // 100)
    # CPU freq: [6]=/100, [7]=%100
    cpu_f_i = cpu_f // 1
    v[6] = b(cpu_f_i // 100)
    v[7] = b(cpu_f_i % 100)
    # CPU voltage (reproduces original's quirky encoding)
    cpu_v_i = cpu_v // 1
    v[8] = b(cpu_v_i)
    v[9] = b(cpu_v_i * 100)
    # GPU temp
    v[10] = b(gpu_t // 1)
    v[11] = b((gpu_t - (gpu_t // 1)) * 100)
    v[12] = unit
    # GPU usage
    v[13] = b(s.gpu_usage())
    # GPU power: [32]=hundreds, [14]=mod100, [15]=decimal*100
    gpu_p_i = gpu_p // 1
    v[14] = b(gpu_p_i % 100)
    v[15] = b((gpu_p - gpu_p_i) * 100)
    v[32] = b(gpu_p_i // 100)
    # GPU freq
    gpu_f_i = gpu_f // 1
    v[16] = b(gpu_f_i // 100)
    v[17] = b(gpu_f_i % 100)
    # Fan RPM
    fan_i = fan // 1
    v[18] = b(fan_i // 100)
    v[19] = b(fan_i % 100)
    # Pump RPM
    pump_i = pump // 1
    v[20] = b(pump_i // 100)
    v[21] = b(pump_i % 100)
    # Date / time
    now = datetime.now()
    yr = f"{now.year:04d}"
    v[22] = b(int(yr[:2]))
    v[23] = b(int(yr[2:]))
    v[24] = now.month
    v[25] = now.day
    v[26] = now.hour
    v[27] = now.minute
    v[28] = now.second
    v[29] = now.isoweekday() % 7   # .NET DayOfWeek: Sunday=0
    v[30] = b(s.ram_usage())
    return v


def build_frame(value_bytes):
    """Full 65-byte buffer for hidapi: [reportID, 0x00,0x01,0x02, 33 vals, pad]."""
    data = [0x00, 0x01, 0x02] + value_bytes      # array[0..35]
    data += [0x00] * (REPORT_LEN - len(data))    # pad to 64
    return bytes([REPORT_ID]) + bytes(data[:REPORT_LEN])


def build_shutdown_frame():
    data = [0x00, 0x0F] + [0x00] * (REPORT_LEN - 2)
    return bytes([REPORT_ID]) + bytes(data[:REPORT_LEN])


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def open_device():
    try:
        d = hid.device()
        d.open(VID, PID)
        d.set_nonblocking(1)
        return d
    except Exception as e:
        print(f"[!] Cannot open HID {VID:#06x}:{PID:#06x}: {e}", file=sys.stderr)
        print("    Check `lsusb`, and the udev rule in PROTOCOL.md (or run as root).",
              file=sys.stderr)
        return None


def main():
    debug = "--print" in sys.argv
    print(f"Opening cooler LCD {VID:#06x}:{PID:#06x} ...")
    dev = open_device()
    if not dev:
        sys.exit(1)
    print("Connected. Streaming sensors (Ctrl-C to stop).")

    sensors = Sensors()
    running = {"on": True}

    def stop(*_):
        running["on"] = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while running["on"]:
            try:
                vals = build_value_bytes(sensors)
                dev.write(build_frame(vals))
                if debug:
                    print(f"CPU {vals[0]}.{vals[1]:02d}C {vals[3]}%  "
                          f"{vals[31]*100+vals[4]}.{vals[5]:02d}W  "
                          f"{vals[6]*100+vals[7]}MHz | "
                          f"GPU {vals[10]}.{vals[11]:02d}C {vals[13]}%  "
                          f"{vals[32]*100+vals[14]}W  {vals[16]*100+vals[17]}MHz | "
                          f"fan {vals[18]*100+vals[19]} | RAM {vals[30]}%",
                          end="\r", flush=True)
            except OSError:
                # device unplugged -> try to reconnect
                print("[!] Write failed, reconnecting...", file=sys.stderr)
                dev.close()
                time.sleep(1.0)
                dev = open_device()
                if not dev:
                    time.sleep(2.0)
                    dev = open_device()
                    if not dev:
                        break
                continue
            time.sleep(REFRESH_S)
    finally:
        try:
            dev.write(build_shutdown_frame())
            dev.close()
        except Exception:
            pass
        print("\nStopped, screen released.")


if __name__ == "__main__":
    main()
