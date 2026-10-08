@echo off
rem Starts the viewer with working "Schedule settings" (a small helper that listens on this computer only).
rem Keep this window open while you use the page; close it to stop the helper.
cd /d "%~dp0"
python rc_audit_trail.py --serve
pause
