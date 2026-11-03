# 2RTK NTRIP Server — Installation Guide

> For Linux / Armbian single-board computers (OneCloud, Raspberry Pi, Orange Pi, etc.).
> One RTK base station module + one small host + your home broadband = your own GNSS base
> station, serving centimeter-level corrections to rovers / handhelds / drones within 20–50 km.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Important: Locate the Antenna Phase Center First](#important-locate-the-antenna-phase-center-first)
3. [Automatic Install](#automatic-install)
4. [Manual Install](#manual-install)
5. [Configure the Base Station](#configure-the-base-station)
6. [Management Commands](#management-commands)
7. [Update & Uninstall](#update--uninstall)
8. [Where Config Is Stored](#where-config-is-stored)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Hardware**: a Linux/Armbian host (x86_64 / arm64 / armhf), an RTK/GNSS base station
  module with serial RTCM3 output, and a USB-to-serial cable.
- **Software**: Python 3.8+ (3.10+ recommended; the version shipped with Armbian/Debian 11
  or Ubuntu 22.04 is fine).
- **Network**: LAN is sufficient for operation; relay mode, map tiles and frontend icons
  need internet access.
- **Recommended**: assign the device a static LAN IP so rovers can connect reliably.

---

## Important: Locate the Antenna Phase Center First

> ⚠️ **This step determines whether your corrections are accurate. Do it before first use.**

RTK corrections are computed relative to the **Antenna Phase Center (APC)**. If this
coordinate is wrong, every rover position will be shifted by the same error.

### What the phase center is

- Satellite signals are not received at the antenna's physical/geometric center, but at a
  point called the **phase center**.
- For survey-grade antennas, the phase center is usually near the top of the antenna and
  does **not** equal the geometric center or the mounting screw hole.
- The antenna datasheet specifies a **Phase Center Offset (PCO)**: the 3D offset of the
  phase center relative to the **Antenna Reference Point (ARP)** (typically the mounting
  screw hole on the bottom).

### How to obtain an accurate coordinate

Pick one, from most to least accurate:

1. **(Recommended) Static survey**: mount the antenna on a known point and run a long
   static observation with another high-precision receiver to solve the exact coordinate.
2. **Known control point**: if you have a control point with known coordinates (e.g. a CORS
   point from a survey authority), mount the antenna there and enter those coordinates.
3. **Long single-point averaging**: run the module in base mode for several hours and
   average the single-point positions. Meter-level accuracy — only for low-precision use.
4. **Map estimation**: click the antenna location in Google Earth / a map tool and read the
   coordinates. Accuracy depends on the map; for temporary testing only.

### Write the coordinate into the module

**Since 3.0 this can be done in the browser**: sign in and open
`http://<device-ip>:5757/calibrate`, then pick a method:

| Method | When to use | Accuracy |
| --- | --- | --- |
| CORS / VRS survey | You have a CORS account (host, port, mountpoint, credentials) | Centimetre (fixed) |
| Survey-in | No external corrections, but an open sky view | Sub-meter |
| Manual entry | You already surveyed the APC by static session or control point | As good as your input |

The calibration page detects the module, switches its mode, collects and filters the
position, writes the coordinate, and then **reads back RTCM 1005/1006 to prove that what was
written is what gets broadcast**. Results are stored in `calibration.db`. When the sky is
obstructed and only a float solution is reachable, the record is labelled as float /
decimetre instead of pretending to be fixed.

For modules without a built-in profile, you still need the vendor's configuration tool
(or serial commands) to write the **phase-centre latitude, longitude and ellipsoidal height**
into the module, so it emits the correct coordinate in RTCM 1005/1006 messages. This program
parses those messages for the map display and the NTRIP source table (STR).

> How to verify: open the "Base Station Location" card on the web home page and check whether
> the map marker matches your expected position; or check the latitude/longitude in the local
> caster's source table (STR line). If it doesn't match, the module's coordinate is not yet
> configured correctly.

---

## Automatic Install

### One-liner (recommended)

Paste this **single line** into the device terminal. It installs `git` if missing, tries the
GitCode mirror first, falls back to GitHub, then runs the installer:

```bash
sudo apt-get install -y git && sudo rm -rf /tmp/2rtk-install && (sudo git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /tmp/2rtk-install || (sudo rm -rf /tmp/2rtk-install && sudo git clone --depth 1 https://github.com/Rampump/2RTKserver.git /tmp/2rtk-install)) && sudo bash /tmp/2rtk-install/install.sh
```

> **Copying from Windows.** The line is plain ASCII with no line breaks — paste it, don't
> retype it. `CR LF` line endings (saved via Notepad/Word) make bash fail with
> `$'\r': command not found`; full-width punctuation also breaks it. If you must use a file,
> convert it first: `sed -i 's/\r$//' file.sh`.

### Step by step

1. Clone the repository:

   ```bash
   git clone https://github.com/Rampump/2RTKserver.git
   cd 2RTKserver
   ```

   (Mirror: `https://gitcode.com/rampump/NTRIPserver.git`)

2. Run the installer:

   ```bash
   chmod +x install.sh
   sudo ./install.sh
   ```

   The script installs system deps, creates a Python venv, installs requirements, configures
   the systemd service, enables boot autostart, and creates the management command.

3. Open the web UI — the installer prints both addresses when it finishes:

   ```
   访问地址：
     本机 IP    : http://192.168.1.71:5757
     固定地址   : http://2rtk.local:5757   （Windows / macOS / iPhone）
     安卓 / 鸿蒙手机不解析 .local，请用上面的 IP 地址访问
   ```

   The fixed address is read back from the running service, so it already reflects a
   rename to `2rtk-2.local` when another device on the LAN claimed `2rtk` first.

   On the same LAN you can also use the fixed address `http://2rtk.local:5757`
   (Windows, macOS, iPhone/iPad, Linux with avahi). **Android cannot resolve `.local`
   names — use the device IP shown on the status page instead.** The fixed address
   depends on the `zeroconf` package; without it everything else still works.

   If the browser reports **502 / "this page isn't working" while the IP works**, a
   local proxy or VPN is forwarding `.local` to itself and cannot resolve it — add
   `*.local` to the proxy bypass list or turn the proxy off. `ping 2rtk.local` returning
   the device IP means the broadcast is healthy and the problem is on the proxy side.

4. Default login (**change the password immediately after logging in**):

   ```
   Username: admin
   Password: admin
   ```

---

## Manual Install

Some minimal Armbian images may fail to auto-install dependencies. Install manually:

```bash
git clone https://github.com/Rampump/2RTKserver.git
cd 2RTKserver

# If pip complains about "externally-managed-environment", add PIP_BREAK_SYSTEM_PACKAGES=1
pip install -r requirements.txt
# or
sudo PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt

python3 server.py
```

---

## Configure the Base Station

1. Log in to the web UI and open "Settings".
2. Choose the mode:
   - **serial**: read RTCM from the module's USB serial port, then upload to an upstream
     caster. Leave the port blank to auto-scan; set the correct baud rate (115200 is common).
   - **relay**: pull data from another NTRIP caster and re-upload (for self-built CORS).
   - **none**: collect only, no forwarding.
3. Fill in the upstream caster's address / port / mount point / password.
4. (Optional) enable the "Local Caster" so LAN rovers connect directly:
   - Default listen `0.0.0.0:2102`, mount point `2RTK`, user/password both `2RTK`.
   - Rovers/handhelds use `device-ip:2102/2RTK`.
   - **About ports**: the local caster's listen port is really bound on this device, while the
     ports typed for relay/upload are **remote** endpoints — this device only dials out to
     them with an ephemeral local port, so 2101 can be used for both without conflict. The
     local caster defaults to 2102 just to keep the defaults distinct; the settings page also
     probes the port before saving and refuses a duplicate value.
5. (Optional) enable "Radio datalink bridge" to transparently forward the same RTCM stream
   to a USB radio modem:
   - Plug a USB radio modem (transmitter) into the Armbian host; nearby rovers without
     network receive corrections over a paired radio (receiver) — **no NTRIP auth required**.
   - **One-way broadcast, send-only**: the program only writes the RTCM byte stream to the
     serial port; it parses nothing and adds no protocol. Radio modems are transparent
     serial devices — transmitter and receiver must share the same frequency / channel /
     baud rate.
   - The serial path can be typed manually, or click the field to auto-scan/enumerate; the
     port already used by the GNSS module is **auto-disabled** in the list to avoid conflict.
   - Default baud rate is 115200 (changeable); it must be ≥ the radio's air rate, otherwise
     data backs up and rovers lose fix.
   - The radio switch, the local caster switch, and the run mode are **fully independent** —
     they can run together or separately without affecting each other.
6. Click "Save & Restart".

---

## Management Commands

After installation:

```bash
2rtkserver start     # start
2rtkserver stop      # stop
2rtkserver restart   # restart
2rtkserver status    # status
```

---

## Update & Uninstall

From the command line:

```bash
chmod +x update.sh
sudo ./update.sh     # backs up config, updates files, restarts service
```

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh  # stops and removes the service and install directory
```

From the web UI (**Settings → Maintenance**, no SSH needed):

| Button | What it does |
| --- | --- |
| Check for updates | Probes both repositories in parallel and lists each commit |
| Upgrade now | Fetch → verify (compile, import smoke test) → backup → stop service and wait for ports to be released → swap files → install deps → start → health check. Rolls back automatically if any step fails |

**Uninstalling is SSH-only.** It deletes the whole install directory, and a browser
confirmation box is not enough protection against a mis-click. `uninstall.sh` asks twice:
once to confirm, once to back up `2RTKserver.db` (settings + admin password) and
`calibration.db` (surveyed coordinates, including pending `-wal`) to `/root/`. The old
`/api/upgrade/uninstall` endpoint answers `410 Gone` with the same instruction.

The web service stops for a few seconds while upgrading, so the page loses its
connection — that is expected. The page keeps polling, refreshes itself on success,
and on failure shows the reason plus a clear note that the previous version was restored.

### One single source for the version number

The version lives in the `VERSION` file at the program root (a single `major.minor.patch`
line such as `3.1.1`):

- Maintenance page shows `v<version> · <commit>`: the version comes from `VERSION`,
  the commit from `.version`;
- the console banner and the mDNS TXT record read the same file, and **no version
  string is hard-coded in the code** (the local contract suite's
  `TestVersionSingleSource` fails on any hard-coded `x.y.z`; the suite is kept out of the
  published repository, so run it from a development checkout);
- `install.sh`, `update.sh` and the upgrade runner all copy `VERSION` and record it in
  `.version`;
- to cut a release, edit **only that one line** and commit. If a package ever ships
  without `VERSION`, the UI shows `v0.0.0` — that is a packaging warning, not a real version.

Two repositories are used, and **the repository names differ on purpose** — the code
addresses them by URL, no renaming required:

- GitCode mirror: `https://gitcode.com/rampump/NTRIPserver.git` (reachable in mainland China, tried first)
- GitHub: `https://github.com/Rampump/2RTKserver.git`

Both are probed concurrently and the first reachable one is used, so devices that cannot
reach GitHub still upgrade. To point at your own mirror, write
`/opt/2RTKserver/.upgrade/sources.json` instead of editing code:

```json
{
  "gitcode": "https://gitcode.com/your-name/your-repo.git",
  "github":  "https://github.com/your-name/your-repo.git",
  "branch":  "main",
  "order":   ["gitcode", "github"]
}
```

Backups live in `/opt/2RTKserver/.upgrade/backups/` (last 3 kept), the upgrade log is
`/opt/2RTKserver/.upgrade/upgrade.log`, and the runner's own output is in
`journalctl -u 2rtk-upgrader -n 100`.

---

## Where Config Is Stored

- All config lives in **`2RTKserver.db`** (SQLite) in the program directory; the admin
  password is stored as a PBKDF2 hash.
- Restart / power loss / systemd restart after a crash — config and password are preserved.
- The database, `server.log` and `.secret_key` are all anchored to the **program directory**,
  so running `python3 /opt/2RTKserver/server.py` from any directory reads the same config.

Backup (recommended):

```bash
sudo cp /opt/2RTKserver/2RTKserver.db /root/2RTKserver-$(date +%F).db
```

Restore: stop the service → overwrite the file → start the service.

---

## Troubleshooting

### Dependency install fails

The script falls back to forced install:

```bash
PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt
```

### Service won't start

```bash
journalctl -u 2rtkserver -n 50
```

### Local caster fails to start / port already in use

The log shows `cannot listen on 0.0.0.0:2102 ([Errno 98] Address already in use)` — something
**on this device** already listens on that port (most often a previous instance that has not
exited yet, or another NTRIP program).

```bash
ss -lntp | grep :2102
sudo lsof -i :2102
```

Fix accordingly: kill the stale instance (`sudo 2rtkserver restart`, or `pkill`), or change the
local caster port in the settings page (2103, etc.). The settings page probes the port before
saving and shows the pid/process name that holds it.

> Note: the 2101 you type for relay/upload is a **remote** port. Outbound connections use an
> ephemeral local port, so it never collides with the local caster's listen port.

### Can't access the web UI

1. Check the service: `2rtkserver status`
2. Open port 5757 in the firewall:

   ```bash
   sudo ufw allow 5757/tcp
   ```

### Forgot the admin password

There is no web reset (by design). SSH into the device and run:

```bash
sudo systemctl stop 2rtkserver
sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes
sudo systemctl start 2rtkserver
```

The admin is reset to `admin / admin` and all connection settings are cleared. Change the
password immediately.

### Map not showing / tile load failure

The home map uses AMap by default (China) and auto-falls back to multiple OpenStreetMap
mirrors. If all fail, the device likely has no internet — this does not affect corrections,
only the map display.

---

## Security Notes

- The web admin port 5757 listens on `0.0.0.0` by default — **LAN/intranet only**, do not
  expose it to the public internet.
- For remote access, put Nginx + HTTPS with access control in front, and change the default
  password first.
- 5 consecutive failed logins lock the account for 5 minutes.
- Anonymous visitors can see signal statistics but not the exact base coordinates; to make
  coordinates public set `PUBLIC_STATION_COORDS=1` (not recommended).
- `2RTKserver.db` contains the upload password in plaintext — keep backups private.
