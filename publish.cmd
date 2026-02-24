@echo off
setlocal
set SCRIPT_DIR=%~dp0
if "%~1"=="" (
  echo Usage: publish.cmd [-Version 1.0.0] [-DisplayName "my skill"] [-Slug "skill-name"] [-Token "clh_..."]
  echo If -Version is omitted, script reads semver git tag on HEAD ^(e.g. v1.2.3^).
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%publish.ps1" %*
endlocal
