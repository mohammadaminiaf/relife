@echo off
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0scripts\setup_project.py" --app-only %*
) else (
    py -3 "%~dp0scripts\setup_project.py" --app-only %*
)
