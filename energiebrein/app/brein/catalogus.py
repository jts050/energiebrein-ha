"""Overzicht van populaire merken: hoe ze gevonden worden en hoe ver de koppeling is.

status:
  sturen   = uitlezen én aansturen werkt
  meten    = uitlezen werkt, aansturen volgt
  ocpp     = aansturen via OCPP (laadpaal verbindt met het brein)
  gevonden = wordt herkend in je netwerk, koppeling volgt nog
  account  = koppelen via je account bij de fabrikant/leverancier
"""
from __future__ import annotations

CATALOGUS = [
    # ---------------- omvormers en thuisaccu's ----------------
    {"soort": "omvormer", "merk": "GoodWe", "status": "sturen", "type": "goodwe", "hoe": "Lokaal via wifi-module (UDP)"},
    {"soort": "omvormer", "merk": "Huawei SUN2000 / LUNA", "status": "sturen", "type": "huawei", "hoe": "Modbus TCP via SDongle; sturen met installateurswachtwoord"},
    {"soort": "omvormer", "merk": "Victron (GX)", "status": "sturen", "type": "victron", "hoe": "Modbus TCP op de GX, sturen via ESS-netsetpoint"},
    {"soort": "omvormer", "merk": "Sungrow", "status": "sturen", "type": "sungrow", "hoe": "Modbus TCP via WiNet-S of LAN"},
    {"soort": "omvormer", "merk": "SolarEdge", "status": "sturen", "type": "solaredge", "hoe": "Modbus TCP; sturen als Storage Control op Remote staat"},
    {"soort": "omvormer", "merk": "SMA", "status": "sturen", "type": "sma", "hoe": "Modbus TCP; laden en vasthouden (leveren gaat via zelfverbruik)"},
    {"soort": "omvormer", "merk": "Fronius Gen24", "status": "sturen", "type": "fronius", "hoe": "Uitlezen via Solar API, sturen via Modbus; laden en vasthouden"},
    {"soort": "omvormer", "merk": "Sessy", "status": "sturen", "type": "sessy", "hoe": "Lokale API met gebruiker en wachtwoord van de sticker"},
    {"soort": "omvormer", "merk": "Enphase IQ Gateway", "status": "meten", "type": "enphase", "hoe": "Lokale API, inloggen met Enlighten-account"},
    {"soort": "omvormer", "merk": "Deye (3-fase hybride)", "status": "meten", "type": "deye", "hoe": "Solarman-wifistick met serienummer"},
    {"soort": "omvormer", "merk": "Growatt / Solis / Sofar", "status": "gevonden", "hoe": "Solarman/ShineWiFi-dongle; andere registerkaart"},
    {"soort": "omvormer", "merk": "Zonneplan Nexus", "status": "account", "hoe": "Gesloten systeem, alleen via Zonneplan"},
    # ---------------- slimme meters ----------------
    {"soort": "p1", "merk": "HomeWizard P1", "status": "meten", "type": "homewizard", "hoe": "Lokale API (aanzetten in de app)"},
    {"soort": "p1", "merk": "Youless", "status": "meten", "type": "youless", "hoe": "Lokale webpagina"},
    {"soort": "p1", "merk": "P1 Monitor", "status": "meten", "type": "p1monitor", "hoe": "Lokale API"},
    {"soort": "p1", "merk": "Shelly Pro 3EM / EM", "status": "meten", "type": "shelly_em", "hoe": "Lokale API"},
    {"soort": "p1", "merk": "SlimmeLezer / ESPHome", "status": "meten", "type": "esphome", "hoe": "ESPHome-API, eventueel met encryptiesleutel"},
    {"soort": "p1", "merk": "Tibber Pulse", "status": "meten", "type": "tibber", "hoe": "Live via je Tibber-code"},
    # ---------------- laadpalen ----------------
    {"soort": "laadpaal", "merk": "Alfen Eve", "status": "ocpp", "type": "ocpp", "hoe": "OCPP-adres instellen in Alfen ACE Service Installer"},
    {"soort": "laadpaal", "merk": "Easee", "status": "account", "type": "easee", "hoe": "Met je Easee-account (e-mail en wachtwoord); uitlezen en laadstroom sturen via de Easee-cloud"},
    {"soort": "laadpaal", "merk": "Zaptec", "status": "ocpp", "type": "ocpp", "hoe": "OCPP via de Zaptec-portal (installateur)"},
    {"soort": "laadpaal", "merk": "Wallbox", "status": "ocpp", "type": "ocpp", "hoe": "OCPP in de Wallbox-app"},
    {"soort": "laadpaal", "merk": "Peblar", "status": "ocpp", "type": "ocpp", "hoe": "OCPP in de webinterface"},
    {"soort": "laadpaal", "merk": "ABL", "status": "ocpp", "type": "ocpp", "hoe": "OCPP-backend instellen"},
    {"soort": "laadpaal", "merk": "EVBox", "status": "ocpp", "type": "ocpp", "hoe": "OCPP-backend instellen (installateur)"},
    {"soort": "laadpaal", "merk": "Etrel / Ecotap / Smappee", "status": "ocpp", "type": "ocpp", "hoe": "OCPP-backend instellen"},
    {"soort": "laadpaal", "merk": "go-e Charger", "status": "sturen", "type": "goe", "hoe": "Lokale API v2 (aanzetten in de go-e app)"},
    {"soort": "laadpaal", "merk": "Tesla Wall Connector", "status": "meten", "type": "tesla", "hoe": "Lokaal uitlezen; sturen kan Tesla alleen via de auto"},
    # ---------------- warmtepompen ----------------
    {"soort": "warmtepomp", "merk": "Elke warmtepomp met SG-ready", "status": "sturen", "type": "sgready_shelly", "hoe": "Twee Shelly-relais op de SG-ready-ingangen"},
    {"soort": "warmtepomp", "merk": "Daikin Altherma", "status": "sturen", "type": "sgready_shelly", "hoe": "Via SG-ready (de LAN-adapter volgt)"},
    {"soort": "warmtepomp", "merk": "Vaillant / Bosch / Nibe / Panasonic", "status": "sturen", "type": "sgready_shelly", "hoe": "Via SG-ready; zonder SG-ready alleen via de fabrikant-cloud"},
    # ---------------- energieleveranciers ----------------
    {"soort": "leverancier", "merk": "Tibber", "status": "account", "type": "tibber", "hoe": "Persoonlijke code van developer.tibber.com: je echte prijs per kwartier"},
    {"soort": "leverancier", "merk": "Frank, ANWB, NextEnergy, EnergyZero, Zonneplan, Vandebron", "status": "openbaar", "hoe": "Openbare tarieven + beursprijs, geen inlog nodig"},
]

STATUS_TEKST = {"sturen": "Uitlezen en sturen", "meten": "Uitlezen", "ocpp": "Sturen via OCPP",
                "gevonden": "Wordt herkend, koppeling volgt", "account": "Via account", "openbaar": "Openbare tarieven"}
