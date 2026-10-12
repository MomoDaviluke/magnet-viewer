@echo off
rem One-command Android release chain: build -> release_check gate -> dist refresh -> upload to GitHub Release.
rem Usage: build_release.bat <version-name> <tag>   e.g.  build_release.bat 1.1.1 v1.1.1
rem Requires: formal keystore at android/magnetviewer-release.jks + android/app/keystore.properties (local, gitignored).
cd /d "%~dp0"
..\.venv\Scripts\python.exe android\tools\publish_apk.py --version-name %1 --tag %2 %3 %4 %5 %6 %7 %8
