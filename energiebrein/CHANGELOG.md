# Wat er nieuw is

## 1.8.0

- **Conditie van de accu** op Thuis: gezondheid volgens de accu zelf, de gemeten capaciteit (hoeveel kWh er echt in past), het aantal keer dat de accu helemaal rond is geweest, het rendement, de temperatuur en het aantal modules. Per maand wordt de capaciteit bewaard, zodat je ziet of de accu slijt. Een melding als een module wegvalt, de capaciteit onder 80% komt of de accu erg warm of koud is.
- **Negatieve prijzen benutten.** Krijg je geld voor stroom, dan laadt de accu zonder winstdrempel vol uit het net. Je krijgt vooraf een melding op je telefoon (en in het logboek) wanneer je geld krijgt voor stroom of wanneer terugleveren geld kost, zodat je de wasmachine, vaatwasser of auto in die tijd kunt laten draaien.
- **Stormstand** (Koppelen → Slim handelen, standaard uit): bij onweer of zware windstoten in de weersverwachting laadt het brein de accu een paar uur van tevoren vol en houdt hem vol, voor als de stroom uitvalt. Alleen zinvol met een noodstroomgroep op de omvormer.

## 1.7.1

- **Solcast als zonvoorspelling.** Bij Koppelen → Zonnepanelen vul je je Solcast API-sleutel en het resource-ID van je rooftop site in (de hele API-link plakken mag ook). Het brein gebruikt dan Solcast in plaats van forecast.solar. Zuinig met de 10 gratis aanvragen per dag: hooguit 8, om de paar uur, niet 's nachts, en de laatste voorspelling blijft bewaard na een herstart. Lukt Solcast niet, dan valt hij terug op forecast.solar.

## 1.7.0

- **Voorspeld en echt.** Onder Plan → Gemeten zie je nu per dag, 7 of 30 dagen wat het brein vooraf verwachtte (gestippeld) naast wat je huis en je panelen echt deden (doorgetrokken), met hoeveel de voorspelling ernaast zat. Het brein bewaart daarvoor de voorspelling van minstens een uur van tevoren. De vergelijking begint vanaf deze versie; oudere dagen hebben nog geen bewaarde voorspelling.

## 1.6.10

- Op een tablet (staand) is het stroomschema op Thuis groter en staat het in het midden. De lijst "Wat de accu straks doet" en de prijsgrafiek gebruiken de hele breedte.

## 1.6.9

- Klemmenproef: gaat er tijdens stap 1 een apparaat aan of uit (warmtepomp, oven), dan doet hij die stap vanzelf opnieuw in plaats van een onbetrouwbare uitslag te geven.
- Duidelijkere aanwijzing als een klem op de verkeerde fase zit: verplaats de klemmen terwijl een apparaat blijft draaien en kijk in de tabel tot de omvormer hetzelfde ziet als de slimme meter.

## 1.6.8

- **Knop "Klemmen controleren"** bij Koppelen → Omvormer en thuisaccu → Meting en regeling. Een proef van een paar minuten: de accu levert even 2,4 kW (gelijk over de drie fasen), daarna zet je de waterkoker aan (één fase). Energiebrein vergelijkt per fase wat de omvormer en je slimme meter zien, en zegt per klem of hij klopt, andersom zit of op de verkeerde fase zit.

## 1.6.7

- **CT-klemmen testen, per fase.** Bij Koppelen → Meting en regeling zie je live per fase wat je slimme meter en de omvormer meten. Zo zie je meteen welke klem op de verkeerde fase zit of andersom.
- Geen vals alarm "Accu doet iets anders dan Energiebrein vraagt" meer vlak nadat Energiebrein zelf een nieuwe stand heeft gestuurd. De omvormer heeft even nodig om die over te nemen.

## 1.6.6

- **Zonne-overschot gaat altijd eerst de accu in** zolang die ruimte heeft. Dit staat standaard aan, onder Koppelen → Slim handelen. Met saldering levert terugleveren bijna evenveel op als zelf gebruiken, en dan vond het plan opslaan in de accu de moeite niet waard. Nu gaat de zon voor.
- Opgelost: had het plan minder zon verwacht en wilde het de lading vasthouden, dan ging echt zonne-overschot naar het net. Nu telt wat de zon op dit moment over heeft.

## 1.6.5

- Opgelost: de accu kon in stand-by blijven hangen. De GoodWe neemt de stand-by soms pas na een paar seconden over; Energiebrein keek te vroeg, dacht dat het niet lukte en vergat de stand-by weer uit te zetten. Daarna laadde de accu niet meer, ook niet met zonne-overschot. Nu kijkt hij een paar keer na, en staat de accu in stand-by terwijl Energiebrein dat niet vraagt, dan zet hij hem zelf terug op automatisch.
- De meter-melding herkent nu ook als alle drie de fasen evenveel afwijken. Dan ligt het niet aan één klem, maar zien de klemmen de stroom van de omvormer zelf niet.

## 1.6.4

- P1-regeling rustiger en fijner: stapjes van 0,1 kW in plaats van 0,4 kW. Hij komt nu ook op 0 kW uit als de omvormer veel meer laadt dan gevraagd.
- Laadt de omvormer bij de kleinste opdracht al meer dan de zon over heeft, dan wacht de accu. Een beetje terugleveren is goedkoper dan laden uit het net.
- De uitleg bij "Laadt met zonne-overschot" zegt wat er echt gebeurt, bijvoorbeeld hoeveel er nog uit het net komt.
- De melding "De meter van de GoodWe ziet iets anders" vergelijkt nu per fase. Zo zie je welke CT-klem niet klopt (andersom, niet om de kabel of op de verkeerde fase).

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
