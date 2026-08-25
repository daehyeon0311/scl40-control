# Claude Code handoff

## Objective

Continue development and verification of the Python SCL-40 multi-pump controller
without LabSolutions. Preserve the existing read-only discovery and never
guess undocumented command values.

## Source files

- `scl40_gui.py`: SCL client, XML parsing, HTTP API and local server
- `scl40_gui.html`: dashboard markup
- `scl40_gui.css`: single stylesheet, light instrument-console styling
- `scl40_gui.js`: polling, SVG trend chart, login and control interactions
- `scl40_probe.py`: original safe endpoint probe
- `scl40_sim.py`: offline SCL-40 emulator with an LCP jet cartridge model
- `scl40_jetrun.py`: server-side fill/run/stop state machine
- `START_SCL40_GUI.cmd`: Windows launcher
- `ENABLE_SCL40_WIFI_FIREWALL.ps1/.cmd`: restricted LAN firewall setup

## Confirmed behavior

- TCP/HTTP connectivity works at `192.168.200.99:80`; HTTPS 443 is closed.
- Server header is `Shimadzu-LC20A/1.00 (httpd)` even though Config identifies
  the controller as SCL-40.
- `Config.cgi`, `Status.cgi`, `Method.cgi`, `Login.cgi`, `Monitor.cgi/{session}`
  and `Event.cgi` are confirmed from the live SCL-40 JavaScript and responses.
- Login `Admin/Admin` succeeded after the user logged out from the touch screen.
- Login result 13 is the live SCL message: user already logged in with touch
  screen; web login is refused.
- Before the latest restart, START returned HTTP 200 and Monitor reported
  `OpState=1`, but the user reported no visible physical pump motion.
- An earlier single-pump Method 0 readback was Flow `0.0460`, Tflow `1.0000`,
  Pmax `10.0`. After adding Unit B, the latest live read was Unit A Flow
  `5.0000` and Unit B Flow `5.000`, both with Pmax `10.0`.
- Config now reports LC-40i Unit A (address 3) and LC-20Ai Unit B (address 4).
- Live Method/Monitor reads confirm separate UnitID A/B values. The Event
  START/STOP request has no confirmed UnitID and must be treated as global.

## Recent implementation

- SET FLOW has no browser confirmation popup per user request.
- START retains a confirmation; STOP is immediate.
- Flow is limited server-side to `0.0000..5.0000 mL/min`, serialized with the
  precision observed for the selected UnitID, and verified by Method readback.
- The dashboard has independent Pump A/B cards, per-unit trend history and a
  selected-unit flow target. START/STOP are labeled START ALL/STOP ALL.
- LCP JET RUN is locked when multiple pumps are connected because its Event
  request cannot yet target one pump safely.
- Multiple clients are supported through one backend session.
- START, STOP and SET FLOW share a non-blocking command lock. A colliding
  command returns HTTP 409.
- Snapshot includes last operator IP, action, timestamp, busy state and result.
- Non-loopback API requests require `X-SCL40-PIN`; local loopback is exempt.
- The real PIN file is ignored by git. Never commit or print SCL passwords.

## Highest priority next steps

0. Multi-pump write verification, only after showing the exact XML and getting
   operator approval:
   a. Change only Unit B flow by a small safe amount and verify Method/Monitor
      plus the LC-20Ai front panel.
   b. Confirm experimentally whether system Event START/STOP really operates
      both pumps. Do not assume a per-pump Event field.
1. First automated-run instrument session, in order:
   a. Log in, read Method 0, note the working pressure at the fill flow and at
      the experiment flow. Those numbers set the two run thresholds.
   b. Write `Pmax` alone through METHOD PARAMETERS and confirm the readback.
      This is the first time the `Usual/Pmax` write path touches hardware.
   c. Run `감시만` mode once on a pump the operator started manually, so the
      controller only has to issue STOP.
   d. Only then run the full LCP JET mode.
2. Capture the exact Method response and post-write Method readback.
3. Compare both pump front-panel values and REMOTE indicators with Monitor XML.
4. Determine whether SCL web control requires an additional documented control
   ownership transition distinct from login. Do not invent one.
5. Add structured, redacted protocol logs that include response XML but never
   passwords, session IDs or the dashboard PIN.
6. ~~Add an explicit logout on graceful server shutdown if a session is active.~~
   Done: `main()` releases an active session in its `finally` block.
7. Add unit tests for XML builders/parsers, login result codes, flow limits,
   PIN authorization and concurrent command locking.

## Purpose

The pump feeds an LCP jet cartridge, not a column. There is no detector and no
chromatography. What matters is a stable low flow and stopping the pump on the
end-of-sample pressure rise.

## Safety requirements

- The LCP jet run controller (`scl40_jetrun.py`) is the one deliberate
  exception to "never send commands automatically": the operator arms it per
  run with an explicit confirmation, and it issues exactly one flow switch and
  one STOP. Everything else still requires an operator action.
- `Pmax` on the instrument remains the real protection. The watchdog is an
  earlier, software-side trigger, not a replacement.
- Never automatically send START, STOP, SET FLOW or pressure-limit writes
  outside that armed run.
- Keep multi-pump LCP automation locked until pump roles and safe targeting are
  explicitly defined.
- Show any new exact write request to the user before first implementation/test.
- Do not use LC-20/SCL-10AVP serial commands on LC-40i.
- Do not claim physical success from HTTP 200 alone.
- Avoid force-login or session takeover unless the user explicitly approves it.
- Keep LAN access subnet-limited and PIN-protected.

## Validation commands

```powershell
python -m py_compile .\scl40_gui.py .\scl40_probe.py
python .\scl40_gui.py 192.168.200.99 --enable-control --bind 0.0.0.0 --access-pin-file .\scl40_access_pin.txt
```

Local status:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/snapshot
```

Do not call write endpoints from automated tests against the real IP. Use a
fake SCL40Client for all write-path tests.

