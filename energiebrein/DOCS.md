# Energiebrein

Slim energiemanagement voor je thuisaccu, zonnepanelen, laadpaal en warmtepomp. Het brein draait dag en nacht op je Home Assistant en start vanzelf opnieuw na een herstart of stroomstoring.

## Openen

- In de zijbalk van Home Assistant: **Energiebrein**.
- Op je telefoon: in de **Home Assistant-app**, ook buitenshuis als je Home Assistant van buiten kunt bereiken.
- Thuis kan ook rechtstreeks: `http://homeassistant.local:8099` (of het IP-adres van je Home Assistant met `:8099`).

## Eerste keer

1. Open Energiebrein en ga naar **Koppelen**.
2. Zet **Simulatie** uit en koppel je omvormer, slimme meter en de rest; druk op **Zoek mijn apparaten** of vul de IP-adressen in.
3. Druk op **Opslaan en herstarten**.

## Waar je gegevens staan

Alles staat in de map `addon_configs/local_energiebrein` (te zien via de Samba-add-on of Studio Code Server):

- `config.yaml`: je instellingen
- `data/thuis/geschiedenis.db`: alle metingen per kwartier, waar de AI van leert
- `data/licentie.json`: je abonnement

Deze map zit in elke Home Assistant-back-up. Kom je van de Windows-versie, zet dan je oude `config.yaml` en de map `data` hier neer en herstart de add-on. Dan gaat het brein verder waar het was.

## Op je eigen dashboard

Energiebrein zet elke 30 seconden zijn gegevens als sensoren in Home Assistant. Je vindt ze bij Ontwikkelaarstools → Toestanden door te zoeken op `energiebrein`, en je kunt ze kiezen in elke kaart en automatisering:

`sensor.energiebrein_zon`, `_net`, `_huis`, `_accu_vermogen`, `_accu_lading`, `_auto`, `_warmtepomp`, `_warmtepomp_stand`, `_inkoopprijs`, `_terugleverprijs`, `_accu_actie`, `_laadpaal_actie`, `_reden`, `_kosten_vandaag`, `_bespaard_vandaag`, `_afname_vandaag`, `_teruglevering_vandaag`, `_zon_vandaag` en `_meldingen`.

Na een herstart van Home Assistant zijn ze binnen een halve minuut weer terug. Uitzetten kan met `home_assistant: entiteiten: uit` in `config.yaml`.

## Belangrijk

- **Eén brein tegelijk.** Stop Energiebrein op je pc als het hier draait, anders sturen twee programma's dezelfde accu.
- **GoodWe:** een GoodWe-wifimodule kan maar slecht met twee programma's tegelijk praten. Zie je haperingen en heb je ook de GoodWe-integratie van Home Assistant aanstaan, schakel die dan uit.
- Bij stoppen of updaten zet Energiebrein je accu terug naar de gewone stand (zelfverbruik).
