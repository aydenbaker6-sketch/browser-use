@echo off
cd /d "%~dp0"
where uv >nul 2>nul || (echo Installing uv... & powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" & set "PATH=%USERPROFILE%\.local\bin;%PATH%")
if not exist .env (copy .env.example .env >nul & echo Fill in .env with your Kroger developer app keys, then run this again. & notepad .env & exit /b 1)
uv run main.py %*
pause
