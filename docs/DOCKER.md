# Docker Compose

Use Docker Engine with the Compose plugin on Linux, or Docker Desktop with Linux containers on Windows or macOS. Run commands from the project root with Docker running.

## Build from source

Copy the example settings:

```sh
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env`.

Edit `.env` to set an optional API key and host port, then start:

```sh
docker compose up -d --build
```

Open `http://localhost:8765` on the Docker host, or `http://SERVER_IP:8765` from your trusted home network. If you changed the host port, use that port instead. Use **Adressen aktualisieren** on the setup page when you want to download the address list. Container startup and its health check do not download addresses or calendars.

The container runs under a non-root account. Named volumes retain cache and logs (`bridge-data`) and the address snapshot (`address-data`) when the container is recreated.

## Settings

| `.env` setting | Purpose |
| --- | --- |
| `LOXONE_BRIDGE_API_KEY` | Optional shared key for pickup and log requests |
| `LOXONE_BRIDGE_PORT` | Published host port; default `8765` |
| `ABFALL_OOE_IMAGE` | Image reference used only by `compose.published.yaml` |

Run `docker compose up -d` after changing settings. Compose publishes the host port on all interfaces by default. Keep it on a trusted home network; the setup page and ordinary address lookups are public within your trusted network; starting a refresh and loading calendar interval choices require the configured API key.

The `.env` file is ignored by Git. Do not include real keys, addresses, or logs when sharing diagnostics.

## Status, logs, updates, and stopping

```sh
docker compose ps
docker compose logs --tail=50
```

Update a source checkout and rebuild:

```sh
git pull
docker compose up -d --build
```

Stop and remove the container while retaining its saved data:

```sh
docker compose down
```

Adding `--volumes` to the removal command permanently deletes the associated saved data, including cached dates, downloaded addresses, and logs.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Docker cannot start the service | Confirm Docker is running and the configured host port is available; inspect `docker compose ps` and the logs. |
| Setup page does not open | Use the Docker host's address and `LOXONE_BRIDGE_PORT`; check the published port and local firewall. |
| Published image cannot be pulled | Confirm the image and tag exist, the `OWNER` placeholder is replaced, and the package permits access. |

Use `docker compose logs --tail=50` for recent logs, or `docker compose logs -f` to follow them. For a published-image installation, include `-f compose.published.yaml` after `docker compose`.

The container's `/health` check verifies only that the bridge responds locally. It does not contact Umweltprofis or verify the Loxone configuration. See [Loxone and API troubleshooting](LOXONE_BRIDGE.md#troubleshooting-requests-and-setup) for missing addresses, authentication errors, stale dates, and pickup request errors.

## Published images

The [publishing workflow](../.github/workflows/publish-docker.yml) is configured to build `linux/amd64` and `linux/arm64` images and publish them to `ghcr.io/OWNER/abfall-ooe`. It accepts existing `vMAJOR.MINOR.PATCH` tags, optionally with a SemVer prerelease suffix. Manual dispatch requires an existing version tag. This configuration alone does not mean an image has already been published or verified on each architecture.

Once an image is published and its package is public, you can run it without a local build. Place `compose.published.yaml`, `.env.example`, and your `.env` file together. In `.env`, replace `OWNER` in `ABFALL_OOE_IMAGE` with the publishing GitHub account or organization. Use an available version tag for a specific release, or `latest` when available.

```sh
docker compose -f compose.published.yaml up -d
```

Update that installation with:

```sh
docker compose -f compose.published.yaml pull
docker compose -f compose.published.yaml up -d
```

For logs or removal, use the same `-f compose.published.yaml` option with the corresponding Compose commands.

Publishing waits for the Windows/Linux Python matrix, dependency audit, and source/history secret scans
for the exact tagged commit. Each architecture is built and tested offline for startup, non-root execution,
volume access, synthetic cached responses, and persistence after restart. Only those tested image archives
are pushed; the release manifest combines them. Stable releases also update `latest`; prereleases do not.
Local-only image checks can be run with `python tools/container_smoke.py IMAGE` when Docker is running.
They create and remove uniquely named temporary containers and volumes and disable external networking.
The manifest uses full version tags; minor-version aliases are not published.
