# Production deployment status

There is no supported Production Mode in 0.2.0.dev0. Do not deploy the existing
demo Compose stack as an enterprise service. The API deliberately refuses to
start when `RAGOPS_ENV=production` is selected.

The exact local start command to verify that refusal is:

```bash
RAGOPS_ENV=production .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

With an available event loop, the application factory rejects startup with
`Production runtime is unavailable`. In the current socket-restricted sandbox,
event-loop initialization can fail earlier. The factory refusal was tested directly.
It is not a working production deployment command. A real command using
`docker-compose.prod.yml` can only be supplied after the production topology and
mandatory service/identity/recovery gates are implemented and pass.

Run the enterprise gate with:

```bash
make product-verify
```

It returns nonzero if any mandatory check fails or cannot run. It writes fresh
reports in evidence/product-v1/. It never deploys services, restores backups or
deletes production data. Missing dependencies, inaccessible Docker and denied
sockets remain visible; mock/stub tests cannot turn those gates green.

Demo Mode without Docker:

```bash
RAGOPS_ENV=demo RAGOPS_LLM_PROVIDER=deterministic make demo
```

Existing Docker demo (requires a working Docker daemon): `make up`.
For the current demonstration, see docs/DEPLOYMENT.md and docs/DEMO_SCRIPT.md.
