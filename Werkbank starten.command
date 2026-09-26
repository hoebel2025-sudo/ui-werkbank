#!/bin/zsh
# Startet die lokale UI-Werkbank (http://127.0.0.1:8119) und oeffnet den Browser. Beenden mit Strg+C.
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  ".venv/bin/python" -B werkbank.py start
else
  python3 -B werkbank.py start
fi
result=$?
if [ "$result" -ne 0 ]; then
  echo "Die Werkbank meldet einen Fehler (siehe oben). Bei fehlender Umgebung: python3 werkbank.py setup"
  read "?Fenster mit Enter schliessen."
fi
exit "$result"
