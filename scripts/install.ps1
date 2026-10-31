$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 venv creation failed' }
& .\.venv\Scripts\python.exe -m pip install '.[drive]'
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
if (-not (Test-Path config.toml)) { Copy-Item config.example.toml config.toml }
& .\.venv\Scripts\python.exe -m unittest discover -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
