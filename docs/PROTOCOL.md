# Working out the SCL-40 control protocol

The SCL-40 has no published control API. This document records how its HTTP/XML
surface was worked out, what has been confirmed on the instrument, and what has
not. The distinction matters: the software refuses to guess, because a guessed
field that the instrument silently ignores looks exactly like success.

## What the instrument actually exposes

| Link | Transport |
|---|---|
| Controller PC ↔ SCL-40 | Ethernet, HTTP/XML over TCP 80 |
| SCL-40 ↔ pumps | Optical REMOTE chain |

Observed on the development system:

| Component | Version / address |
|---|---|
| SCL-40 | Firmware 1.67 · `192.168.200.99` |
| LC-40i | Unit A · firmware 1.00 · optical address 3 |
| LC-20Ai | Unit B · firmware 1.01 · optical address 4 |

TCP 443 is closed; there is no HTTPS. The response header reads
`Server: Shimadzu-LC20A/1.00 (httpd)` even though `Config.cgi` reports the
controller as an SCL-40, so the header cannot be used to identify the model.

**Serial command sets from the SCL-10AVP / LC-20 generation are not assumed to
apply.** They are widely quoted online and are not part of this work.

## Method

### 1. Read-only reconnaissance

`scl40_probe.py` checks TCP reachability and issues plain `GET` requests to a
small allowlist of paths, logging headers and bodies. It sends no credentials,
no XML bodies and nothing that could change state. Everything below started
from those logs.

### 2. The instrument serves its own web UI

The SCL-40 hosts a browser interface for operators. That interface has to speak
the same protocol, so its JavaScript contains the endpoint paths, the XML
document shapes and the numeric result codes. Reading it turned guesswork into
reading comprehension.

This is the single most useful fact about the device: **the protocol
documentation is on the instrument**.

### 3. Fields come from read responses, never from imagination

Every element written back to the instrument is one the instrument itself
returned in a read response, in the position it returned it. `Pmax` is written
inside `<Usual>` and `Pmin` inside `<Detail>` because that is where a Method
read puts them.

### 4. Every write is verified by reading it back

After each write the software reads Method 0 again and compares the fields it
sent. A mismatch raises an error. This is what makes an unsupported field fail
loudly instead of passing silently, and it is the reason the write path can be
extended without access to documentation.

## Endpoints

All are `POST` with `Content-Type: text/xml; charset=UTF-8`.

| Path | Purpose | Session |
|---|---|---|
| `/cgi-bin/Config.cgi` | Controller and connected modules | no |
| `/cgi-bin/Status.cgi` | System summary | no |
| `/cgi-bin/Method.cgi` | Method read and write | write only |
| `/cgi-bin/Login.cgi` | Login and logout | — |
| `/cgi-bin/Monitor.cgi/{SessionID}` | Live values | yes |
| `/cgi-bin/Event.cgi` | System-wide pump start/stop | yes |

### Configuration

```xml
<Config/>
```

`./Info/Ctrl` describes the controller; each `./Info/Pumps/Pump` carries
`UnitID`, `Model`, `SubModel`, `Version`, `Address` and `State`. Enumerating
this list is how the dashboard discovers a second pump rather than assuming one.

### Method

```xml
<Method><No>0</No></Method>
```

Fields confirmed for each pump:

| Path | Meaning |
|---|---|
| `./Usual/Flow` | Flow, mL/min |
| `./Usual/Tflow` | Total flow |
| `./Usual/Pmax` | Pressure ceiling, MPa |
| `./Detail/Pmin` | Pressure floor, MPa |

A flow write, preserving the `Tflow` the device reported. `Flow` and `Tflow`
always travel together: that pairing is the shape confirmed on the instrument.

```xml
<Method><No>0</No><Pumps><Pump><UnitID>A</UnitID>
  <Usual><Flow>0.1000</Flow><Tflow>1.0000</Tflow></Usual>
</Pump></Pumps></Method>
```

A write including the pressure limits:

```xml
<Method><No>0</No><Pumps><Pump><UnitID>A</UnitID>
  <Usual><Flow>0.0460</Flow><Tflow>1.0000</Tflow><Pmax>8.0</Pmax></Usual>
  <Detail><Pmin>0.0</Pmin></Detail>
</Pump></Pumps></Method>
```

Writes are addressed per `UnitID`, so one pump can be changed without touching
the other.

### Login

```xml
<Login><Mode>0</Mode><Certification>
  <UserID>...</UserID><Password>...</Password><SessionID/><Result/>
</Certification></Login>
```

Success means `./Certification/Result` is `0` **and** `SessionID` is non-empty.

| Result | Meaning | Source |
|---|---|---|
| 0 | Success | instrument |
| 1 | Unknown user | device UI |
| 2 | Wrong password | device UI |
| 4 | Already logged in | device UI |
| 5 | Another web session holds control | device UI |
| 6 | Analysis running | device UI |
| 7 | Insufficient rights | device UI |
| 9 | Same application already running | device UI |
| 10 | System locked | device UI |
| 12 | LC workstation connected | device UI |
| 13 | A user is logged in at the touch screen | **instrument** |

Result 13 is the one operators meet in practice: the controller allows a single
control session, so the front panel has to be logged out first.

Logout:

```xml
<Login><Mode>-1</Mode></Login>
```

### Monitor

The session ID is part of the path, not the body:
`POST /cgi-bin/Monitor.cgi/{SessionID}`

```xml
<Monitor/>
```

| Path | Meaning |
|---|---|
| `./SysMon/Method/Pumps/Pump/Press` | Pressure |
| `./SysMon/Method/Pumps/Pump/Flow` | Measured flow |
| `./Config/Situation/Pumps/Pump/OpState` | Operation state, `1` while running |
| `.//SysState` | System state code |

Monitor is a status channel. It is not a route to detector data, and nothing
here acquires a chromatogram.

### Pump events

```xml
<Event><Method><PumpBT>1</PumpBT></Method></Event>   <!-- START -->
<Event><Method><PumpBT>0</PumpBT></Method></Event>   <!-- STOP -->
```

The confirmed payload carries **no `UnitID`**, so this is a system-wide command:
on a two-pump system it moves both. That is why the automatic run refuses to
start when more than one pump is connected — the automation cannot claim a
per-pump start it does not have.

## Still unknown

- Whether `Config` exposes module types other than pumps. The current system has
  only pumps connected, so the question cannot be answered from it.
- The full Method schema, including any gradient time-program nodes.
- Event codes other than `PumpBT`. The device UI references `SelModuleNo` and
  `PurgeAct` for purge, but no purge write has been sent or validated.
- The real resolution of the reported pressure value.

Each of these is answerable by downloading the instrument's own web assets and
reading them, which is a read-only operation.

## The open question

A START request has returned HTTP 200 with `Monitor` subsequently reporting
`OpState=1`, without the pump being observed to physically run. The cause is not
established.

This is why the software treats transport success and physical action as
separate claims:

- a write is only accepted after a readback comparison
- the automatic run will not monitor a stage until it has actually seen the pump
  reported as running, and fails safe if that confirmation never arrives
- the interface never reports a command as physically executed on the strength
  of an HTTP status

**HTTP 200 is not evidence that anything moved.**
