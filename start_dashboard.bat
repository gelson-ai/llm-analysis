@echo off
REM Start the local dashboard server (which provides the on-demand
REM "Update All Latest AI Data" button) and open it in the default browser.
REM
REM Keep this window open while using the dashboard - closing it stops the
REM server. Opening the built HTML directly still works, but the refresh
REM button disables itself because a plain file cannot re-scrape OpenRouter
REM or republish the HTML.
REM
REM --max-age-hours 192: the server default is 72h, which refuses to rebuild
REM chat data older than three days. Real data here arrives on a weekly
REM cadence, so with the default the chat half of a refresh fails until a
REM fresh fetch has run. 192 matches what CI uses.

cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [warn] .venv not found - falling back to "python" on PATH.
)

python dashboard\serve.py --open --max-age-hours 192

echo.
echo Server stopped. Press any key to close.
pause > nul
