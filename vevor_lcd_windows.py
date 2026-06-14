#!/usr/bin/env python3
"""
vevor_lcd_windows.py
====================
Native **Windows, headless** driver for the HT / Vevor cooler LCD
(USB HID  VID 0x5131  PID 0x2007), ported 1:1 from vevor_lcd_linux.py.

The wire protocol is IDENTICAL to Linux (see PROTOCOL.md): HID output report,
64 data bytes, sent every 200 ms. Only the *sensor sources* change — Windows
has no lm-sensors / RAPL / hwmon, so:

    CPU temp / power / voltage / clock  ->  LibreHardwareMonitor (LHM)
    Fan RPM / Pump RPM                  ->  LibreHardwareMonitor (LHM)
    CPU load / RAM                      ->  psutil   (cross-platform)
    GPU temp/usage/power/clock          ->  pynvml   (cross-platform)
    HID frame build + write loop        ->  UNCHANGED from the Linux driver

Designed to run as a Windows **service** (via NSSM, LocalSystem) so it starts
at boot, BEFORE login — the exact equivalent of the systemd unit on Linux.

Dependencies:
    pip install hidapi psutil pynvml pythonnet
Native DLLs (place next to this file, or bundle into the PyInstaller exe):
    LibreHardwareMonitorLib.dll
    HidSharp.dll
(both taken from any LibreHardwareMonitor release)

Usage:
    python vevor_lcd_windows.py                 # run the sensor feed
    python vevor_lcd_windows.py --list-sensors  # dump EVERY sensor LHM sees
    python vevor_lcd_windows.py --print         # run + live console line

FIRST STEP: run `--list-sensors`, then adjust the HINTS dict below to match
your board's exact labels (CPU temp, CPU fan, AIO pump).
"""

import os
import sys
import time
import signal
import warnings
from datetime import datetime

import hid
import psutil

warnings.filterwarnings("ignore", category=FutureWarning)  # pynvml rename notice
try:
    import pynvml
    _HAS_PYNVML = True
except Exception:
    _HAS_PYNVML = False


# ---------------------------------------------------------------------------
# pythonnet + LibreHardwareMonitor bootstrap
# ---------------------------------------------------------------------------
def _dll_dir():
    """Where the native DLLs live: PyInstaller temp (_MEIPASS) or this dir."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


import clr  # pythonnet
sys.path.append(_dll_dir())
clr.AddReference("LibreHardwareMonitorLib")
from LibreHardwareMonitor.Hardware import Computer  # noqa: E402


# ---------------------------------------------------------------------------
# Config (same knobs as the Linux driver)
# ---------------------------------------------------------------------------
VID = 0x5131
PID = 0x2007
REPORT_LEN = 64           # data bytes (excludes leading report-ID byte)
REPORT_ID = 0x00
REFRESH_S = 0.2           # 200 ms — matches the Linux driver / vendor app; the LCD blanks if frames arrive slower than ~300 ms
USE_FAHRENHEIT = False    # array[2]/[12]: 0 = Celsius, 1 = Fahrenheit

# Sensor name hints (case-insensitive substring; first match wins, else the
# first sensor of that type). Tuned for ASUS Z390-A + i7-9700K + AIO Vevor
# (confirmed via --list-sensors on this exact hardware).
HINTS = {
    "cpu_temp":  ["cpu package"],          # CPU Package (Cpu hw)         e.g. 44.0 C
    "cpu_power": ["cpu package"],          # CPU Package (Cpu hw, Power)  e.g. 18.7 W
    "cpu_volt":  ["cpu core"],             # CPU Core (aggregate VID)     e.g. 1.22 V
    "cpu_freq":  ["cpu core #1"],          # CPU Core #1 clock            e.g. 4500 MHz
    "fan_rpm":   ["fan #2"],               # SuperIO Fan #2 = CPU/rad fan ~870 RPM
    "pump_rpm":  ["fan #7"],               # SuperIO Fan #7 = AIO pump   ~4820 RPM
}
_FAN_HW = {"SuperIO", "EmbeddedController", "Motherboard", "Cooler"}
_CPU_HW = {"Cpu"}


# ---------------------------------------------------------------------------
# LibreHardwareMonitor wrapper: open once, refresh + snapshot each cycle
# ---------------------------------------------------------------------------
class _LHM:
    def __init__(self):
        self.c = Computer()
        self.c.IsCpuEnabled = True
        self.c.IsMotherboardEnabled = True
        self.c.IsControllerEnabled = True
        self.c.IsMemoryEnabled = False
        self.c.IsGpuEnabled = False        # GPU handled by pynvml
        self.c.Open()
        self.snap = []                     # (hwType, sensorType, name, value)

    def refresh(self):
        snap = []
        for hw in self.c.Hardware:
            hw.Update()
            for s in hw.Sensors:
                snap.append((str(hw.HardwareType), str(s.SensorType), s.Name, s.Value))
            for sub in hw.SubHardware:      # SuperIO fans live here
                sub.Update()
                for s in sub.Sensors:
                    snap.append((str(sub.HardwareType), str(s.SensorType), s.Name, s.Value))
        self.snap = snap

    def find(self, sensor_type, hints, hw_types=None):
        cands = [r for r in self.snap if r[1] == sensor_type and r[3] is not None]
        if hw_types:
            cands = [r for r in cands if r[0] in hw_types]
        for h in hints:                     # name-hint match first
            for r in cands:
                if h in r[2].lower():
                    return float(r[3])
        return float(cands[0][3]) if cands else 0.0   # fallback: first of type

    def close(self):
        try:
            self.c.Close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sensors — Windows sources, same method names as the Linux driver
# ---------------------------------------------------------------------------
class Sensors:
    def __init__(self):
        self.lhm = _LHM()
        self._gpu = None
        self._gpu_last_try = 0.0
        self._ensure_gpu()
        psutil.cpu_percent(interval=None)   # prime
        self._ema = {}                      # smoothing memory for jittery MSR values
        self.refresh()

    def refresh(self):
        """Pull one fresh snapshot from LHM (called once per frame)."""
        self.lhm.refresh()

    def _smooth(self, key, value, alpha=0.15):
        """EMA: alpha=0.15 with 200ms refresh -> ~1.3s effective time constant.
        Calms SpeedShift jitter on Intel without making the display sluggish."""
        prev = self._ema.get(key, value)
        cur = alpha * value + (1.0 - alpha) * prev
        self._ema[key] = cur
        return cur

    # ---- CPU + board via LibreHardwareMonitor (EMA-smoothed) ----
    def cpu_temp(self):
        return self._smooth("cpu_temp", self.lhm.find("Temperature", HINTS["cpu_temp"], _CPU_HW))

    def cpu_power(self):
        return self._smooth("cpu_power", self.lhm.find("Power", HINTS["cpu_power"], _CPU_HW))

    def cpu_volt(self):
        return self._smooth("cpu_volt", self.lhm.find("Voltage", HINTS["cpu_volt"], _CPU_HW))

    def cpu_freq(self):
        return self._smooth("cpu_freq", self.lhm.find("Clock", HINTS["cpu_freq"], _CPU_HW))

    def fan_rpm(self):
        return self.lhm.find("Fan", HINTS["fan_rpm"], _FAN_HW)

    def pump_rpm(self):
        return self.lhm.find("Fan", HINTS["pump_rpm"], _FAN_HW)

    # ---- CPU load + RAM via psutil (cross-platform) ----
    def cpu_usage(self):
        return psutil.cpu_percent(interval=None)

    def ram_usage(self):
        return int(psutil.virtual_memory().percent)

    # ---- GPU via pynvml (identical to the Linux driver) ----
    def _ensure_gpu(self):
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

    def gpu_temp(self):
        self._ensure_gpu()
        if not self._gpu:
            return 0.0
        try:
            return float(pynvml.nvmlDeviceGetTemperature(self._gpu, pynvml.NVML_TEMPERATURE_GPU))
        except Exception:
            self._gpu = None
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


# ---------------------------------------------------------------------------
# Frame encoder — UNCHANGED from vevor_lcd_linux.py (same byte map)
# ---------------------------------------------------------------------------
def build_value_bytes(s: Sensors):
    s.refresh()                            # one LHM update per frame
    v = [0] * 33

    def b(x):
        return max(0, min(255, int(x)))    # mirror the (byte) cast clamp

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

    # CPU temp
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
    v[29] = now.isoweekday() % 7           # .NET DayOfWeek: Sunday=0
    v[30] = b(s.ram_usage())
    return v


def build_frame(value_bytes):
    """Full 65-byte buffer for hidapi: [reportID, 0x00,0x01,0x02, 33 vals, pad]."""
    data = [0x00, 0x01, 0x02] + value_bytes
    data += [0x00] * (REPORT_LEN - len(data))
    return bytes([REPORT_ID]) + bytes(data[:REPORT_LEN])


def build_shutdown_frame():
    data = [0x00, 0x0F] + [0x00] * (REPORT_LEN - 2)
    return bytes([REPORT_ID]) + bytes(data[:REPORT_LEN])


# ---------------------------------------------------------------------------
# Device I/O — same as Linux
# ---------------------------------------------------------------------------
def open_device():
    try:
        d = hid.device()
        d.open(VID, PID)
        d.set_nonblocking(1)
        return d
    except Exception as e:
        print(f"[!] Cannot open HID {VID:#06x}:{PID:#06x}: {e}", file=sys.stderr)
        print("    Check the device is plugged in and not claimed by the vendor app.",
              file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# --list-sensors : dump everything LHM sees (run this FIRST)
# ---------------------------------------------------------------------------
def list_sensors():
    lhm = _LHM()
    lhm.refresh()
    print(f"{'HARDWARE':<20} {'TYPE':<13} {'NAME':<30} VALUE")
    print("-" * 78)
    for hwt, st, name, val in lhm.snap:
        vs = "—" if val is None else f"{float(val):.2f}"
        print(f"{hwt:<20} {st:<13} {name:<30} {vs}")
    lhm.close()
    print("\nTip: note the exact NAME for CPU temp, CPU fan and the AIO pump,")
    print("then set them in the HINTS dict at the top of this file.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    debug = "--print" in sys.argv
    print(f"Opening cooler LCD {VID:#06x}:{PID:#06x} ...")
    dev = open_device()
    if not dev:
        sys.exit(1)
    print("Connected. Streaming sensors (Ctrl-C / service stop to quit).")

    sensors = Sensors()
    running = {"on": True}

    def stop(*_):
        running["on"] = False
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):        # Windows: NSSM console-stop sends this
        signal.signal(signal.SIGBREAK, stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, stop)
        except Exception:
            pass

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
                          f"fan {vals[18]*100+vals[19]} pump {vals[20]*100+vals[21]} | "
                          f"RAM {vals[30]}%",
                          end="\r", flush=True)
            except OSError:
                print("[!] Write failed, reconnecting...", file=sys.stderr)
                try:
                    dev.close()
                except Exception:
                    pass
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
        try:
            sensors.lhm.close()
        except Exception:
            pass
        print("\nStopped, screen released.")


if __name__ == "__main__":
    if "--list-sensors" in sys.argv:
        list_sensors()
    else:
        main()
