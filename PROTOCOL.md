# HT / Vevor Cooler LCD — USB HID Protocol

Reverse-engineered from `PC Monitoring.exe` (PC Monitor All V3), a .NET WinForms
app using the CyUSB HID API. This documents the wire protocol so the display can
be driven natively on Linux (no Windows, no Cypress driver).

## Device

| | |
|---|---|
| Transport | USB HID |
| Vendor ID | `0x5131` (decimal 20785) |
| Product ID | `0x2007` (decimal 8199) |
| Direction | Host → device: HID **output report** |

The screen has on-board firmware that does all rendering. The host only streams
numeric values; **no framebuffer / image is transferred**.

## Frame format

Every frame is a HID output report. The Windows app builds a 64-byte buffer
(`array`) and the CyUSB layer prepends the report ID, giving 65 bytes on the wire:

```
byte[0]      report ID            = 0x00
byte[1]      header               = 0x00   (array[0])
byte[2]      opcode               = 0x01   (array[1])  -> 1 = data refresh
byte[3]      sub-opcode           = 0x02   (array[2])
byte[4..36]  33 value bytes       = SendValueArray[0..32]
byte[37..64] padding              = 0x00
```

Sent in a loop every **200 ms** (`Thread_Send`).

**Shutdown frame** (sent on app close to blank the screen):
```
byte[1] = 0x00, byte[2] = 0x0F   (opcode 15), rest 0x00
```

## Value map (the 33 bytes)

All values are decimal-split into single bytes (each `(byte)`-cast, so 0–255).

| idx | field | encoding |
|----:|-------|----------|
| 0  | CPU temp | integer part |
| 1  | CPU temp | fractional × 100 |
| 2  | CPU unit | 0 = °C, 1 = °F |
| 3  | CPU usage | percent (floor) |
| 4  | CPU power | `floor(W) % 100` |
| 5  | CPU power | fractional × 100 |
| 6  | CPU freq | `floor(MHz) / 100` |
| 7  | CPU freq | `floor(MHz) % 100` |
| 8  | CPU voltage | integer part *(see quirk)* |
| 9  | CPU voltage | integer × 100 *(quirk in original)* |
| 10 | GPU temp | integer part |
| 11 | GPU temp | fractional × 100 |
| 12 | GPU unit | 0 = °C, 1 = °F |
| 13 | GPU usage | percent (floor) |
| 14 | GPU power | `floor(W) % 100` |
| 15 | GPU power | fractional × 100 |
| 16 | GPU freq | `floor(MHz) / 100` |
| 17 | GPU freq | `floor(MHz) % 100` |
| 18 | Fan RPM | `floor / 100` |
| 19 | Fan RPM | `floor % 100` |
| 20 | Pump RPM | `floor / 100` |
| 21 | Pump RPM | `floor % 100` |
| 22 | Year | first 2 digits (e.g. 20) |
| 23 | Year | last 2 digits (e.g. 26) |
| 24 | Month | 1–12 |
| 25 | Day | 1–31 |
| 26 | Hour | 0–23 |
| 27 | Minute | 0–59 |
| 28 | Second | 0–59 |
| 29 | Weekday | .NET DayOfWeek (Sunday = 0) |
| 30 | RAM/mainboard usage | percent |
| 31 | CPU power | `floor(W) / 100` (hundreds) |
| 32 | GPU power | `floor(W) / 100` (hundreds) |

### Reconstruction (firmware side)
```
CPU_temp  = [0]  + [1]/100
CPU_power = [31]*100 + [4] + [5]/100
CPU_freq  = [6]*100 + [7]
GPU_power = [32]*100 + [14] + [15]/100
GPU_freq  = [16]*100 + [17]
Fan_RPM   = [18]*100 + [19]
Pump_RPM  = [20]*100 + [21]
```

### Quirk: CPU voltage
The original computes `[8] = floor(V)` and `[9] = floor(V)*100` (it never stores
the *fractional* part — looks like a copy/paste bug from the temp/power code).
The Linux driver reproduces this verbatim for firmware compatibility. Voltage is
rarely surfaced on these screens, so it's low-stakes.

## Linux setup

### 1. Dependencies
```bash
pip install hidapi psutil pynvml
sudo apt install libhidapi-hidraw0     # runtime lib for the hidapi wheel
```
Run `sudo sensors-detect` once so CPU temp / fan show up via lm-sensors.

### 2. udev rule (access without root)
Create `/etc/udev/rules.d/99-cooler-lcd.rules`:
```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="5131", ATTRS{idProduct}=="2007", MODE="0666", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="5131", ATTRS{idProduct}=="2007", MODE="0666"
```
Then:
```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```
Replug the cooler. Verify with `lsusb` (look for `5131:2007`).

### 3. Run
```bash
python3 vevor_lcd_linux.py
```

### 4. Autostart as a systemd user service (optional)
`~/.config/systemd/user/cooler-lcd.service`:
```ini
[Unit]
Description=Cooler LCD sensor feed
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 %h/vevor_lcd_linux.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```
```bash
systemctl --user daemon-reload
systemctl --user enable --now cooler-lcd.service
```

## Notes / unknowns to verify on hardware
- **Report ID** assumed `0x00`. If `dev.write` returns -1, dump the HID descriptor
  (`sudo usbhid-dump -d 5131:2007` or `lsusb -v -d 5131:2007`) to confirm the
  output-report ID and length, and adjust `REPORT_ID` / `REPORT_LEN`.
- **Pump RPM** and **CPU voltage** have no universal Linux source; pin a specific
  `hwmon` path in the driver if your board exposes them.
- Values are clamped 0–255 per byte, matching the original `(byte)` cast.
