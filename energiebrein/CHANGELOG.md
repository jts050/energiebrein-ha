# Wat er nieuw is

## 1.6.3

- P1-regeling opnieuw gebouwd: hij stelt het opdrachtvermogen stap voor stap bij tot de slimme meter op 0 kW staat. Dat werkt ook als de omvormer het opgegeven vermogen anders telt dan gedacht. Zonne-overschot gaat zo de accu in, zonder stroom uit het net.

## 1.6.2

- Bij terugverkopen toont Thuis wat er echt gebeurt (gemeten), niet wat er gevraagd is.
- Levert de accu minder dan gevraagd omdat de accu zelf (de BMS) niet meer toestaat, dan zegt het brein dat, en welk ontlaadvermogen je het beste instelt zodat het plan klopt.

## 1.6.1

- De melding "De meter van de GoodWe ziet iets anders dan je slimme meter" komt pas als het verschil een paar minuten blijft. Eén meting, bijvoorbeeld net als de waterkoker aangaat, gaf een vals alarm: de slimme meter meet elke seconde, de omvormer loopt een paar seconden achter.
- Lukt het uitlezen van de ondergrens van de omvormer een keer niet, dan houdt het brein de laatst bekende grens aan. Zo komen er geen meldingen "opgelost" en weer "terug" achter elkaar.
- Een melding "opgelost" op je telefoon zegt nu duidelijk dat het niet meer geldt.
- "Houdt lading vast" zegt nu wanneer stroom duurder wordt en wat die dan kost.

## 1.6.0

- **Slim handelen.** De accu laadt en ontlaadt alleen als dat per kWh minstens de ingestelde winst oplevert (standaard € 0,03), bovenop laadverlies en slijtage. Geen rondjes accu meer voor een paar cent verschil.
- **Terugverkopen aan of uit.** Bij Koppelen → Omvormer en thuisaccu kies je of de accu stroom aan het net mag verkopen, of alleen je eigen huis voedt.
- **De echte ondergrens telt.** Stopt de omvormer zelf eerder (bijvoorbeeld bij 15%), dan rekent het plan daarmee. De melding met de knop om de grens gelijk te zetten verschijnt nu ook bij een klein verschil.
- **Eerlijke besparing.** "Bespaard door de accu" telt de stroom die nog in de accu zit mee. Een nacht goedkoop laden voor de volgende ochtend lijkt dus geen verliesdag meer.
- **Thuis: wat de accu straks doet.** De komende stappen in gewone taal, met tijd en prijs. De prijslijn laat zien wanneer de accu laadt, het huis voedt of verkoopt.
- **Plan: duidelijker.** De lading van de accu staat in een eigen strook met de grenzen erin. Verkopen aan het net is gestreept, het huis voeden geel.
- **Uitleg klopt met wat er gebeurt.** Geen "houdt lading vast" meer met daaronder "accu voedt het huis".
- Opgelost: bij Koppelen stond "de accu wordt gebruikt tussen 0% en 100%" terwijl je 10% en 90% had ingesteld.
- De zoncorrectie die het brein leert, blijft bewaard na een herstart.

## 1.5.1

- Uitleg in het plan zegt "accu is leeg" in plaats van "bewaart lading" als de accu op de ondergrens staat.

## 1.5.0

- P1-regelmodus: Energiebrein kan de accu zelf regelen op de slimme meter, als de CT-meter van de omvormer niet goed meet.
- Slijtage van de accu telt mee in het plan.

## 1.4.2

- De accu stopt met laden op de ingestelde bovengrens; de zon voedt dan het huis.
