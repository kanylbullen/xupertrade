# NU-5 C · Motorägd stängning: plan för sign-off

*Plan, inte kod. Skriven 2026-10-14, när operatören delade #192 längs roadmapens NU-5 (`docs/plans/next-level-roadmap.md`). Underlaget är origin/master eefc492, #192:s huvud 47f844e, A-prototypen och uppdelningsspecen med två kritikers granskning. Ingen kod för C skrivs förrän planen är signerad och frågorna i § 8 har svar.*

> **Hänvisningar.** `runner.py` är `bot/hypertrade/engine/runner.py`, `repo.py` är `bot/hypertrade/db/repo.py`, `portfolio.py` är `bot/hypertrade/engine/portfolio.py`, `strategy_allowlist.py` är `bot/hypertrade/engine/strategy_allowlist.py` och `hyperliquid.py` är `bot/hypertrade/exchange/hyperliquid.py`. Strategierna ligger i `bot/hypertrade/strategies/`. En hänvisning utan commit gäller origin/master. `@47f844e`, `@8d27bbe` och `@b292005` är commits på #192. A0, A, B och avräknings-PR:en finns inte på master än. Deras funktionsnamn kommer från A-prototypen och uppdelningsspecen och märks "(A)" eller "(B)".
>
> **Publiceringsregel (CLAUDE.md § 0).** Repot är publikt. Planen innehåller inga värden, adresser, saldon eller innehav.

---

## 1. Sammanfattning

- **Problemet.** En exit som HL vägrar medan positionen finns kvar försöks aldrig igen på master. Efter A försöks den bara så länge strategins egen regel fortsätter att ge signal. En kanttriggad exit ger signal på en enda bar. Två av de tre kandidater som beslut 5.11 rekommenderar för live, kalman_breakout och cdc_macd, har bara sådana exits och inget SL. När baren är slut håller DB och börs samma position, och ingen stänger den. Reconcile ser inget fel, eftersom de två är överens (exits-never-blocked-1, high).
- **Förslaget.** När en stängning har missat blir den motorns. Den skickas varje tick, reduce-only och med radens DB-storlek, tills den fylls, raden bokförs som stängd utanför boten eller raden stängs. Bara en läsbar paus stoppar den (beslut 5.4). Skulden sparas i en ny nullbar kolumn, `positions.pending_close` (Alembic 0018), så att den överlever en omstart.
- **Vad #192 lärde oss.** #192 byggde samma sak med en markering i `state_json`. Över fyra rundor växte den till ungefär 1 900 kodrader. De två sista medelallvarliga regressionerna kom från extra markeringar i samma fält (`unsure` och `_engine_drift_rest`, § 7). C tar inte med någon av dem.
- **Vad operatören behöver göra:** svara på de tolv frågorna i § 8. Varje fråga har en rekommendation.
- **När.** Koden börjar efter sign-off, och efter att A0, A, B och avräknings-PR:en är mergade. C rör orderstigen och lägger till en migration. Arbetet görs därför med Opus och alla åtta /review-vinklar, och bara en PR på orderstigen får vara öppen i taget (roadmap § 4.1). Den här plan-PR:en rör bara docs och kan därför löpa parallellt.

---

## 2. Problemet

### 2.1 Master i dag

- **En stängning som inte fylls glöms.** `runner.py:_execute_signal` loggar "Order not filled" och returnerar False. Strategin släppte positionen redan när den gav sin exit: `cdc_macd.py:on_candle` sätter `_in_position = False` innan den returnerar CLOSE_LONG. Ingen kod skickar stängningen igen.
- **En flip vars stängningshalva misslyckas** synkas tillbaka till den gamla raden (`runner.py:_flip_close` → `_align_strategy_after_failed_flip_close`). Flippen kommer inte igen förrän vid nästa kant.
- **Reconcile rör inte en rad som börsen backar på samma sida.** `repo.py:_classify_closes` tar bara orphan- och wrong-side-rader. Storleksavvikelser på samma sida loggas bara (`repo.py:_log_size_mismatches`).

### 2.2 Vad som återstår efter A

A (`fix/nu-5a-reduce-only-closes`, ej mergad) gör varje stängning reduce-only och klassar en vägrad stängning (`_close_left_open` (A)). Om börsen har positionen kvar återställs strategin till sitt tillstånd från före signalen (`_pre_signal_state` och `_resync_strategy_to_row` (A)). Exiten skickas då igen nästa tick, om regeln fortfarande ger signal. När regeln slutar ge signal avslutar A serien med en WARNING: "its refused exit … no longer fires" (`_run_strategy` (A)). Det är strikt bättre än master, men tre luckor finns kvar:

1. **Kanttriggade exits tappas efter sin bar** (exits-never-blocked-1, high):
   - `cdc_macd.py:on_candle` och `macd_zero.py:on_candle` stänger på en crossunder, en enda bar. Ingen av dem har SL.
   - I `hash_supertrend.py` är Supertrend-flippen den enda exiten. Strategin saknar SL och TP.
   - I `kalman_breakout.py` är flippen vid motsatt bandbrott den enda exiten. Strategin saknar fast SL och TP.
   - `daily_long_0830.py` stänger bara på 08:00-baren (15m) och saknar SL.

   När baren är slut finns exiten inte längre. DB och börs är överens, så reconcile gör ingenting. En strategi utan SL rider då vidare till nästa kant. Pines `strategy.close` fylls på nästa bar oavsett vad villkoret säger då. Vår stängning gör inte det.
2. **Exiten tappas också när strategin inte körs**, i något av dessa fall:
   - Candles saknas. `runner.py:_run_strategy` returnerar vid en tom frame.
   - `on_candle` kastar. Felet fångas i `runner.py:tick`.
   - Strategin är avstängd eller ligger utanför allowlistan (`strategy_allowlist.py:filter_strategies_for_tick`).
   - Rate-larmet har stängt av den (`runner.py:_check_trade_rate_anomalies` → `control.disable_strategy`).
   - Boten startar om. Allt A håller finns i minnet, och efter en omstart återställs strategin från radens `state_json` från öppningen (`runner.py:_restore_strategies` → `_restore_strategy_from_row`). Då har kanten passerat.
3. **En stängning som fylls kort eskalerar aldrig** (exits-never-blocked-5). Resten blir kvar på raden och strategin synkas om (`runner.py:_execute_signal`, grenen `remaining`), men det räknas inte som en miss. Det ger varken bredare band eller larm. Varje delfill skriver dessutom en trades-rad, och efter några sådana kan rate-larmet stänga av strategin mitt i dess exit.

### 2.3 #192:s lösning och vad den kostade

b292005 gjorde stängningen till motorns: `_PendingClose`@b292005, sparad under `_engine_pending_close` i radens `state_json`. 8d27bbe lade till omförsök av markeringen, återställning av missar och larm efter omstart och stängning av rader vars strategi inte byggs. 47f844e lade till `unsure` och `_engine_drift_rest`. Över fyra rundor blev det ungefär 1 900 kodrader, och de två sista medelallvarliga regressionerna kom därifrån (§ 7):

1. `unsure` håller inne exiten medan börsläsningen fallerar, även när ingen annan rad finns på coinet.
2. En rad med `_engine_drift_rest` vars fills ligger bortom HL:s sida på 2 000 fills släpps aldrig.

### 2.4 Fynd som C tar över från #192

| Fynd | Vad | Hur C hanterar det |
|---|---|---|
| exits-never-blocked-1 (high) | Kanttriggade exits tappas efter sin bar | Skulden lever tills raden är stängd, regel (d) |
| exits-never-blocked-5 (delen om korta fills) | En kort fill eskalerar aldrig | Korta fills räknas som missar, regel (e) |
| r1-exits-never-blocked-1 | Markeringen skrevs om vid varje miss | Kolumnen skrivs bara vid tillståndsbyten (§ 5.3) |
| r1-exits-never-blocked-2 | Ett fel i candle-hämtningen stoppade skulden | Tick-slingan skickar skulder som strategiloopen inte hann med (§ 5.4) |
| r1-exits-never-blocked-4 | En `TradeDbDivergence` stoppar slingan | Avgörs av B:s beslut om DB-skrivvakten (fråga 2) |
| r1-restart-and-state-1…5 | Skulden ska klara flip-läsfel och omstarter, missar och larm ska återställas, `only`, en strategi som inte byggs, runbooken | Kolumnen och restore (§ 5.3), fråga 8 och DR-runbooken |
| exits-money-3 och regression 1 | `unsure` håller inne exiten | Tas bort av regel (a) |
| exits-money-1, r1-booking-1, r1-booking-2, r1-double-close-2 och regression 2 | `_engine_drift_rest` släpps aldrig | Tas bort av regel (b), som lämnar resten till avräknings-PR:ens regel |
| noise-and-logs-5 | Larm redan vid första missen | Fråga 10 |
| exits-never-blocked-6 (delen om bandet) | Flat-all är svagare än det automatiska försöket | Fråga 11 |

---

## 3. Ramar som redan är beslutade

- **Exits blockeras aldrig**, utom av en uttrycklig och läsbar paus (5.4).
- **Ett oläsbart kontrolltillstånd blockerar opens, aldrig exits** (NU-2).
- **Loggar är API:t.** När en skuld uppstår, för varje försök och när den slutar loggas strategi, coin, sida, storlek och orsak.
- **Telegram är för människor.** Inga larm per tick.
- **Stängningsstorleken kommer från `position.size` i DB** (CLAUDE.md § 6). Den klampas till börsen på samma sida, som `runner.py:_resolve_close_size` redan gör.
- **DB och börs får aldrig glida isär tyst.**
- **Högst en PR på orderstigen åt gången** (roadmap § 4.1).
- **#192 står öppen som källmaterial** tills dess ersättare finns.

---

## 4. Tre sätt att spara skulden

### Alternativ 1: i minnet

- En `_PendingClose` per (strategi, coin) i runnern.
- En slinga i ticket skickar radens stängning varje tick, med DB-storlek, reduce-only, klampad och med det band som missarna har gett. Slingan körs oberoende av `on_candle`, disable, allowlist och candle-fel.
- Skulden slutar på en fill, en extern bokning, när raden stängs eller byter sida, eller vid en reset. Korta fills räknas som missar.
- Ungefär 250 kodrader och ingen schemaändring.
- **Svaghet:** en omstart tappar tyst varje skuld. Det kan vara en deploy, en kraschloop eller en omstart som operatören gör mitt i incidenten. Efter restore håller strategin raden igen, men dess kant har passerat.

### Alternativ 2: en markering i radens `state_json`, som #192 byggde den

- **För:**
  - Markeringen överlever en omstart och en Redis-flush (NU-2-vakt 1).
  - Den kräver ingen migration.
  - Den bär tenant och mode och dör med raden.
- **Emot:**
  - Strategins och motorns tillstånd delar ett fält. Restore måste skala bort markeringen (`repo.py:split_pending_close`@47f844e), och en rad utan eget tillstånd behöver flaggan `only` (`repo.py:_mark_row`@47f844e).
  - Markeringen krockar med NU-5d, som ska skriva `export_state()` till samma fält.
  - Vid rollback ger master markeringen till `restore_from_json` (`runner.py:_restore_strategy_from_row`). På en rad med `only` byts då omräkningen mot standardvärden. Runbookens SQL måste därför ta bort markeringen innan en rollback.
  - Varje miss blir en DB-skrivning som behöver omförsöksbokföring (`_save_pending`@8d27bbe, `save_failures`).
  - Fältet bjuder in fler markeringar. `unsure` och `_engine_drift_rest` gav båda regressionerna.
- Ungefär 600–900 kodrader, så som det byggdes.

### Alternativ 3: en nullbar kolumn eller en tabell, via Alembic 0018

En kolumn `positions.pending_close JSONB NULL`, eller en tabell `pending_closes` med position-id som nyckel.

- **För:**
  - Samma livscykel som alternativ 2, men utan att blandas med strategins tillstånd och utan att restore behöver skala bort något.
  - Rollback är ofarlig, eftersom gammal kod inte läser kolumnen. SQLAlchemy-modellen och dashboardens drizzle-schema (`dashboard/src/lib/db.ts`) listar sina kolumner, och en rollback tappar bara skulden, vilket är masters beteende.
  - Dashboarden, health-check-runbooken och en framtida "behåll"-kontroll kan fråga kolumnen direkt.
- **Emot:** en migration, alltså Opus och alla åtta /review-vinklar.
- Ungefär 300–400 kodrader plus en migration på cirka 40 rader, alltså ungefär 900 rader med tester. Det är över budget och kräver dispens (§ 9).

| | 1. Minne | 2. `state_json` | 3. Kolumn |
|---|---|---|---|
| Överlever omstart | nej | ja | ja |
| Migration | nej | nej | ja (0018) |
| Delar fält med strategin och NU-5d | nej | ja | nej |
| Restore måste skala bort något | nej | ja | nej |
| Rollback | ofarlig | kräver SQL först | ofarlig (skulden tappas) |
| Kod utan tester | ~250 | ~600–900 | ~300–400 + ~40 |

---

## 5. Rekommendation

**Alternativ 3 i form av en kolumn, med alternativ 1:s tick-slinga och reglerna (a)–(f).**

### 5.1 Reglerna

- **(a) Storlek och syskon.** Skulden skickas alltid reduce-only med radens DB-storlek, klampad till börsen på samma sida (`runner.py:_resolve_close_size`). En misslyckad läsning håller aldrig inne den: med en oläsbar börs skickas DB-storleken, reduce-only. Det tar bort regression 1.
  Om en annan öppen rad finns på coinet gäller A:s regel: ingen skuld, och masters beteende gäller, där reconcile bokför. HL nettar coinet, så en omsändning skulle kunna ta syskonradens position och en delad likvidations-fill bokföras på fel rad. Med `allow_multi_coin=0`, som beslut 5.11 kräver, kan det inte inträffa.
- **(b) Ingen drift-markering.** När raden är större än börsen bokförs resten med avräknings-PR:ens begränsade regel. Den tar resten från obokförda stängnings-fills, och annars bokförs resten som storleksdrift till pnl 0. Regeln använder aldrig mid-priset och väntar aldrig på HL:s sida på 2 000 fills. Det tar bort regression 2. C återanvänder regeln och landar efter den PR:en.
- **(c) Vad som stoppar skulden.** Bara en läsbar paus (5.4), plus det B bestämmer om DB-skrivvakten (fråga 2). Enligt 5.4 blir det kill switch, och kill switch stoppar inte exits.
  Disable, allowlist, rate-larm, kill switch, daily-loss-taket och NU-2-holden stoppar den inte. Kill switch, daily-loss och holden gäller redan bara opens (`portfolio.py:check_risk_limits(is_open=False)`). Disable, allowlist och rate-larm hindrar strategin från att köras, men inte motorns slinga.
- **(d) Vad som avslutar skulden:** en fill, en extern bokning, att raden stängs eller byter sida, eller en reset. Skulden hör till rad-id:t, så en ny rad har ingen. "Reset" betyder `runner.py:_reset_strategy_state`. Alla som anropar den har redan sett att strategins rad är stängd eller aldrig öppnades: reconcile, flat-all, en flip som slutade platt, en CLOSE som inte hittade någon öppen rad (`_reset_strategy_state`@47f844e säger det i sin docstring). C ska ändå jämföra mot rad-id:t, så att en reset aldrig avslutar skulden för en rad som fortfarande är öppen.
- **(e) Missar och larm.** Korta fills räknas som missar. Från `CLOSE_REJECTS_BEFORE_WIDE_BAND` (3) missar får varje försök `WIDE_CLOSE_BAND` (5 %) (A).
  En episod ger ett larm. Ett CRITICAL-larm går ut vid den första missen på det breda bandet, och sedan kommer en långsam påminnelse i stället för tystnad (fråga 4). Inget av larmen går ut per tick.
- **(f) Strategin förblir re-armad.** Dess SL och trail fortsätter att räknas. Medan skulden lever är det ändå motorns stängning som skickas (fråga 5).

### 5.2 Livscykel

- **Uppstår.** En stängning av raden kommer tillbaka vägrad, nollfylld eller kortfylld medan börsen fortfarande håller raden (`_close_left_open` (A), fall b). Det kan vara strategins exit, flippens stängningshalva eller ett tidigare försök på skulden. Skulden uppstår också när stängningen kastar innan utfallet är känt (B:s re-arm, `_close_raised`@b292005). Inte om en annan öppen rad finns på coinet (regel a).
- **Skickas.** Ett försök per rad och tick, genom samma stängningsväg som en signal: en syntetisk CLOSE till `runner.py:_execute_signal`, sedan `_resolve_close_size`, reduce-only och det band som missarna har gett. Reduce-only gör ett dubbelt försök ofarligt, till exempel när flat-all och skulden skickar samma tick. Det andra försöket klipps eller vägras, och klassningen bokför från fills.
- **När DB inte kan läsas** skickas skulden med B:s regel: börsens storlek på samma sida, klampad och reduce-only. Syskonkontrollen gjordes när skulden uppstod, och med `allow_multi_coin=0` kan ingen ny rad öppnas på coinet (`runner.py:_open_refusal`, "coin held").
- **Stoppas** bara av en läsbar paus. Då hoppar ticket över alla strategier, som i dag ("Tick skipped — bot is paused" i `runner.py:tick`).
- **Slutar** enligt regel (d). Om en annan öppen rad dyker upp på coinet slutar skulden med en WARNING, och masters beteende tar över (regel a).

### 5.3 Kolumnen

- **Schema.** `positions.pending_close JSONB NULL`, med innehållet `{"side", "reason", "since", "misses", "alerted"}`. Inget annat. Skulden flaggar inget som kan hålla inne en exit.
- **Skrivning.** En riktad UPDATE av bara den kolumnen, villkorad på rad-id, `is_open` och `side`. Den skriver aldrig `state_json`, så NU-5d kan skriva sitt fält utan att de två skriver över varandra.
  Kolumnen skrivs när skulden uppstår, när bandet breddas, när ett larm levereras och när skulden slutar på en rad som fortfarande är öppen (då till NULL). Missarna däremellan räknas i minnet.
  En skrivning som misslyckas försöks igen vid nästa försök. Den loggas på WARNING första gången och på INFO därefter, som `_save_pending`@8d27bbe gjorde. Skulden skickas ändå.
- **Stängd rad.** Kolumnen nollas inte när raden stängs. Allt som läser den filtrerar på `is_open`, och bokföringsvägarna (`repo.py:record_trade_and_close_position` och A0:s `_close_row_priced`) ändras inte.
- **Restore.** `runner.py:_restore_strategies` läser kolumnen från de öppna raderna och bygger skulderna i minnet med band och levererade larm (som `_pending_from_marker`@8d27bbe). Det gäller även rader vars strategi inte byggs i boten (fråga 8).
- **RLS.** Kolumnen ärver `positions`-tabellens policy från 0010 (`_TENANT_TABLES`). Inga nya grants behövs.
- **Kolumn eller tabell.** Kolumnen rekommenderas. En `pending_closes`-tabell skulle behöva `tenant_id`, en RLS-policy och grants, alltså en sådan RLS-migration som inte får delegeras. Tabellens enda fördel är att en saknad migration då bara slår ut skuldens egna satser.
- **Deploy.** Kolumnen måste finnas innan den nya koden startar. `select(PositionRecord)` listar modellens kolumner, så utan migrationen misslyckas varje läsning av `positions`. Då kan restore inte läsa raderna, och boten startar pausad (`runner.py:startup` → `_halt_for_unrestored_state`). Det felet syns direkt, men pausen fryser också exits.
  Deploy-runbooken migrerar redan före Start (`docs/runbooks/deploy.md`, steg 3). C:s PR-text ska säga det uttryckligen.
- **Rollback.** Gammal kod ignorerar kolumnen. Skulderna tappas, vilket är masters beteende, men inget fel uppstår. PR-texten ska säga det.
- **Återställning från PBS.** En skuld i en backup kan gälla en stängning som gick igenom efter backupen. Steg 7 i `docs/runbooks/disaster-recovery.md` listar därför `SELECT … FROM positions WHERE is_open AND pending_close IS NOT NULL`. Operatören nollar de skulder hen vill behålla med `UPDATE positions SET pending_close = NULL WHERE id = <id> AND is_open`, innan bottarna startar.

### 5.4 Tick-slingan

- **Strategier som körs.** Varje opausad tick körs strategierna som i dag. För en strategi med skuld gäller:
  - Om strategins egen signal stänger raden, alltså en exit eller en flip, används signalen i stället för skulden det ticket (`_closes_side`@b292005). En flip öppnar då när stängningen har gått igenom, genom alla grindar.
  - Annars skickas skulden. Signalen körs inte och loggas på INFO.
- **Efter strategiloopen** skickar motorn varje skuld som inte fick något försök i ticket. Det gäller strategier som är avstängda, utanför allowlistan, rate-larmade, inte fick några candles, kastade eller inte byggs i boten. Det ersätter #192:s hakar i varje felgren av `_run_strategy`@b292005: ett försök per rad och tick, och slingan tar resten.
- **Skulden behöver bara raden.** Den kräver varken candles, strategi eller signal (`_send_unbuilt_pending_close`@8d27bbe).

### 5.5 Larm och loggar

- **Loggar:**
  - När skulden uppstår: en WARNING med strategi, coin, sida, storlek, HL:s orsak och börsens läge.
  - Vid varje försök därefter: en rad på INFO.
  - När skulden slutar: en rad med orsaken (fill, extern bokning, raden stängd, syskonrad).
  - Inget annat på WARNING eller högre per tick. A:s INFO-nivåer i HL-wrappern för vägrade reduce-only-ordrar gäller.
- **Telegram:**
  - Ett larm per episod (fråga 10 avgör vid vilken miss).
  - CRITICAL en gång, vid första missen på det breda bandet.
  - Därefter en påminnelse med fast takt så länge skulden lever (fråga 4).
  - Ett meddelande när en skuld som har larmats CRITICAL till slut går igenom.
  - Ett larm som inte levererades skickas igen med nästa miss (A:s `_alert_rejected_close`).

### 5.6 Reservplaner

- **Om operatören inte vill ha någon migration** fungerar alternativ 2, men bara med exakt en motornyckel, utan `unsure` och utan drift-nycklar. Rollback-runbooken ska dessutom få en anteckning före merge om att markeringen tas bort innan master startar.
- **Alternativ 1 ensamt rekommenderas inte.** Omstarter är just de tillfällen då en skuld behövs.

---

## 6. Vad C återanvänder från #192

| Commit | Funktion eller del | Hur C använder den |
|---|---|---|
| b292005 | `_PendingClose` | Minnesposten med fälten från b292005 (side, reason, misses, alerted, logged) och `since` från 8d27bbe. `unsure` (47f844e) tas inte med. `saved` och `save_failures` (8d27bbe) ersätts av en flagga för en skrivning till kolumnen som inte har landat. |
| b292005 | Slingan i `tick` som kör `_run_one(strategy, evaluate=False)` för strategier utanför `active` | Motorns slinga efter strategiloopen (§ 5.4) |
| b292005 | `_send_pending_close` och `_closes_side` | Skulden i stället för signalen, och strategins egen exit eller flip i stället för skulden |
| b292005 | `_pend_close` och `_close_missed` | Ett ställe där skulden uppstår eller räknar en miss, ihop med A:s `_alert_rejected_close` |
| b292005 | `_pending_close_marker` och skuldens restore i `_restore_strategies` | Restore från kolumnen i stället för från `state_json` |
| b292005 | Testerna `test_an_edge_triggered_exit_is_sent_until_it_fills`, `test_the_close_the_engine_owes_needs_no_signal[...]`, `test_a_disabled_strategy_still_gets_the_close_the_engine_owes`, `test_a_readable_pause_freezes_the_close_the_engine_owes` och `test_a_pending_close_survives_a_restart` | Portas till kolumnen |
| 8d27bbe | `_send_unbuilt_pending_close` och `_run_guarded` | Skulden för en strategi som inte byggs, med felen klassade som i dag |
| 8d27bbe | `_pending_from_marker` | Missar och levererade larm återställs från kolumnen |
| 8d27bbe | `_save_pending` | Idén att försöka igen tills skrivningen lyckas, men skrivningen sker bara vid tillståndsbyten och bara mot kolumnen |
| 8d27bbe | Testerna `test_a_restart_keeps_the_band_and_the_alerts_delivered`, `test_a_pending_close_still_goes_after_its_strategy_is_dropped`, `test_a_failed_marker_write_is_retried_with_the_next_attempt` och `test_two_failed_reads_keep_the_close_the_engine_owes` | Portas till kolumnen |
| 8d27bbe och 47f844e | Avsnitt 1.4 och steg 7 i DR-runbooken, posten i CLAUDE.md § 10 | Skrivs om för kolumnen: en `SELECT` och en `UPDATE … SET pending_close = NULL`, utan `only` och utan drift-nyckel |
| 47f844e | `test_a_refused_close_beside_a_row_the_exchange_does_not_back_is_retried` | Idén behålls: en bestämd vägran bredvid en fantomrad försöks igen |

**Det C inte tar** från #192: `unsure`, `_others_on_side`, `_may_have_filled` som grind för storleken, `split_pending_close`, `_mark_row`, `mark_pending_close`, `mark_drift_rest`, `drift_rest_of`, `DRIFT_REST_KEY`, `fill_ledger(strict=)` och grenen `drift_price` i `close_row_externally`@47f844e. Flat-alls avräkning hör till avräknings-PR:en, sena fills till PR:en för sena fills, och kontrolläsningarna till B.

---

## 7. Vad C inte får upprepa

### 7.1 `unsure` håller inne exiten (regression 1)

- **Var.** `runner.py:_resolve_close_size`@47f844e. Efter ett försök vars utfall var okänt (`_may_have_filled`) markeras episoden `unsure`. Om börsläsningen därefter misslyckas returnerar funktionen 0.0, före beräkningen av `_others_on_side`. `runner.py:_execute_signal`@47f844e skickar då ingen order och går till `_close_left_open(..., unsent=True)`. Där räknar `_held_after_close` den misslyckade läsningen som "hållen", och `_close_missed` räknar en miss.
- **Följd.** Så länge läsningarna fallerar skickas ingen exit, varje tick, också när ingen annan rad finns på coinet. Det strider mot NU-5 ("ett läsfel tolkas aldrig som platt") och mot 5.4.
- **C:s regel.** Regel (a). En skuld finns aldrig bredvid en syskonrad, så en omsändning med DB-storlek och reduce-only kan inte ta någon annans position. HL klipper eller vägrar den, och klassningen bokför från fills. Det finns ingen flagga som kan hålla inne en exit.

### 7.2 `_engine_drift_rest` släpps aldrig (regression 2)

- **Var.** `repo.py:close_row_externally`@47f844e skriver `_engine_drift_rest` (`mark_drift_rest`) när läsningen av fills misslyckades eller nådde `_HL_FILL_PAGE_CAP` (2 000). Raden står då öppen tills en komplett läsning avgör. `repo.py:fill_ledger`@47f844e markerar läsningen som ofullständig vid 2 000 poster, och `repo.py:_price_orphan_close`@47f844e returnerar då None. Reconcile rapporterar "LEFT OPEN (size drift unconfirmed)" (`repo.py:reconcile_positions`@47f844e).
- **Följd.** Fönstret börjar vid radens `opened_at` och kan bara växa, och `hyperliquid.py:fetch_user_fills`@47f844e bläddrar inte. När 2 000 fills ligger efter öppningen blir därför ingen läsning komplett igen, och raden släpps aldrig. DB säger öppen medan börsen är platt. Raden spärrar dessutom coinet och dess familj för andra strategiers opens (`runner.py:_open_refusal`, "coin held" och "family exposed").
- **C:s regel.** Regel (b). C har ingen egen regel för resten. Avräknings-PR:ens regel bokför från fills när de räcker, annars som storleksdrift till pnl 0, med en WARNING. Den använder aldrig mid-priset och väntar aldrig på HL:s sida.

### 7.3 Det gemensamma mönstret

Båda regressionerna kom från tillstånd som lades i `state_json` för att lappa ett hörnfall. Båda höll sedan något öppet eller ostängt utan slut. C följer därför tre regler:

- Bara en sak sparas: skulden, i en egen kolumn.
- Inget tillstånd får hålla inne en exit eller vänta på en läsning som kanske aldrig blir komplett.
- Varje skuld har ett slut som inte beror på HL:s sidstorlek.

---

## 8. Öppna frågor: operatörens beslut krävs

> **Kräver operatörens beslut.** Ingen kod för C skrivs förrän de här frågorna har svar. Rekommendationen står vid varje fråga. Ett svar som avviker ändrar § 5 före kodstart.

1. **Persistens.** Ska skulden ligga i minnet (förloras vid omstart), som en markering i `state_json` (ingen migration, men fältet delas med strategin och NU-5d) eller i en ny kolumn eller tabell (migration, rent)?
   **Rekommendation:** kolumnen `positions.pending_close`, via Alembic 0018. Om migrationen avvisas blir det en markering med exakt en nyckel (§ 5.6).
2. **Vad får stoppa en skuld?** Förslaget är att bara en läsbar paus (5.4) gör det, och inte disable, allowlist, rate-larm, kill switch, daily-loss-taket eller NU-2-holden. Svaret beror på B:s beslut om DB-skrivvakten.
   **Rekommendation:** bara en läsbar paus. B byter dessutom masters automatiska paus vid DB-skrivfel (`runner.py:_execute_signal`, `set_paused(True)`) mot kill switch, så att ingen automatisk vakt stoppar en exit. Om pausen blir kvar i B stoppar den också C:s skulder, och C:s PR ska säga det.
3. **Hur behåller operatören en position som motorn är skyldig att stänga?** Alternativen är paus plus SQL enligt runbooken, en "behåll"-åtgärd i dashboarden eller Telegram, eller att disable avbryter skulden.
   **Rekommendation:** paus plus en dokumenterad SQL i C (`UPDATE positions SET pending_close = NULL WHERE id = <id> AND is_open`), och en "behåll"-åtgärd senare i NA-9:s kontrollpanel. Disable avbryter inte skulden, eftersom disable enligt 5.4 ska ge exits-only.
4. **Vad händer efter CRITICAL-larmet?** Skulden kan skickas varje tick på 5 % utan slut, backa av, eller stoppas efter N försök och lämnas till en människa. Och hur ofta ska påminnelsen gå?
   **Rekommendation:** fortsätt varje tick på det breda bandet så länge raden är öppen. Reduce-only kan inte göra positionen större, och före SE-1 finns inga stopp på börsen, så ett tak lämnar positionen utan exit. Påminnelse var 30:e minut, samma takt som NU-2-holdens påminnelse (`runner.py:HOLD_REALERT_SECONDS`), och ett meddelande när stängningen går igenom.
5. **Är strategin re-armad medan motorn äger stängningen, eller hålls den platt?** Re-armad betyder att dess SL och trail räknas och att dess egen flip kan ge signal.
   **Rekommendation:** re-armad (regel f). En signal som själv stänger raden, alltså exit eller flip, används i stället för skulden det ticket, så att en flip kan öppna när stängningen har gått igenom. Andra signaler körs inte medan skulden lever.
6. **Syskonrader.** `allow_multi_coin` är uteslutet av 5.11. Får C behålla A:s regel, där en coin med en annan öppen rad inte får någon skuld och reconcile bokför? Eller måste C äga stängningarna även där?
   **Rekommendation:** behåll A:s regel. Under 5.11 kan fallet inte uppstå, men flaggan finns kvar i Redis (`control.get_allow_multi_coin`). Regeln skyddar mot att en delad likvidations-fill bokförs på fel rad och mot att en omsändning tar en annan rads position.
7. **Avräkning före C.** Får C:s skuld på en rad som är större än börsen slutföras med avräknings-PR:ens regel (fills, annars storleksdrift till pnl 0, aldrig mid, aldrig vänta på sidan med 2 000 fills)? Eller ska C ha en egen regel?
   **Rekommendation:** ja. C landar efter avräknings-PR:en och har ingen egen regel.
8. **Omstart när radens strategi inte längre byggs** (allowlist, mainnet opt-in eller ett strategitak). Ska skulden skickas ändå, som #192 gjorde, eller ska boten larma och låta positionen ligga?
   **Rekommendation:** skicka skulden ändå (`_send_unbuilt_pending_close`@8d27bbe). Den behöver bara raden, och skälen att inte bygga strategin gäller opens. Behåll larmet om ohanterade positioner (`runner.py:_restore_strategies`), och lägg till i det att skulden fortfarande skickas.
9. **Räknas en kort fill som en miss** för det breda bandet och CRITICAL-larmet? #192 svarade ja.
   **Rekommendation:** ja.
10. **Ska larmet gå ut vid första missen eller först vid den andra** (noise-and-logs-5)?
    **Rekommendation:** vid andra missen i rad. Med C försöks stängningen alltid igen nästa tick, så en tillfällig vägran som läker sig själv når aldrig Telegram. Första missen loggas på WARNING, och CRITICAL är oförändrat. A larmar vid första missen, eftersom A:s omförsök beror på regeln.
11. **Ska flat-all använda `WIDE_CLOSE_BAND`** för coins som har en skuld eller en serie missar (exits-never-blocked-6)?
    **Rekommendation:** ja, när coinets skuld har nått det breda bandet. Operatörens nödutgång ska inte vara svagare än det automatiska försöket.
12. **Ordningen mot NU-5c och NU-5d.** C:s persistens måste fungera ihop med NU-5d:s skrivningar till `state_json`. Ska NU-5d landa först, eller designas tillsammans med C?
    **Rekommendation:** med kolumnen delar C och NU-5d inget fält. NU-5d kan därför landa efter C, i den föreslagna kön (NU-5c, avräkning, C, sena fills, NU-5d, NU-5e), utan gemensam design. Med alternativ 2 måste de designas ihop. NU-5c bör komma före C, eftersom flippen är enda exiten för kalman_breakout och en grind som vägrar hela flippen är NU-5c:s fall, inte C:s.

---

## 9. Ordning, storlek och review

- **Kö på orderstigen,** en PR i taget: A0 → A → B, och sedan i den ordning operatören väljer. Specen föreslår NU-5c, avräkning, C, sena fills, NU-5d och NU-5e. C förutsätter A:s `_close_left_open` och `_alert_rejected_close`, B:s regel för DB-fel och avräkningens regel för rader som är större än börsen.
- **Storlek:** ungefär 300–400 kodrader plus en migration på cirka 40 rader, alltså ungefär 900 rader med tester. Det är över budgeten på 600 rader.
  Som alternativ till dispens kan arbetet delas i två PR:er. C0 innehåller migration 0018, modellen och `Repository`-metoderna för att läsa och skriva kolumnen, utan nytt beteende, och blir ungefär 150 rader med tester. C0 deployas i ett veckofönster före C, så att kolumnen finns när C startar. C blir då ungefär 750 rader och behöver fortfarande dispens för testerna. **Operatören väljer** mellan dispens och delning.
- **Review:** Opus, alla åtta /review-vinklar (orderstig och migration). Varje vakt muteras bort en gång, och ett test ska då falla.
- **Deploy:** i veckofönstret, med migrationen före Start (deploy-runbooken, steg 3). Rollback tappar skulderna, vilket är masters beteende.
- **Docs i C:** avsnitt 1.4 och steg 7 i `docs/runbooks/disaster-recovery.md`, en post om `positions.pending_close` i CLAUDE.md § 10, och en fråga på `pending_close` i health-check-runbooken.

---

## 10. Klart när

Tester mot riktig `Repository` på SQLite och runnerns egna vägar, i samma harness som A:s `test_reduce_only_closes.py`:

1. En kanttriggad exit som HL vägrar skickas varje tick tills den fylls. Testet använder en riktig strategi (cdc_macd) och portar `test_an_edge_triggered_exit_is_sent_until_it_fills`.
2. Skulden överlever en omstart med bandet och de larm som redan levererats, läst från kolumnen och inte från `state_json`. Strategins `state_json` är orörd.
3. Skulden skickas fast strategin inte får några candles, kastar, får ett fel i hämtningen, är avstängd, ligger utanför allowlistan, är rate-larmad eller inte byggs.
4. Bara en läsbar paus fryser skulden. Kill switch, daily-loss-taket och NU-2-holden gör det inte.
5. En misslyckad läsning håller aldrig inne skulden. Efter en timeout med okänt utfall och en börsläsning som fallerar skickas DB-storleken reduce-only. Det testet vaktar mot regression 1.
6. En rest som börsen inte håller bokförs med avräkningens regel även när läsningen av fills når 2 000 poster, och raden stängs. Det testet vaktar mot regression 2.
7. Korta fills räknas som missar: bandet breddas, och CRITICAL går ut en gång.
8. Med en syskonrad på coinet uppstår ingen skuld, och masters beteende gäller.
9. Larmen: ett per episod, CRITICAL en gång, en påminnelse med den beslutade takten, inget per tick, och ett meddelande när en skuld som larmats CRITICAL går igenom.
10. En skrivning till kolumnen som misslyckas stoppar inte skulden och försöks igen vid nästa försök.
11. Migrationsjobbet i CI kör `alembic upgrade head` med 0018. Ett test visar att masters modell, som saknar kolumnen, läser `positions` på det nya schemat.
12. Grindarna är gröna: `cd bot && uv run pytest -q`, och i CI `bot`, `dashboard`, `migrations` och `gitleaks`.
