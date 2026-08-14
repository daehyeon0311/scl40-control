Shimadzu SCL-40 Local GUI
==========================

Default target: 192.168.200.99
Default local dashboard: http://127.0.0.1:8765/
Same-Wi-Fi dashboard: http://172.30.148.225:8765/
Remote access PIN: see scl40_access_pin.txt

START
-----
Double-click START_SCL40_GUI.cmd

Or in PowerShell:
  cd "C:\Users\IBS\Documents\Codex\2026-08-14\wd\outputs"
  python .\scl40_gui.py 192.168.200.99

Stop the GUI by closing its command window or pressing Ctrl+C.

SAME WI-FI ACCESS
-----------------
Run ENABLE_SCL40_WIFI_FIREWALL.cmd once and accept the Windows administrator
prompt. The firewall rule permits only remote addresses in 172.30.148.0/24 to
reach this PC's Wi-Fi address 172.30.148.225 on TCP port 8765.

CONTROL MODE
------------
START and STOP are enabled by START_SCL40_GUI.cmd. To send one command:
  1. Enter the SCL-40 User ID and password and click LOGIN.
  2. Click START or STOP.
  3. START shows a final safety confirmation; STOP is immediate.

The password is sent only to the SCL-40 and is kept only in memory during the
login request; it is not written to the GUI log. START uses the flow rate
already set on the SCL-40. Set-flow and pressure-limit writes are not enabled yet.
