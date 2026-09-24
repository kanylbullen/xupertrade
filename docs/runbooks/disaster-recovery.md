# Katastrofåterställning: Postgres och Redis

Runbooken gäller xupertrade-hosten (`/opt/hypertrade`). Den hör till roadmapens
NU-2. Skripten är `ops/backup.sh`, som körs varje natt, och `ops/restore-test.sh`,
som körs varje månad.

- Backupen körs kl. 03:30 UTC och skickas krypterad med restic till extern
  lagring. Man kan alltså förlora upp till ungefär 24 timmars data (RPO ≈ 24 h).
- Återställningen i avsnitt 4 kräver att en människa är närvarande från första
  steget tills kill switch är avslagen.
- Engångsuppsättningen står i avsnitt 6. När detta skrevs (2026-09-24) var inget
  av det gjort ännu.

## 1. Vad backupen täcker

| Del | Hur | Innehåll |
|---|---|---|
| Postgres-databasen `hypertrade` | `pg_dump -Fc` i postgres-containern (en konsistent ögonblicksbild) → `hypertrade.dump` | Alla tabeller: trades, positions, equity, funding, tenants, tenant_bots, tenant_secrets (tenanternas krypterade HL-nycklar), vault-tabellerna och `alembic_version` |
| Postgres-rollerna (`tenant_<hex>`) | `pg_dumpall --globals-only --no-role-passwords` → `globals.sql` | Rollerna som dumpen ger rättigheter till. Utan dem avbryts `pg_restore`. Lösenorden följer inte med, men dashboarden sätter dem igen när respektive bot startas. Lösenordet finns i Redis. |
| Hela Redis | `BGSAVE`, sedan en kopia av RDB-filen → `redis-dump.rdb` | Kontrollflaggorna per mode (`paused`, `kill_switch`, `disabled`, `leverage`, mainnet-opt-in), paper-lägets simulerade börs, dashboardens inloggningskonfiguration och session secret, API-nyckeln för varje bot, tenant-rollernas lösenord och strategisnapshots |
| `manifest.txt` | Skrivs av skriptet | Tidpunkt, alembic-revision, antal Redis-nycklar, checkoutens git-SHA och pg_dump-version. Inga hemligheter. |

restic krypterar allt på hosten innan något skickas iväg. Kopiorna sparas med 14
dagliga, 8 veckovisa och 6 månatliga versioner. Filerna samlas i
`/var/backups/xupertrade` under körningen och raderas efteråt, så ingen kopia
ligger kvar på hosten.

Ett återställt katalogträd är hemligt. Redis-kopian innehåller session secret,
OIDC-klienthemligheten och CF-token. Radera trädet när du är klar.

## 2. Vad backupen inte täcker

- **Det som hänt sedan senaste körningen.** Det kan vara upp till cirka 24
  timmars trades, positioner, funding och Redis-ändringar. `archive_mode=off`,
  så det går inte att återställa till en valfri tidpunkt.
- **Positionerna på börsen.** HyperLiquid är facit. Efter en återställning är
  det databasen som ska anpassas efter börsen, inte tvärtom.
- **Phase och det som bara finns där.** Det gäller runtime-hemligheterna
  (`POSTGRES_PASSWORD`, `API_KEY`, Telegram, `CLOUDFLARE_TUNNEL_TOKEN`,
  `OIDC_*` med flera) och restic-hemligheterna själva. Om Phase ligger på samma
  nod som boten försvinner nyckeln till backupen samtidigt som backupen behövs.
  Därför ska `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` och lagringsnyckeln också
  finnas i lösenordshanteraren, utanför noden.
- **Hostens egen konfiguration.** Det gäller `.phase.json`, Phase-servicetoken,
  root-crontab och SSH-nycklarna.
- **Det som byggs om i stället för att återställas.** Det gäller Docker-images,
  checkouten (git) och Caddy-volymerna. Caddy-volymerna innehåller certifikat
  och den pushade TLS-konfigurationen, och bootstrap-certet kommer tillbaka av
  sig självt. Hit hör också `bot/data/historical/`, som laddas ner igen, och
  containerloggarna.
- **Tenant-bottarnas containrar.** De skapas om från dashboarden med respektive
  tenants passfras.

## 3. Farorna vid en återställning

### 3.1 Reconcile pass 2 stänger börspositioner som saknar DB-rad

En position som öppnades efter backupen finns på börsen men inte i den
återställda databasen.

- En bot som inte är pausad kör reconcile när den startar och sedan var femte
  minut. Pass 2 (`reconcile_positions` i `repo.py`) marknadsstänger då varje
  sådan position.
- På mainnet är det en riktig order med riktiga pengar, och ingen har bett om
  den.
- Det omvända fallet finns också. En DB-rad för en position som stängdes efter
  backupen stängs av pass 1 och bokförs från HL:s fill-historik. Det läggs ingen
  order, men PnL-raden behöver kontrolleras efteråt.

**Det enda som skyddar är paus.** En pausad bot kör reconcile som en
torrkörning (`_run_reconcile` i `runner.py`). Den skriver ingenting och lägger
inga order. **Kill switch skyddar inte här.** Den blockerar bara nya öppningar,
och reconcile går förbi den.

Torrkörningen rapporterar bara pass 1, alltså de DB-rader den skulle ha stängt
(`PAUSED — not closed`). Pass 2 hoppas över helt medan boten är pausad. De
börspositioner som saknar DB-rad, alltså just de som står på spel, syns därför
inte i loggen. Det enda som syns är antalet i raden
`Reconcile: … db rows, … exchange positions`. Du måste själv jämföra börsen med
databasen (steg 1 och steg 7).

### 3.2 En tom Redis läses som att allt kör som vanligt

- `is_paused()` svarar ja bara när nyckeln är `"1"`. Om nyckeln saknas räknas
  boten alltså som ej pausad.
- Om kill switch saknas används env-värdet (`KILL_SWITCH`), som är `false`
  som standard.
- `disabled`-mängderna är tomma. Därmed är **alla** strategier påslagna igen,
  även de som operatören har stängt av.
- Hävstångsoverrides är borta.
- Mainnet-opt-in är också tom, och det stänger mainnet. Det är det enda som
  hamnar rätt av sig självt.
- Dashboardens inloggning låser sig (`locked`). Se CLAUDE.md § 3, "Dashboard
  auth recovery".

### 3.3 Paus fryser även exits

En pausad bot kör inga strategier. Då körs inte heller några SL- eller
TP-exits, och före SE-1 finns inga stopp på börsen. Därför får pausen i
proceduren vara högst 30 minuter. Efter pausen tar kill switch över. Den
blockerar öppningar men låter exits gå.

### 3.4 Bottarna startar av sig själva

Tenant-bottarna skapas med restart policy `unless-stopped`. Efter en omstart av
hosten eller Docker startar de alltså med sin gamla env. Det gäller även om
Redis är tom eller databasen bara är halvt återställd. Stoppa dem därför först
(steg 1). En container som har stoppats med `docker stop` förblir stoppad även
efter en omstart.

### 3.5 Vad kodvakterna ändrar (grenen `fix/restore-safe-guards`)

Kodvakterna i NU-2 punkt 4 och 5 var inte mergade när detta skrevs. Tills de är
deployade är stegen nedan det enda skyddet. Så här ändrar vakterna läget:

- **Redis-sentinel.** Om sentinel saknas startar boten med kill switch aktiv.
  Då blockeras öppningar medan exits fortsätter, och boten larmar med listan
  över öppna positioner.
  - Om databasen dessutom ser inaktuell ut startar boten pausad. Databasen ser
    inaktuell ut när börsens `userFills` innehåller fills som är nyare än den
    nyaste trade-raden. Larmet upprepas då och eskaleras efter N minuter i
    frysning.
  - Vakten reagerar på en Redis som saknar sentinel, alltså en tom eller ny
    Redis. Förutsatt att sentinel sparas i Redis, som roadmapen beskriver,
    innehåller en korrekt återställd RDB den. Då ser läget normalt ut för
    vakten.
  - Vakten fångar alltså fallet där Redis inte återställdes alls, men den
    ersätter inte steg 4.
- **Reconcile pass 2** larmar i stället för att stänga i tre fall:
  - när ett pass hittar mer än en orphan
  - när symbolen ligger utanför det registrerade universumet
  - när den nyaste DB-raden är äldre än N timmar

  Larmet måste kvitteras manuellt. Efter en återställning är den nyaste raden
  minst lika gammal som backupen, så en enstaka orphan larmar i stället för att
  stängas om backupen är äldre än N timmar.
- **Stegen nedan gäller även efter vakterna.** Vakterna är ett skyddsnät för
  den som glömmer ett steg och ersätter inte proceduren. Fyll i nyckelnamnen och
  värdena för N här när PR:en är mergad.

## 4. Återställning, med en människa närvarande

Förutsättningar:

- En person som kan låsa upp bottarna med tenantens passfras och som kan se
  kontona på HyperLiquid. Personen stannar tills steg 10 är klart.
- Tillgång till hemligheterna, antingen via `phase run` eller från kopian utanför
  noden om Phase inte finns kvar.
- Ungefär en timme. Steg 7 och 8 har en tidsgräns: högst 30 minuter från första
  bot-start till unpause.

Alla kommandon körs som root på hosten, i `/opt/hypertrade`.

### Steg 1: stoppa allt som kan handla

```sh
docker ps --format '{{.Names}}' | grep -E '^xupertrade-bot-' | xargs -r docker stop
docker stop hypertrade-dashboard-1   # så att ingen startar en bot mitt i återställningen
```

Skriv sedan ner vad börsen har just nu, för varje konto (testnet och mainnet).
Det är facit för steg 7. HL:s webbgränssnitt räcker, men positionerna går också
att läsa utan nyckel:

```sh
curl -s https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' \
  -d '{"type":"clearinghouseState","user":"<kontoadress>"}'
# testnet: https://api.hyperliquid-testnet.xyz/info
```

Paper har ingen riktig börs. Dess simulerade börs ligger i Redis och kommer
tillbaka tillsammans med RDB-filen i steg 3.

### Steg 2: hämta snapshoten

```sh
cd /opt/hypertrade
phase run -- restic snapshots --host xupertrade --tag xupertrade --latest 5
install -d -m 700 /root/restore
phase run -- restic restore <snapshot-id> --target /root/restore
R=/root/restore/var/backups/xupertrade
cat "$R/manifest.txt"
```

Välj normalt den senaste snapshoten. Om databasen har varit skadad en tid,
välj i stället den sista från före skadan.

### Steg 3: återställ Redis

Redis får inte köra när filen byts ut. När Redis stängs skriver den sitt minne
till `dump.rdb` och skriver då över den återställda filen.

```sh
docker stop hypertrade-redis-1
# Ny host utan container: phase run -- docker compose up --no-start redis
docker cp "$R/redis-dump.rdb" hypertrade-redis-1:/data/dump.rdb
docker start hypertrade-redis-1
docker exec hypertrade-redis-1 redis-cli DBSIZE                  # = redis_keys i manifestet
docker exec hypertrade-redis-1 redis-cli CONFIG GET appendonly   # ska vara "no"
```

`docker cp` fungerar mot en stoppad container och skriver direkt in i volymen
`redisdata`. Redis-imagen rättar ägaren till filen när containern startar.

Om RDB-filen inte går att använda: starta Redis tom och gå vidare till steg 4.
Lägg sedan tillbaka `disabled`-mängderna för hand innan första bot startar.

### Steg 4: sätt kill switch och paus innan någon bot startar

Skriv först ner vad backupen hade, så att steg 10 kan återställa det:

```sh
for m in paper testnet mainnet; do
  echo "$m paused=[$(docker exec hypertrade-redis-1 redis-cli GET hypertrade:$m:control:paused)]" \
       "kill_switch=[$(docker exec hypertrade-redis-1 redis-cli GET hypertrade:$m:control:kill_switch)]"
done
```

Tomma hakparenteser betyder att nyckeln saknas och att env-standarden gäller.
Sätt sedan båda flaggorna för alla tre moder:

```sh
for m in paper testnet mainnet; do
  docker exec hypertrade-redis-1 redis-cli SET hypertrade:$m:control:kill_switch 1
  docker exec hypertrade-redis-1 redis-cli SET hypertrade:$m:control:paused 1
done
docker exec hypertrade-redis-1 redis-cli BGSAVE
```

`BGSAVE` behövs eftersom standardpolicyn sparar sex ändringar först efter en
timme. Om Redis startar om innan dess försvinner flaggorna.

Kontrollera också `SMEMBERS hypertrade:<mode>:control:disabled` mot det som ska
vara avstängt.

### Steg 5: återställ Postgres

Om databasen finns men är skadad: behåll den under ett annat namn, eftersom rader
som är nyare än backupen kan gå att rädda ur den. Skapa sedan en tom databas:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d postgres \
  -c "ALTER DATABASE hypertrade RENAME TO hypertrade_broken_$(date -u +%Y%m%d)"
docker exec hypertrade-postgres-1 createdb -U postgres hypertrade
```

På en ny host eller med en tom volym skapar
`phase run -- docker compose up -d postgres` en tom `hypertrade`. Fortsätt
sedan så här:

```sh
docker cp "$R/globals.sql" hypertrade-postgres-1:/tmp/globals.sql
docker cp "$R/hypertrade.dump" hypertrade-postgres-1:/tmp/hypertrade.dump
docker exec hypertrade-postgres-1 psql -U postgres -d postgres -f /tmp/globals.sql
#   ERROR: role "postgres" already exists   <- väntat och ofarligt
docker exec hypertrade-postgres-1 pg_restore -U postgres -d hypertrade --exit-on-error /tmp/hypertrade.dump
docker exec hypertrade-postgres-1 rm /tmp/globals.sql /tmp/hypertrade.dump
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -tAc 'SELECT version_num FROM alembic_version'
```

Revisionen ska vara samma som `alembic_revision` i manifestet. Om master har
fler migrationer: kör dem först när återställningen är kontrollerad. Se
`bot/scripts/migrate.sh`.

### Steg 6: starta dashboarden

```sh
phase run -- docker compose up -d --no-deps dashboard
```

`--no-deps` är inte valfritt. Utan den kan compose återskapa Redis, se CLAUDE.md
§ 3. Starta Caddy och cloudflared på samma sätt om de inte redan kör.

Logga sedan in. Om `/login` säger "Authentication is locked" har
Redis-återställningen misslyckats. Gå då tillbaka till steg 3 i stället för att
konfigurera om inloggningen.

### Steg 7: kontrollera pariteten och starta bottarna

Jämför facit från steg 1 med de öppna raderna i databasen:

```sh
docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
  "SELECT mode, strategy_name, symbol, side, size, entry_price, opened_at
     FROM positions WHERE is_open = true ORDER BY mode, symbol;"
```

| Börsen | Databasen | Vad som händer vid unpause | Vad du gör |
|---|---|---|---|
| Position | Ingen rad | Pass 2 marknadsstänger positionen | Bestäm nu. Stäng den själv på HL (reduce-only), eller låt reconcile stänga den vid unpause. Att skapa en DB-rad för hand avråds från, eftersom strategin saknar SL-state för positionen. |
| Ingen position | Öppen rad | Pass 1 stänger raden och bokför den från fill-historiken | Notera raden och kontrollera PnL efteråt. |
| Position | Rad med annan storlek | Avvikelsen loggas bara | Notera den. |

Starta sedan en bot i taget från dashboarden (Settings → Bots, med passfras).
Börja med paper och ta mainnet sist. **Tiden börjar gå vid första bot-starten:
unpausa inom 30 minuter.**

Varje bot startar pausad och med kill switch på. Reconcile vid start körs som
torrkörning:

```sh
docker logs --since 10m <bot-container> 2>&1 | grep -i reconcile
```

- `PAUSED — not closed (…)`-raderna ska vara exakt raderna i tabellens andra
  rad.
- `… db rows, … exchange positions` ska stämma med antalen i facit och i
  tabellen.
- Börspositioner utan DB-rad syns inte här (avsnitt 3.1). För dem är tabellen
  det enda underlaget.

Om något inte stämmer: stoppa boten och utred innan du går vidare. Om
`docker logs` fallerar, läs loggfilen direkt enligt CLAUDE.md § 3.

### Steg 8: unpausa, inom 30 minuter

När varje avvikelse från steg 7 har fått ett beslut: unpausa de moder som körde
före incidenten. Använd dashboardens kontroller eller:

```sh
docker exec hypertrade-redis-1 redis-cli SET hypertrade:<mode>:control:paused 0
```

- En mode som var pausad före incidenten (enligt steg 4) förblir pausad. Det
  beslutet är operatörens.
- Det första riktiga reconcile-passet körs inom fem minuter. Följ loggen tills
  det har gått. Det ska göra exakt det som tabellen i steg 7 förutsade.
- Kill switch är fortfarande på. Inga nya positioner öppnas, men exits körs.
- Om besluten inte hinner fattas inom 30 minuter: stäng hellre de oklara
  positionerna för hand på HL än att låta dem ligga pausade utan stopp.

### Steg 9: kontrollera

- Kör paritetskontrollen i CLAUDE.md § 3 ("Standard 'is the bot OK?' check") för
  varje mode. Börsen och databasen ska stämma överens.
- Heartbeat ska vara färsk, equity-snapshots ska skrivas och loggen ska vara fri
  från fel.
- `disabled`-mängderna och hävstångsoverrides ska vara som avsett.

### Steg 10: slå av kill switch sist

Återställ varje modes kill switch till värdet från steg 4, inte bara till "av".
Mainnet har haft kill switch på sedan NU-1.

```sh
docker exec hypertrade-redis-1 redis-cli SET hypertrade:<mode>:control:kill_switch 0   # värdet var "0"
docker exec hypertrade-redis-1 redis-cli DEL hypertrade:<mode>:control:kill_switch     # nyckeln saknades
# Värdet var "1": låt den stå.
```

Samma sak går att göra via botens `POST /api/control/kill-switch` med
`{"active": false}` eller `{"clear": true}`.

### Efteråt

- Kör `rm -rf /root/restore`. Trädet innehåller hemligheter.
- Radera `hypertrade_broken_*` när den inte behövs längre.
- Kontrollera att nästa nattliga backup blir grön, och kör `ops/restore-test.sh`
  en gång.
- Skriv ner incidenten, också vilka positioner som stängdes och varför.

## 5. Är backupen färsk?

- **healthchecks.io.** Backupkontrollen ska vara grön. Den har schemat
  `30 3 * * *` (UTC) och 60 minuters grace. En körning som uteblir larmar
  därför senast 04:30 dagen efter, alltså inom 25 timmar. En `/fail`-ping larmar
  direkt.
- **På hosten.** Kör `tail -n 20 /var/log/xupertrade-backup.log`. Sista raden ska
  vara `backup: OK`, med `snapshot <id> saved` strax ovanför.
- **Direkt mot restic-repot.** Kör
  `cd /opt/hypertrade && phase run -- restic snapshots --host xupertrade --tag xupertrade --latest 1`.
  Tidsstämpeln ska ligga inom de senaste 24 timmarna.
- **Varje månad.** `phase run -- ./ops/restore-test.sh` skriver
  `RESTORE-TEST PASS ... age=<h>h`. Skriptet skriver FAIL om den nyaste
  snapshoten är äldre än 26 timmar.

## 6. Engångsuppsättning

1. **Börja med Proxmox (fem minuter).** Kontrollera om vzdump eller PBS redan
   täcker LXC:n. Kör på Proxmox-noden:

   ```sh
   cat /etc/pve/jobs.cfg                  # backupjobb (äldre PVE: /etc/pve/vzdump.cron)
   pvesm status                           # vilka lagringar som finns
   pvesh get /nodes/<nod>/tasks --typefilter vzdump --limit 5
   pvesm list <lagring> --vmid <ctid>     # finns det kopior av just den här containern?
   zfs list -t snapshot | grep <ctid>     # om noden kör ZFS
   ```

   Svara på följande frågor:
   - Ingår containern i ett jobb?
   - Hur ofta körs jobbet?
   - Var hamnar kopian? En kopia på samma nod räknas inte som utanför hosten.
   - Hur länge sparas kopian?
   - När lyckades jobbet senast?

   En vzdump är kraschkonsistent och snabb att återställa hela hosten från. Den
   ersätter ändå inte den logiska dumpen utanför noden. Skriv svaren i den
   privata bilagan, inte i repot, eftersom VMID och lagringsnamn är detaljer om
   infrastrukturen.
2. **healthchecks.io.**
   - Skapa ett konto med Telegram och e-post som kanaler. NU-3 återanvänder
     kontot.
   - Skapa kontrollen `xupertrade-backup` med schemat `30 3 * * *`, tidszon UTC
     och 60 minuters grace.
   - Skapa gärna en andra kontroll för restore-testet med schemat `0 5 1 * *`
     och några timmars grace.
3. **Lagring.**
   - Skapa en privat B2-bucket och en applikationsnyckel som bara gäller den
     bucketen, inte kontots huvudnyckel.
   - Nyckeln måste få radera, eftersom `restic forget --prune` behöver det.
   - Sätt bucketens livscykel till "Keep only the last version". Annars ligger
     raderade filer kvar och kostar pengar.
4. **Phase** (appen `hypertrade`, env `Development`):

   | Nyckel | Värde |
   |---|---|
   | `RESTIC_REPOSITORY` | `b2:<bucket>:<sökväg>` |
   | `RESTIC_PASSWORD` | ett långt slumpat lösenord, till exempel från `openssl rand -base64 36` |
   | `B2_ACCOUNT_ID` | applikationsnyckelns ID |
   | `B2_ACCOUNT_KEY` | applikationsnyckeln |
   | `HC_PING_URL` | `https://hc-ping.com/<uuid>` |
   | `RESTORE_TEST_HC_PING_URL` | valfri, för restore-testet |

   Lägg samtidigt de fyra första i lösenordshanteraren, utanför noden. Se
   avsnitt 2.
5. **restic på hosten.** Installera med `apt install restic` och kontrollera med
   `restic version`. Versionen ska vara 0.14 eller senare. Skripten behöver också
   `curl` och `flock`. Restore-testet behöver dessutom `xupertrade-bot:latest`
   och laddar ner `postgres:16` första gången.
6. **Initiera repot en gång:** `cd /opt/hypertrade && phase run -- restic init`.
7. **Kör första gången för hand**, och kör sedan restore-testet:

   ```sh
   cd /opt/hypertrade
   phase run -- ./ops/backup.sh
   phase run -- ./ops/restore-test.sh     # ska skriva RESTORE-TEST PASS
   ```

8. **Crontab** (root). Hostens klocka ska gå i UTC, kontrollera med `date`:

   ```
   PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
   30 3 * * * cd /opt/hypertrade && phase run -- ./ops/backup.sh >> /var/log/xupertrade-backup.log 2>&1
   0 5 1 * * cd /opt/hypertrade && phase run -- ./ops/restore-test.sh >> /var/log/xupertrade-restore-test.log 2>&1
   ```

   `PATH`-raden behövs för att cron ska hitta `phase` och `restic`. Crontab
   finns bara på hosten, så kontrollera den efter varje ombyggnad (CLAUDE.md
   § 3, "Host-side cron jobs").
9. **Städa.** När den första externa backupen och restore-testet är gröna:
   radera de gamla lokala dumparna i checkouten på hosten (`*.sql`, `*.dump`,
   `*.rdb` i `/opt/hypertrade`). De är okrypterade kopior på samma disk.
