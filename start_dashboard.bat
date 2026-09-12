@echo off
REM Start the local dashboard server (which provides the on-demand
REM "Refresh data" button) and open it in the default browser.
REM
REM Keep this window open while using the dashboard - closing it stops the
REM server. Opening dashboard\price_performance_final.html directly still
REM works, but the refresh button disables itself because a plain file
REM cannot re-scrape OpenRouter or republish the HTML.

cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [warn] .venv not found - falling back to "python" on PATH.
)

python dashboard\serve.py --open

echo.
echo Server stopped. Press any key to close.
pause > nul
