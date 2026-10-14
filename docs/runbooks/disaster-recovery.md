# Katastrofåterställning från PBS

Runbooken gäller xupertrade-hosten, en LXC på Proxmox med koden i
`/opt/hypertrade`. Den hör till roadmapens NU-2 punkt 3.

- Proxmox Backup Server (PBS) tar en backup av hela containern varje dygn.
  Det är den enda backupen. Det finns inga dumpar, inget restore-test och
  ingen extern kopia, eftersom operatören har accepterat risken (2026-09-24).
  Man kan alltså förlora upp till ungefär 24 timmars data (RPO ≤ 24 h).
- Postgres-kopian är kraschkonsistent. Postgres gör crash recovery när den
  startar, som efter ett strömavbrott. Ingen återställning har provats.
- Redis kommer tillbaka från den `dump.rdb` som låg på disk när backupen togs,
  alltså från senaste `BGSAVE`. Med standardpolicyn kan filen vara upp till en
  timme äldre än databasen i samma backup.
- En människa måste vara närvarande från första boot tills kill switch är
  avslagen.
- Repot är publikt. CTID, nodnamn och lagringsnamn skrivs därför inte här.
  Kommandona använder `<ctid>`, `<pbs-lagring>` och `<lagring>`.

## 1. Farorna vid en återställning

### 1.1 Bottarna startar av sig själva

- Tenant-bottarna skapas med restart policy `unless-stopped`
  (`dashboard/src/lib/bot-orchestrator.ts`). Varje container har sin
  dekrypterade HL-nyckel i sin env.
- När en återställd container bootar startar Docker alltså varje bot som
  körde när backupen togs. Bottarna läser den återställda Redis, där `paused`
  och `kill_switch` har de värden de hade då.
- Reconcile körs direkt när boten startar (`EngineRunner.startup` i
  `runner.py`), innan någon hinner göra något. Därför bootar den återställda
  containern första gången utan nätverk (avsnitt 2, steg 3).
- En container som har stoppats med `docker stop` förblir stoppad, även när
  Docker eller containern startar om.

### 1.2 Reconcile stänger börspositioner som databasen inte känner till

En position som öppnades efter backupen finns på börsen men inte i den
återställda databasen.

- En bot som inte är pausad kör reconcile när den startar och sedan var femte
  minut. Pass 2 (`reconcile_positions` i `repo.py`) marknadsstänger en sådan
  position. På mainnet är det en riktig order med riktiga pengar, och ingen
  har bett om den.
- Hur mycket pass 2 stänger beror på vilken kod containern kör. En container
  kör alltid den image den skapades från, även efter `docker start` och när
  Docker startar den själv (avsnitt 1.1). En bot vars container skapades från
  en image med #186 håller i första passet efter boot alla sådana positioner
  och sätter reconcile hold (avsnitt 1.6). Först när en människa har rensat
  hold stänger pass 2 en ensam sådan position på ett mynt som boten handlar.
  En bot från före #186 har inget sådant skydd. Dess första opausade pass
  marknadsstänger varje sådan position, på alla mynt, i ett och samma pass.
  Steg 7 visar vilken sort varje container är.
- Samma sak gäller en position som har bytt sida efter backupen. Pass 1 stänger
  DB-raden som `wrong-side`, och sedan saknar börspositionen rad. En bot från
  före #186 marknadsstänger den i samma pass.
- En DB-rad för en position som stängdes efter backupen stängs av pass 1 och
  bokförs från HL:s fill-historik. Reconcile lägger ingen order för den, men
  radens strategi kan hinna före och lägga en (avsnitt 1.4).

**Mot hela reconcile skyddar bara paus.** En pausad bot kör reconcile som en
torrkörning (`_run_reconcile` i `runner.py`). Den skriver ingenting och lägger
inga order. Reconcile hold, som bara bottar med #186 har, skyddar bara mot
pass 2. **Kill switch skyddar inte här.** Den blockerar bara nya öppningar,
och reconcile går förbi den. Paus stoppar inte allt (avsnitt 1.5).

Torrkörningen rapporterar bara pass 1, alltså raderna `PAUSED — not closed
(orphan)` och `PAUSED — not closed (wrong-side)`. Pass 2 hoppas över helt
medan boten är pausad. Börspositioner som saknar DB-rad syns därför bara som
ett antal i raden `Reconcile: … db rows, … exchange positions`. Börsen och
databasen måste jämföras för hand (steg 1 och 7).

### 1.3 En tom eller gammal Redis läses som att allt kör som vanligt

- `is_paused()` svarar ja bara när nyckeln är `"1"`. Om nyckeln saknas räknas
  boten som ej pausad.
- Om kill switch saknas används env-värdet (`KILL_SWITCH`), som är `false` som
  standard.
- Tomma `disabled`-mängder betyder att **alla** strategier är påslagna, även de
  som operatören har stängt av.
- Hävstångsoverrides är borta.
- Mainnet-opt-in är också tom, och det stänger mainnet. Det är det enda som
  hamnar rätt av sig självt.
- Dashboardens inloggning låser sig (`locked`). Se
  [dashboard-auth-recovery.md](dashboard-auth-recovery.md).

En återställd Redis är inte tom, men den saknar allt som ändrats efter
backupen. Kontrollera flaggorna mot det operatören vill ha (steg 4).

### 1.4 Paus fryser exits, och kill switch släpper igenom dem

En pausad bot kör inga strategier. Då körs inte heller några SL- eller
TP-exits, och före SE-1 finns inga stopp på börsen. Därför får pausen vara
högst 30 minuter. Efter pausen tar kill switch över. Den blockerar öppningar
men släpper igenom varje stängning (`check_risk_limits` i `portfolio.py`).

En stängning är en vanlig marknadsorder, inte reduce-only (`place_order` i
`hyperliquid.py`). Om HL saknar positionen, eller har den på andra sidan,
skickas DB-radens storlek ändå (`_resolve_close_size` i `runner.py`).
Strategierna återställs från de öppna DB-raderna när boten startar och körs
från första tick efter unpause, men det första riktiga reconcile-passet kan
dröja flera minuter. En strategi vars rad HL inte backar kan därför öppna en
ny position, eller förstora en position på motsatt sida, trots kill switch.
Steg 7 stänger av de strategierna före unpause.

### 1.5 Två saker går förbi pausen

- **En väntande flat-all.** `tick()` utför den innan den läser paus
  (`runner.py`), och den marknadsstänger alla positioner i moden. En begäran
  som inte har kvitterats kommer tillbaka med Redis. Det gäller till exempel en
  flat-all som misslyckades under ett HL-avbrott, eftersom den då försöks igen
  varje tick.
- **Hävstången.** När boten startar skickar `main.py` hävstången per symbol
  till HL, med overrides ur Redis, oavsett paus.

Steg 4 går igenom båda innan någon bot startar.

### 1.6 Sentinel och reconcile hold (NU-2)

Det här avsnittet gäller bara en bot vars container skapades från en image
med #186. Steg 7 visar hur du ser det. En bot från före #186 läser ingen av
nycklarna nedan och har ingen hold.

Varje bot har två egna nycklar i Redis, med tenant-id från
`tenant_bots.tenant_id` (`control.py`):

- `hypertrade:<mode>:t:<tenant_id>:control:sentinel` betyder att botens
  Redis-tillstånd finns kvar. Dashboarden skriver den när en bot skapas.
- `hypertrade:<mode>:t:<tenant_id>:control:reconcile_hold` finns medan boten
  håller. Då öppnar boten ingenting, och pass 2 marknadsstänger ingen
  börsposition utan DB-rad. Exits körs som vanligt, och pass 1 påverkas inte.
  Hold pausar inte och rör inte kill switch.

Boten sätter hold själv i tre fall. Bara en människa rensar den, och den
påminner var 30:e minut tills dess.

- **Sentinel saknas.** Boten läser den varje tick. Saknas den har Redis
  tappat sitt tillstånd: boten sätter hold, larmar en gång med de öppna
  DB-positionerna och skriver sentinel igen. En återställd `dump.rdb` har
  normalt sentinel kvar, men en tom Redis (avsnitt 3) ger hold vid första
  tick.
- **Första passet efter boot.** Det första läsbara, opausade passet håller
  varje börsposition utan DB-rad (`HELD exchange-orphan … — not closed:
  first reconcile pass since the bot started`). Om någon av dem ligger på ett
  mynt som boten handlar sätts hold (`Reconcile hold SET`). Efter en
  återställning är det här positionerna som öppnades efter backupen hamnar.
  Inga öppningar görs innan ett sådant pass har gått.
- **Flera på en gång.** Ett senare pass som hittar mer än en börsposition utan
  rad på mynt som boten handlar håller dem alla och sätter hold.

Rensningen tar bara bort nyckeln. Boten märker den i början av nästa tick,
inom ungefär en minut (`POLL_INTERVAL_SECONDS`, 60 sekunder som standard), och
kör då ett reconcile-pass före alla öppningar. Passet marknadsstänger en ensam
börsposition utan rad på ett mynt som boten handlar, och sätter hold igen om
flera finns kvar. En position på ett mynt som ingen
strategi i boten handlar stänger reconcile aldrig. Steg 8 visar hur du läser
och rensar hold.

## 2. Återställ hela containern

Förutsättningar:

- root på Proxmox-noden, och en person som kan se kontona på HyperLiquid.
  Personen stannar tills steg 10 är klart.
- Ungefär en timme. Högst 30 minuter får gå från första bot-start (steg 7)
  till unpause (steg 8).
- Phase behövs inte. Containrarna i backupen har redan sin env.

### Steg 1: stäng av den gamla containern och skriv ner facit

Två containrar med samma nycklar får aldrig köra samtidigt. Om den gamla
fortfarande kör, gör så här på Proxmox-noden:

```sh
pct shutdown <ctid>
pct set <ctid> --onboot 0     # annars startar den igen när noden bootar
```

Skriv sedan ner vad börsen har just nu, för varje konto (testnet och mainnet).
Det är facit för steg 7. HL:s webbgränssnitt räcker, men positionerna går också
att läsa utan nyckel, från vilken dator som helst:

```sh
curl -s https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' \
  -d '{"type":"clearinghouseState","user":"<kontoadress>"}'
# testnet: https://api.hyperliquid-testnet.xyz/info
```

Anteckna symbol, sida, storlek, `entryPx` och `leverage` för varje position.

Paper har ingen riktig börs. Dess simulerade börs ligger i Redis och kommer
tillbaka med backupen.

### Steg 2: återställ från PBS utan att starta

```sh
pvesm list <pbs-lagring> --vmid <ctid>     # välj backup, normalt den senaste
pct restore <nytt-ctid> <pbs-lagring>:backup/ct/<ctid>/<tidpunkt> --storage <lagring>
pct set <nytt-ctid> --onboot 0             # restore tar med backupens onboot
```

- `pct restore` startar inte containern. Om du använder GUI:t: bocka ur
  "Start after restore". Men backupens `onboot` följer med, och utan raden
  ovan startar containern med nät om noden bootar före steg 3.
- Återställ helst till ett nytt CTID och låt den gamla containern stå
  avstängd tills den nya är kontrollerad. Den gamla kan innehålla rader som är
  nyare än backupen.
- Om den gamla inte går att använda kan du återställa över samma CTID med
  `--force`. Då försvinner den gamla.
- Om databasen har varit skadad en tid: välj den sista backupen från före
  skadan.

Nedan betyder `<ctid>` den återställda containern.

### Steg 3: första boot utan nätverk

```sh
pct config <ctid> | grep '^net'     # spara raderna, de behövs i steg 5
pct set <ctid> --delete net0        # och varje annan netN
pct start <ctid>
pct enter <ctid>                    # SSH fungerar inte utan nät
```

- Docker startar bottarna nu, men utan nät kan ingen order nå HL.
- En reconcile som inte kan läsa börsen skriver ingenting och lägger inga
  order, eftersom en misslyckad läsning inte räknas som "platt"
  (`reconcile_positions` i `repo.py`).
- Bottarna kan hamna i en omstartsloop. Det är väntat.
- Paper-boten läser sin simulerade börs ur Redis och kan därför köra reconcile
  även utan nät. Det gäller bara låtsaspengar, men kontrollera paper i steg 7
  som de andra.

### Steg 4: stoppa bottarna och gå igenom Redis

Kör detta inne i containern:

```sh
docker ps -a --format '{{.Names}}' | grep -E '^xupertrade-bot-' | xargs -r docker stop
docker stop hypertrade-dashboard-1   # så att ingen startar en bot mitt i återställningen
```

Skriv sedan ner vad backupen hade, så att steg 10 kan återställa det:

```sh
r() { docker exec hypertrade-redis-1 redis-cli "$@"; }
for m in paper testnet mainnet; do
  k=hypertrade:$m:control
  echo "$m paused=[$(r GET $k:paused)] kill_switch=[$(r GET $k:kill_switch)]"
  echo "  flat_request_id=[$(r GET $k:flat_request_id)] flat_request_done=[$(r GET $k:flat_request_done)]"
  echo "  disabled: $(r SMEMBERS $k:disabled | xargs)"
  echo "  leverage: $(r HGETALL $k:leverage | xargs)"
done
```

Tomma hakparenteser betyder att nyckeln saknas och att env-standarden gäller.
Se också vilka bottar som har sentinel och hold (avsnitt 1.6):

```sh
r --scan --pattern 'hypertrade:*:control:sentinel'
r --scan --pattern 'hypertrade:*:control:reconcile_hold'
```

En bot med #186 som saknar sentinel startar med hold. En bot från före #186
läser inte sentinel alls (steg 7). Skriv inte in någon sentinel för hand här.
Hold är rätt läge tills steg 8.

Rätta sedan det här innan någon bot startar:

- **Flat-all.** Om `flat_request_id` har ett värde som skiljer sig från
  `flat_request_done` väntar en flat-all. Boten stänger då alla positioner i
  moden vid första tick, pausad eller inte (avsnitt 1.5). Ska den inte köras,
  avbryt den:
  `r SET hypertrade:<mode>:control:flat_request_done '<värdet i flat_request_id>'`.
- **`disabled` och hävstång.** Jämför med det operatören vill ha. Redis-kopian
  kan sakna ändringar från den sista timmen före backupen. Hävstången måste
  stämma redan nu, eftersom bot-starten i steg 7 skickar den till HL. Rätta med
  `SADD`/`SREM hypertrade:<mode>:control:disabled <strategi>` och
  `HSET`/`HDEL hypertrade:<mode>:control:leverage <strategi> <hävstång>`.

Sätt sist kill switch och paus för alla tre moder och spara:

```sh
for m in paper testnet mainnet; do
  r SET hypertrade:$m:control:kill_switch 1
  r SET hypertrade:$m:control:paused 1
done
r SAVE     # svarar OK först när allt ovan ligger på disk
```

`SAVE` behövs eftersom standardpolicyn sparar färre än 100 ändringar först
efter en timme. Om Redis startar om innan dess försvinner ändringarna.

### Steg 5: koppla in nätet igen

På Proxmox-noden, med det som stod efter `net0: ` i raden från steg 3:

```sh
pct set <ctid> --net0 'name=eth0,bridge=…,hwaddr=…,ip=…'     # och varje annan netN
```

Inne i containern ska `docker ps --format '{{.Names}}' | grep '^xupertrade-bot-'`
inte ge någonting. Om nätet inte kommer upp går det bra att köra
`pct reboot <ctid>` nu, eftersom bottarna är stoppade och flaggorna sparade.

### Steg 6: kontrollera databasen

Kör detta inne i containern:

```sh
docker logs hypertrade-postgres-1 2>&1 | grep -iE 'recover|ready to accept|PANIC|FATAL' | tail
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -tAc 'SELECT version_num FROM alembic_version'
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, max(timestamp) AS nyaste_trade FROM trades GROUP BY mode;"
```

- Loggen ska visa crash recovery och sedan `ready to accept connections`.
  `PANIC` eller upprepade `FATAL` betyder att kopian inte går att använda. Gå
  då tillbaka till steg 2 och välj en äldre backup.
- `nyaste_trade` visar hur gammal datan faktiskt är. Det som hänt efter den
  tidpunkten finns bara på börsen.
- Om master har en nyare migration än `version_num` (filerna i
  `bot/alembic/versions/` börjar med sitt nummer): vänta tills
  återställningen är kontrollerad. Bygg sedan om imagen som i steg 1 av
  bot-deploy-fönstret i [deploy.md](deploy.md#bot-deploy-window), och kör
  först därefter kommandot i `bot/scripts/migrate.sh`. Det kör alembic ur
  `xupertrade-bot:latest`, och före ombygget är det backupens image, som bara
  känner till sina egna migrationer och ändå avslutar med 0. Kontrollera
  `version_num` igen efteråt.
- Om `docker logs` fallerar: läs containerns loggfil direkt, enligt
  [health-check.md](health-check.md).

### Steg 7: kontrollera pariteten, stäng av strategier och starta bottarna

Starta dashboarden med `docker start hypertrade-dashboard-1` och logga in. Om
`/login` säger "Authentication is locked" kom Redis inte tillbaka. Stanna då
och utred, i stället för att konfigurera om inloggningen.

Ta reda på vilken kod varje bot kommer att köra. `docker start`, och Docker
när den startar en container själv, kör containern på den image den skapades
från. Ett nytt `xupertrade-bot:latest`, också ett ombygge i steg 6, ändrar
inget för en container som redan finns. Läs filen ur varje stoppad container
utan att starta den:

```sh
for c in $(docker ps -a --format '{{.Names}}' | grep '^xupertrade-bot-'); do
  n=$(docker cp "$c:/app/hypertrade/engine/control.py" - 2>/dev/null | grep -ac reconcile_hold)
  if [ "$n" -gt 0 ]; then echo "$c: #186"; else echo "$c: FÖRE #186"; fi
done
```

En bot `FÖRE #186` har ingen sentinel, ingen hold och inget första pass som
håller (avsnitt 1.2). Vid unpause marknadsstänger den varje börsposition utan
DB-rad i sin mode. Välj en av två vägar för varje sådan bot innan den startar:

- **Ny kod.** Bygg om imagen som i steg 1 av bot-deploy-fönstret i
  [deploy.md](deploy.md#bot-deploy-window). Det kräver Phase. Starta sedan
  boten från dashboarden med Stop och därefter Start (med passfras), i stället
  för med `docker start` nedan. Start skapar en ny container ur den nya
  imagen. Den håller i sitt första opausade pass (avsnitt 1.6). Start skriver
  ingen sentinel, så om Redis saknar en sätter boten hold redan vid första
  tick.
- **Gammal kod.** Före unpause i steg 8 stänger du själv på HL (reduce-only)
  varje position i botens mode som saknar DB-rad eller har en rad på motsatt
  sida (tabellen nedan), på alla mynt. Det första riktiga passet kommer då
  inom ungefär sex minuter efter unpause, inte inom en.

Jämför facit från steg 1 med de öppna raderna i databasen:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, strategy_name, symbol, side, size, entry_price, opened_at
     FROM positions WHERE is_open = true ORDER BY mode, symbol;"
```

| Börsen | Databasen | Vad som händer efter unpause | Vad du gör, innan bottarna startar |
|---|---|---|---|
| Position | Ingen rad | Med #186: första passet håller positionen och sätter hold om boten handlar myntet (avsnitt 1.6). När hold rensas marknadsstänger reconcile en ensam sådan position på ett mynt som boten handlar. Före #186: första opausade passet marknadsstänger den | Bestäm nu. Stäng den själv på HL (reduce-only), eller låt reconcile stänga den i steg 8 om den är ensam och boten har #186. Att skapa en DB-rad för hand avråds från, eftersom strategin saknar SL-state för positionen. |
| Position | Rad med motsatt sida | Radens strategi kan skicka en exit som förstorar positionen (avsnitt 1.4). Pass 1 stänger raden (`wrong-side`), och pass 2 håller sedan positionen som en position utan rad. Före #186 marknadsstänger pass 2 den i samma pass | Stäng av strategin (nedan). Bestäm om positionen som för en position utan rad. |
| Ingen position | Öppen rad | Radens strategi kan skicka en exit som öppnar en ny position (avsnitt 1.4). Pass 1 stänger raden och bokför den från fill-historiken | Stäng av strategin (nedan). Kontrollera PnL efteråt. |
| Position | Rad med samma sida, men `entry_price` klart skilt från HL:s `entryPx` (flera rader på symbolen: jämför det storleksviktade snittet) | Ingenting. Reconcile ser paritet, men positionen öppnades efter backupen och styrs av en strategi som inte öppnade den | Stäng positionen själv på HL (reduce-only). Då är raden en "Ingen position / Öppen rad". |
| Position | Rad med samma sida och annan storlek | Avvikelsen loggas bara | Notera den. |

Stäng av strategin för varje rad som HL inte backar, alltså för raderna
"motsatt sida" och "Ingen position / Öppen rad". En avstängd strategi körs
inte alls (`filter_strategies_for_tick`), så den kan inte lägga någon order:

```sh
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:<mode>:control:disabled <strategy_name>
docker exec hypertrade-redis-1 redis-cli SAVE
```

Skriv ner vilka du lade till. Steg 8 slår på dem igen.

Bottarna som ska startas är de som databasen markerar som körande:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, container_name FROM tenant_bots WHERE is_running ORDER BY mode;"
```

Starta dem en i taget med `docker start <container_name>`, utom de bottar
från före #186 som ska få ny kod (ovan). Börja med paper och ta mainnet sist.
**Tiden börjar gå vid första bot-starten: unpausa inom 30 minuter.**

Om dashboarden inte når en bot (401) är Redis-kopian äldre än botens senaste
start. Starta då om just den boten från dashboarden (Settings → Bots → restart,
med passfras). Containern skapas då på nytt med en ny nyckel.

Varje bot startar pausad och med kill switch på, så reconcile körs som
torrkörning:

```sh
docker logs --since 10m <container_name> 2>&1 | grep -i reconcile
```

- Raderna med `PAUSED — not closed (orphan)` och `(wrong-side)` ska vara
  exakt tabellens rader "Ingen position / Öppen rad" och "motsatt sida".
- `… db rows, … exchange positions` ska stämma med antalen i facit och i
  tabellen.
- Börspositioner utan DB-rad syns inte här (avsnitt 1.2). För dem är tabellen
  det enda underlaget.
- `hold <container_name>` (funktionen i
  [health-check.md](health-check.md#reconcile-hold-nu-2)) visar vilken kod
  boten faktiskt kör. `{"reconcile_hold": …}` betyder #186, och
  `HTTP Error 404` betyder före #186. Svaret ska stämma med kontrollen ovan.
  En bot som har startats om från dashboarden kör `xupertrade-bot:latest`, och
  det är backupens image om den inte har byggts om. Svarar en bot 404 gäller
  valet ovan för den.

Om något inte stämmer: stoppa boten och utred innan du går vidare.

### Steg 8: unpausa, inom 30 minuter

När varje avvikelse från steg 7 har fått ett beslut: unpausa de moder som
körde före incidenten. Använd dashboardens kontroller eller:

```sh
docker exec hypertrade-redis-1 redis-cli SET hypertrade:<mode>:control:paused 0
```

- En mode som var pausad i backupen (steg 4) förblir pausad. Det beslutet är
  operatörens.
- Det första riktiga reconcile-passet körs vid första tick efter unpause,
  alltså inom ungefär en minut. En bot från före #186 kör det inom ungefär
  sex minuter. Följ loggen tills det har gått. Det ska göra exakt det som
  tabellen i steg 7 förutsade, med en rad `Reconcile: closed orphan` eller
  `closed wrong-side` för varje sådan rad, och en rad `HELD exchange-orphan`
  för varje börsposition utan rad (avsnitt 1.6). En bot från före #186
  skriver i stället `closed exchange-orphan` för varje sådan position som
  finns kvar.
- Läs varje bots hold med funktionen `hold` i
  [health-check.md](health-check.md#reconcile-hold-nu-2). Den tar nyckeln ur
  containerns env, så den fungerar även när Redis-kopian är äldre än boten.
  `hold <container_name>` svarar `{"reconcile_hold": true}` eller `false`.
  En 503 betyder att boten inte kan läsa Redis, och då håller den också.
  `HTTP Error 404` betyder att boten kör kod från före #186 och inte har
  någon hold (steg 7).
- Rensa hold först när varje hållen position är stängd för hand på HL, eller
  när bara en återstår och reconcile ska stänga den:
  `hold <container_name> false`. Det går också med
  `docker exec hypertrade-redis-1 redis-cli DEL hypertrade:<mode>:t:<tenant_id>:control:reconcile_hold`.
  Båda kräver att Redis tar emot skrivningar. Annars svarar API:t 503, och
  hold ligger kvar. Använd hellre API:t. Det avslutar också en hold som boten
  bara har i minnet för att skrivningen till Redis misslyckades, och det
  skriver sentinel om boten har sett att den saknas. I båda fallen sätter
  boten hold igen vid nästa tick efter en DEL.
- Boten märker rensningen i början av nästa tick, inom ungefär en minut, och
  kör då ett reconcile-pass (avsnitt 1.6). Läs hold igen först när det passet
  har gått. Vänta minst två minuter, och tills loggen efter rensningen har en
  ny rad `Reconcile: … db rows, … exchange positions`. Passet skriver
  `closed exchange-orphan` för en position det stängde och
  `Reconcile hold SET` om det satte hold igen.
- Slå på en strategi från steg 7 igen först när dess rad är stängd, och bara de
  du själv lade till:
  `docker exec hypertrade-redis-1 redis-cli SREM hypertrade:<mode>:control:disabled <strategy_name>`.
- Kill switch är fortfarande på. Den blockerar öppningar men inte exits, och
  en exit är en riktig order (avsnitt 1.4).
- Om besluten inte hinner fattas inom 30 minuter: stäng hellre de oklara
  positionerna för hand på HL än att låta dem ligga pausade utan stopp.

### Steg 9: kontrollera

- Kör paritetskontrollen i [health-check.md](health-check.md) för varje mode.
  Börsen och databasen ska stämma överens, och ingen bot ska hålla (steg 8).
- Heartbeat ska vara färsk, equity-snapshots ska skrivas och loggen ska vara
  fri från fel.
- `disabled`-mängderna ska vara som avsett, och `leverage` per position på HL
  (kommandot i steg 1) ska vara den avsedda. En override som rättas nu når HL
  först vid nästa öppning på den symbolen.
- Sätt `pct set <ctid> --onboot 1` på den återställda containern. Den gamla ska
  ha 0.

### Steg 10: slå av kill switch sist

Återställ varje modes kill switch till värdet från steg 4, inte bara till
"av". Mainnet har haft kill switch på sedan NU-1.

```sh
docker exec hypertrade-redis-1 redis-cli SET hypertrade:<mode>:control:kill_switch 0   # värdet var "0"
docker exec hypertrade-redis-1 redis-cli DEL hypertrade:<mode>:control:kill_switch     # nyckeln saknades
# Värdet var "1": låt den stå.
```

Samma sak går att göra via botens `POST /api/control/kill-switch` med
`{"active": false}` eller `{"clear": true}`.

### Efteråt

- Låt den gamla containern stå avstängd tills du vet att inga rader behöver
  räddas ur den. Ta sedan bort den med `pct destroy`.
- Skriv ner incidenten, också vilka positioner som stängdes och varför.

## 3. Återställ bara Redis

Använd det här när hosten fungerar men Redis är tom eller skadad, till
exempel efter `FLUSHALL` eller en borttagen volym.

1. Stoppa bottarna och dashboarden som i steg 4 ovan. Skriv ner facit som i
   steg 1.
2. Hämta `dump.rdb` ur backupen. I Proxmox GUI: containern → Backup → välj
   backup → File Restore. Filen ligger i
   `/var/lib/docker/volumes/hypertrade_redisdata/_data/dump.rdb`. Skapa
   katalogen med `install -d -m 700 /root/restore` på hosten och kopiera in
   filen som `/root/restore/dump.rdb`, med scp eller med `pct push` från
   noden.
3. Byt filen. Redis får inte köra medan filen byts, eftersom den skriver sitt
   minne till `dump.rdb` när den stängs. Behåll den nuvarande filen:

   ```sh
   docker stop hypertrade-redis-1
   docker cp hypertrade-redis-1:/data/dump.rdb /root/restore/redis-pre-restore.rdb   # om den finns
   docker cp /root/restore/dump.rdb hypertrade-redis-1:/data/dump.rdb
   docker start hypertrade-redis-1
   docker exec hypertrade-redis-1 redis-cli DBSIZE
   ```

   `docker cp` fungerar mot en stoppad container och skriver direkt in i
   volymen. Redis-imagen rättar ägaren till filen när containern startar.
4. Om den återställda filen inte går att använda (Redis startar inte, eller
   `DBSIZE` är 0): lägg tillbaka `redis-pre-restore.rdb` på samma sätt. Starta
   inte med en tom Redis om det finns en fil att gå tillbaka till.
5. Fortsätt med steg 4 i avsnitt 2 (från "Skriv sedan ner vad backupen hade")
   och därefter steg 7 till 10. Steg 5 och 6 behövs inte, eftersom nätet och
   databasen inte har rörts. En återställd Redis är upp till ett dygn gammal,
   så gå igenom flat-all, `disabled`-mängderna och hävstången i steg 4 extra
   noga.
6. Kör `rm -rf /root/restore` när allt är klart. Filerna innehåller session
   secret, OIDC-klienthemligheten och CF-token.

Om bara **Postgres** är skadad: använd avsnitt 2 och återställ till ett nytt
CTID. Redis rullas då också tillbaka, men det som ändrats där sedan backupen
är konfiguration, och den går steg 4 och 9 igenom. Den gamla containern finns
kvar, så rader som är nyare än backupen går att rädda ur den.

## 4. Engångsstädning

I checkouten på hosten ligger gamla dumpar. De är okrypterade kopior av
databasen och Redis på samma disk som originalet, och PBS har redan allt de
innehåller. Lista dem och radera dem:

```sh
git -C /opt/hypertrade ls-files --others | grep -E '\.(sql|dump|rdb)$'
```

`.gitignore` ignorerar de här filtyperna, så att en dump inte kan committas av
misstag.
