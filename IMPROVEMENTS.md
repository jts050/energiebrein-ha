# Versie 1.19.0: reserve, eerlijke kosten en tijdelijke bediening

Deze wijzigingen zijn voorbereid op basis van versie 1.18.0. Ze zijn lokaal getest, maar nog niet in een echte Home Assistant getest. De bestaande tests die elders bij het project horen zijn niet in deze repository aangetroffen; dit concept voegt eigen regressietests toe.

## Onzekerheidsreserve (punt 4)

De bestaande lage/gemiddelde/hoge scenario's blijven bestaan. Optioneel bewaart het plan daarboven extra acculading voor fouten in de zon- en huisvoorspelling van de komende vier uur. De extra reserve is maximaal 10 procentpunt boven de hoogste ondergrens van app en omvormer en blijft minstens twee procentpunt onder de bovengrens. De grens wordt getoond bij Bediening. Zonder leerhistorie gebruikt de reserve 35% relatieve onzekerheid als voorzichtige aanname; een bekende nulafwijking wordt niet als ontbrekende historie behandeld.

De reserve staat standaard **uit** en is instelbaar bij Koppelen → Omvormer en thuisaccu. Het is een vuistregel, geen statistisch betrouwbaarheidsinterval en geen garantie op noodstroom. Meer reserve kan meer stroomkosten betekenen. Een omvormer zonder werkende vasthoudstand kan de reserve alleen in het plan volgen; de live uitleg meldt dit wanneer de reserve ingrijpt. Er worden geen instellingen voor noodstroom of ontlaaddiepte in de omvormer aangepast.

De planner houdt zijn oorspronkelijke energie-as: een accu onder de reserve krijgt geen fictieve startlading. Onder de extra reserve zijn laden en rusten toegestaan, ontladen niet. Bestaande vermogens- en laadgrenzen blijven gelden.

## Kosten (punt 3)

Het kostenoverzicht splitst het berekende accu-voordeel in:

- kasverschil tegenover hetzelfde gemeten verbruik zonder accu, met de toen vastgelegde tarieven;
- geschatte verandering in de waarde van opgeslagen lading;
- geschatte slijtage per gemeten geladen kWh, instelbaar tussen € 0 en € 1;
- netto modelmatig accu-voordeel na slijtage.

De inventariswaarde blijft een schatting uit SOC, capaciteit, gemiddeld dagtarief en ontlaadrendement; dit is geen gerealiseerde factuurkorting. Kosten en het scenario zonder accu blijven gebaseerd op de gemeten netstroom en dezelfde apparaatvraag. Dit is geen bewijs van het aparte effect van alle slimme autoplanning of warmtepompsturing. Laadverliezen zitten al in de gemeten netkosten en worden niet nogmaals afgetrokken.

Nieuwe kwartiermetingen bewaren positieve laadenergie apart: laden en ontladen binnen één kwartier mogen de slijtagegrondslag niet wegstrepen. Een bestaande database krijgt een nullable kolom; oude rijen worden niet achteraf met verzonnen nulslijtage gevuld. Zodra oude rijen ontbreken, is het netto perioderesultaat expliciet onbekend. Niet vastgelegde tarieven worden als geschat gemarkeerd. De jaarprojectie op basis van enkele dagen is uit de kostenweergave verwijderd. Bestaande bruto API-velden blijven behouden voor compatibiliteit met het dashboard en HA-sensoren.

## Tijdelijke bediening (punt 6)

Bij Bediening kies je vooraf 30 minuten, 1, 2, 4 of 24 uur. Elke nieuwe handmatige stand voor accu, laadpaal, warmtepomp of zonnepanelen krijgt een eigen UTC-eindtijd; na afloop gaat dat apparaat terug naar Slim. De eindtijd staat in de kaart en wordt met de stand opgeslagen. Het opnieuw kiezen vervangt de vorige timer, en Slim wist hem meteen. De telefoonsnelkeuzes krijgen standaard twee uur. De auto-optie ‘Eenmalig anders’ en het weekschema blijven apart werken.

Verlopen timers worden bij opstarten, opnieuw plannen en elke regelronde gecontroleerd. Dit is dus geen secondeprecieze klok: uitvoering gebeurt uiterlijk bij de volgende regelronde wanneer de app draait. Oudere opgeslagen handmatige standen zonder eindtijd blijven compatibel; kies ze opnieuw of kies Slim om een timer te gebruiken. Een onleesbare eindtijd valt terug naar Slim. De normale apparaat-, vermogens-, comfort- en licentiebeperkingen blijven actief.

## Validatie en installatie

Run vanuit de repository met de benodigde Python-libraries:

```sh
python -m pytest -q
```

Daarnaast is de frontend met Node op syntax gecontroleerd en is de bediening in een geïsoleerde lokale simulatie bekeken en gebruikt. Er is geen verbinding met Jordy's echte apparaten gebruikt voor deze tests.

Controleer de update eerst met simulatie en een reserve die uit staat. Test in de echte installatie specifiek de vasthoudstand en de terugkeer naar Slim met de aangesloten omvormer. Pas daarna kan de optionele reserve worden ingeschakeld. Deze broncode-zip is geen rechtstreeks importeerbaar Home Assistant-dashboardbestand.
