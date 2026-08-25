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

MULTI-PUMP CONTROL MODE
-----------------------
The dashboard displays LC-40i Unit A and LC-20Ai Unit B as separate pump cards.
Select a card to view its pressure/flow trend or set that pump's target flow.

START ALL and STOP ALL are enabled by START_SCL40_GUI.cmd. The confirmed SCL-40
Event request has no UnitID, so these buttons affect the pump system rather than
one selected pump. To send one command:
  1. Enter the SCL-40 User ID and password and click LOGIN.
  2. Click START ALL or STOP ALL.
  3. START ALL shows a final safety confirmation; STOP ALL is immediate.

The password is sent only to the SCL-40 and is kept only in memory during the
login request; it is not written to the GUI log. START ALL uses the flow rates
already set on the SCL-40. Automatic LCP JET RUN is disabled while two pumps are
connected until safe per-pump roles and start behavior are confirmed.
