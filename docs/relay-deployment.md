# Self-hosted relay deployment

The same Docker Compose broker can run on a local Linux machine or on a VPS. The default bind address is `127.0.0.1`, so a fresh local deployment is reachable only from that machine. For a VPS, set `GH0ST_RELAY_BIND_ADDRESS=0.0.0.0` and restrict inbound TCP port 8765 with the host firewall. In both cases the broker terminates TLS itself; no reverse proxy is required.

## Requirements

- Linux host with Docker Engine and the Docker Compose plugin.
- A TLS certificate and private key valid for the relay hostname. Local development may use a locally trusted certificate; mobile devices must trust the certificate chain. Public VPS deployments should use a certificate issued for the VPS DNS name.
- A token of at least 32 random bytes for each daemon device.

## Prepare secrets

Run from the repository root:

```bash
mkdir -p secrets
chmod 700 secrets
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Create `secrets/devices.json` as a JSON object mapping each stable device ID to its generated token. Save the certificate and private key as `secrets/tls.crt` and `secrets/tls.key`. Keep all three files out of version control. The container runs as UID 10001; make the files readable by that account and not by other users:

```bash
sudo chown 10001:10001 secrets/*
sudo chmod 0400 secrets/*
```

## Local machine

Keep the Compose default bind address (`127.0.0.1`). Configure the daemon on the same machine to use the hostname covered by the certificate:

```bash
export GH0ST_RELAY_URL=wss://localhost:8765
docker compose -f compose.broker.yaml up --build --detach
```

If using a self-signed certificate, trust its issuing CA on the host before connecting. For a phone or another machine to connect, use a LAN address or DNS name, bind to an address reachable on that network, and install a certificate chain trusted by that device. Do not expose the broker to the public internet for local-only use.

## VPS

Point a DNS name (for example, `relay.example.com`) to the VPS and provision a certificate covering that name. Create a `.env` file beside `compose.broker.yaml`:

```dotenv
GH0ST_RELAY_BIND_ADDRESS=0.0.0.0
```

Open inbound TCP 8765 only in the VPS firewall and any cloud firewall/security group. Then start the broker:

```bash
docker compose -f compose.broker.yaml up --build --detach
```

Set each daemon's URL to `wss://relay.example.com:8765`. Keep SSH management access restricted independently; do not expose any gh0st daemon control API on the VPS.

## Daemon and pairing setup

On each local machine running a gh0st daemon, configure its matching device ID and token from `secrets/devices.json`:

```bash
export GH0ST_RELAY_URL=wss://relay.example.com:8765
export GH0ST_RELAY_TOKEN=<same-token-as-devices.json>
export GH0ST_DEVICE_ID=my-linux
gh0st-relay --device-id my-linux
```

For local hosting, set `GH0ST_RELAY_URL` to the local certificate hostname, commonly `wss://localhost:8765`. Create a one-time pairing URI from a machine with the local database/key:

```bash
gh0st pair --relay-url "$GH0ST_RELAY_URL" --device-id my-linux
```

The pairing URI contains the end-to-end secret; share it only with the intended phone and do not give it to the broker. The broker routes encrypted messages and stores no task prompts, pair secrets, or application database. Remote dispatch is bounded by `GH0ST_RELAY_MAX_BUDGET_USD` and `GH0ST_RELAY_MAX_TOKENS`.

## Operations

```bash
docker compose -f compose.broker.yaml logs --follow
docker compose -f compose.broker.yaml restart
docker compose -f compose.broker.yaml down
```

The container runs as an unprivileged user with a read-only root filesystem, dropped Linux capabilities, and no writable mount except temporary memory. The broker keeps live sockets and pair routes in memory; after restart, connected daemons republish active pair IDs and mobile clients reconnect. Use the host's monitoring, certificate renewal, backup, and firewall processes. Rotate a device token by updating the broker map and matching daemon secret, then restart the broker and reconnect that daemon.
