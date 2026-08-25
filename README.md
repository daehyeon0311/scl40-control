# Shimadzu SCL-40 Multi-Pump Python Control GUI

Local/LAN Python dashboard for a Shimadzu SCL-40 connected to LC-40i and
LC-20Ai pumps through the optical REMOTE link. The PC communicates with the SCL-40 over
HTTP/XML on Ethernet without LabSolutions or Clarity.

The pump is not used for chromatography here. It pushes water behind the
plunger of an LCP jet cartridge to extrude sample, so the dashboard is built
around that workflow: fill the reservoir fast, switch to the experiment flow
when the pressure jumps, and stop the pump when it jumps again.

## Current hardware

- Controller: SCL-40, firmware 1.67
- Pump A: LC-40i, Unit A, firmware 1.00, optical address 3
- Pump B: LC-20Ai, Unit B, firmware 1.01, optical address 4
- SCL-40 IP: `192.168.200.99`
- Controller PC Ethernet: `192.168.200.101/24`
- Controller PC Wi-Fi at development time: `172.30.148.225`

## Features

- Detect SCL-40 and connected pumps
- Detect and display every pump reported by SCL-40 Config
- Read Method 0 flow and pressure limits independently by UnitID
- SCL web login and Monitor session
- Read pressure, flow and logical operation state independently for Pump A/B
- Select Pump A or Pump B for details, trend and flow setting
- System-wide Pump START/STOP (`Event.cgi` contains no confirmed UnitID)
- Set the selected pump flow in the range `0.0000` to `5.0000 mL/min`
- Verify a flow write by immediately reading Method 0 back
- Fixed pressure trend chart showing Pump A and Pump B together, with 5, 15 and
  60 minute windows and server-side per-pump history.
- Compact model-specific instrument drawings distinguish LC-40i and LC-20Ai
  pump cards at a glance.
- Multiple GUI clients sharing one SCL session
- Serialize device commands; concurrent commands receive HTTP 409
- Display the last operator IP and command
- Optional same-Wi-Fi access protected by a separate dashboard PIN

## Run on Windows

1. Copy `scl40_access_pin.example.txt` to `scl40_access_pin.txt` and replace its
   contents with a private PIN of at least six characters.
2. Double-click `START_SCL40_GUI.cmd`.
3. Open `http://127.0.0.1:8765/`.
4. Log in to the SCL-40. Factory defaults are `Admin` / `Admin`; installations
   may have changed them.

The GUI uses only the Python standard library. The supplied launcher looks for
`py -3`, then `python`.

## LCP jet run

`LCP JET RUN` in the dashboard automates the extrusion workflow. It runs in the
server, so closing the browser does not stop it.

| Stage | What it does | Leaves the stage when |
|---|---|---|
| 물 채우는 중 | writes the fill flow, starts the pump | pressure ≥ 실험 유량 전환 압력 |
| 실험 중 | writes the experiment flow, pump keeps running | pressure ≥ 실험 끝 판단 압력 → STOP |

Both thresholds are absolute pressures typed in by the operator.

The automatic LCP run is deliberately disabled when more than one pump is
connected. The confirmed START/STOP request operates at system level, so pump
roles must be defined and a per-pump start mechanism must be confirmed before
multi-pump automation is enabled.

For a single-pump configuration, the fill stage watches immediately and switches to the experiment flow on the
first pressure sample at or above `실험 유량 전환 압력`. After that switch the
experiment stage starts watching only once the settle time has passed **and**
the pressure has been seen below its end threshold. That prevents the falling
pressure from falsely ending the run. `절대 압력 상한` aborts the run from any
stage, and each stage has a timeout.

`감시만` mode skips the fill stage for a pump the operator started themselves.

Priority of protection, strongest first:

1. Method `Pmax` in the instrument - the pump stops itself, no PC involved.
2. `절대 압력 상한` in the run controller.
3. The stage thresholds above.

Set `Pmax` on the instrument as well; the software watchdog is not a substitute
for it. `--pressure-ceiling` caps what the dashboard may write to `Pmax`
(default 10.0 MPa).

## Offline simulator

`scl40_sim.py` emulates the SCL-40 HTTP/XML endpoints so the dashboard can be
developed and demonstrated when the instrument is unreachable. It replays only
response shapes already confirmed against the real device and adds a simple
pump model: flow ramps toward the method flow when the pump is on, and
pressure follows flow at roughly 10 MPa per mL/min.

```powershell
python .\scl40_gui.py --simulator --enable-control
```

The dashboard then shows a purple `SIMULATION MODE` banner and a `SIMULATOR`
badge. Simulator credentials are `Admin` / `Admin`.

Run it standalone to point several clients at one fake device:

```powershell
python .\scl40_sim.py --port 9099
python .\scl40_gui.py 127.0.0.1:9099 --enable-control
```

`--touch-logged-in` makes the simulator answer login result 13 so the
touch-screen-conflict path can be exercised.

Simulator output never proves anything about physical instrument behavior.

## Same-Wi-Fi access

The launcher listens on `0.0.0.0:8765`. Run
`ENABLE_SCL40_WIFI_FIREWALL.cmd` once as administrator to create a restricted
firewall rule for the network values present during development. Review and
update the IP addresses in the PowerShell script if the PC Wi-Fi address changes.

Remote clients open `http://172.30.148.225:8765/` and enter the dashboard PIN.
The PIN is separate from the SCL login. Do not commit `scl40_access_pin.txt`.

## Confirmed device requests

Read configuration:

```xml
<Config/>
```

Read current method:

```xml
<Method><No>0</No></Method>
```

Login:

```xml
<Login><Mode>0</Mode><Certification><UserID>...</UserID><Password>...</Password><SessionID/><Result/></Certification></Login>
```

Monitor after login:

```text
POST /cgi-bin/Monitor.cgi/{SessionID}
```

```xml
<Monitor/>
```

Pump ON/OFF:

```xml
<Event><Method><PumpBT>1</PumpBT></Method></Event>
<Event><Method><PumpBT>0</PumpBT></Method></Event>
```

Set a selected pump flow while preserving that pump's current Tflow (Unit A
shown; Unit B uses `B` and the precision returned by its Method response):

```xml
<Method><No>0</No><Pumps><Pump><UnitID>A</UnitID><Usual><Flow>0.1000</Flow><Tflow>1.0000</Tflow></Usual></Pump></Pumps></Method>
```

Write the pressure limits. Element names and their position come from the
instrument's own Method read response; `Flow` and the device-reported `Tflow`
are always resent because that pairing is the confirmed request shape. `Tflow`
is not exposed as a user-writable dashboard parameter. **Not yet executed against
the real instrument** - check the exact request in the log before the first
write:

```xml
<Method><No>0</No><Pumps><Pump><UnitID>A</UnitID><Usual><Flow>0.0460</Flow><Tflow>1.0000</Tflow><Pmax>8.0</Pmax></Usual><Detail><Pmin>0.0</Pmin></Detail></Pump></Pumps></Method>
```

Every write is verified by reading Method 0 back and comparing each field, so a
field the instrument ignores raises an error instead of passing silently.

Logout:

```xml
<Login><Mode>-1</Mode></Login>
```

## Safety and known limitations

- Do not infer compatibility from old SCL-10AVP/LC-20 serial protocols.
- Do not add undocumented write fields or automatically test writes.
- Config, Method and Monitor have confirmed independent Unit A/B entries.
- The confirmed Event START/STOP XML has no UnitID. The GUI therefore labels
  these actions `START ALL` / `STOP ALL` and treats them as system-wide.
- Unit B Method writes have passed simulator tests only. They have not yet been
  sent to the physical SCL-40.
- The physical pump response still requires verification. The SCL monitor has
  reported Pump A `OpState=1`, flow `0.0460`, pressure `0.0 MPa`, and system
  state `401`, but the user reported no visible physical pump motion.
- The SCL firmware permits one control login. Result code 13 means a user is
  already logged in at the SCL touch screen. Log out there before web login.
- LabSolutions must not simultaneously own the workstation/control session.
- Multiple dashboard clients share one SCL login. Commands are serialized, but
  clients should coordinate who is operating the instrument.
- A successful HTTP/XML echo is not sufficient proof of physical execution;
  use Monitor readback and physical observation.
- Pressure-limit writes are not implemented.


