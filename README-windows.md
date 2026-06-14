# CoolerLCD — Windows headless service

Windows port of `vevor_lcd_linux.py`. **Same HID protocol, same byte map** — only the
sensor sources change (LibreHardwareMonitor + PawnIO instead of lm-sensors/RAPL/hwmon).
Runs as a Windows **service** so it starts at boot, **before login**, just like the
systemd unit on Linux. No vendor app required, no GUI dependency at runtime.

## What's identical to the Linux driver

`build_value_bytes`, `build_frame`, `build_shutdown_frame`, the 200 ms write loop,
the reconnect logic, and even the voltage encoding quirk — all 1:1. Only the
`Sensors` class differs.

| Sensor | Linux source | Windows source |
|---|---|---|
| HID I/O | `hidapi` | `hidapi` (identical) |
| GPU temp/usage/power/clock | `pynvml` | `pynvml` (identical) |
| CPU usage / RAM | `psutil` | `psutil` (identical) |
| **CPU temp/power/voltage/clock** | psutil/RAPL/hwmon | **LibreHardwareMonitor + PawnIO** |
| **Fan + Pump RPM** | psutil/hwmon | **LibreHardwareMonitor + SuperIO** |
| Boot autostart | systemd user unit | **NSSM service (LocalSystem, auto-start)** |

---

## Requirements

- Windows 10 or 11 (64-bit)
- Python 3.10+ (any of: Microsoft Store, python.org, conda — pick one)
- Admin rights — PawnIO/SuperIO access **requires** elevation
- **Vendor app must be uninstalled** (PC Monitor All / PC Monitoring) — it holds the
  HID handle and will fight the driver, making the LCD blink

---

## 1. Install Python dependencies

```powershell
pip install hidapi psutil pynvml pythonnet
```

If `pip` warns about user-site install, that's fine; if it warns the Scripts dir
isn't on PATH, run PyInstaller later via `python -m PyInstaller`.

---

## 2. Install LibreHardwareMonitor + PawnIO driver

**This step matters.** LHM v0.9.6+ uses a signed kernel driver called **PawnIO** to
read CPU MSRs and motherboard SuperIO chips. Without it, all CPU temps/clocks and
motherboard fans come back as `None` / 0.

1. Download the latest release ZIP from
   <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest>
   — pick **`LibreHardwareMonitor.zip`** (the .NET Framework build, ~6 MB), **not**
   `LibreHardwareMonitor.NET.10.zip` unless you have .NET 10 runtime installed.
2. Extract the zip somewhere (e.g. into a `lhm/` folder next to this script).
3. **Run `LibreHardwareMonitor.exe` once as Administrator.** It will prompt to
   install PawnIO; accept. After this, you don't need the GUI again — keep it
   closed or uninstall it (it competes for some HID devices and can cause issues).
4. Copy **all `*.dll`** from the LHM root folder next to `vevor_lcd_windows.py`
   (there are roughly 27 of them — `LibreHardwareMonitorLib.dll`, `HidSharp.dll`,
   and a stack of .NET deps like `System.Text.Json.dll`, `System.Buffers.dll`, etc.).
   Don't skip any; PythonNet loads `LibreHardwareMonitorLib.dll` which transitively
   pulls in the others.

```powershell
# Example (from the script's folder):
Copy-Item lhm\*.dll .
```

---

## 3. Map your sensors to your motherboard

Fan/pump labels differ per board. Run the dump (Administrator PowerShell):

```powershell
python vevor_lcd_windows.py --list-sensors
```

You'll get a table of every sensor LHM detects. Note the **exact NAME** for:
- CPU temperature (typically `CPU Package`)
- CPU fan / radiator fan
- AIO pump (usually the only fan running ~4000-5500 RPM constant)

Then edit the `HINTS` dict at the top of `vevor_lcd_windows.py`. The hints are
case-insensitive substring matches; **first match wins**, so order them from
specific to generic.

**Known-good example** (ASUS Z390-A + i7-9700K + Vevor AIO):
```python
HINTS = {
    "cpu_temp":  ["cpu package"],
    "cpu_power": ["cpu package"],
    "cpu_volt":  ["cpu core"],
    "cpu_freq":  ["cpu core #1"],
    "fan_rpm":   ["fan #2"],   # CPU/rad fan ~870 RPM
    "pump_rpm":  ["fan #7"],   # AIO pump ~4830 RPM
}
```

Quick live test:

```powershell
python vevor_lcd_windows.py --print
```

You should see CPU/GPU/fan/pump values scroll on the console **and** show on the
LCD. The pump RPM should be stable and high (~4000-5500); the fan RPM should
respond to thermal load. If they're swapped, swap the `fan_rpm` / `pump_rpm`
hints.

> **Note on refresh rate:** the LCD has an internal watchdog and blanks if frames
> arrive slower than ~300 ms. The driver hardcodes 200 ms to stay under it. Don't
> raise `REFRESH_S` above ~0.25 or the screen will flicker. EMA smoothing
> (`alpha = 0.15`) handles SpeedShift/MSR jitter on aggressive boost CPUs.

---

## 4. Build the standalone exe (PyInstaller)

```powershell
pip install pyinstaller

# Build with all LHM DLLs bundled + pythonnet runtime collected
$pyiArgs = @(
    "--onefile", "--console", "--name", "cooler-lcd",
    "--collect-all", "clr_loader",
    "--collect-all", "pythonnet"
)
foreach ($dll in Get-ChildItem -Filter *.dll) {
    $pyiArgs += "--add-binary"
    $pyiArgs += "$($dll.Name);."
}
$pyiArgs += "vevor_lcd_windows.py"

python -m PyInstaller @pyiArgs
```

Output: `dist\cooler-lcd.exe` (~15-30 MB). First launch is slower (~5 s) because
`--onefile` unpacks to a temp dir; subsequent launches are fast.

**Always test the exe interactively before installing as a service:**

```powershell
.\dist\cooler-lcd.exe --print
```

If the exe fails to find a DLL, the bundle missed something — re-run PyInstaller
with `--add-binary` for the missing file, or fall back to `--collect-all clr`
in addition to `clr_loader` / `pythonnet`.

---

## 5. Install as a Windows service (NSSM)

NSSM = "Non-Sucking Service Manager", the cleanest way to wrap an arbitrary exe
into a real Windows service that starts at boot, before any user logs in. This is
the Windows equivalent of a systemd unit.

```powershell
# Install NSSM via the Windows package manager (nssm.cc has been flaky)
winget install --id NSSM.NSSM --source winget
# Restart your PowerShell so `nssm` is on PATH.
```

```powershell
$exe = "C:\Program Files\CoolerLCD\cooler-lcd.exe"   # move dist\cooler-lcd.exe here

nssm install CoolerLCD $exe
nssm set CoolerLCD Start SERVICE_AUTO_START
nssm set CoolerLCD ObjectName LocalSystem
nssm set CoolerLCD AppStopMethodConsole 3000
nssm set CoolerLCD Description "Headless driver for Vevor/HT cooler LCD"

nssm start CoolerLCD
Get-Service CoolerLCD          # should be Running
```

What each setting buys you:
- `SERVICE_AUTO_START` -> launches at boot, **before** any user logs in.
- `ObjectName LocalSystem` -> full admin rights so PawnIO can read MSRs.
- `AppStopMethodConsole 3000` -> on stop, NSSM sends Ctrl-C; the driver catches it,
  pushes the **blank-screen shutdown frame**, then exits cleanly (3 s grace).

Manage the service:
```powershell
nssm restart CoolerLCD
nssm stop CoolerLCD
nssm remove CoolerLCD confirm
```

---

## 6. Validate boot-before-login

The whole point of the service: it must run before anyone logs in.

1. Reboot Windows.
2. At the login screen — **before** typing your password — look at the LCD.
3. It should already be showing data (CPU temp, fan, pump). [OK]

If the LCD only lights up after login -> start order issue. Check that PawnIO and
the device driver load before the service:

```powershell
nssm set CoolerLCD DependOnService "PawnIO HidUsb"
nssm restart CoolerLCD
```

---

## Troubleshooting

**LCD flickers / blinks every second.** Another process is talking to the HID
device. Suspects, in order: vendor app (PC Monitor All) still installed or its
service running, LibreHardwareMonitor GUI left open, or our service running
twice. Kill them all and keep only one writer.

**CPU temps, clocks, motherboard fans all `None` / 0 in `--list-sensors`.** PawnIO
isn't loaded. Run the LHM GUI once as Administrator to install it. Verify with:
```powershell
Get-Service PawnIO
```

**`Could not load file or assembly 'LibreHardwareMonitorLib'` in the exe.** A DLL
is missing from the bundle. Re-run PyInstaller and confirm all the LHM root
`*.dll` files were picked up (check the build log for `INFO: Copying ... .dll`).

**Service starts but no LCD output (works fine interactively).** Service is
running, but probably not as `LocalSystem` (no MSR access) or PawnIO isn't loaded
yet at service start. Check:
```powershell
Get-Service CoolerLCD | Format-List Name, Status
nssm get CoolerLCD ObjectName
```

**CPU temp seems 3-5 C higher than Linux.** Normal. Windows reaches deep C-states
less aggressively than Linux, and background services (Defender, Search, SysMain,
telemetry) keep idle CPU% non-zero. Not a driver bug — the MSR readings are
correct.

---

## Cleanup (optional, after the service is rock-solid)

Once `cooler-lcd.exe` runs as a service and survives reboots, you can uninstall
the dev tooling:
- Python and all pip packages
- The source folder and `*.dll` next to the script
- LibreHardwareMonitor GUI

**Keep**:
- `cooler-lcd.exe` (move to `C:\Program Files\CoolerLCD\`)
- The `CoolerLCD` Windows service
- **PawnIO** kernel driver — without it, MSR reads die and the LCD goes back to
  showing zeros
