# Deploying the hosted service

The container serves the web page, the JSON API and the public MCP endpoint on port 8000.

```bash
# build in Azure (no local Docker needed), from the repository root
az acr build --registry <registry> --image justify:<tag> --file deploy/Dockerfile .

# run it in an existing Container Apps environment
az containerapp create -g <group> -n justify --environment <env> \
  --image <registry>.azurecr.io/justify:<tag> --registry-server <registry>.azurecr.io \
  --ingress external --target-port 8000 --cpu 2 --memory 4Gi --min-replicas 1 --max-replicas 1 \
  --env-vars JUSTIFY_PUBLIC_HOSTS=<host> JUSTIFY_PUBLIC_URL=https://<host> JUSTIFY_WORKERS=2
```

Settings (environment): `JUSTIFY_PUBLIC_HOSTS`, `JUSTIFY_PUBLIC_URL`, `JUSTIFY_WORKERS` (2),
`JUSTIFY_QUEUE_MAX` (30), `JUSTIFY_SCAN_TIMEOUT_S` (600), `JUSTIFY_CLONE_TIMEOUT_S` (180),
`JUSTIFY_MAX_REPO_MB` (400), `JUSTIFY_RATE_PER_HOUR` (20 per address), `JUSTIFY_SCAN_MEM_MB` (3072),
`JUSTIFY_DATA_DIR`. One replica keeps the job store in one place; scans are cached per commit.
