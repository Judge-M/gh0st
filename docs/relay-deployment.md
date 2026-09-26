# Self-hosted relay deployment

The broker can run on any Linux host with Docker Compose. It terminates TLS itself and listens on port 8765. Keep `devices.json` and the TLS private key out of the repository and grant read access only to the container operator.

## Prepare secrets

Create a token of at least 32 random bytes for each daemon device. For example, on Linux:

```bash
mkdir -p secrets
chmod 700 secrets
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Create `secrets/devices.json` as a JSON object mapping each stable device ID to its token. Issue a certificate whose DNS name matches the relay hostname and save the certificate and private key as `secrets/tls.crt` and `secrets/tls.key`. Set file permissions so the deployment account and container can read them, and do not commit them.

The container process uses UID 10001. For a local Compose deployment, make the mounted secret files readable only by that UID, for example with `sudo chown 10001:10001 secrets/* && sudo chmod 0400 secrets/*`.

## Start the broker

```bash
docker compose -f compose.broker.yaml up --build --detach
```

Allow inbound TCP port 8765 only where needed. Configure each daemon with the matching `GH0ST_DEVICE_ID`, `GH0ST_RELAY_TOKEN`, and `GH0ST_RELAY_URL=wss://<relay-host>:8765`. A TLS reverse proxy is not required by this compose configuration.

The container runs as an unprivileged user, with a read-only root filesystem, dropped Linux capabilities, and no writable mount except temporary memory. The broker keeps live sockets and pair routes in memory; on restart, connected daemons republish their active pair IDs and mobile clients reconnect. This deployment stores no task prompts, pair secrets, or application database.

## Operations

```bash
docker compose -f compose.broker.yaml logs --follow
docker compose -f compose.broker.yaml restart
docker compose -f compose.broker.yaml down
```

Use the host's existing monitoring, certificate renewal, backup, and firewall processes. Rotate a device token by updating the broker's device map and the matching daemon secret, then restart the broker and reconnect that daemon.
