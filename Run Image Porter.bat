@echo off
REM Launches the Air-Gapped Image Porter with no console window.
REM pythonw runs without a terminal; falls back to python if needed.
start "" pythonw "%~dp0airgap_porter.py" 2>nul || start "" python "%~dp0airgap_porter.py"
