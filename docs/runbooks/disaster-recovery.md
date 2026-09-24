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
  minut. Pass 2 (`reconcile_positions` i `repo.py`) marknadsstänger då varje
  sådan position. På mainnet är det en riktig order med riktiga pengar, och
  ingen har bett om den.
- Samma sak gäller en position som har bytt sida efter backupen. Pass 1 stänger
  DB-raden som `wrong-side`, och pass 2 marknadsstänger sedan börspositionen.
- En DB-rad för en position som stängdes efter backupen stängs av pass 1 och
  bokförs från HL:s fill-historik. Det läggs ingen order, men PnL-raden behöver
  kontrolleras efteråt.

**Det enda som skyddar är paus.** En pausad bot kör reconcile som en
torrkörning (`_run_reconcile` i `runner.py`). Den skriver ingenting och lägger
inga order. **Kill switch skyddar inte här.** Den blockerar bara nya öppningar,
och reconcile går förbi den.

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
- Dashboardens inloggning låser sig (`locked`). Se CLAUDE.md, "Dashboard auth
  recovery".

En återställd Redis är inte tom, men den saknar allt som ändrats efter
backupen. Kontrollera flaggorna mot det operatören vill ha (steg 4).

### 1.4 Paus fryser även exits

En pausad bot kör inga strategier. Då körs inte heller några SL- eller
TP-exits, och före SE-1 finns inga stopp på börsen. Därför får pausen vara
högst 30 minuter. Efter pausen tar kill switch över. Den blockerar öppningar
men låter exits gå.

### 1.5 Vad kodvakterna ändrar (PR #180)

Kodvakterna var inte mergade när detta skrevs. Tills de är deployade är stegen
nedan det enda skyddet.

- **Redis-sentinel** (`hypertrade:<mode>:control:sentinel`). Om nyckeln saknas
  startar boten med kill switch på och larmar med de öppna positionerna. Om
  databasen dessutom ser inaktuell ut startar boten pausad och larmar igen med
  jämna mellanrum. En återställd Redis som redan hade sentinel ser normal ut
  för vakten. Vakten fångar alltså en tom Redis, inte en gammal.
- **Reconcile pass 2** larmar i stället för att stänga när ett pass hittar mer
  än en orphan, när symbolen inte handlas av någon strategi i boten, eller när
  den nyaste trade-raden är äldre än `RECONCILE_ORPHAN_MAX_DB_AGE_HOURS`.
  Larmet måste kvitteras manuellt.
- **Stegen nedan gäller även efter vakterna.** Vakterna är ett skyddsnät för
  den som glömmer ett steg. En enstaka orphan mot en färsk databas stängs
  fortfarande.

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

Paper har ingen riktig börs. Dess simulerade börs ligger i Redis och kommer
tillbaka med backupen.

### Steg 2: återställ från PBS utan att starta

```sh
pvesm list <pbs-lagring> --vmid <ctid>     # välj backup, normalt den senaste
pct restore <nytt-ctid> <pbs-lagring>:backup/ct/<ctid>/<tidpunkt> --storage <lagring>
```

- `pct restore` startar inte containern. Om du använder GUI:t: bocka ur
  "Start after restore".
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

### Steg 4: stoppa bottarna och sätt kill switch och paus

Kör detta inne i containern:

```sh
docker ps -a --format '{{.Names}}' | grep -E '^xupertrade-bot-' | xargs -r docker stop
docker stop hypertrade-dashboard-1   # så att ingen startar en bot mitt i återställningen
```

Skriv sedan ner vad backupen hade, så att steg 10 kan återställa det:

```sh
for m in paper testnet mainnet; do
  echo "$m paused=[$(docker exec hypertrade-redis-1 redis-cli GET hypertrade:$m:control:paused)]" \
       "kill_switch=[$(docker exec hypertrade-redis-1 redis-cli GET hypertrade:$m:control:kill_switch)]"
done
```

Tomma hakparenteser betyder att nyckeln saknas och att env-standarden gäller.
Sätt sedan båda flaggorna för alla tre moder och spara:

```sh
for m in paper testnet mainnet; do
  docker exec hypertrade-redis-1 redis-cli SET hypertrade:$m:control:kill_switch 1
  docker exec hypertrade-redis-1 redis-cli SET hypertrade:$m:control:paused 1
done
docker exec hypertrade-redis-1 redis-cli BGSAVE
docker exec hypertrade-redis-1 redis-cli INFO persistence | grep rdb_last_bgsave_status   # ok
```

`BGSAVE` behövs eftersom standardpolicyn sparar sex ändringar först efter en
timme. Om Redis startar om innan dess försvinner flaggorna.

Kontrollera också `SMEMBERS hypertrade:<mode>:control:disabled` mot det som ska
vara avstängt. Redis-kopian kan sakna ändringar från den sista timmen före
backupen.

### Steg 5: koppla in nätet igen

På Proxmox-noden:

```sh
pct set <ctid> --net0 '<raden från steg 3>'     # och varje annan netN
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
- Om master har fler migrationer än backupen: kör dem först när återställningen
  är kontrollerad. Se `bot/scripts/migrate.sh`.
- Om `docker logs` fallerar: läs containerns loggfil direkt, enligt CLAUDE.md
  § 3.

### Steg 7: kontrollera pariteten och starta bottarna

Starta dashboarden med `docker start hypertrade-dashboard-1` och logga in. Om
`/login` säger "Authentication is locked" kom Redis inte tillbaka. Stanna då
och utred, i stället för att konfigurera om inloggningen.

Jämför facit från steg 1 med de öppna raderna i databasen:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, strategy_name, symbol, side, size, entry_price, opened_at
     FROM positions WHERE is_open = true ORDER BY mode, symbol;"
```

| Börsen | Databasen | Vad som händer vid unpause | Vad du gör |
|---|---|---|---|
| Position | Ingen rad | Pass 2 marknadsstänger positionen | Bestäm nu. Stäng den själv på HL (reduce-only), eller låt reconcile stänga den vid unpause. Att skapa en DB-rad för hand avråds från, eftersom strategin saknar SL-state för positionen. |
| Position | Rad med motsatt sida | Pass 1 stänger raden (`wrong-side`), och sedan marknadsstänger pass 2 positionen | Bestäm nu, som för en position utan rad. |
| Ingen position | Öppen rad | Pass 1 stänger raden och bokför den från fill-historiken | Notera raden och kontrollera PnL efteråt. |
| Position | Rad med samma sida och annan storlek | Avvikelsen loggas bara | Notera den. |

Med PR #180 deployad kan pass 2 larma i stället för att stänga (avsnitt 1.5).
Planera ändå som om positionen stängs.

Bottarna som ska startas är de som databasen markerar som körande:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, container_name FROM tenant_bots WHERE is_running ORDER BY mode;"
```

Starta dem en i taget med `docker start <container_name>`. Börja med paper och
ta mainnet sist. **Tiden börjar gå vid första bot-starten: unpausa inom 30
minuter.**

Om dashboarden inte når en bot (401) är Redis-kopian äldre än botens senaste
start. Starta då om just den boten från dashboarden (Settings → Bots → restart,
med passfras). Containern skapas då på nytt med en ny nyckel.

Varje bot startar pausad och med kill switch på, så reconcile körs som
torrkörning:

```sh
docker logs --since 10m <container_name> 2>&1 | grep -i reconcile
```

- Raderna med `PAUSED — not closed (orphan)` ska vara exakt tabellens
  "Ingen position / Öppen rad". Raderna med `(wrong-side)` ska vara exakt
  "Rad med motsatt sida".
- `… db rows, … exchange positions` ska stämma med antalen i facit och i
  tabellen.
- Börspositioner utan DB-rad syns inte här (avsnitt 1.2). För dem är tabellen
  det enda underlaget.

Om något inte stämmer: stoppa boten och utred innan du går vidare.

### Steg 8: unpausa, inom 30 minuter

När varje avvikelse från steg 7 har fått ett beslut: unpausa de moder som
körde före incidenten. Använd dashboardens kontroller eller:

```sh
docker exec hypertrade-redis-1 redis-cli SET hypertrade:<mode>:control:paused 0
```

- En mode som var pausad i backupen (steg 4) förblir pausad. Det beslutet är
  operatörens.
- Det första riktiga reconcile-passet körs inom fem minuter. Följ loggen tills
  det har gått. Det ska göra exakt det som tabellen i steg 7 förutsade.
- Kill switch är fortfarande på. Inga nya positioner öppnas, men exits körs.
- Om besluten inte hinner fattas inom 30 minuter: stäng hellre de oklara
  positionerna för hand på HL än att låta dem ligga pausade utan stopp.

### Steg 9: kontrollera

- Kör paritetskontrollen i CLAUDE.md § 3 ("Standard 'is the bot OK?' check")
  för varje mode. Börsen och databasen ska stämma överens.
- Heartbeat ska vara färsk, equity-snapshots ska skrivas och loggen ska vara
  fri från fel.
- `disabled`-mängderna och hävstångsoverrides ska vara som avsett.
- `onboot` ska vara 1 på den återställda containern och 0 på den gamla.

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
   så gå igenom `disabled`-mängderna och hävstången extra noga.
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
