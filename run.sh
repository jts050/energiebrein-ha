#!/bin/sh
# Energiebrein in Home Assistant. Instellingen en gegevens staan in /config (in Home Assistant: addon_configs/…_energiebrein),
# zodat ze een update, herstart of back-up overleven. Stopt het programma onverwacht, dan start het na 10 seconden opnieuw.
mkdir -p /config/data
cd /app
while true; do
  python start.py --config /config/config.yaml --poort 8099
  echo "Energiebrein is gestopt (code $?). Over 10 seconden start het opnieuw."
  sleep 10
done
