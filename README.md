# unofficial-triki-api-py

An unofficial Python driver using `bleak` to stream IMU accelerometer and gyroscope data from the Żabka Triki BLE gaming controller.

**This fork** keeps the original driver unchanged and adds three browser apps that run on top of it, in [`apps/`](apps): a live sensor dashboard, a twist-to-set kitchen timer and a tilt-steered paint app. It also corrects the documented order of the sensor channels (see [Data Packet Structure](#data-packet-structure)). The original project is [Wojtekb30/unofficial-triki-api-py](https://github.com/Wojtekb30/unofficial-triki-api-py).

```text
TrikiPy.py            driver: TrikiDevice (connect, stream, LED, battery) and TrikiKnob
RunDemo.py            examples from the original project
LedExample.py
knobExample.py
knobExampleSimple.py
apps/
  TrikiApps.py        local server: holds the BLE connection, serves the three apps
  dashboard.html      live charts, 3D orientation, raw data, CSV export
  timer.html          twist to set, hold still to start, LED + sound alarm
  paint.html          tilt to steer, twist for brush size, shake to paint
  icons/              icons for Windows desktop shortcuts
```

> **Disclaimer:** This is an unofficial, open-source project. It is **not** affiliated with, endorsed by, or sponsored by Żabka Polska. "Żabka", "Żappka", and "Triki" are trademarks of their respective owners. This software is provided strictly for educational purposes and to enable hardware interoperability. No proprietary firmware or applications were decompiled or distributed in the creation of this project.

## Features
- **Asynchronous & Fast:** Built on top of `bleak` for non-blocking native OS Bluetooth support.
- **Auto-Discovery:** Scans and connects to the controller automatically via its advertised BLE name.
- **Live IMU Streaming:** Unpacks and parses the raw BLE byte stream into a clean Python Data Class containing 6-DoF integers (Accel X/Y/Z and Gyro X/Y/Z).
- **LED Control:** Allows the controller LED to be turned on or off with `setLED(True)` and `setLED(False)`.
- **LED State Getter:** `getLEDstatus()` returns the locally tracked LED state.
- **Graceful State Management:** Handles the undocumented "wake-up" and "sleep" hex commands to preserve the device's battery when not in use.
- **Knob helper:** The `TrikiKnob` class that allows easy use of the device as a knob. Please look into `knobExample.py` for details.

## Prerequisites
- Python 3.7 or higher
- Windows, macOS, or Linux (requires an OS with standard Bluetooth Low Energy support)

Install the required BLE library:
```bash
pip install bleak
```

## Quick Start

1. Turn on your Triki device (ensure it is not actively connected to your mobile phone, but paired with your PC).
2. Clone this repository and run the demo script:

```bash
python RunDemo.py
```

### Example Usage (`RunDemo.py`)

```python
import asyncio
from TrikiPy import TrikiDevice

async def main():
    triki = TrikiDevice(BTName="Triki", literal=False)
    
    if await triki.connectTriki():
        print(f"Connected to {triki.getName()}!")
        print(f"Battery: {await triki.getBatteryLevel()}%")
        
        if await triki.startTriki():
            print("Streaming Live IMU Data...")
            for _ in range(20):
                data = await triki.getTrikiData()
                print(f"Accel(X,Y,Z): {data.ax:6}, {data.ay:6}, {data.az:6} | Gyro(X,Y,Z): {data.gx:6}, {data.gy:6}, {data.gz:6}")
                
            await triki.stopTriki()

if __name__ == "__main__":
    asyncio.run(main())
```

## Apps: live dashboard, twist timer and paint

`apps/TrikiApps.py` keeps the BLE connection open and serves three browser apps on `http://127.0.0.1:8770` (local only). It needs only `bleak` and the standard library.

```bash
python apps/TrikiApps.py                 # opens the dashboard in your browser
python apps/TrikiApps.py --page timer    # or open the timer / paint app directly
pythonw apps/TrikiApps.py --app --exit-when-idle 20   # app-style window, no console, quits once closed
```

| App | URL | What it does |
|---|---|---|
| **Dashboard** | `/` | Live accelerometer and gyroscope charts, a 3D orientation view (sensor fusion), battery, sample rate, raw packets, LED toggle and CSV export |
| **Timer** | `/timer` | A kitchen timer: twist clockwise to set the time, hold still to start, shake to pause or resume. When time is up the LED flashes and an alarm plays. |
| **Paint** | `/paint` | Tilt steers the brush, twist changes its size, shake turns painting on or off, and holding the Triki upside down clears the canvas. Mouse, pen and touch draw too. |

All three share a tab bar, and the server reconnects by itself if the controller drops. Only one program can be connected to the Triki at a time, so close the Żabka app on your phone first.

For a Windows desktop shortcut, point it at `pythonw.exe` with the last command above (add `--page timer` or `--page paint` for the other apps) and use an icon from `apps/icons/`.

## LED Control

The controller LED can be controlled after a successful `connectTriki()` call:

```python
await triki.setLED(True)   # Turn LED on
await triki.setLED(False)  # Turn LED off
```

The currently tracked LED state can be retrieved with:

```python
led_is_active = triki.getLEDstatus()
```

`getLEDstatus()` returns the state most recently requested through `setLED()`. It does not perform a live read from the controller.

## Protocol Documentation (Under the Hood)

For developers looking to port this to other languages (like C++, JS, or Rust), here is the reverse-engineered GATT protocol the Triki device uses to communicate.

The device utilizes the **Nordic UART Service (NUS)** structure with an additional custom LED-control characteristic:

* **RX UUID (Write):** `6e400002-b5a3-f393-e0a9-e50e24dcca9e`
* **TX UUID (Notify):** `6e400003-b5a3-f393-e0a9-e50e24dcca9e`
* **LED UUID (Write):** `6e400004-b5a3-f393-e0a9-e50e24dcca9e`

### LED Commands

The LED-control characteristic accepts a single-byte value:

* **LED On:** `0x01`
* **LED Off:** `0x00`

Unlike the IMU wake and sleep commands, these values are written directly to the LED characteristic (`6e400004-...`), not to the Nordic UART RX characteristic.

### Wake / Sleep Commands

To preserve battery, the internal IMU is asleep by default upon connection. You must write specific byte arrays to the `RX` line to begin the data stream.

* **Wake Up Command:** `0x20 0x10 0x00 0xD0 0x07 0x34 0x00 0x03`
* **Sleep Command:** `0x20 0x00 0x00 0x00 0x00 0x00 0x00`

### Data Packet Structure

Once awake, the device pushes 14-byte data packets over the `TX` line via Notifications. The data is formatted as Little-Endian, 16-bit signed integers (`<h`).

| Byte Index | Length | Description |
| --- | --- | --- |
| `0-1` | 2 bytes | **Header** (Always `0x22 0x00` for stream data) |
| `2-3` | 2 bytes | **Gyroscope X** |
| `4-5` | 2 bytes | **Gyroscope Y** |
| `6-7` | 2 bytes | **Gyroscope Z** |
| `8-9` | 2 bytes | **Accelerometer X** |
| `10-11` | 2 bytes | **Accelerometer Y** |
| `12-13` | 2 bytes | **Accelerometer Z** |

*Note: The device occasionally sends a 5-byte Status/Acknowledge packet starting with `0x21` when state changes occur (e.g., waking up or going to sleep).*

**Channel order and units (added in this fork).** The original documentation listed the accelerometer first. Measurements show the gyroscope comes first: at rest the last three values always have a magnitude of about 2048, which is exactly 1 g, even while the controller is moving. `TrikiPy.py` keeps its original field names so existing code still works, which means `TrikiData.ax/ay/az` hold the **gyroscope** and `gx/gy/gz` hold the **accelerometer**.

The wake command appears to select these settings, and the measurements are consistent with them:

| | Range | Scale |
|---|---|---|
| Accelerometer | ±16 g | 2048 per g |
| Gyroscope | ±2000 °/s | 16.4 per °/s |
| Output rate | 52 Hz (about 51 Hz measured) | |

## License

This project is licensed under the MIT License. The driver and protocol notes are by [Wojtekb30](https://github.com/Wojtekb30); the apps in `apps/` were added in this fork.
