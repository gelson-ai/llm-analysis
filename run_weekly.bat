@echo off
REM Weekly OpenRouter pipeline refresh. Registered with Windows Task Scheduler.
REM Logs each run's output so you can check whether it succeeded.

cd /d "C:\Users\Mark Gelson\Desktop\Hurdman\Data Analysis\LLM Analysis"

if not exist "logs" mkdir "logs"

set LOGFILE=logs\run_%date:~-4,4%-%date:~-10,2%-%date:~-7,2%.log

call .venv\Scripts\activate.bat
python run_pipeline.py >> "%LOGFILE%" 2>&1

echo Finished at %date% %time% >> "%LOGFILE%"
