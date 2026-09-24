# xupertrade: master-roadmap för "next level"

*Master 02ed3c1, 2026-09-24. Faktagranskning samma dag: 24 bärande påståenden prövades av två oberoende granskare vardera; 18 bekräftades, 6 bekräftades delvis och är rättade, 0 motbevisades. Planen bygger på fyra perspektiv (kvant, tillförlitlighet, produkt och plattform). Varje perspektiv har granskats för fakta, för pengasäkerhet och för ROI för en ensam operatör. Underlaget är dessutom tolv lagerkartor och en fullständighetsgranskning. Påståenden som visade sig vara fel eller redan åtgärdade har strukits eller rättats (se bilagan). Insatsnivåerna följer ROI-granskningen.*

> **Publiceringsregel (CLAUDE.md § 0).** Repot är publikt, och dokumentet är skrivet så att det kan committas som det står. Viss information står därför inte här utan i **bilaga P**. Det gäller två slags uppgifter:
> - Det som gör hosten lättare att angripa medan luckorna är öppna: den konkreta attackkedjan med fil och rad, samt hostens patch- och filtillstånd.
> - Det som identifierar operatörens pengar: mainnet-kontots saldohistorik och nyckeldatum, vault-innehaven och beloppen från operatörens egen handel 2025.
>
> Huvudsessionen skriver bilaga P i vecka 1 (NU-8) och den hålls utanför git. Säkerhetsdelen får flyttas in i repot först när NU-8 och NA-15 är klara.

---

## 1. Sammanfattning

Mycket i xupertrade fungerar redan, men bara när någon kör det för hand. Ingenting av det upprätthålls automatiskt.

- **Det som finns:** 1 188 gröna bottester, grindar som stänger vid fel, paritetskontroll efter varje trade och septemberfixarna #167–#176.
- **Det som saknas:**
  - CI kör bara hemlighetsskanning.
  - Backuperna tas dagligen av PBS på containernivå, men Postgres kopieras bara kraschkonsistent och ingen återställning har testats.
  - Inga stopp ligger på börsen.
  - Telegram, digesten, HODL och vault-scannern körs i mainnet-botens process. Trade- och fellarm från testnet och paper går ut den vägen, men digesten rapporterar bara det tomma mainnet, och allt tystnar om mainnet-boten stoppas. Bot-down-larmen går redan förbi via dashboardens watchdog.

Besluten vilar på siffror som är fel eller ofullständiga:

- Funding har en enda rad sedan april, och paper modellerar ingen funding alls.
- 29 % av de historiska testnet-stängningarna är fantomer.
- Avgifterna uppskattas i stället för att läsas från fills.
- Dashboarden visar $10 000 som inte finns.

Med de siffrorna har ingen strategi påvisbar edge:

- Paper ger +$0,35 per trade (95 % KI −0,80…+1,67).
- Testnet har tappat 12,9 % sedan start medan BTC steg 7,6 %.
- Alfan mot BTC/ETH har t=0,63 på paper och t=−1,37 på testnet.
- kalman_breakout är den enda stadiga bidragsgivaren. Den har t≈1,0 och ett bootstrap-KI på [−1,83; 4,19]. Bland 22 strategier utan edge är den förväntade bästa Sharpe ungefär 2,6–2,75 på samma period, så kalman går inte att skilja från brus.

Mainnet handlar inte. Operatörens egna vault-innehav stäms inte av mot scannerns filter, och inget larm säger till när ett innehav underkänns (detaljer i bilaga P).

**Det viktigaste** är att inom tio veckor få ett ärligt och reproducerbart svar på om någon strategi har edge efter avgifter och funding. Fram till dess ska riktiga pengar hållas borta från en motor vars stopp bara finns i minnet.

**Målbilden** är en bok på högst sju strategier, var och en med dokumenterad out-of-sample-evidens. Ledgern ska stämma mot börsen på dollarn och stoppen ska ligga på börsen. Larmen ska köras utanför hosten, en återställning ska inte kunna likvidera och CI ska grinda varje merge. Det ska vara ett system som en person med agenter kan ändra varje dag utan att gissa.

**Planen har tre steg:**

1. **Säkerhetsgolv, vecka 1–4.** 27 PR:er, de flesta små. 11 av dem får full review.
2. **Forskningsspåret, vecka 4–9 (NA-1, NA-2).** Det besvarar edge-frågan och löper parallellt med steg 1, eftersom det inte rör orderstigen.
3. **Beslutspunkt B1, senast 2026-12-04.** B1 avgör edge, mainnet och fler tenanter.

Den kritiska vägen till B1 är 35 PR:er. Resten av Nästa görs när kapaciteten räcker och påverkar inte B1-datumet. Dit hör kontrollpanelen, E2E-testet, deploy per SHA, intern segmentering, den länkade ledgern och avregistreringen av strategier.

Följande startar först efter ett ja i B1: netting-executorn, WebSocket-fillströmmen, per-tenant-plattformen, maker-exekvering, portföljbacktesten och HIP-3-forskningen. Blir svaret nej fryses strategiforskningen, paper fortsätter som framåtbevis och mainnet finansieras inte.

---

## 2. Nuläge per nivå

### Motor
- **Motorn pollar.** Den är en asyncio-loop per container med 60 s polling (main.py:322-333).
  - Ungefär 99 % av utvärderingarna ser en bar som inte har ändrats.
  - En signal kommer 0–76 s efter bar-close.
  - `runner.py` är 2 613 rader, och `_execute_signal` ensam är 453.
- **Exits fungerar bara så länge tick-loopen lever:**
  - En pausad tick hoppar över alla strategier, även deras exits (runner.py:826-827).
  - Redis-läsningar saknar felhantering. Ett fel avbryter hela ticken, inklusive alla stopp (runner.py:752, 789-791).
  - Disable, rate-larmet och allowlisten tar bort en strategi ur ticken även när den har en öppen position (api.py:443-457, runner.py:2290, strategy_allowlist.py:44-51).
  - En avvisad CLOSE loggas bara (runner.py:1492-1499).
  - En tick har ingen tidsgräns. När HL inte svarar tar varje candle-hämtning ungefär 48 s: 15 s timeout gånger 3 försök med backoff, hårdkodat i feed.py:58-77. Strategierna körs en i taget, så en tick med 15 strategier kan ta ungefär 12 minuter utan en enda stoppkontroll.
- **Trailing-state sparas bara vid OPEN** (runner.py:1604-1629). Varje deploy lossar därför stopp som har dragits åt.
- **Hävstången per coin** tas som max över alla strategier, även avstängda (main.py:279-281). ETH körs därför med 8x cross på testnet, satt av den avstängda volatility_breakout. Det påverkar marginalreservationen men inte positionsstorleken.
- **Redis-state är nycklat per mode, inte per tenant** (control.py:23-38, paper.py:47, bus.py:18-19). Det är latent i dag eftersom beta-tenanten har 0 bottar.
- **Coin-grinden delar ut slots i registerordning.** kalman_breakout nekades 53 gånger på 28 h för att sma_rsi höll ETH.
- **Styrkor:**
  - Kill switch och daily-loss stänger vid fel och överlever omstart.
  - Paritetskontrollen gav 0 avvikelser på 28 h.
  - Det finns 259 motortester.

### Exekvering och börs
- **Alla order är taker IOC** på mid ±0,5 %. Ingen order har reduce_only, cloid eller tpsl (0 träffar i grep; hyperliquid.py:551-559).
- **En stängning kan öppna motsatt sida.** När börsen är platt eller på motsatt sida skickar `_resolve_close_size` hela DB-storleken (runner.py:2507-2515).
- **Avgifter räknas i stället för att läsas.** Avgiften beräknas som 0,045 % × pris × storlek (runner.py:1521) och läses inte från fills. Entry-avgiften saknas i realized PnL (runner.py:1566-1577).
- **REST-vikten mot HL** uppskattas till 77–96 % av taket på 1 200/min per IP. Kartorna är oeniga, och siffran är inte mätt.
  - En 429 ger en tom frame, och då hoppas strategins stoppkontroll över (feed.py:82-87).
  - Den enda 429 som faktiskt har observerats kom från vault-API:t.
- **Testnet kan inte förhandsvisa mainnet.** Signalerna räknas på mainnet-candles, men ordrarna fylls på testnets bok.
  - Tre mätningar 2026-09-24 gav 0,33–0,63 % mellan testnet och mainnet på BTC/ETH, beroende på ögonblicksbild. På HYPE var skillnaden −35,5 %.
  - Testnets bok var 34–460× tunnare.
- **Mainnets nyckelslot läses aldrig.** Boten läser bara `HYPERLIQUID_PRIVATE_KEY` (config.py:43-47). Det finns en TODO om detta i secrets/[key]/route.ts:50-53.

### Strategier
- **Registret:** 22 strategier (20 Pine-portar och 2 egna).
  - 21 av 22 handlar BTC, ETH eller SOL, som har daglig korrelation 0,84–0,90.
  - 10 av 22 saknar stop-loss. Hit hör 4 av de 7 kandidater som planen behåller: kalman_breakout, cdc_macd, macd_zero och sma_rsi.
  - bb_short är en naken short och den enda strategin på mainnets allowlist.
- **8 Pine-portar beslutar en bar sent.** Nio strategifiler tar bort sista baren en gång till efter runnern (t.ex. kalman_breakout.py:152, cdc_macd.py:55). För de åtta Pine-portarna kommer entréer och signalbaserade exits därför en hel timeframe sent: 1d för cdc_macd och macd_zero, 4h för keltner_breakout, pivot_supertrend och hash_momentum, 1h för ema_crossover, kalman_breakout och penguin_volatility. Den nionde är egna vvv_hedge. En bars förskjutning flyttar backtestresultatet 15–30 procentenheter.
- **11 av 20 portar kör en coin eller timeframe som källartikeln aldrig mätte.** Ett exempel är kalman, som mättes på ETH 15m men körs på ETH 1h.
- **Några strategier lever på avgifterna:**
  - hash_momentum betalar avgifter på 102 % av bruttot.
  - daily_long_0830 står för 39,5–46 % av testnet-avgifterna och korrelerar 0,99 mot BTC.
  - Rekommendationen från 2026-09-15 att stänga av båda genomfördes aldrig.
- **kalmans defaultparametrar sitter på en skarp topp.** Defaulten ger +46 %, grannarna +8…+19 %.

### Forskning och validering
- **Backtestern har inte ändrats sedan 2026-05-02.** Fönstren kortas tyst: `--days 180` utvärderar 44 d på 15m och 130 d på 1d. Det beror på taket på 4 500 bars (__main__.py:43-52) och på att värmningen tas ur fönstret.
- **Valideringen saknas helt:**
  - Det finns ingen out-of-sample-test, walk-forward, bootstrap eller multiple-testing-korrektion.
  - Funding förekommer inte alls i koden.
- **Backtestern kör en strategi i taget och modellerar inte coin-grinden.** I live skulle grinden ha blockerat 58–67 % av de backtestade entréerna.
- **Headline-måtten styrs av positionen som är öppen vid slutet.** ema_crossover har 0 vinster och 10 förluster men visar +0,99 %.
- **Nästa backtest kommer inte att sparas.** tenant_id är NOT NULL sedan migration 0011 men sätts aldrig (repo.py:579-602), och CLI:t sväljer felet med exit 0. Inga resultat har gått förlorade ännu: ingen backtest har körts sedan 2026-05-01, tio dagar före migrationen.
- **Delar av historiken saknar HL-data.** HL:s dagscandles före 2023-02-26 har n=0, alltså inga HL-trades. "5-årsbevisen" för cdc_macd, macd_zero, sma_rsi och ath_breakout vilar delvis på priser från okänd källa.

### Live-resultat
- **Testnet och paper mot BTC:**

  | Mode | Utveckling | Sharpe | BTC samma period |
  |---|---|---|---|
  | Testnet | 995 → 867 (−12,9 %) | −1,41 | +7,6 % |
  | Paper | +1,2 % | 1,25 | +9,8 % |

  Paper hade i snitt bara 4,6 % bruttoexponering.
- **Alfa mot BTC/ETH:** t=0,63 på paper och t=−1,37 på testnet. Varken edge eller anti-edge går att visa.
- **Historiken är korrupt:**
  - 65 av 227 stängningar på testnet är fantomer (39 % i augusti).
  - funding har 1 rad.
  - Ett fiktivt mainnet-resultat från backfill av 2025 års fills ingår i totalen (belopp i bilaga P).
- **Sedan fixen 09-23** har det varit 0 fantomer, men på bara 4 stängningar.
- **Mainnet** har inte handlat sedan 2026-05-10 och har haft 2 positioner totalt.

### Dashboard och produkt
- **Det finns ingen pause- eller flatten-knapp sedan #105.** Stop stänger inga positioner och lämnar dem utan stopp (bots-client.tsx ~380-418).
- **Huvudtalen är vilseledande:**
  - $10 000 visas grönt när data saknas (_overview-view.tsx:153-154, 198).
  - "Equity P&L since first snapshot" täcker ungefär 3,3 h (queries.ts:161-176).
  - Minustecknet tappas (pnl-breakdown.tsx:182-195).
  - "Today" visar senaste aktiva dag.
- **Beta-tenanten har stått still sedan 2026-05-11.** Den har ingen passfras och ingen bot.
- **Spegeln för mainnet-opt-in har fel symbol eller timeframe** för 8 av 22 strategier (lib/admin/strategy-names.ts:18-45).
- **Trade-reason trunkeras.** Alla 875 trades har en reason, men UI:t kortar den (trade-table.tsx:76), och det finns ingen PnL-kolumn.
- **Dashboardens anrop mot bottarna saknar timeout** (lib/bot-api.ts:75). En hängande bot drar därför ner översikten och alla pollers.
- **Options-sidan gör 45 anrop per 10 s och flik.**

### Dashboard-arkitektur och säkerhet
- **Säkerheten har luckor i tre lager:**
  - vid kanten: den publika inloggningen
  - mellan den publika webbprocessen och hosten: processen har bredare rättigheter mot containerhanteringen än den behöver
  - mellan containrarna: delade interna tjänster litar på nätverket i stället för på autentisering

  I värsta fall når någon de HL-nycklar som finns på hosten. Kedjan beskrivs i bilaga P. NU-8 stänger kanten och hosten, och NA-15 stänger det interna.
- **Dashboarden ansluter som superuser** och går därmed förbi RLS.
- **Identiteten nycklas på e-post**, och invite-grinden är inte inkopplad (NU-8).
- **Styrkor:**
  - 612 vitest-tester och ren tsc/eslint.
  - 0 prod-sårbarheter i npm.
  - Autentiseringen stänger vid fel.
  - `max_active_bots` per tenant finns (alembic 0016).

### Data
- **Funding kan bara lagra en rad.** `funding_payments.hash` är globalt UNIQUE (models.py:317). HL returnerar samma hash med bara nollor för varje funding-händelse; det gällde 500 av 500 i live-anrop. Därför kan bara en rad någonsin lagras.
- **Schemat definieras på tre ställen:** Alembic, SQLAlchemy (37 avvikelser mot prod) och Drizzle. Alembic kan inte migrera en tom DB, eftersom 0011 kastar ett fel om operatörstenanten saknas.
- **equity_snapshots är 80 % av DB:n.** 99,99 % av mainnet-raderna är dubbletter, med 1 424 identiska rader per dag.
- **positions och trades ger olika PnL.** Testnet visar −75,58 i den ena och −101,40 i den andra. Det finns inga CHECK-constraints.
- **Postgres saknar skyddsinställningar.** `statement_timeout` och `pg_stat_statements` saknas, och connection-poolerna står på default.
- **Storleken är inget problem.** DB:n är 117 MB, och de heta frågorna tar 0,19 ms.

### Drift och övervakning
- **Backuper: PBS dagligen, men otestat.** Proxmox Backup Server tar dagligen backup av hela containern, inklusive Docker-volymerna (uppgift från operatören 2026-09-24; syns inte inifrån containern). Det ger RPO ≤24 h. Inuti containern finns inga schemalagda dumpar, `archive_mode` är av, och Postgres-kopian i en PBS-snapshot är därför bara kraschkonsistent. Ingen återställning har testats.
- **Ingenting utanför hosten övervakar den.** Watchdogen körs i dashboarden på samma host. Bottarna har stått still 22,7 h (06-17, en deploy som väntade på upplåsning) och 5,2 h (09-01, hosten avstängd), plus flera kortare luckor, medan 1–3 positioner saknade skydd på börsen. Upptiden är 98,8 % över 120 d.
- **Deploy är manuell.** Den består av tre SSH-steg med `latest`-taggar. En bot-uppgradering kräver tenantens passfras, och därför körde en 98 dagar gammal image.
- **Hosten ligger efter med säkerhetsuppdateringar och härdning** (bilaga P).
- **LAN-TLS har fallit tillbaka till ett self-signed-certifikat**, trots att TLS är påslaget.

### Sidofunktioner
- **Allt hänger på mainnet-botens process.** Telegram, digests, HODL, vault-scannern och nyckelpåminnelserna körs bara där (bot-orchestrator.ts:386; runner.py:693, 732). Notifiern prenumererar på alla modes, så trade- och fellarm från testnet och paper kommer fram, men inget av det överlever att mainnet-boten stoppas.
  - Digesten har sagt "No trades" i ungefär 134 dagar.
  - `position.closed` publiceras aldrig.
- **En enkel flytt av ägarskapet räcker inte.** Tre saker går sönder:
  - Vault-spårningsadressen faller bara tillbaka på kontoadressen på mainnet (config.py:58-86).
  - /hodl och /vaults är låsta till `mode='mainnet'` (app/hodl/page.tsx:67, app/vaults/page.tsx:72).
  - `send-unlock-link` väljer en godtycklig körande bot (route.ts:101-110), och alla bottar utom Telegram-ägaren svarar 503.
- **Vault-scannern är stabil men säger inget framåt.** Den har 5 496 snapshots. "Qualified" har ändå ingen framåt-edge: median +1,1 % mot +7,6 % för avvisade vaults.
  - Kvalificeringen flappar (49 events på 90 d).
  - Innehav i underkända vaults larmas inte.
- **I HODL** har `hodl_purchases` och `manual_onchain_levels` 0 rader. macro_backdrop säger alltid "no data", och verdict-texter med dagräknare skapar brus.
- **Veckoutvärderingen i § 8 görs för hand** och har körts 2 gånger sedan maj.

### Kodkvalitet och arbetsflöde
- **CI grindar ingenting.** Rulesetet kräver 0 checks.
  - 14 PR mergades på ungefär 6 h den 2026-09-23, och sex av dem var över 1 400 rader.
  - Testsiffrorna skrevs in för hand.
- **Motortesterna mockar nästan allt.** 15 av 16 testfiler med EngineRunner saknar riktig Repository, och det finns 0 E2E-tester.
- **mypy hittar 10 Optional-dereferenser på åtta ställen.** Fyra ligger på orderstigen i runner.py (268, 1039, 1154, 2072). Resten ligger i vaults/api.py (50, 58, 303) och telegram.py:304.
- **CLAUDE.md är för stor och inaktuell.** Den är 82,7 KB, ungefär 21k tokens per session.
  - Reconcile står som Open-Critical trots #167.
  - Definition of done motsäger AGENTS.md.
- **aiohttp har en HIGH-advisory** som varit öppen sedan 2026-08-04.
- **Styrkor:**
  - Varje incident har fått regressionstester.
  - Hemlighetsskyddet har tre lager.
  - De manuella reviewerna har varit effektiva: 27 fynd på #167, #172 och #174.

---

## 3. Målbild och north-star-mått

**Vad next level betyder.** För en operatör som arbetar genom agenter betyder next level inte fler strategier eller fler sidor. Det betyder att appen ger sanna svar på fyra frågor:

1. Tjänar jag pengar efter avgifter och funding?
2. Finns det evidens för att det inte är tur?
3. Vad står på spel just nu, och vad händer om hosten dör?
4. Hur stoppar jag den säkert?

**Om ungefär sex månader ska följande gälla:**

- Högst sju strategier, var och en med en reproducerbar validate-rapport: walk-forward, bootstrap, Deflated Sharpe och kostnader inklusive funding.
- En ledger som stämmer mot börsen, där varje fill är länkad till sin position och kodversion.
- Larm och backuper som inte beror på hosten.
- Varje merge grindas av CI, och varje deploy går att spåra till en SHA.
- Om B1 säger ja: en liten mainnet-pilot med stopp på börsen. Pilotens första uppgift är att mäta exekveringskostnad, inte att bevisa edge.

| North-star-mått | Omfång | Nuvärde | Mål | Senast |
|---|---|---|---|---|
| Ledger-residual: \|Δequity − (realized + funding − avgifter + Δunrealized)\| per månad | testnet; mainnet från pilotens första dag | testnet −$54…+$40/mån | < $1/mån | Nästa |
| Funding-täckning: funding-rader per öppen positionstimme | testnet och paper | testnet: 1 rad sedan april; paper: modelleras inte, så paperns residual på $0 säger ingenting | ≥1 rad per öppen positionstimme i båda | Nu (NU-6) |
| Återställbarhet (RPO) | DB och Redis | ≤24 h via PBS, kraschkonsistent | Oförändrat. Operatören har accepterat risken (2026-09-24); en återställning får inte likvidera (NU-2-vakterna) | Nu |
| Tid till upptäckt av host- eller botavbrott | alla körande bottar | obegränsad (22,7 h obemärkt) | ≤10 min, externt | Nu |
| Tid till upptäckt av penningfel | testnet och paper | ≈108 d (fantomer), ≈150 d (funding) | <24 h | Nu |
| Exits som fryses automatiskt vid Redis- eller DB-fel | alla modes | alla exits stoppas | 0; paus bara vid bekräftad inaktuell DB, med eskalerande larm | Nu |
| Merges med gröna krävda checks | repo | 0 % | 100 % | Nu |
| Körande image mot master | dashboard och bottar | upp till 98 d | dashboard ≤24 h efter merge; bottar kör senaste veckofönstrets SHA (≤8 d) | Nästa |
| Edge-bevis: walk-forward OOS-netto-Sharpe och DSR | validate, kandidaterna i båda förregistrerade varianterna | kan inte beräknas | beräknat för alla ≤7 × 2; grind OOS ≥0,8 och DSR ≥0,95 för allt som får kapital | B1 |
| Kostnadskvot (avgifter + funding)/brutto | validate per kandidat och variant | hash_momentum 102 % (backtest); funding okänd | ≤30 % för allt som får kapital | B1 |
| Andel öppna positioner med vilande reduce-only-stopp på HL | testnet, sedan mainnet | 0 % | 100 % före första riktiga dollarn | Sedan |
| Oskyddade positionsminuter vid avbrott | testnet, sedan mainnet | 15 av 17 avbrott >30 min hade 1–3 oskyddade positioner | 0 utöver ett placeringsfönster på 60 s | Sedan |
| Agentkontext: storlek på CLAUDE.md | repo | 82,7 KB, med motsägelser | ≤25 KB, 0 motsägelser mot AGENTS.md | Nu |

---

## 4. Roadmap

### 4.0 Konflikter mellan perspektiven och hur de avgjorts

| Fråga | Vad perspektiven sa | Beslut | Varför |
|---|---|---|---|
| Hur mycket maskineri? | Kvant, tillförlitlighet och plattform ville ha trial registry, nattligt scorecard, PBO, en notifier-tjänst med Redis Streams, WS-fills och netting. ROI ville ha en smal kärna. | **Smal kärna plus beslutspunkt.** Det tunga ligger kvar men startar först när ett beslut utlöser det. | Mainnet har inget kapital, och bästa strategin har t≈1. De ursprungliga planerna skulle kräva 40–60 granskade PR:er. Fyra nya permanenta jobb skulle mest bekräfta ett nej. |
| Stopp på börsen | Tillförlitlighet ville ha L-stora, trail-synkade stopp i fas 1. ROI ville ha en statisk backstop först, före mainnet. Pengasäkerhet krävde att placeringen är fail-safe. | reduce_only och resync vid avvisad stängning nu (NU-5). Statisk backstop när mainnet får ett ja (SE-1). Trail-synk senare (SN-1). | I dag skyddar stoppen låtsaspengar. De billiga delarna tar bort buggklasser som finns redan nu. |
| Pausens betydelse | Tillförlitlighet och produkt ville att pause ska betyda "inga nya opens". Pengasäkerhet påpekade att kill switch redan gör just det (portfolio.py:204-236). | **Pause förblir frys.** Knappen "Pausa nya entréer" kopplas till kill switch. Automatiska vakter slår på kill switch, inte paus. Exits-only gäller vid Redis- och DB-fel samt vid disable. | Ingen tredje semantik behövs. En automatisk paus skulle frysa exits, och före SE-1 finns inga stopp på börsen. |
| DR-vakt vid saknad Redis-sentinel | Första versionen bootade pausad. | Saknad sentinel ger kill switch (exits körs). Paus bara när även DB är inaktuell, och då med eskalerande larm (NU-2). | Samma skäl som ovan: en tom Redis får inte lämna positioner utan exit. |
| Beskärning | Kvant ville vänta på ett scorecard och ROI ville göra det nu. Pengasäkerhet sa: bara när strategin är platt. | Alla 14 stängs av när de är platta, redan i Nu (NU-10, efter beslut 5.2). De avregistreras i Nästa (NA-4). **cdc_macd och macd_zero slås inte ihop.** | Att stänga av är reversibelt och kräver inget bevis. Sammanslagningen minskar ingen exponering (SOL mot BTC) och skulle bryta en öppen paper-position. |
| Coin-grinden efter beskärning | Grinden ger live-data som formas av vem som hann först. Kvar blir tre ETH-, tre BTC- och en SOL-kandidat. | Beslut 5.11. Rekommendation: en kandidat per coin i live. `allow_multi_coin=1` är uteslutet. | Prioritet utan preemption löser inte att en 1d-strategi håller coinet i dagar. |
| Stopplösa kandidater | Grinden kräver ett hårt stopp, men 4 av 7 kandidater saknar ett. | En variant B med katastrofstopp på stängd bar förregistreras nu (NU-10) och valideras i NA-2. | Ett stopp som läggs till efter B1 utan validering ändrar beteendet okontrollerat (5.9). |
| Live-lookback på 1 000 bars | Kvant ville ha det. ROI och pengasäkerhet sa nej innan en candle-cache finns, eftersom en 429 hoppar över stopp. | Simulatorn speglar live (300 bars). Större lookback testas som en registrerad variant. Live ändras bara efter NU-9 och en godkänd validering. | Vikten per anrop ökar från ungefär 25 till 37 på en IP som redan ligger nära taket. |
| Intrabar-stopp i simulatorn | Kvant ville ha det. Pengasäkerhet sa nej så länge stoppet inte ligger på börsen. | Live-semantik är default. Intrabar-läge per strategi först efter SE-1. | Annars validerar grinden stopp som inte finns. |
| Friktion i backtest | Kvant ville sänka till ungefär 10 bps. Pengasäkerhet ville behålla nivån. | 19 bps tur-retur är default, och strategin måste överleva 2× modellkostnad. Verklig kostnad mäts i piloten. | Den lägre siffran bygger på en enda ögonblicksbild av orderboken, inte på fills. |
| Promotionsgrind | Kvant och tillförlitlighet ville ha grinden i kod. ROI ville ha en markdown-checklista. | Den statistiska grinden blir en **checklista med bifogad evidens**, och kravet på hårt stopp uppfylls av variant B. Mekaniska förutsättningar blir en **boot-preflight i kod** för mainnet (SE-3). | Operatören som kan kringgå grinden är samma person som finansierar. De mekaniska kontrollerna är billiga och skyddar mot misstag. |
| Portföljbacktest | Forskningskartan ville ha en barsynkron bokreplay med live-grindarna före B1. | Skjuts till SN-7. B1 bedömer strategier en och en, och piloten kör 1–2 strategier på disjunkta coins. | En bokreplay är L och behövs först när två kapitalbärande strategier delar coin. |
| Framåt-holdout | Kvant ville bygga infrastruktur direkt. ROI påpekade att replay går att köra i efterhand. | Två varianter per kandidat förregistreras i vecka 1 (NU-10). En engångsreplay görs vid B1. Replay-jobbet startar bara om B1 ger en kandidat (SE-5). | Frysdatumet är det enda tidskritiska. Utan variant B skulle NA-3 och stoppvarianterna nollställa klockan för sex kandidater. |
| Tenant-isolering | Tillförlitlighet, produkt och plattform ville göra M–L-arbete tidigt. ROI ville bara ha en grind. | `max_active_bots=0` och invite-grinden nu. Intern segmentering i Nästa (NA-15). Full scoping först om multi-tenancy blir ett ja (SE-4). | Beta-tenanten har 0 bottar och har inte rört sig på 4,5 månader. |
| Säkerhetsarbetets omfång i Nu | Tillförlitlighet och plattform ville ha hela kedjan i fas 1. Kapacitetsgranskningen visade att Nu inte rymmer det. | Kant och host i Nu (NU-8). Intern segmentering i Nästa (NA-15). | Den största exponeringen ligger vid kanten. Den interna vägen kräver att en bot redan är komprometterad. |
| Notifier-ägare | Tillförlitlighet och plattform ville ha en ny tjänst med Streams. ROI ville ha en owner-switch till testnet. | Ägar-mode = **paper** (beslut 5.12), plus digest per mode, nu (NU-7). En egen tjänst senare. | Paper körs alltid, behöver ingen börsnyckel och kan därför inte rensas av HL. Då kan B1 stoppa testnet utan att tysta larmen. |
| Maker/ALO, netting, HIP-3, runner-dekomposition | Olika perspektiv ville ha dem i fas 2–3. | Alla fyra skjuts till Senare med tydliga triggers. Dekomposition bara när en ändring kräver det, och då bakom NA-7. | Besparingen är ensiffriga dollar per år efter beskärning. Netting och HIP-3 förutsätter validerade strategier som inte finns. |
| Avgifter från fills | Tillförlitlighet ville ha det nu. ROI ville ha det först vid mainnet. | Görs i piloten (SE-3). | Paper har inga fills, och testnets avgifter är låtsaspengar. |
| Testnet som evidens | Förslagen var skugg-PnL eller testnet-candles. | Testnet dokumenteras som rörtest. Ingen skugg-PnL. | Evidensen kommer från validate och framåt-replay. Live-paper är en trohetskontroll. |
| NUMERIC för penningkolumner | Plattform ville byta. | Görs inte. | Avvikelsen är <1e-12 USD, och Decimal på orderstigen ger risk för TypeError. |

### 4.1 Arbetsregler för hela planen

- **WIP-gräns.** Högst en PR i taget får röra orderstigen. Kön ser ut så här: NU-2-vakten, NU-5a–e, NU-7.3, NU-9.
  - Sikta på ≤600 tillagda rader per PR, och lägg mekaniska refaktorer i egna PR:er.
  - Merga sekventiellt och kör om CI mellan varje merge.
  - Forskningsspåret (NA-1, NA-2) rör inte orderstigen och löper parallellt.
- **Review.** Små diffar får 3–4 `/review`-vinklar. Allt som rör orderstig, pengar eller migrationer får alla åtta vinklar. En full review kostar ungefär 1,5 M Opus-tokens.
- **Delegering.**
  - Det mekaniska och verifierbara kan gå till sonnet eller kanban: CI-yaml, dashboardtal, radering av död kod, CLAUDE.md-omskrivning och UI-knappar.
  - Detta görs med Opus eller i huvudsessionen: orderstigen, migrationer mot live-DB, backfill mot produktion, hemligheter och Redis i produktion.
  - Kanbans regler gäller: inga produktionsskrivningar och inga RLS-migrationer.
- **Veckofönster för botkod.** Botkod deployas en gång i veckan, till exempel på tisdagar. Då låser operatören upp bottarna en gång. Dashboard och Caddy deployas efter merge, och nödfixar går utanför fönstret. Det ersätter "≤24 h" för bottarna tills lösenfrasfri uppgradering eventuellt beslutas (5.5).
- **Privata värden.** Ping-URL:er, eventuella lagringsnycklar, LAN-intervall, e-postadresser i Access-policyer och wallet-adresser hämtas ur Phase eller ur lokala filer på hosten. De hamnar aldrig i repot.
- **Tidslinje.**
  - Nu = v1–4 (2026-09-28 till 2026-10-23).
  - Forskningsspåret = v4–9.
  - B1 senast 2026-12-04 (v10).
  - Övrigt i Nästa = v5–13.

**Kapacitet före B1:**

| Initiativ | PR:er | varav orderstig eller migration (alla åtta vinklar) | Vecka |
|---|---|---|---|
| NU-1 | 0 (operatörsåtgärd) | 0 | 1 |
| NU-2 | 2 | 1 | 1–2 |
| NU-3 | 2 | 0 | 2–3 |
| NU-4 | 3 | 1 | 1 |
| NU-5 | 5 | 5 | 2–4 |
| NU-6 | 5 | 2 | 2–4 |
| NU-7 | 4 | 1 | 2–4 |
| NU-8 | 2 | 0 | 1–3 |
| NU-9 | 1 | 1 | 4 |
| NU-10 | 1 | 0 | 1 |
| NU-11 | 2 | 0 | 1–2 |
| **Nu totalt** | **27** | **11** | |
| NA-1 | 4 | 1 | 4–7 |
| NA-2 | 3 | 0 | 6–9 |
| NA-14 | 1 | 0 | 10 |
| **Kritisk väg till B1** | **35** | **12** | |

Den kritiska vägen kostar ungefär 12 × 1,5 M + 23 × 0,6 M ≈ 32 M Opus-tokens i review. Om färre än 8 av 11 Nu-initiativ är klara i vecka 4 skjuts följande till v5–6 i stället för att B1 flyttas:

- NU-9
- NU-11-hooken
- NU-6.5 (paper-funding)
- NU-7.4

B1 flyttas bara om NA-1 eller NA-2 inte är klara i vecka 9. Då flyttas det högst två veckor, och beslutet skrivs ner.

### 4.2 Nu (0–4 veckor): säkerhetsgolv och sanna siffror

| Vecka | Arbete |
|---|---|
| 1 | NU-1 (dag 1); besluten 5.2, 5.11 och 5.12; NU-10; NU-4; NU-2 punkt 0–3; NU-11 (CLAUDE.md); NU-8 punkt 0–2 |
| 2 | NU-2 kodvakter (första PR:en på orderstigen); NU-3; NU-6.1; NU-7.1; NU-8 punkt 3–6 |
| 2–4 | NU-5 (fem PR:er, en i taget); NU-6.2–6.5; NU-7.2–7.4 |
| 4 | NU-9; NU-11-hooken; NA-1 startar |

#### NU-1 · Avväpna mainnet per mode och stäng dörren för nya tenanter (operatörsåtgärd, dag 1)
- **Vad:**
  1. Slå på kill switch för mainnet via bottens egen endpoint (`POST /api/control/kill-switch`, api.py:399-428). Den lagras som Redis-overriden `hypertrade:mainnet:control:kill_switch`, gäller bara mainnet och överlever omstart.
  2. Töm tenantens mainnet-opt-in, antingen i MainnetStrategiesCard på /settings/bots eller direkt i setet i control.py:268-287. Ett tomt set betyder noll strategier (stänger vid fel) och läses om varje tick (runner.py:792-801). Därmed kan bb_short inte handla, även om env-taket fortfarande nämner den.
  3. Sätt `max_active_bots=0` för beta-tenanten i /admin.
  4. Radera hävstångs-overrides för avstängda strategier (`HDEL` i `hypertrade:{mode}:control:leverage`, bland annat volatility_breakout och bb_short), så att ETH inte längre körs med 8x cross. Kodfixen kommer i NA-4.
  5. Stäng PR #9 och #139.

  Mainnet-containern får fortsätta köra tills NU-7 har flyttat larmen.
- **Varför:**
  - bb_short har 0 SL-referenser (bb_short.py:14-15) och är den enda allowlistade strategin. Om kontot finansieras i dag beväpnas alltså en naken short med riktiga pengar.
  - `KILL_SWITCH` och `MAINNET_ENABLED_STRATEGIES` ligger i Phase som `HYPERTRADE_BOT_*` på dashboard-tjänsten (docker-compose.yml:195-196). De injiceras i alla bottar i alla modes (bot-orchestrator.ts:259-283). Att ändra dem skulle slå på kill switch även för paper och testnet vid nästa omstart, och det kräver att dashboarden återskapas och att varje bot låses upp med passfras.
  - Mainnet-boten startas av orchestratorn, så compose har inget att återskapa. Redis-vägen kräver ingen omstart alls.
  - Kontrollnycklarna är per mode (control.py:23-24). Beta-tenantens första bot skulle alltså dela pause, flat-all, kill switch och daily_pnl med operatören. Spärren som förhindrar det finns redan (lib/admin/limits.ts:97-99).
- **Hur:**
  - Anropa endpointen inifrån mainnet-containern med containerns egen `API_KEY`. Använd python urllib, eftersom imagen saknar wget, och kontrollera payloadformatet i api.py:399-428 först.
  - Töm opt-in-setet i UI:t.
  - Huvudsessionen gör HDEL.
  - `gh pr close` med en kommentar.
- **Insats:** S (timmar, ingen kod) · **Påverkan:** 4/5 · **Beroenden:** inga · **Delegering:** huvudsessionen (Redis i produktion).
- **Risker:** Kill switch blockerar bara opens, och mainnet har inga positioner. Om Redis töms försvinner overriden, men då är även opt-in-setet tomt, och det stänger. NU-2:s sentinel-vakt slår dessutom på kill switch igen.
- **Klart när:**
  - `GET /api/control/kill-switch` på mainnet-boten svarar aktiv, och tickloggen visar 0 aktiva strategier.
  - Paper och testnet har kill switch av.
  - reserveBotStart vägrar beta-tenanten.
  - Hävstångshashen saknar overrides för avstängda strategier (ETH 8x försvinner ur bootloggen vid nästa omstart).

#### NU-2 · En återställning som inte likviderar
- **Vad:** två kodvakter och en kort runbook för återställning från PBS. Backupen i sig ändras inte.
- **Varför:**
  - PBS tar dagliga backuper av containern. Postgres-kopian är kraschkonsistent och otestad, men operatören har bedömt risken som godtagbar (2026-09-24). Dumpar, restore-test och extern lagring görs därför inte.
  - En återställning är farlig även när backupen är bra. Med upp till ett dygn gammal data marknadsstänger reconcile pass 2 alla börspositioner som saknar DB-rad (repo.py:1039-1117), och en återställd eller tömd Redis läses som "ej pausad" (control.py:48-52). Samma sak händer om Redis töms utan någon återställning alls.
  - En vakt som bootar pausad skulle frysa även exits, och före SE-1 finns inga stopp på börsen. Därför slår den automatiska vakten på kill switch, inte paus.
- **Hur:**
  1. Kodvakt för Redis (S). Om Redis-sentinel saknas bootar boten med kill switch aktiv, så att opens blockeras men exits körs. Den larmar med listan över öppna positioner. Om DB dessutom verkar inaktuell, alltså om börsens userFills innehåller fills som är nyare än den nyaste trade-raden, bootar boten pausad. Larmet upprepas då och eskaleras efter N minuter i frysning.
  2. Kodvakt för reconcile (S). Reconcile pass 2 larmar i stället för att stänga i tre fall:
     - fler än 1 orphan per pass
     - symbolen ligger utanför det registrerade universumet
     - nyaste DB-rad är äldre än N timmar
  3. `docs/runbooks/disaster-recovery.md` (kort): återställning från PBS när en människa är närvarande.
     1. Sätt kill switch och `paused=1` innan första boten startar.
     2. Kör paritetskontrollen.
     3. Unpausa inom 30 minuter.
     4. Slå av kill switch sist.

     Radera också dumparna som ligger i checkouten, och rätta `bot/scripts/migrate.sh`, där rad 7 är en inaktuell användningskommentar.
- **Insats:** S · **Påverkan:** 4/5 · **Beroenden:** inga · **Delegering:** Opus med full /review, eftersom vakterna rör orderstigen.
- **Risker:** orphan-vakten får inte göra verkliga orphans permanenta, så larmet måste kvitteras manuellt.
- **Klart när:** tester visar att
  - tom DB plus börspositioner ger larm och inte stängning
  - saknad sentinel ger kill switch med fungerande exits
  - saknad sentinel plus inaktuell DB ger paus med eskalerande larm

#### NU-3 · Extern dead-man's switch och dagliga penninginvarianter
- **Vad:** kontroller i healthchecks.io och ett SQL-jobb som larmar på Telegram bara när något bryts.
- **Varför:** watchdogen körs i dashboarden på samma host.
  - Avbrotten på 22,7 h (06-17, deploy som väntade på upplåsning) och 5,2 h (09-01, hosten avstängd) larmade inte utanför hosten.
  - Fantomer pågick i ungefär 108 dagar, trots att audit M2 (2026-05-10) beskrev signaturen.
  - Funding har varit död sedan 2026-04-28.
  - En image på 98 dagar gick obemärkt.
- **Hur:**
  1. Skapa ett gratis healthchecks.io-konto med Telegram och e-post. Ping-URL:er hämtas från Phase eller env, aldrig ur repot, och mönster för hc-ping-URL:er läggs i `.githooks/pre-commit` och `.gitleaks.toml`. Kontroller:
     - en per körande bot (period 1 min, grace 10 min), som watchdogen pingar efter varje lyckad probe
     - en för invariantjobbet
     
     Ping-URL:erna hämtas från Phase.
  2. `python -m hypertrade.ops.invariants` körs dagligen som operatörens tenant-roll och kontrollerar fem saker:
     - (a) fantomsignaturen: pnl=0, exit=entry och ingen trade inom ±2 min
     - (b) realized + funding − avgifter mot Δequity per mode och dag, med larm först vid brott två dagar i rad
     - (c) funding som är äldre än 48 h medan positioner är öppna
     - (d) image-drift: dashboarden skiljer sig från master i mer än 24 h, eller en bot kör en annan SHA än senaste deployfönstret i mer än 8 dagar
     - (e) en innehavd vault underkänns av filtret eller har 30-dagarsdrawdown över X %; deduplicerat per tillståndsbyte
     
     Senare läggs stopptäckning (SE-1) och HL:s adressbudget till.
  3. Första veckan loggar jobbet bara.
- **Insats:** S–M (ROI: ungefär 2 dagar) · **Påverkan:** 5/5 · **Beroenden:**
  - NU-6.1 och NU-6.2 för (b)
  - NU-6.1 för att (c) ska tystna
  - NU-4 för (d)
- **Risker:** larmtrötthet, eftersom insättningar och uttag slår ut (b). Därför krävs två dagar i rad.
- **Klart när:**
  - Ett simulerat host-stopp larmar inom 10 min via en kanal som inte går genom hosten.
  - Jobbet körs mot data från maj till september och flaggar (a) den första fantomen inom 24 h efter 2026-05-29, samt (c) funding-avbrottet inom 48 h.
  - (b) valideras bara framåt, efter NU-6.1–6.2 och en veckas loggning, eftersom historiken saknar funding och har otaggade fantomer.
  - Efter två veckor ger jobbet högst ett falsklarm per vecka.

#### NU-4 · CI som krävd merge-grind, versionsstämpel och beroendehygien
- **Vad:** GitHub Actions som kör alla lokala grindar och krävs av ruleset 15967890, en SHA i images och API, och säkerhetsuppdateringar.
- **Varför:**
  - I dag körs bara gitleaks och CodeQL.
  - PR #167 (+2 975), #172 (+2 528) och #171 (+2 573) mergades på handskrivna testsiffror, och #170–#172 inom 24 s.
  - Inget visar vilken version som kör.
  - aiohttp 3.14.2 har en HIGH-advisory.
- **Hur:**
  1. Bot-jobbet: `uv sync --frozen && uv run pytest -q -n auto`, med pytest-xdist och en fixture som sätter tenacity-väntan till noll.
  2. Dashboard-jobbet: `npm ci && npx tsc --noEmit && npm run lint && npm test && npm run build`.
  3. Migrationsjobbet: `alembic upgrade head` på postgres:16.
     - Först måste 0011 klara en tom DB, via en seed-migration eller no-op på tomma tabeller (0011:55-67).
     - Ingen downgrade-loop.
     - `alembic check` blockerar inte förrän modellerna är rättade.
  4. Gör jobben och gitleaks till krävda checks. Dokumentera admin-bypass för nödfixar.
  5. `GIT_SHA` som build-arg och OCI-label, plus `GET /api/version` i bot och dashboard. SHA:n syns också i /status.
  6. Uppgradera aiohttp till ≥3.14.3. Lägg till `dependabot.yml` för säkerhetsuppdateringar och en månatlig grupp för actions och docker. Pinna uv-imagen.
- **Insats:** S–M (ROI) · **Påverkan:** 5/5 · **Beroenden:** inga · **Delegering:** kanban eller sonnet. 0011-fixen får full review.
- **Risker:** flakiga tester, eftersom 35 tester använder väggklockan. Använd en derandomiserad hypothesis-profil och karantän med ägare och utgångsdatum.
- **Klart när:**
  - Rulesetet har minst 4 krävda checks.
  - 20 mergade PR:er i rad är gröna.
  - CI p50 ≤10 min.
  - `/api/version` svarar i alla tre bottar.

#### NU-5 · Exits som alltid fungerar (orderstigens S-paket)
- **Vad:** ändringar som ser till att en hållen position alltid kan stängas, att en stängning aldrig kan öppna motsatt sida, och att en avvisad stängning hanteras rätt.
- **Varför:**
  - Ingen stängning är reduce-only. När börsen är platt eller på motsatt sida skickar `_resolve_close_size` full DB-storlek (runner.py:2507-2515). Efter en likvidation eller en manuell stängning öppnar strategins exit därför en ny position åt andra hållet.
  - En avvisad CLOSE glöms bort efter att strategin redan har nollställt `_in_position` (keltner_breakout.py:116).
  - Ett Redis-fel hoppar över alla exits.
  - Disable överger positioner.
  - Trailing-stopp lossas vid varje deploy.
  - Stop-grace är 10 s (docker.ts:98), medan en order kan ta upp till 45 s.
- **Hur** (fem PR:er, alla med fault-injection-test):
  - **a.** `reduce_only` på alla stängningsvägar, på flat-all (runner.py:1097) och på reconcile-orphan-close. Skicka aldrig en order utan reduce_only när börsen är platt eller på motsatt sida. Samma PR klassar avvisade stängningar i två fall:
    - **Börsen är platt eller mindre än raden.** Det gäller bara när beskedet kommer från en lyckad läsning; ett `ExchangeReadError` tolkas aldrig som platt. Då triggas en omedelbar reconcile av raden, bokförd som extern stängning från fills. Inget omförsök, ingen breddning av bandet och bara ett informationslarm.
    - **Övriga avslag.** `_resync_strategy_to_row` och ett larm per (strategi, coin). Efter K avslag i rad breddas IOC-bandet en gång, och därefter kommer ett kritiskt larm.
  - **b.** Kontrolläsningarna körs i try. Ett oläsbart värde blockerar opens men låter exits köras. Om DB-läsningen kastar används börsens storlek, klampad och med reduce_only.
  - **c.** Ett **close-only-läge** för en hållen strategi som har stängts av, åkt ur allowlisten eller larmats av rate-grinden. Strategin tas aldrig ur ticken förrän den är platt, och ett test visar att den aldrig öppnar.
  - **d.** `export_state()` sparas till `state_json` och snapshot varje gång den ändras under en position. Det gäller till exempel ath_breakouts trail.
  - **e.** Stop-grace höjs till 60 s, och SIGTERM avslutar bara den pågående signalen.
  - Hävstången hanteras operativt i NU-1 och i kod i NA-4.
  - **Gör inte:** lägg inte `asyncio.wait_for` runt orderstigen. SDK-anropet går i en tråd och avbryts inte, så en fylld order skulle bokas som misslyckad.
- **Insats:** M (ROI: ungefär en vecka) · **Påverkan:** 5/5 · **Beroenden:** NU-4 · **Delegering:** Opus med alla åtta /review-vinklar.
- **Risker:** close-only-strategier fortsätter att skicka nekade opens. Behåll refusal-dedup.
- **Klart när:** tester visar att
  - en överstor stängning inte kan flippa positionen
  - en reduce-only-stängning mot en platt börs ger en reconcile-stängning från fills, utan omförsök eller kritiskt larm
  - ett läsfel aldrig tolkas som platt
  - en annan avvisad stängning försöks igen nästa tick
  - ett Redis- eller DB-fel inte stoppar SL-exit
  - en avstängd strategi som håller en position stänger enligt sin regel
  - en omstart bevarar ett ratchetat SL

#### NU-6 · Sanna siffror: funding (även i paper), ren ledger-vy, backtest-sparande och dashboardens huvudtal
- **Vad:** laga de fel som gör varje PnL-siffra fel eller ofullständig.
- **Varför:**
  - Funding dedupliceras på en hash som alltid är noll (models.py:317, repo.py:505-509), och pollern pagar inte förbi 500 händelser (runner.py:2402-2419).
  - Paper räknar bara avgifter (paper.py:166), men 5.6 gör paper till en huvudkälla. Long-tunga kandidater ser därför 6–13 % bättre ut än de är.
  - 2025 års backfill ger ett stort fiktivt minus i mainnet-totalen (queries.ts:311-333).
  - Dashboarden visar påhittade eller trunkerade tal.
  - `save_backtest_run` saknar tenant_id.
  - meta/*.json visar handskrivna och inaktuella siffror, till exempel kalman "−3.8% APR".
- **Hur** (fem PR:er):
  1. Alembic:
     - Ersätt `UNIQUE(hash)` med `UNIQUE(tenant_id, mode, coin, timestamp)` och `ON CONFLICT DO NOTHING`.
     - Paga förbi 500 händelser.
     - Attribuera varje händelse till den position vars [opened_at, closed_at) täcker den.
     - Huvudsessionen kör backfill från 2026-04-28.
     - Testa med två händelser som båda har en hash med bara nollor.
  2. Vyerna `positions_clean` och `equity_clean`:
     - De taggar fantomsignaturen och utesluter `reconciled`-backfill och falska nollrader.
     - `net_pnl = pnl − entry-avgift + attribuerad funding`.
     - Taggarna går att ta bort igen och får **aldrig** mata kill switch eller daily-loss.
     - Peka om `weekly_eval.py:104-140` och queries.ts till vyerna.
  3. Dashboard:
     - tomt läge i stället för $10k
     - tecken på alla belopp
     - rätt fönsteretikett, eller 24h/7d/30d via timaggregat
     - Today som dagens netto
     
     I samma PR tas de handskrivna prestandasiffrorna ur meta/*.json; NA-2 ersätter dem.
  4. `save_backtest_run(tenant_id=…)`, exit ≠0 när sparandet misslyckas, och ett Postgres-test.
  5. PaperExchange tillämpar funding varje timme på öppna positioner. Räntorna hämtas från HL:s fundingHistory, med mainnet-räntor från samma källa som candles. Funding skrivs till funding_payments med `mode='paper'`.
- **Insats:** M (ROI: fem S-delar) · **Påverkan:** 5/5 · **Beroenden:** inga · **Delegering:** punkt 3–5 till kanban. Punkt 1–2 görs med Opus och full review, eftersom de är migrationer.
- **Risker:** attributionen är heuristisk när innehav överlappar. Backfillen begränsas av HL:s historikgränser.
- **Klart när:**
  - Funding får rader varje timme medan positioner är öppna, både på testnet och på paper.
  - Backfillen redovisar total funding sedan april.
  - Mainnets realized PnL exkluderar backfill.
  - Det finns ett komponenttest för negativa belopp.
  - En CLI-körning skapar en rad i backtest_runs (CI-test).

#### NU-7 · Larm och sidotjänster med paper som ägare
- **Vad:** flytta hjälptjänsterna till ett konfigurerbart ägar-mode, som blir paper (beslut 5.12). Flytta även de konsumenter som förutsätter mainnet, och gör larmen värda att läsa.
- **Varför:** allt detta körs bara i mainnet-botens process, som inte kan handla och som planen vill kunna stoppa:
  - Telegram, digests, HODL, vaults och nyckelpåminnelser.
  - Digesten filtrerar på sin egen mode (repo.py:625).
  - `PositionClosed` konstrueras aldrig (runner.py:1702 är den enda platsen där trades publiceras).
  - Event-loopen saknar reconnect (telegram.py:383-406).
  
  En ren flytt av ägarskapet räcker inte heller:
  - Vault-spårningsadressen faller bara tillbaka på kontoadressen på mainnet (config.py:58-86).
  - /hodl och /vaults är låsta till mainnet.
  - `send-unlock-link` väljer en godtycklig bot med `.limit(1)` (route.ts:101-110), och alla utom ägaren svarar 503 (api.py:174).
  
  Watchdogens bot-down-larm går redan förbi mainnet (lib/telegram-alert.ts) och påverkas inte.
- **Hur** (fyra PR:er):
  1. Ägar-mode för Telegram och för HODL- och vault-grindarna blir en variabel i orchestratorn, med default paper. Därtill:
     - `VAULT_TRACKING_ADDRESS` sätts uttryckligen på ägarboten. Den är en wallet-adress och ligger därför i Phase, aldrig i repot.
     - /hodl och /vaults pekas om till ägar-mode (app/hodl/page.tsx:67, app/vaults/page.tsx:72).
     - `send-unlock-link` väljer den bot vars mode är ägar-mode.
     - Orchestratorn varnar om ägar-mode inte har någon körande bot.
  2. Digest och /eval loopar över alla modes. /pause och /flat tar ett mode-argument med bekräftelse. Positionsdata för andra modes läses ur DB.
  3. Publicera `PositionClosed` med netto-PnL, hålltid och orsak. Det ligger på orderstigen och får därför full review.
  4. Två delar i samma PR:
     - Övervakad reconnect, och `send()` med backoff som respekterar `retry_after`.
     - En `extraAgents`-kontroll vid boot och dagligen för signerarna på **både testnet och mainnet**, med larm 14 dagar före `validUntil` och direkt vid "pruned". Master-adresserna hämtas från Phase.
  
  HODL-zoner och vault-veckodigest flyttas till NA-11. Larmet för innehavda vaults ligger i NU-3(e).
- **Insats:** S–M (ROI) · **Påverkan:** 4/5 · **Beroenden:** NU-1 · **Delegering:** punkt 2 till sonnet, punkt 1, 3 och 4 till Opus.
- **Risker:**
  - Två processer som pollar getUpdates med samma token krockar, så det får bara finnas en ägare.
  - Om paper stoppas utan att en ny ägare först har satts tystnar larmen. Orchestratorvarningen fångar det.
- **Klart när:**
  - En drill med mainnet-containern stoppad visar att följande fortsätter att fungera:
    - Telegram-larm och kommandon
    - HODL
    - /vaults, med operatörens innehav listade
    - DM med upplåsningslänk
    - nyckelpåminnelser
  - Varje stängning ger ett meddelande med netto-PnL.
  - Digesten har sektioner för testnet och paper.
  - Högst 3 informationsmeddelanden per dag utöver trades.

#### NU-8 · Kant- och hosthärdning
- **Vad:** stänga de luckor som exponerar hosten utåt, alltså kanten och hosten. De interna luckorna mellan containrarna tas i NA-15.
- **Varför:** den publika inloggningen, den publika processens rättigheter mot hosten och hostens patch- och SSH-läge ger tillsammans en väg till de HL-nycklar som finns på hosten. Kedjan beskrivs i bilaga P. Dessutom är invite-grinden inte inkopplad, så varje IdP-användare får en tenant.
- **Hur:**
  0. Skriv bilaga P från lagerkartorna, utanför repot.
  1. Autentisering vid kanten för det publika hostnamnet med Cloudflare Access (gratis upp till 50 användare, ungefär 1 h). Policyns e-postadresser konfigureras bara i Cloudflare.
  2. Koppla in `OIDC_REQUIRED_GROUP` i dashboardens environment, med en varning vid boot om den är tom. Nya tenanter får `max_active_bots=0` som default. Detta är en S-PR.
  3. Den publika processens åtkomst till containerhanteringen går via en minsta-behörighets-proxy som bara får skapa, starta, stoppa och inspektera containrar. Detta är en PR, och den testas med start och stopp av en paperbot.
  4. Härdning av hosten i ett underhållsfönster, med kill switch på (inte paus):
     - säkerhetsuppdateringar
     - bara nyckelbaserad SSH
     - unattended-upgrades
     - tak för journald
     - maskad console-getty
     
     Allt skrivs i `ops/host/bootstrap.sh`. Hostspecifika värden, som LAN-intervallet för brandväggen, läses från en lokal fil på hosten. En nft-drop-policy införs bara när LAN-konsolen är tillgänglig.
  5. Granska, rotera och radera gamla env-kopior på hosten.
  6. Pusha TLS-konfigurationen nu. Återapplicering vid boot kommer i NA-15.
- **Insats:** S–M (2 PR:er plus 2–3 dagars operatörsarbete) · **Påverkan:** 4/5 · **Beroenden:** inga (PBS-backup före patch) · **Delegering:** Opus och huvudsessionen (hemligheter, produktion).
- **Risker:**
  - Access kan låsa ute operatören, så break-glass via LAN beskrivs i bilaga P.
  - NU-8 skyddar inte mot en komprometterad bot. Ett gemensamt lösenord på interna tjänster hjälper inte heller mot det hotet, eftersom bottarna då delar lösenordet. Det skyddet kommer med NA-15.
- **Klart när:**
  - Det publika hostnamnet kräver Access-inloggning.
  - En IdP-användare utan gruppen får ingen tenant (test).
  - Dashboarden når containerhanteringen bara via proxyn.
  - Det finns 0 säkerhetspaket äldre än 14 dagar, och SSH accepterar bara nycklar.
  - LAN serverar ett Let's Encrypt-certifikat.
  - Bilaga P finns utanför repot.

#### NU-9 · Candle-cache, 429-hantering, tidsgräns per tick och färskhetsvakt
- **Vad:** halvera REST-lasten, begränsa hur länge en tick kan hänga, och sluta hoppa över stoppkontroller vid throttling.
- **Varför:**
  - Varje strategi gör en REST-hämtning per tick med en ny session (feed.py:70, runner.py:1214). Det blir 22 hämtningar för 11 unika par.
  - En 429 ger en tom frame och hoppar över stoppkontrollen (feed.py:82-87).
  - En tick som hänger på HL kan ta ungefär 12 minuter utan stoppkontroll.
  - Ingen kontrollerar hur färsk den senaste stängda baren är.
- **Hur:**
  - Cachea per tick med nyckeln (symbol, tf, senast stängda bar) och använd en delad session.
  - Hantera 429 och Retry-After som transienta fel med backoff.
  - Strategier som håller position utvärderas först.
  - Varje par får ett försök med kort timeout (cirka 5 s) per tick, och omförsök sker nästa tick. Efter den första transporttimeouten i en tick hämtas bara par för strategier som håller position.
  - Lägg till räknare för 429 och för "stoppkontroll överhoppad".
  - Om den senast stängda baren är äldre än 2× tf blockeras opens och ett larm går. Exits fortsätter. Det byggs ingen andra exitmotor på mark-pris.
  - Cachea indicator-status i 60 s.
  - En WS-candle-tjänst och en delad token-bucket skjuts upp tills 429 faktiskt har mätts.
- **Insats:** S · **Påverkan:** 3/5 · **Beroenden:** inga · **Delegering:** Opus. Det rör orderstigen och får full review. Nämn fällan uttryckligen i prompten: nyckeln måste innehålla senast stängda bar.
- **Klart när:**
  - Hämtningarna per tick är halverade enligt loggräknaren.
  - "Stoppkontroll överhoppad" är 0 per vecka.
  - I test med en källa som inte svarar tar en tick ≤ cirka 20 s, och strategier med position utvärderas först.
  - Med en frusen källa blockeras opens inom 2 bars.

#### NU-10 · Förregistrera två varianter per kandidat och stäng av de 14
- **Vad:** en förregistreringsfil som startar framåt-evidensens klocka i vecka 1, och avstängning av de strategier som ska pensioneras.
- **Varför:**
  - Framåt-holdout behöver bara ett frysdatum. Data efter frysdatum går att hämta i efterhand (Binance USD-M-arkiv, HL fundingHistory ≥1 år).
  - NA-3 och stoppvarianterna skulle ändra sex av sju kandidater. Om bara nuvarande version förregistreras nollställs klockan för dem, och holdouten vid B1 krymper till veckor.
  - hash_momentum är negativ i paper, testnet och backtest.
  - daily_long_0830 betalar största delen av testnets avgifter för ungefär 0 netto.
- **Hur:**
  1. Skriv `docs/preregistration-2026-10-01.md` för kalman_breakout, keltner_breakout, cdc_macd, macd_zero, sma_rsi, ath_breakout och btc_mean_reversion, med två varianter per kandidat:
     - **Variant A**, som i dag: commit-SHA, parametrar, symbol och timeframe.
     - **Variant B**, som specifikation:
       - Dubbelstrippen tas bort för kalman, keltner, cdc_macd och macd_zero.
       - Ett katastrofstopp läggs till för kalman, cdc_macd, macd_zero och sma_rsi. Stoppavståndet är det större av 3×ATR vid entry och 8 % från entry, och det utvärderas på stängd bar.
       - Där ingen ändring gäller är B lika med A.
     
     Specen fryses nu. Koden skrivs i NA-2 och får inte avvika från den. Varje senare ändring blir en ny rad med nytt datum.
  2. Efter beslut 5.2 läggs de 14 strategier som ska pensioneras i disabled-set i paper och testnet, var och en när den är platt. Kontrollera med `SELECT … WHERE is_open`.
     - daily_long_0830 stängs av under 08:00–08:30 UTC, eftersom den ligger i position ungefär 23,5 h per dygn.
     - Strategier som redan är avstängda hoppas över.
  3. Tillämpa beslut 5.11 i live på samma sätt, när strategierna är platta.
- **Insats:** S · **Påverkan:** 4/5 · **Beroenden:** beslut 5.2 och 5.11 (vecka 1). Inget beroende av NU-5, eftersom allt görs när strategierna är platta · **Delegering:** filen till sonnet. Redis-ändringarna görs i huvudsessionen.
- **Klart när:**
  - Förregistreringen är mergad i vecka 1.
  - Alla 14 finns i disabled-set i paper och testnet, utan föräldralösa positioner.
  - Live-setet följer 5.11.

#### NU-11 · En bantad CLAUDE.md och agentskydd
- **Vad:** en kort och sann agentmanual, och skydd som harnessen upprätthåller.
- **Varför:**
  - CLAUDE.md är 82,7 KB, ungefär 21k tokens per session och subagent. 26 % är Done-historik och 27 % runbooks.
  - Reconcile står som Open-Critical (CLAUDE.md:660-670), och testantalet anges som "756 passed".
  - Definition of done (rad 80-85) motsäger AGENTS.md.
  - Det finns inga hooks.
- **Hur:**
  - Flytta Done-historiken till `docs/CHANGELOG.md` och runbooks till `docs/runbooks/`.
  - Stryk inaktuella Open-poster.
  - Definition of done blir PR → review → merge, och operatören deployar i veckofönstret.
  - § 4 pekar på arbetsytans modellpolicy. § 9 behålls ordagrant.
  - En PreToolUse-hook nekar `git push … master` och `--no-verify`, med en env-override för nödfall.
  - AGENTS.md får PR-budgeten, WIP-gränsen och publiceringsregeln.
- **Insats:** S · **Påverkan:** 3/5 · **Beroenden:** inga · **Delegering:** sonnet.
- **Klart när:** CLAUDE.md är ≤25 KB utan motsägelser, och hooken nekar en testpush.

### 4.3 Nästa (1–3 månader): ett ärligt svar på edge-frågan

Nästa har två delar:

- **Kritisk väg till B1** (NA-1, NA-2, NA-14). Den bestämmer B1-datumet.
- **Övrigt i Nästa.** Det görs i ordningen NA-9, NA-7, NA-15, NA-8, NA-12, NA-13, NA-4, NA-6, NA-3, NA-11, NA-10, NA-5 när kapaciteten räcker, och det flyttar aldrig B1. Tre av dem är krav före Sedan-initiativen: NA-6 och NA-7 före SE-1, och NA-13 före SE-3.

#### Kritisk väg till B1

##### NA-1 · Ärlig datagrund och en simulator som beter sig som live
- **Vad:** en parquet-cache för lång historik och funding, och en backtester vars fönster, stopp och kostnader speglar live.
- **Varför:**
  - Fönstren kortas tyst.
  - Datan är spot i stället för perp och saknar innevarande månad, HYPE och VVV (binance_dump.py:30).
  - Funding saknas. HL:s fundingHistory räcker dessutom bara ett år bakåt, medan walk-forward behöver fyra.
  - Live använder 299 bars medan backtesten använder full historik.
  - Den öppna slutpositionen bokförs utan exit-avgift.
  - Ett svep tar ungefär 590 s.
- **Hur** (fyra PR:er):
  1. binance_dump.py hämtar USD-M futures från månads- och dagsarkiven, plus fundingRate-arkivet (data.binance.vision) för perioden före HL:s fundingHistory. HL fundingHistory laddas ner till parquet på dev-boxen. Ingen inspelartjänst.
  2. Loadern tar `--start/--end` och hämtar värmning före fönstret. Den ger ett tydligt fel vid för kort historik och skriver ut antal dagar och bars. Varje bar taggas med källa, och bars från före HL-listningen flaggas.
  3. `LOOKBACK_BARS` blir en delad konstant på 300 tillsammans med `fetch_candles`. Det gör också svepet ungefär 5,5× snabbare.
  4. Stoppen har live-semantik som default: brott upptäcks vid bar-close och fylls nästa tick.
  5. Kostnader:
     - Funding räknas timvis. Före HL-perioden används Binance-funding, markerad som proxy. Om funding saknas för en fold redovisas folden som brutto och räknas inte mot kostnadskvoten.
     - Entry- och exit-avgift tas med i varje trade och vid slutmarkeringen.
     - Realized och unrealized redovisas separat.
  6. Friktionen är 19 bps tur-retur som default.
  7. `backtest_runs` får `params_json`, `git_sha`, `data_hash`, `eval_start/end`, `unrealized_end`, `funding_paid`, `funding_source` och `kind`.
- **Insats:** M (ROI: S för data och M för simulatorn) · **Påverkan:** 5/5 · **Beroenden:** NU-6.4 · **Delegering:** Opus.
- **Risker:** Binance-perp och HL skiljer sig åt (basis), och det gäller även funding. Kontrollera att den dagliga avkastningskorrelationen är >0,99 innan källorna blandas.
- **Klart när:**
  - Cachen har ≥4 år 1h och ≥2 år 15m för BTC/ETH/SOL, och funding för alla folds (HL de senaste 12 månaderna, Binance före det).
  - `--days 365` på 1h utvärderar 365 d.
  - Samma data_hash och SHA ger byte-identiskt resultat.
  - Inget headline-mått räknar den öppna slutpositionen som realiserad.

##### NA-2 · `validate`-CLI och en dokumenterad promotionsgrind
- **Vad:** ett kommando som visar om en strategi går att skilja från brus, i båda förregistrerade varianterna, och en grind som ingen strategi passerar utan den utskriften.
- **Varför:**
  - kalman har t≈0,99 och P(SR≤0)=0,19, och den sitter på en skarp parametertopp.
  - Nollhypotesens förväntade bästa Sharpe är ungefär 1,3 på 783 d och ungefär 2,6 på kalmans 195 d. Det är övre gränser, eftersom försöken är korrelerade.
  - 4 av 7 kandidater saknar stopp, så grinden kräver en förregistrerad stoppvariant.
- **Hur** (tre PR:er):
  1. `python -m hypertrade.research.validate --strategy X --variant A|B [--grid spec.yaml]` rapporterar:
     - walk-forward med fasta parametrar
     - block-bootstrap-KI och P(SR≤0)
     - buy-and-hold och en exponeringsmatchad benchmark
     - ett grannskapstest på ±20 %, där ≥70 % av grannarna ska vara netto-positiva
     - kostnadskvot, och vilka folds som bara har brutto-funding
     - Deflated Sharpe med ett handsatt **effektivt** N som skrivs ut i rapporten
     - andelen evidens från tiden före HL-listningen
     
     Variant B implementeras i simulatorn exakt enligt specen i förregistreringen.
  2. Ett look-ahead-test över hela registret: `on_candle` på trunkerade frames ska ge identiska signaler.
  3. `docs/plans/promotion-gate.md`, med bifogade rapporter, kräver:
     - OOS netto-Sharpe ≥0,8 och DSR ≥0,95
     - kostnadskvot ≤30 %
     - överlevnad vid 2× modellkostnad
     - godkänt grannskapstest
     - ett hårt stopp i den variant som bedöms (variant B uppfyller kravet för de fyra stopplösa kandidaterna)
     - sämsta trade och p95-förlust inom gräns
  4. Dagliga strategier har bara 3–8 trades per sexmånadersfold. Deras trades poolas **statistiskt** över oberoende körningar när KI räknas. Det är inte en bokreplay med coin-grinden. Poolningen är giltig för beslutet eftersom piloten (SE-3) bara kör strategier på disjunkta coins. En riktig portföljbacktest är SN-7.
- **Insats:** M (ROI) · **Påverkan:** 5/5 · **Beroenden:** NA-1, NU-10.
- **Risker:** grinden kan underkänna allt. Det är ett giltigt resultat som sparar pengar.
- **Klart när:**
  - Alla förregistrerade kandidater har en rapport i båda varianterna.
  - kalmans känslighetskarta och DSR är publicerade.
  - Varje beslut om att behålla eller pensionera hänvisar till en rapport.

##### NA-14 · Beslutspunkt B1 (senast 2026-12-04)
- **Vad:** ett skriftligt beslut i `docs/decisions/2026-12-B1.md`.
- **Underlag:**
  - validate-rapporter för båda varianterna av alla förregistrerade kandidater
  - en engångsreplay av perioden efter frysdatum (ungefär 9 veckor) med NA-1-simulatorn; den är en rimlighetskontroll och inget bevis
  - den rena ledgern (NU-6)
  - live-paperns trohet för kandidaterna enligt 5.11
  
  Veckorapporter och vault-validering ingår inte. De hinner inte bli klara och avgör inte edge-frågan, och 5.8 fattas separat.
- **Beslut:**
  - (a) Klarar minst en kandidat grinden i NA-2, i någon variant?
  - (b) Ska en mainnet-pilot finansieras?
  - (c) Är multi-tenancy ett aktivt mål?
  - (d) Ska mainnet- och/eller testnet-boten stoppas? Larmägaren är paper (5.12), så (d) tystar inga larm.
  - (e) Ska SN-7 startas, om fler än en strategi per coin ska få kapital?
- **Regel:** klarar ingen kandidat grinden fryses strategiforskningen. Paper fortsätter som framåt-holdout, mainnet finansieras inte, och bara drift och säkerhet underhålls.
- **Insats:** S · **Påverkan:** 5/5.

#### Övrigt i Nästa

##### NA-3 · Pine-trohet i live för godkända kandidater
- **Vad:** implementera den godkända varianten i live för de kandidater som B1 godkänner. Det innebär tre saker:
  - kontraktet att `on_candle` bara får stängda bars
  - dubbelstrippen borttagen bakom en flagga
  - katastrofstopp på stängd bar, där variant B har ett
- **Varför:**
  - 9 strategier beslutar en bar sent, och för cdc_macd och macd_zero är fördröjningen en dag.
  - keltner låser sitt SL vid entry, medan Pine räknar om det varje bar.
  - validate har redan räknat på båda varianterna, så liveändringen kan vänta tills den behövs.
- **Hur:**
  - Ett universellt test visar att ett kors på sista stängda bar ger signal på just den baren. Testet kan göras när som helst.
  - Flagga per strategi. Paper först, sedan testnet.
  - keltners SL-omräkning bara tillsammans med ett golv för maxförlust, eftersom Pine-stoppet vidgas när ATR stiger, och bara som en ny förregistrerad rad.
  - TradingView-fixturer bara för en strategi som ska få kapital.
  - Pensionerade strategier rörs inte.
- **Insats:** S (ROI) · **Påverkan:** 3/5 · **Beroenden:** NA-2, B1.
- **Klart när:** kontraktstestet är grönt för alla strategier, och varje aktiverad flagga har en jämförelse mellan live och replay på paper.

##### NA-4 · Avregistrera 14 strategier och rensa hävstångslogiken
- **Vad:** avregistrera de 14 som redan är avstängda. Koden behålls, så att en strategi kan återinföras via grinden.
- **Varför:**
  - 22 strategier beter sig som 2–3 korrelerade satsningar på krypto-beta.
  - Förlorarna i svepet: oleg Sharpe −4,1, volatility_breakout −$845 per $1k, hash_supertrend $98 i avgifter och supertrend 1 vinst av 8.
  - bb_short är en naken short, och dess backtest slutar i en öppen short på −45 %.
- **Hur:**
  1. Förutsättning: 0 öppna rader för de berörda namnen i alla modes. Strategierna är redan avstängda via NU-10.
  2. Flytta de 14 till `strategies/retired/`: daily_long_0830, moon_phases, hash_momentum, oleg_aryukov, qullamagi_breakout, volatility_breakout, hash_supertrend, supertrend, pivot_supertrend, ema_crossover, penguin_volatility, bb_rsi_scalper, rsi_momentum och bb_short. .pine-filerna behålls.
  3. Flytta vvv_hedge ut ur det handelbara registret, och ta bort det hårdkodade personliga innehavet ur HEAD. Git-historiken behåller värdet. En skrubbning med `git filter-repo` är valfri och destruktiv, och den kräver samordning (CLAUDE.md § 0 steg 3).
  4. Kvar blir kalman_breakout, keltner_breakout, cdc_macd, macd_zero, sma_rsi, ath_breakout (overlay, märkt in-sample) och btc_mean_reversion (bevakning).
  5. Hävstången:
     - Målet räknas bara från aktiva och allowlistade strategier.
     - En raderad override återställs till klassens default.
     - API:t gränsar hävstången till operatörens max per mode i stället för 1–50x.
  6. Uppdatera family-testerna. Ersätt spegeln i `lib/admin/strategy-names.ts` med registret, och uppdatera tenanternas allowlists. Väljer operatören (a) i 5.11 läggs ett `priority`-fält till.
- **Insats:** S–M (ROI: S; faktagranskningen: M på grund av antalet filer) · **Påverkan:** 3/5 · **Beroenden:** NU-10 och beslut 5.2. Att ta bort strategier kräver att operatören tillfrågas först, enligt CLAUDE.md § 7.
- **Klart när:**
  - Registret har exakt 7 handelbara strategier: 22 − 14 − vvv_hedge.
  - Ingen öppen rad hör till en avregistrerad strategi.
  - Spegeln är borta.
  - Ett test visar att en raderad override ger klassens default.

##### NA-5 · Radera död kod och funktioner som ingen använder
- **Vad:** ta bort det ingen använder innan det måste härdas, testas och övervakas.
- **Varför:**
  - HODL-tabellerna har 0 rader, och macro_backdrop visar alltid "no data".
  - LiveLog/SSE lyssnar på en kanal som ingen publicerar till (redis.ts:20) och läcker en anslutning per reconnect (events/route.ts:67-69).
  - golden_cross är oregistrerad.
  - `Repository.open_position/close_position` är inte atomära och har 0 anropare i produktion.
  - `indicators_status.py` är 980 rader med 6 % täckning och saknar kalman.
- **Hur:**
  - Radera allt ovan, inklusive `record_*`-skripten och CLI-sektionerna på /hodl.
  - /hodl läser 6-timmarsutvärderingen från Redis. I dag tar varje anrop 3,9 s.
  - Indikatorpanelen visar bara de strategier som finns kvar, eller tas bort.
  - Oanvända exporter som knip hittar tas också bort.
- **Insats:** S · **Påverkan:** 2/5 · **Beroenden:** NA-4 för indikatordelen · **Delegering:** kanban.
- **Klart när:** filerna, tabellerna (via migration) och endpoints är borta, och /hodl svarar på <500 ms.

##### NA-6 · Länkad ledger: fyra kolumner, signaltabell och cloid
- **Vad:** den minsta schemautvidgning som gör varje fill spårbar till position, avsikt och kodversion.
- **Varför:**
  - trades saknar `position_id` och `kind`.
  - Signalpriset ligger bara i minnet (runner.py:1282).
  - Signaler och avslag finns bara i loggar som roteras (runner.py:1885).
  - Ingen order har cloid.
- **Hur:**
  1. Migrationen lägger till `trades.kind` (open, close, flip_close, reconcile_close, backfill), `position_id`, `expected_price` och `engine_version`. De skrivs i de atomära metoderna (repo.py:242-357). Historiken flaggas utan att något raderas.
  2. En tabell `signals` med:
     - outcome (executed, refused eller error) och `refusal_gate`
     - 180 dagars retention
     - dedup per (strategi, bar, outcome)
     
     Den skrivs **best-effort utanför trade-transaktionen**, och ett test visar att ett insert-fel inte blockerar en stängning.
  3. cloid på varje order härleds ur (strategi, bar_ts, action, attempt). attempt räknas upp först när föregående cloid är terminal enligt orderStatus.
  
  Penningkolumnerna förblir float.
- **Insats:** S–M (ROI) · **Påverkan:** 4/5 · **Beroenden:** NU-4, NU-6. Krävs före SE-1 · **Delegering:** Opus med /review.
- **Klart när:**
  - Alla nya trades har kind och position_id.
  - Varje signal kan spåras till executed, refused (med grind) eller error.
  - Alla nya order har cloid.

##### NA-7 · E2E-invarianttest med paper-replay i CI
- **Vad:** ett deterministiskt test som kör den riktiga motorn mot inspelade candles och injicerar fel.
- **Varför:**
  - Det finns 0 tester där motor, PaperExchange, riktig DB och strategi körs tillsammans.
  - Granskare hittade 11 respektive 4 interaktionsfel i #172 och #174 genom att läsa kod.
  - Optional-repo är en buggklass som saknar skydd.
- **Hur:** `bot/tests/e2e/test_paper_replay.py` kör EngineRunner, PaperExchange, aiosqlite, fakeredis (nytt dev-beroende), 3–4 strategier och en injicerad klocka.
  - Felen som injiceras:
    - HL 502
    - Redis nere
    - saknad Redis-sentinel
    - DB som kastar
    - omstart mitt i en position
    - avvisad stängning, både mot en platt börs och av andra skäl
  - Efter varje tick kontrolleras invarianter, inte golden logs:
    - DB och börs stämmer.
    - Varje stängning har en trade-rad.
    - Det finns inga orphan-stängningar.
    - SL-exit körs trots kill switch, close-only (disable) och Redis- eller DB-fel.
  - Ett separat test visar att paus fryser allt, även exits, eftersom det är den beslutade semantiken (5.4).
  - `EngineRunner.repo` och `control` görs icke-Optional.
  - En delad `tests/conftest.py` ersätter 16 lokala runner-fabriker.
  - Testet blir en krävd check när det är stabilt.
- **Insats:** M (ROI) · **Påverkan:** 4/5 · **Beroenden:** NU-4, NU-5. Krävs före SE-1 och före varje ny PR på orderstigen efter NU-5.
- **Klart när:** testet körs på <90 s i CI med minst 6 felscenarier.

##### NA-8 · Deploy per SHA, med migration och rollback
- **Vad:** bygg i CI, dra ner på hosten, migrera som del av deployen och rulla tillbaka med ett kommando.
- **Varför:**
  - Cache-fällan har orsakat minst tre incidenter.
  - Det finns bara `latest`-taggar: 11 dinglande images och 3,2 GB.
  - Migrationer ingår inte i deployen.
  - Caddy-imagen är från maj.
- **Hur:** vid merge bygger CI `ghcr.io/…/xupertrade-{bot,dashboard,caddy}:<sha>`. `scripts/deploy.sh <sha>` gör följande i ordning:
  1. kontrollerar diskutrymme med df
  2. tar en dump
  3. slår på kill switch
  4. kör `alembic upgrade head` i en engångscontainer (ägarroll)
  5. kör `up -d --no-deps`
  6. anropar healthz
  7. listar bottar som kör en annan SHA
  8. slår av kill switch
  
  Rollback är samma skript med föregående SHA. Bot-omstart kräver fortfarande passfras och görs i veckofönstret (5.5). Tunna skills, `/deploy` och `/health`, läggs ovanpå. Tills NA-8 är klar gäller veckofönstret i 4.1 med manuell deploy.
- **Insats:** M (ROI) · **Påverkan:** 4/5 · **Beroenden:** NU-2, NU-4.
- **Klart när:**
  - En deploy är ett kommando och tar <10 min.
  - En rollback-drill tar <10 min.
  - Inget byggs längre på hosten.
  - Image-drift-invarianten är tyst i 30 dagar.

##### NA-9 · Kontrollpanel, säker Stop och tidsgränser i dashboarden
- **Vad:** reglage för incidenter inom två tryck, en Stop som inte i det tysta överger positioner, och ett UI som inte hänger när en bot hänger.
- **Varför:**
  - UI:t togs bort i #105, och proxyrutterna har 0 anropare.
  - Stop saknar bekräftelse och stänger inte positioner.
  - Daily-loss-brytaren stoppar opens utan att det syns (portfolio.py:227-233).
  - Dashboardens anrop mot bottarna saknar timeout (lib/bot-api.ts:75).
  - Options-sidan gör 45 anrop per 10 s och flik.
  - USER_GUIDE.md:153 är inaktuell.
- **Hur:**
  - **Kontrollrad per mode** med fyra knappar:
    - "Pausa nya entréer" (= kill switch, via en ny proxyroute)
    - "Återuppta"
    - "Stäng allt" (alert-dialog; på mainnet måste mode-namnet skrivas in)
    - "Frys" (endast operatör)
    
    Raden visar tillståndet, inklusive daily-loss.
  - **Stop-dialog** som listar öppna positioner och erbjuder två val: "Stäng allt och stoppa" eller "Stoppa – positionerna förblir öppna utan hantering". Separat finns "Starta om/uppgradera – behåll positioner". En pågående flat-all visas som pågående.
  - **Tidsgränser och polling:**
    - `AbortSignal.timeout(4000)` på dashboardens anrop mot bottarna (lib/bot-api.ts:75, _overview-view.tsx:107, options/page.tsx:42, vaults/page.tsx:97), och 504 med orsak vid timeout.
    - En delad config-poller per mode ersätter de 45 anropen.
    - Pollers pausas när fliken är dold.
  - **Övrigt:**
    - Audit-logg för varje åtgärd.
    - `frame-ancestors 'none'` och Secure-cookie.
    - USER_GUIDE rättas.
- **Insats:** S (ROI) · **Påverkan:** 4/5 · **Beroenden:** NU-5, NU-7 · **Delegering:** UI till kanban. Route och headers görs med Opus.
- **Klart när:**
  - Ett routetest täcker pause → flatten → stop.
  - Varje åtgärd syns i `tenant_audit_log`.
  - En hängande bot fördröjer översikten med högst 4 s.
  - Options gör 1 anrop per 10 s och flik.

##### NA-10 · Automatiserad veckoutvärdering (§ 8)
- **Vad:** ett schemalagt jobb som gör § 8 på ren data.
- **Varför:** § 8 har körts manuellt två gånger sedan maj, och den inbyggda digesten utvärderar ett tomt mainnet.
- **Hur:** ett cron-jobb eller en schemalagd agent som:
  - läser `positions_clean` per mode
  - räknar avgifter, funding, strategier som varit tysta i ≥14 d, och avvikelser mot validate-förväntan (2σ)
  - markerar veckor med kända datafel
  - skriver `bot/reports/weekly-YYYY-MM-DD.md` via en PR och skickar en sammanfattning på Telegram
- **Insats:** M (ROI) · **Påverkan:** 2/5 · **Beroenden:** NU-6, NU-7, NA-2. Jobbet är inget underlag för B1.
- **Klart när:** det har kommit minst 4 rapporter per månad under två månader.

##### NA-11 · Vault-filtret och HODL: hysteres, framåtvalidering och mindre brus
- **Vad:** ta reda på om "qualified" betyder något innan någon handlar på det, och sluta skicka meddelanden som bara är brus.
- **Varför:**
  - Kvalificerade vaults gav +1,1 % mot +7,6 % för avvisade.
  - 6 vaults flippade 5–11 gånger.
  - Ett misslyckat scan väntar 24 h i stället för att försöka igen (poller.py:98-102, runner.py:737-740).
  - HODL-verdikten innehåller dagräknare (btc_ath_breakout.py:100-104), och vault_picks upprepar vault-events.
- **Hur:**
  - Hysteres: en vault diskvalificeras först efter N misslyckade scans i rad.
  - Ett CLI och ett kort på /vaults visar 30/60/90-dagars framåtavkastning för kvalificerade vaults mot kandidater, ur vault_nav_history. FilterConfig justeras efter det.
  - Retry med backoff vid 429.
  - HODL notifierar bara vid zonbyte (enum, senaste zonen sparas i Redis), och vault_picks notifierar inte alls.
  - Vault-events samlas i en veckodigest.
- **Insats:** M (ROI) · **Påverkan:** 2/5 · **Beroenden:** NU-7.
- **Klart när:** framåtrapporten är publicerad och beslut 5.8 är fattat utifrån den.

##### NA-12 · Equity-snapshots, index och Postgres-grundskydd
- **Vad:** sluta lagra dubbletter, gör frågorna oberoende av antalet tenanter, låt databasen vägra dubbla öppna positioner, och ge Postgres tidsgränser och mätning.
- **Varför:**
  - 80 % av DB:n är dubbletter och nollrader.
  - Det finns inget kompositindex, och ingen partiell unik constraint, trots att koden använder `scalar_one_or_none()` (repo.py:336, 375).
  - `statement_timeout` och `idle_in_transaction_session_timeout` är 0, `pg_stat_statements` är inte laddat, och poolerna står på default (repo.py:133, lib/db.ts:30).
- **Hur:**
  - Skriv snapshots bara när värdet ändras, plus en heartbeat var 15:e minut. Ingen snapshot när equity är 0 och inga positioner finns.
  - `CREATE INDEX CONCURRENTLY (tenant_id, mode, timestamp DESC)`.
  - Ett partiellt unikt index på öppna positioner, **speglat som en kontroll före ordern**. Då vägrar ett brott ordern i stället för att skrivningen efter fill misslyckas. Körs i ett fönster med kill switch på.
  - `statement_timeout=30s` och `idle_in_transaction_session_timeout=60s` på app- och tenant-roller.
  - Ladda `pg_stat_statements` och sätt `log_min_duration_statement=250ms`.
  - Explicita poolstorlekar: bot pool_size 2 och max_overflow 3; postgres.js max 5–10.
- **Insats:** S (ROI) · **Påverkan:** 2/5 · **Beroenden:** NU-2, NA-8.
- **Klart när:**
  - Tillväxten är högst ungefär 2 400 rader per dag totalt, och EXPLAIN visar kompositindexet.
  - Frågor över 250 ms syns i loggen.
  
  Underlagets mål på ≤400 rader per dag var orealistiskt, eftersom bara ungefär 10 % av paper- och testnet-raderna är identiska.

##### NA-13 · Förbered mainnet-nyckeln (utan att finansiera)
- **Vad:** rätta nyckelmappningen och ta reda på vad som är fel med plånboken, så att ett ja i B1 inte börjar med en känd bugg.
- **Varför:** mainnet-boten signerar med den nyckel som UI:t kallar testnet (config.py:43-47, bot-orchestrator.ts:312-321). Det är inte bekräftat om felet "User or API Wallet does not exist" beror på mappningen eller på att HL rensade agenten.
- **Hur:**
  - `HYPERLIQUID_MAINNET_*` mappas till `HYPERLIQUID_PRIVATE_KEY` och `ACCOUNT_ADDRESS`, bara för mainnet. Saknas de vägrar boten att starta, utan fallback.
  - En knapp "Testa anslutning" per nätverk.
  - Diagnosen via `extraAgents` görs redan i NU-7.
  - Runbook: skapa en ny agent, och återanvänd aldrig en rensad nyckel (HL varnar för replay-risk). Nuvarande nyckel får löpa ut om B1 blir nej.
- **Insats:** S (ROI) · **Påverkan:** 3/5 · **Beroenden:** NU-1 och NU-2 (orphan-vakten, annars stänger en ny nyckel manuella positioner). Krävs före SE-3.
- **Klart när:** ett orchestrator-test bekräftar att nyckeln är MAINNET_*-hemligheten, TODO:n är borta och diagnosen är dokumenterad i bilaga P.

##### NA-15 · Intern segmentering och minsta behörighet
- **Vad:** skyddet mot en komprometterad bot eller ett komprometterat beroende, som NU-8 inte täcker.
- **Varför:** bottarna och dashboarden delar interna tjänster som i dag litar på nätverket (detaljer i bilaga P). Ett gemensamt lösenord ensamt hjälper inte, eftersom bottarna då har samma lösenord som dashboarden.
- **Hur:**
  1. Redis-ACL med två användare:
     - `dashboard` med full åtkomst
     - `bot` med bara `~hypertrade:*`, `~paper_exchange:*` och kanalerna `&hypertrade:*`
     
     Default-användaren stängs av, och inloggningsuppgifterna går genom REDIS_URL. Verifiera nyckelmönstret för mainnet-opt-in mot control.py:268-287 innan ACL:en skrivs. En bot som nekas läsning där ser noll strategier; det är säkert, men det måste testas.
  2. Administrativa gränssnitt ligger på ett nät som bottarna inte är med i.
  3. Bot-containrar körs med `USER 10001`, `CapDrop ALL`, `no-new-privileges` och `PidsLimit 256`.
  4. Dashboarden återapplicerar önskad TLS vid boot.
  5. ACL per tenant skjuts till SE-4.
- **Insats:** M · **Påverkan:** 4/5 · **Beroenden:** NU-8 och veckofönstret, eftersom bottarna startas om med en ny REDIS_URL.
- **Risker:** en för snäv ACL ger en bot som tyst ser noll strategier eller ingen paus. Testa i paper först, med ett test som läser varje kontrollnyckel som bot-användaren.
- **Klart när:**
  - Bot-användaren kan bara läsa och skriva sina egna prefix (test).
  - Adminslutpunkten går inte att nå från en bot.
  - Bottarna kör som icke-root.
  - Paus, kill switch och opt-in fungerar i paper med bot-användaren.

### 4.4 Sedan (3–6 månader): bara det B1 motiverar

#### SE-1 · Statisk katastrof-backstop på HyperLiquid (startar vid mainnet = ja)
- **Vad:** ett brett reduce-only stop-market-trigger på börsen för varje öppen position.
- **Varför:**
  - Inga stopp ligger på börsen.
  - 15 av 17 avbrott över 30 min lämnade 1–3 positioner oskyddade, som längst i 22,7 h.
  - 10 av 22 strategier saknar SL, och audit M3 byggdes aldrig.
- **Hur:**
  1. Ett kontrakt `Strategy.current_stop()` med tester. Runnern läser i dag aldrig `Signal.stop_loss`.
  2. Efter varje OPEN-fill läggs ett stopp på det större av 3×ATR och −8 %, men högst 0,5 × avståndet till likvidation. Stoppet räknas från **fyllnadspriset**, eftersom testnet ligger 0,3–0,6 % från mainnet (olika ögonblicksbilder).
  3. Fail-safe: om stoppet inte syns vilande i openOrders inom N s stängs positionen reduce-only och ett larm går.
  4. Stoppets livscykel:
     - Det avbryts vid stängning eller flip.
     - reconcile verifierar det och lägger om det.
     - Trigger-fills bokas via cloid innan orphan-logiken körs, och strategins state återställs.
  5. Koden kräver `allow_multi_coin=0` medan börsstopp är aktiva, eftersom positionTpsl gäller hela coinet.
  6. HL:s adressbudget räknas, med larm vid 50 %.
  7. Strategier som stänger på close, som ath_breakout, får bara backstop. scheduleCancel kombineras aldrig med stoppen.
- **Insats:** M (ROI), plus 1–2 dagar för current_stop · **Påverkan:** 5/5 · **Beroenden:** NU-5, NA-6, NA-7, NA-13 och B1 = ja.
- **Risker:** testnets trigger-semantik är inte verifierad. Testa med minsta storlek.
- **Klart när:**
  - I 14 dagar på testnet har 100 % av de öppna positionerna ett vilande reduce-only-stopp. En invariant kontrollerar det var 5:e minut.
  - Drill: med botcontainern stoppad i 2 h ligger stoppet kvar och stänger positionen, och DB bokför korrekt vid omstart.

#### SE-2 · Riskbudget i procent av equity
- **Vad:** tak och storlekar som betyder samma sak på $870 som på $10k, och en brytare som räknar med orealiserade förluster.
- **Varför:**
  - Fast notional (runner.py:2604-2613) ger ungefär 35 gånger så stor risk per trade i den ena änden av skalan som i den andra.
  - Daily-loss räknar bara realiserat (portfolio.py:228). $100 motsvarar 11,5 % av testnet men 1 % av paper.
  - `signal_size_max_multiplier=10`.
- **Hur:**
  - En ren storleksfunktion som både motor och simulator importerar: `notional = equity × risk_pct / max(stoppavstånd, k·ATR)`, med `MAX_POSITION_SIZE_USD` som tak.
  - Taken räknas i % av equity, inklusive orealiserat.
  - En brytare vid −10 % från 30-dagarshögsta sätter close-only.
  - Oläsbar eller noll-equity blockerar opens, och två konsekventa läsningar krävs. Testnet har 431 falska nollrader.
  - Marginal och avstånd till likvidation övervakas på mainnet.
  - Multiplikatorn sätts till 1–2 på mainnet.
  - EWMA-kovarians, vol-target och beta-tak stryks tills mer än en validerad strategi har kapital.
- **Insats:** M (ROI: S för storlek och tak; HWM och marginal tillkommer för riktiga pengar) · **Påverkan:** 4/5 · **Beroenden:** NU-6, NA-1 och B1 = ja.
- **Klart när:**
  - Dollarrisken vid stopp skiljer högst 2× mellan strategier.
  - En simulerad drawdown på −10 % ger close-only inom en tick.
  - Alla tak visas i %.

#### SE-3 · Mainnet-pilot enligt kapitalstege
- **Vad:** de första riktiga pengarna. Uppgiften är att **mäta exekveringskostnad**, inte att bevisa edge.
- **Varför:** testnet kan inte förhandsvisa mainnet, och verklig slippage mot modellen är okänd.
- **Hur:**
  - **Kapitalstegen** skrivs i `docs/plans/capital-ladder.md` (en halv sida):
    - Steg 1: 1–2 strategier som klarat grinden, på **disjunkta coins**, en liten ram och total exponering ≤1× equity. Ingen coin delas, så grinden binder inte och ingen portföljbacktest behövs.
    - Kapitalet dubblas bara när fyra villkor är uppfyllda: live-trades ligger inom backtestens 95 %-band, shortfall ≤ modell + 2 bps, 0 ledger-brott och 100 % stopptäckning.
    - Vid brott sänks steget automatiskt.
  - **Boot-preflight i kod:** extraAgents giltig i mer än 14 d, backstop på, orphan-vakt på, stopp för varje allowlistad strategi, och kill switch som default.
  - Avgift och closedPnl läses från fills (reconcile/fills.py har redan parsningen), plus `mid_at_submit`.
  - HL-referralkod (−4 %).
- **Insats:** M (ROI: S för stege och preflight; bokföringen från fills är M) · **Påverkan:** 4/5 · **Beroenden:** SE-1, SE-2, NA-13, NU-8, NA-15.
- **Klart när:** inom 60 dagar finns fillbaserad shortfall, avgift och funding per trade jämfört med modellen, och varje stegändring har ett skriftligt evidensprotokoll.

#### SE-4 · Multi-tenant-grund (startar vid multi-tenancy = ja)
- **Vad:** det som måste finnas innan en andra tenant kör en bot.
- **Varför:**
  - Nio nyckelfamiljer, paper-state och kanalerna saknar tenant.
  - En tenants flat-all stänger en annan tenants konto.
  - Telegram läcker mellan tenanter.
  - Dashboarden går förbi RLS.
  - Identiteten nycklas på e-post.
- **Hur:**
  - Nycklar på formen `hypertrade:t:{tenant}:{mode}:*` (tenant_bots är unik på tenant och mode). tenant_id på Event, och en notifier per tenant.
  - Migrationen körs med kill switch på och med dual-read, där en union stänger vid fel: boten räknas som pausad om någon nyckel säger pausad.
  - Isolationstester i CI.
  - Redis-ACL per tenant-bot, som bygger på NA-15.
  - Tenant-scopad SSE, och identitet på (iss, sub).
  - En `/setup`-checklista (passfras → paperbot → larm) och grå status "ej skapad".
  - **Fråga betaanvändaren först** om hen vill vara med.
- **Insats:** L (ROI: M–L plus M) · **Påverkan:** 4/5 · **Beroenden:** NU-8, NA-7, NA-15.
- **Klart när:** en Redis-skanning hittar 0 nycklar utan tenant-del, och en drill visar att operatörens flat-all inte påverkar en beta-paperbot.

#### SE-5 · Nattlig replay och avstämning av framåt-holdout (startar om B1 ger en kandidat)
- **Vad:** löpande framåtkurvor för de förregistrerade kandidaterna, beräknade på data efter frysdatum, plus jämförelse mellan live och replay. Vid B1 görs samma sak en gång manuellt (NA-14).
- **Varför:**
  - Framåtbevis i live tar 0,7–5 år, för kalman ungefär 2,5 år.
  - Replay kostar inget och påverkas inte av coin-grinden.
- **Hur:**
  - Kör validate-simulatorn på perioden efter frysdatum, för båda varianterna, och publicera kurvan på /strategies.
  - Diffa mot trades. Larm under 95 % matchning.
  - Avvikelser kategoriseras: omstart, 429, testnet-basis och grindnekanden (ur signals-tabellen).
- **Insats:** M · **Påverkan:** 3/5 · **Beroenden:** NA-1, NA-2, NA-6.
- **Klart när:** kurvorna uppdateras varje dag och larm kommer inom 24 h.

### 4.5 Senare (bara med en uttalad trigger)

| ID | Initiativ | Trigger | Insats |
|---|---|---|---|
| SN-1 | Trail-synkade börsstopp, en order-intent-journal (avsikt skrivs före ordern, och okända fills adopteras i stället för att orphan-stängas), och WS-ström för userFills och orderUpdates | Piloten skalas förbi steg 1 | L |
| SN-2 | Target-position- eller netting-executor (tar bort coin-grinden och flip-maskineriet) | Minst 2 validerade strategier delar coin och grinden begränsar mätbart efter beskärningen | XL |
| SN-3 | Diversifierande forskning. Först en offline-sond på en dag: kalman/Donchian i validate på S&P-, guld- och oljefutures. HIP-3 i live-wrappern bara efter att grinden klarats. | En strategi med korrelation <0,3 mot boken klarar grinden | L |
| SN-4 | Plattformsskala: flottutrullning utan lösenfras, PgBouncer och admission control på hela hosten, WS-candle-tjänst och delad token-bucket, flera hostar | Fler än 20 bottar eller fler än 5 tenanter | L |
| SN-5 | Kodhälsa: runner-komponenter extraheras bakom NA-7 när en ändring kräver det; mypy-ratchet; `Strategy.describe()`; `Mapped[]`; krävd `alembic check` | När en konkret ändring behöver det | L |
| SN-6 | Maker-first-exekvering (ALO → IOC) | Mer än ungefär $25k deployerat eller fler än 200 tur-retur per år, och SN-1 klar | M |
| SN-7 | Portföljbacktest. Grindlogiken i `_open_refusal` (coin, familj, exponering) görs till en ren funktion som både motor och simulator använder, och en barsynkron `run_portfolio_backtest` körs över kandidaterna. | Två kapitalbärande strategier delar coin eller familj, eller 5.11 ändras till (a) och boken ska bevisas som helhet | L |

---

## 5. Beslut som operatören måste fatta

| # | Beslut | Rekommendation |
|---|---|---|
| 5.1 | **Mainnet** | (a) Avväpna nu med kill switch per mode och tom opt-in (NU-1), inte via de globala env-variablerna. (b) Stoppa mainnet-containern när NU-7 är klar och drillen är grön. Containern skriver ungefär 1 424 identiska rader per dag, plånboken avvisas och nyckeln löper snart ut. (c) Finansiera inte före B1. Blir det ja: pilot enligt SE-3 med en ny agent, aldrig med den gamla nyckeln. |
| 5.2 | **Strategier att pensionera** | Pensionera 14: daily_long_0830, moon_phases, hash_momentum, oleg_aryukov, qullamagi_breakout, volatility_breakout, hash_supertrend, supertrend, pivot_supertrend, ema_crossover, penguin_volatility, bb_rsi_scalper, rsi_momentum och bb_short. vvv_hedge tas ur det handelbara registret. Behåll 7 (NA-4). Slå **inte** ihop cdc_macd och macd_zero. Alla 14 stängs av redan i vecka 1, när de är platta (NU-10). Det går att ångra. Avregistreringen, som kräver att operatören tillfrågas först enligt CLAUDE.md § 7, kan vänta. |
| 5.3 | **Fler tenanter** | Inte nu. Behåll `max_active_bots=0`, fråga betaanvändaren en gång om intresse, och ta beslutet i B1. Multi-tenant-plattformen (SE-4) har inget värde förrän det finns både edge och en användare som vill in. |
| 5.4 | **Vad pause betyder** | Behåll pause som frys. Automatiska vakter slår på kill switch, inte paus (NU-2, NU-5). Knappen "Pausa nya entréer" använder kill switch, så exits fortsätter. |
| 5.5 | **Lösenfrasfri botuppgradering (trust model B)** | Nej så länge det finns 3 bottar. Lås upp en gång i veckofönstret (4.1), inte vid varje merge. Ta upp frågan igen vid fler än 5 tenanter. |
| 5.6 | **Testnets roll** | Rörtest för exekvering, inte evidens för strategier. Kör bara kandidatuppsättningen där, enligt 5.11. Evidens för strategier kommer från validate och replay. Live-paper, med funding från NU-6.5, är en trohetskontroll. |
| 5.7 | **/kelly** | Ta bort kommandot. Det är bara rådgivande, bara daily_long har tillräckligt med trades, och siffrorna blir vilseledande. Visa i stället f* med KI i validate-rapporten. Att ta bort en endpoint kräver att operatören tillfrågas först. |
| 5.8 | **Vault-innehaven** | Gå igenom innehaven mot filtret manuellt nu (bilaga P), eftersom det är de enda riktiga pengarna. NU-3(e) larmar framöver när ett innehav underkänns. Låt NA-11 avgöra om etiketten "qualified" ska visas alls, eller om scannern ska bli ren bevakning. |
| 5.9 | **Börsstopp: primärt eller backstop** | Bara en bred backstop först (SE-1). Ett tätt primärstopp ändrar beteendet jämfört med Pine och kräver ny validering. Katastrofstoppet i variant B är redan validerat på stängd bar. |
| 5.10 | **Om B1 blir nej** | Frys strategiforskningen och låt paper löpa. Finansiera inte. Underhåll bara drift och säkerhet (NU-2, NU-3, NU-8, NA-8, NA-15). |
| 5.11 | **Coin-grinden för den beskurna boken** | Det finns två alternativ. (a) Behåll grinden och acceptera att live-datan formas av den. (b) Kör en kandidat per coin i live-paper och testnet, och validera övriga kandidater bara offline och i replay. **Rekommendation: (b)**, med ETH kalman_breakout, SOL cdc_macd och BTC btc_mean_reversion. btc_mean_reversion har SL och handlar tillräckligt ofta för en trohetskontroll. macd_zero kan inte köras samtidigt som cdc_macd, eftersom de delar familj. Med (b) får kalman en live-historik som inte störs av grinden, och ingen prioritet behövs. `allow_multi_coin=1` är uteslutet i båda fallen, eftersom HL nettar per coin. I båda fallen kommer evidensen för edge från validate och replay, inte från live-PnL. |
| 5.12 | **Ägare för larm och sidotjänster** | Paper, konfigurerbart (NU-7). Paper körs alltid, behöver ingen börsnyckel och kan därför inte rensas av HL. Då kan B1 stoppa testnet och mainnet utan att larmen tystnar. Ägaren får bara bytas eller stoppas när en ny ägare redan körs. |

---

## 6. Vad vi medvetet inte gör, och varför

- **Inga nya strategiportar från TradingView eller "money-printer"-listor.** De 20 som finns valdes ut bland 236, mestadels på in-sample-resultat. Varje ny port höjer tröskeln för Deflated Sharpe.
- **Ingen gridsökning över hela urvalet**, varken för kalman eller andra. Standardinställningen ligger redan på en skarp topp. Justering sker bara inuti walk-forward-folds.
- **Ingen portföljbacktest före B1.** B1 bedömer strategier en och en, och piloten kör 1–2 strategier på disjunkta coins, så coin-grinden binder inte. Bokreplayen (SN-7) startar när två kapitalbärande strategier delar coin.
- **Ingen Kelly- eller PnL-baserad storlek i live** innan ledgern stämmer och SE-2 finns.
- **Rå testnet-PnL och historik per strategi från före 2026-09-23 används inte som strategievidens.**
- **Mainnet finansieras inte före grinden, och en rensad agentnyckel återanvänds aldrig.**
- **`allow_multi_coin=True` används inte** för att kringgå coin-grinden, eftersom HL nettar per coin.
- **Ingen ML, inga tidsramar på 1m eller 5m, och ingen market making.** Med 60 s polling, och med avgifter som utgör hela exekveringskostnaden, överlever inget högfrekvent.
- **Ingen funding-carry-motor.** Den skulle ge ungefär $50–80 per år på nuvarande storlek.
- **Andra HL-funktioner vi avstår från:**
  - vault leading (10k USDC i avgift)
  - builder codes
  - TWAP och scale orders
  - sub-accounts (kräver $100k i volym)
  - HyperEVM
- **Ingen netting-executor, WS-fillström, maker-exekvering eller HIP-3 i live nu.** Alla ligger under Senare med triggers.
- **Ingen NUMERIC-migrering, ingen TimescaleDB, Kubernetes, Kafka eller egen Prometheus/Grafana-stack.** DB:n är 117 MB. healthchecks.io och SQL-invarianter räcker.
- **Ingen inspelartjänst för OHLCV, inget nattligt scorecard, inget trial registry i DB och ingen kapitalstege i kod med CUSUM.** Parquet, markdown och en boot-preflight gör samma jobb med en bråkdel av underhållet.
- **Live-lookback på 1 000 bars införs inte** före candle-cache och validering.
- **Ingen djupbaserad impact-modell och inget vol-target för portföljen** med 7 korrelerade strategier.
- **Ingen omskrivning av runner.py i ett svep**, och inga extraktioner utan NA-7.
- **Ingen PWA eller native-app, ingen AI-insiktschatt, ingen marknadsplats och ingen fakturering.** Det finns en aktiv användare och ingen bevisad produkt.
- **Ingen massformatering med ruff, ingen downgrade-loop i CI och inga Drizzle-drifttester nu.** Det är churn utan motsvarande skydd.
- **Ingen merge på testsiffror som en agent har rapporterat, och inga back-to-back-merges av stora PR:er** när NU-4 väl finns.
- **Inget i committade dokument som bryter mot CLAUDE.md § 0.** Det gäller detaljer om öppna säkerhetsluckor, innehav, saldon, nyckeldatum, ping-URL:er, LAN-intervall och e-postadresser. Allt sådant ligger i bilaga P, i Phase eller i lokala filer på hosten.

---

## 7. Risker med planen och hur vi mäter att den fungerar

| Risk | Tidig signal | Motåtgärd |
|---|---|---|
| Kapaciteten räcker inte. Nu är 27 PR:er (11 med full review) och den kritiska vägen till B1 är 35. | Färre än 8 av 11 Nu-initiativ klara i vecka 4, eller NA-1 inte klar i vecka 7 | Skjut NU-9, NU-11-hooken, NU-6.5 och NU-7.4 till v5–6. Övrigt i Nästa väntar. B1 flyttas högst två veckor och bara med ett skrivet beslut. |
| Reviewkostnaden äter Fable-/Opus-kvoten och OpenRouter-budgeten | Över 1,5 M tokens per liten PR, eller över cirka 32 M före B1 | 3–4 vinklar på små diffar, kanban för det mekaniska och PR:er under 600 rader. |
| Grinden underkänner allt | B1-rapporten | Det är ett giltigt utfall. Beslut 5.10 är skrivet i förväg så att planen inte driver iväg. |
| Framåt-holdouten är bara ungefär 9 veckor vid B1 | – | B1 vilar på validate, och replayen är en rimlighetskontroll. SE-5 fortsätter efter ett ja. |
| Variant B (fixad och med stopp) sänker kalman | Validate för båda varianterna | Båda är förregistrerade. Beslutet fattas på siffrorna, och liveändringen görs först i NA-3 efter B1. |
| Heuristisk märkning av fantomer och backfill blir fel | Invariant (b) avviker efter NA-6 | Taggarna går att ta bort igen och matar aldrig grindar. Originalraderna raderas inte. |
| Larmtrötthet gör invarianterna verkningslösa | Mer än 1 falsklarm per vecka | En vecka med bara loggning, krav på två dagar i rad, och trösklar trimmade mot historiken. |
| Den automatiska DR-vakten fryser exits | Eskalerande larm | Paus bara vid bekräftat inaktuell DB, och eskalering efter N minuter. Annars kill switch. |
| Larmägaren stoppas av misstag | NU-7-drillen, orchestratorvarningen | Ägaren är paper. Ägaren får bara bytas eller stoppas när en ny ägare redan körs. |
| Säkerhetsändringar låser ute operatören | Misslyckad Access-inloggning | Break-glass via LAN beskrivs i bilaga P. nft införs först när konsolen är tillgänglig. |
| Säkerhetsdetaljer, innehav eller privata värden hamnar i det publika repot | Träff i gitleaks eller hook; granskning av docs-PR:er | Publiceringsregeln överst. Privata värden hämtas ur Phase eller från lokala filer, och nya mönster läggs till i pre-commit och `.gitleaks.toml` (NU-2). |
| En för snäv ACL i NA-15 ger en bot som tyst ser noll strategier | Ett test av kontrollnycklarna i paper | Paper först och ett test per kontrollnyckel. Opt-in-setet stänger vid fel, så felet är säkert. |
| En migration mot live-DB slår sönder en skrivning efter fill | TradeDbDivergence eller auto-paus | Kill switch under migrering (NA-8). Constraints speglas som kontroll före order, och en dump tas före varje migration. |
| B1 skjuts upp och planen fortsätter på slentrian | Inget beslutsdokument 2026-12-04 | B1 är ett eget initiativ (NA-14). Utan beslut startas inget Sedan-initiativ. |
| CI blir flakigt och kringgås | Admin-bypass används mer än en gång per månad | Karantän med ägare och utgångsdatum. Bypass loggas och granskas varje månad. |

**Uppföljning.** North-star-tabellen (avsnitt 3) uppdateras vid varje månadsskifte och vid B1, i `docs/roadmap-status.md`. Följande ledande indikatorer följs varje vecka:

- andel Nu-initiativ som är klara, och PR:er kvar på den kritiska vägen
- median för tillagda rader per mergad PR
- antal invariantlarm och falsklarm
- CI-tid p50

Planen fungerar om följande gäller vid B1:

1. De externa larmen är bevisade med drill, och NU-2-vakterna är testade.
2. Alla merges har gått igenom krävd CI.
3. Ledger-residualen på testnet är under $1 per månad, och paper och testnet får funding-rader varje timme med öppen position.
4. Varje kandidat har en validate-rapport i båda varianterna.
5. B1-beslutet är fattat på det underlaget.

---

## Bilaga: påståenden i underlaget som har korrigerats

**Från tidigare granskningar:**

- **"29 % fantomstängningar"** gäller historiken. Det har varit 0 sedan #167 (09-23). Det som återstår är korrupt historik, ingen pågående läcka.
- **"Funding har aldrig bokförts"** stämmer inte helt. Det finns exakt 1 rad, från 2026-04-28.
- **"19 av 22 strategier på BTC/ETH/SOL"** ska vara 21 av 22 (BTC 11, ETH 7, SOL 3, VVV 1). Det finns 11 unika (symbol, tf)-par, inte "11–12".
- **bb_short "−45 %"** är backtestens slutmarkering, ingen live-position.
- **"kalman 15/18 fills mot live"** jämför två simuleringar med varandra, inte simulering mot live.
- **Nollhypotesens trösklar 1,3/1,9** gäller för 783 dagar och oberoende försök. De är övre gränser. För kalmans 195 dagar är tröskeln ungefär 2,6.
- **Kill switch läser inte ledgern.** Daily-loss bokför reconcile-PnL från fills sedan #167.
- **ETH 8x** påverkar marginalreservationen, inte positionsstorleken.
- **Den enda observerade 429:an** kom från vault-API:t, inte från candles. REST-vikten är uppskattad, inte uppmätt.
- **Att spärra beta-bottar kräver ingen kod**, eftersom `max_active_bots` finns sedan alembic 0016.
- **`migrate.sh:7`** är en inaktuell användningskommentar, inte ett trasigt anrop.
- **`cancel_order` finns redan** i Exchange-ABC:n. positionTpsl-grouping är en parameter till `bulk_orders`, inte till `order()`.
- **Alembic kan inte migrera en tom DB**, eftersom 0011 kräver en operatörstenant. Det blockerar CI-migrationsjobbet tills det är rättat (NU-4). Det här var ett nytt fynd i förra rundan.
- **Målet ≤400 equity-rader per dag** går inte ihop med write-on-change. Ett rimligt mål är ungefär 2 400 per dag.
- **Minustecken-buggen** sitter på pnl-breakdown.tsx:182-195, inte 271-284.
- **Dubbelstrippen i pivot_supertrend** är en repaint av historiska pivotcentra, inte look-ahead. Strategin pensioneras ändå.

**Från den slutliga faktagranskningen (2 granskare per påstående):**

- **Backtester:** buggen med tenant_id är latent. Ingen backtest har körts sedan 2026-05-01, så inga resultat har förlorats.
- **Backuper:** PBS tar dagliga backuper av containern (operatören, 2026-09-24). "Inga backuper, RPO ≈136 d" var fel; det som saknas är konsekventa dumpar och ett testat restore, och den risken har operatören accepterat. Manuella Redis-kopior finns dessutom från 07-29 och 09-23.
- **Avbrottet 06-17** var en deploy som väntade på upplåsning, inte ett tyst haveri. Positionerna var ändå oskyddade.
- **Candle-timeouten** är hårdkodad i feed.py, inte i config.py.
- **Larmen** från testnet och paper går ut via mainnet-processen. Problemet är beroendet av den processen och digestens filter.
- **Den sena baren** gäller 8 Pine-portar plus egna vvv_hedge.

**Från fullständighetsgranskningen:**

- **NU-1:s mekanism var fel.** `KILL_SWITCH` och `MAINNET_ENABLED_STRATEGIES` är globala variabler på dashboard-tjänsten och injiceras i alla bottar (bot-orchestrator.ts:259-283). Mainnet-boten har inget compose-mål. Den rätta vägen är Redis-overriden per mode och en tom tenant-opt-in.
- **Kriteriet "NOAUTH från en bot" i NU-8** motsade att bottarna skulle få samma Redis-lösenord. Skyddet mot en komprometterad bot kräver ACL och ligger nu i NA-15.
- **NA-7:s invariant "SL-exit trots paus"** motsade beslutet att pause är frys. Nu gäller "trots kill switch, close-only och Redis- eller DB-fel", plus ett test att paus fryser.
- **"kalman nekas inte längre av sma_rsi"** gick inte att nå med prioritet utan preemption. Det ersätts av beslut 5.11.
- **bb_short saknades i beskärningen.** 13 + 7 + vvv_hedge är 21 av 22. Nu pensioneras 14.
- **"Alla larm går genom mainnet-boten"** gäller trade-, fel- och digestlarm. Bot-down-larmen går redan förbi.
- **"Den bästa beta-justerade alfan (keltner, t=1,63)"** finns inte i underlaget och har strukits. Alfan på boknivå är t=0,63 på paper och t=−1,37 på testnet.
- **mypy:s 10 Optional-dereferenser** ligger på åtta ställen. Fyra av dem finns i runner.py, resten i vaults/api.py och telegram.py.
- **"Ledger-residual < $1/mån på paper"** var trivialt uppfyllt, eftersom paper saknar funding och slippage. Måtten har nu ett omfång per mode.
- **Testnet-basisen** skiljer sig mellan ögonblicksbilderna: BTC/ETH 0,33–0,63 %. Den anges nu som ett spann.
- **Sekvensfel som har rättats:**
  - healthchecks-kontot skapas i NU-2 och inte i NU-3.
  - Den historiska acceptanskörningen i NU-3 gäller bara (a) och (c).
  - Förregistreringen omfattar båda varianterna.
  - B1:s underlag omfattar inte veckorapporter och vault-validering.
  - Botimagens mått följer veckofönstret.
  - Larmägaren är paper, så att B1 kan stoppa testnet.
- **Uppgifter om mainnet-saldo, nyckeldatum, vault-innehav och 2025 års belopp** har flyttats till bilaga P, enligt CLAUDE.md § 0.
