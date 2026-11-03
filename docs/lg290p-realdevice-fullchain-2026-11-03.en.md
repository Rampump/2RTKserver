# LG290P full-chain real-device test report (upgrade → mDNS → one-click calibration → NTRIP uplink)

- Date: 2026-11-03, 15:42 – 16:19 (GMT+8); follow-up issues fixed in 3.1 the same evening
- Host: Armbian on `192.168.1.71` (Aml.S905l3b, kernel 6.1.98-ophub, Python 3.10.12)
- Module: Quectel LG290P, firmware `LG290P03AANR01A03S`, SN `00006FE9326C3E41`, port `/dev/ttyUSB1`
- Antenna: outdoor window ledge in a narrow alley, sky partially blocked by the building and the
  window frame (mostly low-elevation satellites in view)
- Chinese original: [lg290p-realdevice-fullchain-2026-11-03.zh.md](lg290p-realdevice-fullchain-2026-11-03.zh.md)
- Verdict: **the whole chain works. Calibration reached an RTK fixed solution, the ECEF was stored,
  the base is pushing to a public caster, and the coordinate broadcast in RTCM 1005 differs from
  the one written by at most 0.10 mm.**

---

## 0. Summary

| Stage | Result |
|---|---|
| One-click upgrade 2.2.3 → 3.0.0 | ✅ Service restarted by itself; configuration and calibration databases preserved |
| mDNS fixed address | ✅ `http://2rtk.local:5757` resolves on Windows and on the device |
| One-click CORS calibration | ✅ **RTK fixed (quality = 4)**, 60 samples, **1.1 cm** accuracy |
| ECEF stored | ✅ Record #1, with serial number and firmware complete |
| NTRIP uplink to `43.142.126.162:2101/2RTK` | ✅ Authenticated and streaming: 84 KB / 126 frames in 2 minutes, 0 reconnects |
| Defects found on hardware | 5 in the first pass (fixed and re-verified, §7) + 5 usability issues reported during review (fixed in 3.1, §8) |

---

## 1. Starting state

After logging in as `root@192.168.1.71`:

```
Linux 2RTKserver 6.1.98-ophub #1 SMP ... aarch64
BOARD="S905l3b"   BOARDFAMILY="meson-gxl"
Python 3.10.12
/opt/2RTKserver            ← an older release was already installed
VERSION = 2.2.3
.version = {"commit":"1fa77a2","date":"2026-11-01 11:49:44",...}
systemctl is-active 2rtkserver → active (up for 2 days)
```

Serial ports:

```
crw-rw---- 1 root dialout 188, 0  /dev/ttyUSB0
crw-rw---- 1 root dialout 188, 1  /dev/ttyUSB1
lrwxrwxrwx ... usb-1a86_USB_Serial-if00-port0 -> ../../ttyUSB1
```

Both ports are CH340 bridges (`VID:PID=1A86:7523`), so the device name alone cannot tell you
which one has the module — it has to be identified by talking to it.

Disk 6.5 GB with 4.5 GB used (2.0 GB free), 1795 MB RAM (1506 MB available): enough to upgrade.

---

## 2. One-click upgrade (2.2.3 → 3.0.0)

### How

```bash
rm -rf /root/ntrip-src
git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /root/ntrip-src
cd /root/ntrip-src && bash update.sh
```

`update.sh` treats its own directory as the source tree (`SRC_DIR`), so the new sources have to
be fetched first. The script has no interactive prompts (`read -p` only exists in
`uninstall.sh`), so it can run unattended.

### Result

```
Stopping service... → backing up configuration... → DB backed up to /opt/2RTKserver/2RTKserver.db.bak
→ updating program files → updating Python dependencies (all "already satisfied")
→ setting permissions → starting service → reading fixed address (mDNS)
=== Update complete! ===
  Local IP  : http://192.168.1.71:5757
  Fixed     : http://2rtk.local:5757
```

Post-upgrade checks:

```
VERSION  = 3.0.0
.version = {"app":"3.0.0","commit":"b7d99c9","source":"update.sh","channel":"update"}
rtkserver/calibration/ present (backup/cors/geo/lease/proto/service/store/writer/drivers)
LISTEN 0.0.0.0:5757
GET /            → 200
GET /calibrate   → 302 (not logged in; authentication works)
```

**No new dependency was needed.** The calibration subsystem only uses `pyserial` and `pyrtcm`,
both already installed with 2.2.3, so pip printed `Requirement already satisfied` throughout.
`zeroconf 0.139.0` was also present, so mDNS worked.

### Three incremental upgrades afterwards

Five hardware defects were fixed during the session, each verified through the same path
(`git pull` + `bash update.sh`), which also proved the upgrade is repeatable:

| # | Commit | Content |
|---|---|---|
| 1 | `b7d99c9` | 3.0.0 release (initial install) |
| 2 | `750701e` | Abort button always returned 500 |
| 3 | `cbea14e` | Wrong banner when a read-only diagnosis was interrupted |
| 4 | `5f00eab` | NaN deviation / bogus noise warning / stage timestamps |

All four upgrades succeeded, the service came back every time, and neither `2RTKserver.db` nor
`calibration.db` was overwritten.

---

## 3. mDNS access and login

On Windows, `ping 2rtk.local` resolved to `192.168.1.71` immediately and
`curl http://2rtk.local:5757/` returned 200. The built-in browser rendered
`http://2rtk.local:5757/` with the expected navigation: Status / Settings / Calibration /
Updates / Change password / Log out.

### The admin password was unknown

`admin_password` on the device was a 118-character PBKDF2 hash set during an earlier install,
and nobody on site knew the plaintext. The configuration database was empty (`mode=none`, no
serial port, no caster), so resetting cost nothing:

```bash
systemctl stop 2rtkserver
venv/bin/python reset_config.py -y      # admin/admin, connection settings cleared, calibration.db untouched
systemctl start 2rtkserver
```

Output confirmed `[info] Calibration records (calibration.db) untouched.` That design decision —
never delete the calibration database during a config reset — proved correct on hardware: the
calibration database is a site asset, not configuration.

> Note: `admin/admin` was a temporary password for the test. Change it under "Change password"
> before any real deployment.

---

## 4. Serial port and module identification

On the calibration page, `/dev/ttyUSB1` was entered and "Read module info" pressed (read-only
diagnosis, writes nothing):

```
Firmware     LG290P03AANR01A03S
Hardware ID  00006FE9326C3E41
Mode         base
Unique ID    00006FE9326C3E41
```

`/dev/ttyUSB0` (the other CH340) was probed as well: it sat at `DETECT 20%` until the timeout —
**not a GNSS module**, presumably a datalink or other serial device. Harmless, but it exposed a
wording bug (§7.2).

> Cross-check at the API level: `GET /api/serial_ports` returned two `USB Serial` entries
> (`1A86:7523`, locations 1-1 / 1-2), matching `ls /dev/ttyUSB*`.

---

## 5. One-click CORS calibration

### 5.1 Parameters

| Item | Value |
|---|---|
| Serial port | `/dev/ttyUSB1` |
| Baud rate | empty (auto) |
| Module model | *Unknown · auto detect* (detected as Quectel LG290P) |
| Method | CORS differential calibration |
| CORS server | `120.253.239.161` |
| Port | `8002` (WGS84) |
| Mount point | `RTCM33_GRCEJ` (confirmed via "Fetch mount points" first) |
| Account | site account (not recorded in plain text here) |
| Collection time | 3 minutes (quick path) — see note |
| Fix wait limit | 180 s |
| Coordinate message | 1005 (base station position) |
| Observation tier | MSM4 (lower bandwidth, recommended) |

> Note: the collection time should have been the recommended 5 minutes; 3 minutes was clicked by
> mistake. It did **not** affect the result — convergence was reached after 63 s and the run
> ended early, so 3 minutes was only ever an upper bound. A production calibration should use
> the 5-minute tier.

### 5.2 Timeline

"Start CORS calibration" → confirmation dialog ("the service will be unavailable for about
300 s; downstream NTRIP clients will lose the stream") → confirmed.

Stage log (with the timestamps **after** the fix in §7.5):

```
16:04:21  PREPARE / validate / OPEN / DETECT / BACKUP
16:04:32  ROVER_SETUP   switch to rover
16:04:34  NMEA_SETUP    enable NMEA output (GGA/GSA only)
16:04:36  RTK_REINIT    save parameters and reboot, re-initialising the RTK engine
16:05:36  FIX_WAIT      waiting for a fixed solution (quality=4, 24 sats, PDOP=1.54)
16:06:32  COLLECT       sampling and filtering
16:06:46  MSG_SETUP     enabling RTCM3-1005
16:06:48  RESTART       soft reboot (SRR), waiting for the module to come back
16:06:52  VERIFY_MODE   verify mode and coordinate
16:06:54  VERIFY_RTCM   verify the broadcast 1005 coordinate (2 s capture)
16:06:56  DONE          write complete; 1005 broadcast matches what was written
```

**The key observation: this run really did reach an RTK fixed solution.** During `FIX_WAIT` the
quality indicator read `4 (RTK fixed)`, 24 satellites, PDOP 1.54, and it held for 30 s before
collection started — the float fallback never triggered. That contrasts sharply with 2026-11-02
on Windows (stuck at q=5 for 8 minutes), which confirms the closed loop "`SAVEPAR` + reboot so
the RTK engine rebuilds its state" is what matters, not the antenna environment.

> About the antenna: it stayed in the alley outside the window for this test. The hypothesis
> "poor sky view prevents a fix" simply does not hold here — hdop 0.8–0.9, 24–28 satellites,
> healthy solution geometry, and the fix was still established. What the alley really costs is
> float-solution accuracy and multipath, not the ability to fix at all.

### 5.3 Result (`calibration.db` record #1)

```
module_sn             00006FE9326C3E41
module_model          lg290p
firmware              LG290P03AANR01A03S
method                cors
ecef_x/y/z            -2001277.3805, 5416696.8658, 2699549.3072
lat/lon/height        25.203523744810735, 110.27748337038595, 138.31627280265093
samples               60
duration              63 s
accuracy              0.01103 m (≈1.1 cm)
raw_sample_std_e/n/u  0.003081 / 0.008683 / 0.006066 m
kalman_std_e/n/u      0.004428 / 0.004428 / 0.004428 m
verified              1
verify_delta          0.0001 m (written == broadcast)
nvm_state             saved
cors_mountpoint_used  RTCM33_GRCEJ
gga_guard_used        1
gga_jump_dropped      0
remark                Web one-click calibration
task_id               20261103-160421-542
```

`arp`, `apc` and `ecef` hold identical values, so no antenna-height offset was entered by mistake.

### 5.4 Independent re-check after the run

A read-only diagnosis was run again (page button "Read module info"):

```
Configured coordinate matches broadcast coordinate
1005 broadcast deviation: 0.01 cm
Next step: configured coordinate equals the 1005 broadcast (max axis delta 0.10 mm)
```

0.10 mm is exactly the RTCM 1005 quantization step — the smallest difference that can be
measured, i.e. an exact match.

---

## 6. NTRIP uplink

### 6.1 Configuration (Settings page)

| Item | Value |
|---|---|
| Mode | `serial` — read the port and push |
| Port / baud | `/dev/ttyUSB1` / `115200` |
| Upload to upstream NTRIP caster | ✅ enabled |
| Host / port | `43.142.126.162` / `2101` |
| Mount point | `2RTK` |
| Password | site password (not recorded in plain text here) |
| Local caster / radio | both disabled |

"Save and restart" restarted the service; the configuration read back identically (the password
is stored masked).

### 6.2 Verification

Service log:

```
16:12:56  Configuration valid, starting main program...
16:12:56  Connecting to 43.142.126.162:2101
16:12:56  Upload outlet started: 43.142.126.162:2101/2RTK (dedicated thread, auto-reconnect)
16:12:56  Connected to 43.142.126.162:2101
16:12:56  Upload authenticated, streaming: 43.142.126.162:2101/2RTK
16:12:58  Using configured serial port: /dev/ttyUSB1 @ 115200bps
```

"Upstream NTRIP Caster" card:

```
Status        Authenticated · streaming
Target        43.142.126.162:2101/2RTK
Sent          84.0 KB · 126 frames
Measured rate 664 B/s
Reconnects    0
Session       0h 2m 4s
```

The RTCM inventory on the status page read
`1005(1), 1074(1), 1084(1), 1094(1), 1114(1), 1124(2), 1134(1)` — five MSM4 constellations plus
1005, all broadcasting. Satellite side: 6 constellations observed (BDS+GPS+GLO+GAL+QZS+NAV),
28 satellites, 17 with strong signal.

### 6.3 Evidence from the caster side

Pulling the caster's source table from the device:

```
STR;2RTK;none;RTCM3.x;1005;0;GPS;2rtk;CHN;25.2034;110.2775;0;0;2RTK_NtirpCaster;N;B;N;500;NO
```

The mount point `2RTK` is registered, the coordinates `25.2034 / 110.2775` match the calibration
result, the format is RTCM3.x and 1005 is present.

> Note: using `2rtk:<upload password>` as a *reader* account returns `401 Unauthorized`, which
> means this caster keeps push and pull credentials separate. It does not affect the uplink
> (which authenticated fine), it only means a rover needs its own pull account.

### 6.4 Data plane yields the port and recovers

Calibration takes exclusive ownership of the serial port, so the uplink has to pause and then
resume. The log shows both transitions:

```
16:16:38  Calibration stage → OPEN (opening serial port)
16:16:38  Upload outlet stopped
16:17:01  Calibration stage → DONE
16:17:01  Upload outlet started: 43.142.126.162:2101/2RTK
16:17:02  Upload authenticated, streaming
16:17:03  Using configured serial port: /dev/ttyUSB1 @ 115200bps
```

---

## 7. Defects the hardware exposed

This section is the most valuable part of the session: four of the five problems below only
appear on real hardware.

### 7.1 The abort button always returned 500 (P1)

**Symptom** — pressing "Abort" on the calibration page produced a server error, although the
task did stop.

```
api_calibration.py:315  ok, message, payload = calib.abort()
ValueError: not enough values to unpack (expected 3, got 2)
```

**Root cause** — `service.abort()` has always returned `(ok, message)`; the other two call sites
in `service.py` (CLI, watchdog) unpack two values. Only the web handler expected a payload.

**Impact** — cancellation did work (the flag was set and the module was shut down cleanly), but a
500 makes the operator press the button again, and repeatedly aborting during SAVE/RESTART risks
interrupting the cleanup path.

**Fix** — unpack two values; the snapshot is fetched separately via `_snapshot_payload()`. A new
contract test (`tests/test_contract.py::TestCalibrationApiUnpacking`) pins it down (two-tuple →
200, idle → 409). **Re-verified**: `POST /api/calibration/abort` now answers 409 when idle.

### 7.2 A read-only diagnosis lied about "module left in a half-configured state"

**Symptom** — probing `/dev/ttyUSB0` (not a GNSS port) timed out; after the follow-up restart the
startup banner read:

> The previous calibration was interrupted during "identify module" (port /dev/ttyUSB0). The
> module may be left in a half-configured state; recalibrate or run a read-only diagnosis first.

**Root cause** — the startup hook never looked at the task type and always used the wording for an
interrupted *write*. "Identify module" is read-only: it does not even change the baud rate, so
there is no half-configured state to be in.

**Impact** — the operator recalibrates for nothing; worse, such false alarms dilute the real ones.

**Fix** — `SerialLease` carries an `action` field into `lease.json` and into the snapshot; the
startup hook branches on it and says "read-only diagnosis does not write the module, the current
configuration is unchanged, you can start again" instead. Old leases without the field fall back
to the stage (`DETECT`/`OBSERVE`).

### 7.3 "1005 broadcast deviation: NaN cm" (P1, on the main path)

**Symptom** — the diagnosis concluded "configured coordinate matches broadcast coordinate",
immediately followed by `1005 broadcast deviation: NaN cm` and `next step: ... (max axis delta
0.00 mm)`.

**Root cause** — the backend sends `delta` as an object `{dx,dy,dz,max_axis,norm}` (metres), the
frontend computed `(Number(delta) * 100).toFixed(2)`. `Number({...})` is always `NaN`.

**Impact** — this is the only hard evidence that the chain actually works; showing it as NaN turns
the one quantitative proof into noise, and contradicts the "matches" verdict right above it.

**Fix** — read `delta.max_axis` and show an em dash when it is not finite.
**Re-verified**: the same diagnosis now prints `1005 broadcast deviation: 0.01 cm`.

### 7.4 "Discarded bytes exceed received lines" — a permanent false alarm from mixed units

**Symptom** — every diagnosis carried the same warning inside a green result box:

> Discarded bytes exceed received lines; the baud rate may be wrong, or the serial line is noisy.

**Root cause** — the condition was `noise > lines`, but `noise` counts **bytes** while `lines`
counts **lines**. In base mode the port carries almost nothing but RTCM binary frames of several
hundred to a thousand bytes each, so every healthy base station hits this permanently.

**Impact** — an alarm that is always true is no alarm at all, and it buries genuine baud-rate
problems.

**Fix** — compare noise bytes against **received bytes** (`rx_bytes`), warn only above 20%, and
state the actual ratio in the message. **Re-verified**: the warning is gone.

### 7.5 All 13 stage rows shared one timestamp

**Symptom** — during a 2.5-minute calibration every row of the stage log showed `16:04:21` (the
task start), while the header said `16:04:21 — 16:06:55`.

**Root cause** — the frontend used `snap.started_at` for each row; the backend never published
the time the current stage was entered.

**Impact** — the only purpose of that list is "where is it stuck and how long did each step take".
Identical timestamps make it useless — and it was exactly this list that showed `FIX_WAIT` took
60 s while `COLLECT` took 56 s.

**Fix** — `_set_stage` records `stage_at` (updated on every stage change) and ships it in the
snapshot; the frontend prefers it. **Re-verified**: rows now read `16:16:39 … 16:17:01`,
increasing per stage.

### 7.6 Side finding: the contract test entry point silently skipped 160+ tests

**Symptom** — the suite was always run as `python -m tests.test_contract`, and always printed
`Ran 41 tests ... OK`.

**Root cause** — `tests/test_contract.py` had a stray `if __name__ == '__main__': unittest.main()`
in the middle of the file (line 433). Execution stopped there, so the remaining 160+ tests (all
calibration contracts and the upgrade runner contracts) had never run.

**Fix** — the stray entry point was removed and the repository root is inserted into `sys.path` at
the top of the file, so both `python tests/test_contract.py` and
`python -m unittest discover -s tests -t .` work. The full suite is now **203 tests** (1 skipped).

> The upgrade runner itself uses `unittest discover`, so the production upgrade path was always
> running everything; only the manual local invocation was affected.

### 7.7 Observed, not changed

- **The status page shows `--` for device model / firmware / antenna**: the data plane uses a
  generic reader and never queries the module's identity. The calibration page reads it fine, so
  the data exists — it just is not wired to the dashboard yet.
- **The calibration page and the settings page share the word `mode` but not the meaning**:
  finishing a calibration does not automatically make that port the data source; it has to be set
  in Settings (or via the "Use as source" button on the result card). Expected, but one extra step.

---

## 8. Follow-up issues found during review of this run (fixed in 3.1)

These were reported by the operator while reviewing the report above. All five are user-interface
defects that no unit test would have caught, and all are covered by regression tests now.

### 8.1 Login page did not say what the credentials are

A freshly installed device gave no hint of `admin` / `admin` and no advice to change it. The login
page now shows a warning banner with the default credentials and the instruction to change them —
**and the banner disappears as soon as the password is no longer the factory default**
(`security.is_default_admin()`), so it can never advertise a password that no longer works.

### 8.2 "Scan ports" / "Fetch mount points" opened their list anywhere but under the input

Filling a native `<datalist>` does not open its popup, and the popup the browser does draw is
positioned by the browser — any ancestor with a `transform` breaks that calculation. Every card on
these pages runs a `.fade-in` animation whose final keyframe still holds `transform:
translateY(0)`, which is enough to make the list appear somewhere else entirely (the report from
the field: "the list flies around the screen").

Fix: a small replacement component (`static/picker.js`) that detaches the `list` attribute from the
input, keeps reading candidates from the same `<datalist>` (so nothing that fills the list had to
change), and renders a panel appended to `<body>` with `position: fixed` coordinates computed from
`input.getBoundingClientRect()` on every open. The panel also opens by itself once a scan or a
source-table fetch returns, supports keyboard navigation, filtering, and shows but greys out
entries marked `disabled` (a port already used by the GNSS module).

### 8.3 The three outlet cards had different heights

Upload / local caster / radio have 7, 4 and 6 key-value rows. `align-items: start` let each card
size itself, producing three boxes of visibly different heights side by side. They now stretch to
one height, with the footnote pinned to the bottom edge so the three explanation lines line up.

### 8.4 The baud rate was guessed, not read

The vendor documents list five rates and no fixed factory default, so the previous flow climbed the
candidate list and paid a 2-second timeout on every wrong step. The module can simply be asked.

- Drivers now implement `read_uart()`: LG290P sends `$PQTMCFGUART,R` (reply
  `$PQTMCFGUART,OK,<Index>,<BaudRate>,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>`), Unicore N4 sends
  `CONFIG` and collects the three per-port replies, LC29HBS declares "not supported" because it
  only has a write command.
- The result is shown in the hardware panel (`UART1 115200 8N1`) and stored per model in
  `module_profiles.uart_baud` (schema v5).
- The next run tries, in order: what you typed → what the module reported last time → the vendor
  table (highest first) → a global fallback.

Measured effect on this device: the first run still had to find 115200 by scanning (5 candidates,
worst case ~8 s of timeouts). Subsequent runs go straight to 115200, and the hardware panel
confirms it came from the module rather than from a table.

### 8.5 "One-click calibration has no module selection"

It does — the select box sits in the Device card — but nothing on the page said what it changed, so
it read as decoration. The page now explains both paths in place: *auto detect* sends one
identification query per registered model until one answers (1–2 s, also probes optional
commands), while a pinned model sends exactly one query and fails loudly with
`you selected X, detected Y` on a mismatch instead of writing base-station settings into a
different receiver.

---

## 9. Reproduction steps

```bash
# 1. upgrade / install
ssh root@192.168.1.71
rm -rf /root/ntrip-src
git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /root/ntrip-src
cd /root/ntrip-src && bash update.sh          # use install.sh for a fresh install

# 2. reset a forgotten password (calibration.db is not touched)
systemctl stop 2rtkserver
/opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py -y
systemctl start 2rtkserver

# 3. browser: http://2rtk.local:5757  (Android: http://192.168.1.71:5757)
#    log in → Calibration → port → Read module info → method CORS → CORS parameters
#    → Fetch mount points → Start → wait for DONE

# 4. Settings: mode serial, port, host/port/mountpoint/password → Save and restart

# 5. verify
curl -s -H "Ntrip-Version: Ntrip/2.0" http://43.142.126.162:2101/ | grep 2RTK
```

---

## 10. Timeline

| Time | Event |
|---|---|
| 15:42 | Started; SSH login; confirmed 2.2.3 with the service running |
| 15:44 | Cloned and upgraded to 3.0.0; service restarted, mDNS up |
| 15:45 | `2rtk.local` resolved; dashboard opened in the browser |
| 15:47 | Admin password reset (config DB empty, calibration DB preserved) |
| 15:49 | `/dev/ttyUSB1` identified as Quectel LG290P |
| 15:50 | `/dev/ttyUSB0` timed out → §7.2 and §7.1 discovered |
| 15:57 | 7.1 / 7.2 fixed and deployed as `cbea14e` |
| 16:00 | Module info read again: base mode, configured == broadcast |
| 16:04 | One-click CORS calibration started |
| 16:05 | FIX_WAIT: **quality 4, 24 sats, PDOP 1.54** (fixed) |
| 16:06 | 60 samples converged → written → mode switched to base → 1005 verified → DONE |
| 16:08 | §7.3 / §7.4 / §7.5 found and fixed; deployed as `5f00eab` |
| 16:12 | Upload outlet configured; authenticated and streaming |
| 16:14 | Read-only re-check: 0.01 cm, no false warning, correct timestamps |
| 16:19 | Uplink stable (84 KB / 126 frames / 0 reconnects); test finished |
| later | Review produced §8.1 – §8.5, fixed in 3.1 with regression tests |

---

## 11. Open items

1. **Change the temporary `admin/admin` password** and check the strength of the caster and upload
   passwords.
2. **Run the production calibration on the 5-minute tier** (this run used the 3-minute tier by
   mistake; convergence took 63 s either way, but the tier should be the documented one).
3. Wire device model / firmware / antenna status on the dashboard to the module information — the
   data is already read by the calibration page.
4. A rover pulling from this caster needs a separate **pull account** (push credentials cannot
   read).
5. `tests/` is excluded from the release archive, so the contract suite does not run on a deployed
   device; keeping it in the repository but excluding it from the install package would preserve
   both properties.

---

## 12. Second field session: swapping in an LC29H(BS) (fixed in 3.1.1)

After the run above, the LG290P was unplugged and an LC29H(BS) inserted **on a different USB port**,
on the same Armbian host. None of the five problems below was a wrong parameter — every one was a
gap in the contract between the pipeline and the drivers, and none of them would surface in a unit
test on its own.

### 12.1 One missing driver method = one HTTP 500

"Read module info" answered:

```
AttributeError: 'LC29HBSDriver' object has no attribute 'read_rtcm_config'
```

To be precise about the cause: no command was wrong. The diagnosis, backup and restore paths call
a **documented surface of methods unconditionally** (`read_rtcm_config`, `config_rtcm`,
`get/set_message_rate`, `disable_survey_in`, `get_svin_config`, `read_antenna_info`, …). The
convention is "implement it if the model has the command, otherwise return unsupported". The
LG290P and the N4 happened to implement all of them; the LC29H(BS) was missing five — and nothing
enforced the convention, so "this model has no such command" (a perfectly normal situation) turned
into a 500.

Fixed in two layers:

1. **Base-class fallback.** `BaseDriver` now provides defaults for the whole optional surface (and
   the write-path methods `save` / `reboot` / `set_mode`), all returning
   `(False, '<why>', {'error': 'unsupported'})`. A missing method is no longer an exception but a
   reason that can be shown to the operator.
2. **Real implementations.** The five LC29H(BS) methods are implemented from the vendor manual
   rather than papered over:
   - `read_rtcm_config()` reads back `PAIR433/435/437` and translates the global Mode
     (0 = MSM4, 1 = MSM7) into the `msm_type` the pipeline uses. This also fixed a silent bug:
     the old code read `data['mode']` while `request()` only returns `{'fields': [...]}`, so the key
     was always `None` and the page had been reporting "RTCM mode unknown" forever.
   - `config_rtcm()` rejects MSM5/6 outright — `PAIR432` only has two values — instead of quietly
     downgrading.
   - `get/set_message_rate()` accepts only `PQTMSVINSTATUS` and `PQTMEPE`; everything else is
     declared unsupported (saving a guaranteed timeout). `PQTMEPE` uses `<MsgVer>` **2**, not 1.
   - `disable_survey_in()` sends `PQTMCFGSVIN,W,0,0,0,0,0,0` — seven fields, not the LG290P's eight.
3. **Contract tests.** `TestDriverProtocolSurface` walks every registered driver and asserts all 19
   methods exist and return a 3-tuple; `TestLc29hbsMissingCommands` additionally asserts the
   **command body actually sent**, which rules out the harder-to-spot "returns True but sends
   nothing" implementation.

### 12.2 "Scan serial ports" looked dead

After the USB swap the button appeared to do nothing. Two design decisions combined into that
illusion:

- candidates were only written into a `<datalist>`, so a **second click** on the input was needed
  before anything appeared (and the self-drawn panel added in 3.1 only opens when the input is
  interactive);
- failures were reported in the **page-bottom global error bar**, several screens away from the
  port field.

Scan results now render as chips directly under the input (`#cal-port-chips`), with the outcome and
any failure reason in `#cal-port-hint`. A single port is **filled in automatically**. The backend
`/api/serial_ports` now returns `ok` / `count` / `error`, so "enumeration failed" (permissions,
driver) is distinguishable from "no device attached" (cable).

### 12.3 "Measure only" must not be offered to a base-only module

CORS calibration was correctly greyed out for the LC29H(BS) (no rover mode), but "measure
coordinates only" was still selectable — and that path is precisely the one that **requires
receiving CORS corrections** to compute the antenna position. A base-only module on that path
collects for the full duration and returns a **metre-level single-point solution** presented as a
successful calibration. That is the worst kind of success: the operator then writes that
metre-level coordinate into the base station.

Now enforced on both sides:

- front end `computeLocks()`: model pinned and `dual_mode === false` → the option is locked, with
  alternatives spelled out;
- back end `start_cors(measure_only=True)`: same condition returns 400 (so a direct POST or a stale
  cached page cannot bypass it);
- **no model selected → still allowed**, because an unknown module may well be a ready-made rover,
  which is exactly why this path exists.

### 12.4 Swapping the module made the history "disappear"

In "write known coordinates", "reuse the local historical coordinate" was greyed out with
"this machine has no history for this module" — half an hour after a coordinate had been measured
with the LG290P on the very same antenna.

The reuse source was filtered by `module_sn`. The intent was to prevent writing another antenna's
coordinate into this receiver, but it also blocked the most common and entirely legitimate case:
same antenna, different module. The program cannot know whether the antenna has moved — that is the
real risk — so substituting its own judgement only blocks correct operations.

The reuse source is now **the most recent record in the database**, regardless of model or serial.
The page lists model, serial, timestamp, lat/lon and ECEF so a human can confirm it is the same
antenna. The "measure it first" hint was also completed: measuring needs a **rover-capable module**
fed by CORS or another NTRIP caster.

### 12.5 Coordinates must be stored in both forms

Sources differ: 1005 carries ECEF, while NMEA, CORS result sheets and vendor tools usually carry
lat/lon/height. Only the ECEF → lat/lon direction existed; a record that arrived as lat/lon had no
ECEF, so reusing it meant converting on the spot — and whoever converts it next may round
differently.

`store._normalize_record()` now handles both directions:

| Input | Stored result |
| --- | --- |
| ECEF only | lat/lon/height derived (existing behaviour) |
| lat/lon only | ECEF computed, `arp_*` filled as well (that is the value written into the module) |
| both present but inconsistent | **ECEF wins**: lat/lon recomputed and a warning logged (ECEF is what goes into 1005) |

Values are quantised to four decimals (0.1 mm), matching RTCM 1005 resolution — anything finer
cannot be broadcast and would only leave unalignable tails in the database. Measured round-trip
error: `9e-11°` in latitude, `4e-10°` in longitude (about 0.01 mm), far below the 1.1 cm calibration
accuracy.

The consistency threshold is 5 cm: supplied lat/lon often has 7–9 decimals, or comes from a
different datum (CGCS2000 versus WGS84 differ by less than 1 cm), so millimetre differences are
expected quantisation. Beyond 5 cm it is not rounding any more — it is two numbers from different
places stitched into one row.

### 12.6 Verification for this round

- Full contract suite: **238 tests pass** (216 previously; 22 new ones cover the five issues above).
- `/api/calibration/options` no longer returns `own_history`; `reuse_history` carries the most recent
  record (2026-11-02 11:59:06 / lg290p / SN 00006FE9326C3E41 / 25.2035236°,110.2774837°).
- `external_rover` + `lc29hbs` → 400 with alternatives; no model selected → 202 accepted.
- The bidirectional conversion is regression-tested against the **values measured on the device**:
  `25.20352374°,110.27748337°,138.316 m` converts to `-2001277.3805, 5416696.8658, 2699549.3066`,
  0.6 mm away from the value read back from the module (four-decimal rounding).
