# Max fans on Linux Mint (Acer Nitro)

Adds **Fans: Max / Auto** to the Mint menu. One click switches both fans to
full speed, and another click puts them back on Auto. A notification shows the
fan speed after each change. Every restart begins in Auto.

## Setup (once, about 5 minutes)

1. Install Linux Mint and, if offered, the NVIDIA driver from Driver Manager.
2. Put `nitro_fans.py` in Downloads, open Terminal and paste:

   ```sh
   sudo python3 ~/Downloads/nitro_fans.py install
   ```

   Enter the password when asked. On Mint 22.x this also installs a newer
   kernel, which takes a few minutes.
3. Restart.
4. Open the menu, search for "Fans", and right-click the entry to add it to the
   panel if wanted.

If the notification says Max fans aren't available, the laptop's firmware
refused the request. The fans stay on Auto, and Mint's power modes still work.

To remove everything: `sudo python3 /usr/local/bin/nitro-fans uninstall`, then
restart. To check the state: `nitro-fans status`.

## How it works

The kernel's `acer_wmi` driver already supports Acer's firmware fan modes
(kernel 6.19 and later), the same ones NitroSense uses on Windows. It only
enables them for models on its list, and the AN515-56 isn't on it. The
installer adds one driver option (`force_caps`). That option keeps the
features the driver already detects (Wi-Fi and Bluetooth switches, brightness)
and adds performance modes, fan readouts and fan modes. The menu entry can only
choose firmware Max or firmware Auto. It never sets a fixed speed and never
writes to the embedded controller.

Not yet confirmed on an AN515-56. The same approach is reported working on an
AN517-55, which isn't on the kernel's list either.
