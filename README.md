# Shimadzu SCL-40 / LC-40i Python Control GUI

Local/LAN Python dashboard for a Shimadzu SCL-40 connected to an LC-40i pump
through the optical REMOTE link. The PC communicates with the SCL-40 over
HTTP/XML on Ethernet without LabSolutions or Clarity.

## Current hardware

- Controller: SCL-40, firmware 1.67
- Pump reported by firmware: LC-40i, Unit A, firmware 1.00, optical address 3
- SCL-40 IP: `192.168.200.99`
- Controller PC Ethernet: `192.168.200.101/24`
- Controller PC Wi-Fi at development time: `172.30.148.225`

## Features

- Detect SCL-40 and connected pumps
- Read Method 0 target flow, Tflow and pressure limits
- SCL web login and Monitor session
- Read pressure, flow and Pump A logical operation state
- Pump START/STOP
- Set Pump A flow in the hard-limited range `0.0000` to `1.0000 mL/min`
- Verify a flow write by immediately reading Method 0 back
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
the bundled Codex Python first, then `py -3`, then `python`.

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

Set Pump A flow while preserving current Tflow:

```xml
<Method><No>0</No><Pumps><Pump><UnitID>A</UnitID><Usual><Flow>0.1000</Flow><Tflow>1.0000</Tflow></Usual></Pump></Pumps></Method>
```

Logout:

```xml
<Login><Mode>-1</Mode></Login>
```

## Safety and known limitations

- Do not infer compatibility from old SCL-10AVP/LC-20 serial protocols.
- Do not add undocumented write fields or automatically test writes.
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

See `CLAUDE_CODE_HANDOFF.md` for implementation details and next work.

