@echo off
cd /d "%~dp0"
python --version >nul 2>&1 || (echo [ERROR] Python not found. Install Python 3.10+ from python.org ^(check "Add Python to PATH"^). & pause & exit /b 1)
python -c "import streamlit, pandas, requests, FinanceDataReader, pykrx" >nul 2>&1 || (echo Installing packages for first run... & python -m pip install -r requirements.txt || (pause & exit /b 1))
python -m streamlit run app.py
pause