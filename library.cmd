@echo off
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0scripts\launch_library.py" %*
) else (
    py -3 "%~dp0scripts\launch_library.py" %*
)
