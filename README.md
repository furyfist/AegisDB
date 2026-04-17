From the repo root `C:\Users\himan\OneDrive\Desktop\aegisDB`, use this now:

```powershell
docker compose --env-file openmetadata.env up -d
```

That is the new “start everything” command. It uses the root-level [docker-compose.yml](C:/Users/himan/OneDrive/Desktop/aegisDB/docker-compose.yml), so you no longer need:

```powershell
docker compose -f docker\openmetadata\docker-compose.yml up -d
```

To check what’s running:

```powershell
docker compose ps
```

If you want just the target Postgres container:

```powershell
docker compose --env-file openmetadata.env up aegisdb-postgres --detach
```

Small note:
- `execute-migrate-all` may show as exited after finishing, and that is normal.
- The ones you usually want running are `openmetadata_postgresql`, `openmetadata_server`, `openmetadata_ingestion`, `aegisdb_postgres`, and usually `elasticsearch`.

So your usual next-session flow is just:

```powershell
cd C:\Users\himan\OneDrive\Desktop\aegisDB
docker compose --env-file openmetadata.env up -d
docker compose ps
```