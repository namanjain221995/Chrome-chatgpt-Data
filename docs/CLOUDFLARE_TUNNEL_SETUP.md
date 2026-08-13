# Cloudflare Tunnel setup

The tunnel is the **only** public ingress. The EC2 instance needs no inbound
application port, no origin certificate and no public IP for application
traffic: `cloudflared` opens outbound connections to the Cloudflare edge and
Cloudflare routes the public hostname back down them.

```text
employee browser
      | HTTPS (Cloudflare edge certificate)
      v
Cloudflare
      | tunnel (outbound-only, QUIC/HTTP2)
      v
cloudflared container on EC2
      | Docker `egress` network
      v
http://api:8000        <- FastAPI, plain HTTP, no host port
```

These steps are manual and are done once, in the Cloudflare dashboard. Nothing
here is scripted, because none of it can be inferred: the account, the zone and
the hostname are yours.

## 1. Create the tunnel

1. Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels**.
2. **Create a tunnel** → connector type **Cloudflared** → **Next**.
3. Tunnel name: `techsara-chatgpt-production`.
4. **Save tunnel**.

Cloudflare now shows an install command containing the tunnel token. You need
the token, not the command: it is the long string after `--token` (it begins
with `ey`). Copy it.

Do not run the command Cloudflare shows. It installs `cloudflared` as a host
service; this deployment runs it as a Compose service instead, so the token
lives in a root-owned file rendered from SSM rather than in a systemd unit.

## 2. Store the token in AWS SSM

From an administrator workstation with AWS access:

```bash
aws ssm put-parameter \
  --name /techsara-chat-archive/cloudflare_tunnel_token \
  --type SecureString \
  --value '<paste-the-token>' \
  --overwrite \
  --region us-east-1
```

or interactively, which keeps it out of shell history:

```bash
./scripts/put_secrets.sh --config-only=false
```

The token is a credential for your Cloudflare account. Never commit it, never
paste it into a workflow file, and never pass it to `cloudflared` on the
command line — `docker inspect` and `ps` would both show it. The deployment
writes it to `/srv/techsara-chat-archive/secrets/cloudflared.env` as
`TUNNEL_TOKEN=...`, mode `0400`, owned by root, and Compose loads it with
`env_file`.

## 3. Add the public hostname

Still in the tunnel's configuration:

1. Open the tunnel → **Public Hostname** → **Add a public hostname**.
2. **Subdomain:** `archive`
   **Domain:** your company domain
   → the public hostname becomes `archive.<company-domain>`.
3. **Service type:** `HTTP`
   **URL:** `api:8000`

   Cloudflare stores this as `http://api:8000`. `api` is the Compose service
   name, resolved by Docker's embedded DNS on the shared `egress` network. It
   is not a hostname that exists anywhere else, and that is intentional: the
   tunnel is the only thing that can reach it.
4. **Save hostname**.

Cloudflare creates the proxied DNS record for you. Confirm under **DNS** that
`archive` is a `CNAME` to `<tunnel-id>.cfargotunnel.com` with the proxy
(orange cloud) **enabled**. A grey-clouded record would bypass the tunnel and
resolve to nothing.

### Additional hostname settings

* **TLS → Origin Server → No TLS Verify:** leave **off**. The hop to the origin
  is `http://api:8000` inside a private Docker network on the same host; there
  is no TLS to verify. TLS from the browser to Cloudflare is unaffected.
* **HTTP Settings → HTTP Host Header:** leave empty so the original
  `archive.<company-domain>` header is forwarded. FastAPI's
  `TrustedHostMiddleware` only accepts `ARCHIVE_HOSTNAME`, and that value is
  derived from `public_base_url`, so a rewritten Host header would produce
  `400 Invalid host header`.

## 4. Zone settings

Under the zone (not Zero Trust):

* **SSL/TLS → Overview → Encryption mode:** `Full (strict)`. With a tunnel
  there is no origin certificate to configure; this setting governs the
  browser-to-edge leg and any non-tunnel origins in the zone.
* **SSL/TLS → Edge Certificates → Always Use HTTPS:** on.
* **SSL/TLS → Edge Certificates → Minimum TLS Version:** 1.2.
* **Security → WAF:** the managed ruleset is enough. If you add a rate-limit
  rule, exempt `"/health/live"` and `"/health/ready"` so probes are not
  throttled.

## 5. Set the public base URL

```bash
aws ssm put-parameter \
  --name /techsara-chat-archive/public_base_url \
  --type String \
  --value 'https://archive.<company-domain>' \
  --overwrite --region us-east-1
```

`ARCHIVE_HOSTNAME` is derived from this by `fetch_ssm_secrets.sh`, so the two
cannot drift apart.

## 6. Deploy and verify

Deploy (see [EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md)), then on the instance:

```bash
# The tunnel's own readiness endpoint, bound to loopback only.
curl -fsS http://127.0.0.1:2000/ready
# {"status":200,"readyConnections":4,...}

sudo /opt/techsara-chat-archive/scripts/verify_production.sh
```

From anywhere:

```bash
curl -fsS https://archive.<company-domain>/health/ready
# {"status":"ok","checks":{"database":true,"object_storage":true,"config":true},...}
```

In the Cloudflare dashboard the tunnel should show **Healthy** with four
connections, typically to two different Cloudflare data centres.

## What is deliberately not used

* **Quick Tunnels / `trycloudflare.com`.** Ephemeral, unauthenticated, and the
  hostname changes on every restart. `scripts/verify_production_config.sh`
  fails the build if `trycloudflare` appears in the tunnel command.
* **`cloudflared` auto-update.** The container runs with `--no-autoupdate` and
  the image is pinned by tag *and* digest in `compose.prod.yaml`, so the
  connector version changes only when the repository changes.
* **A locally managed tunnel config file.** The tunnel is remotely managed:
  routes live in the Cloudflare dashboard, so adding a hostname does not
  require a deployment.

## Upgrading the connector

1. Find the new digest:
   ```bash
   docker buildx imagetools inspect cloudflare/cloudflared:<version>
   ```
2. Update the `image:` line in `compose.prod.yaml` with both the tag and the
   index digest.
3. Open a pull request. CI validates the topology, and the deployment pulls the
   new digest.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Deployment aborts with "Cloudflare tunnel token missing" | `cloudflare_tunnel_token` is absent from SSM | Create the parameter, redeploy. |
| `cloudflared` restarts in a loop | Invalid or revoked token | Rotate the token in the dashboard, update SSM, redeploy. |
| Tunnel healthy, public URL returns 502 | Hostname service is not `http://api:8000`, or the API is unhealthy | Check the hostname route; run `verify_production.sh`. |
| Public URL returns 400 `Invalid host header` | Host header is being rewritten, or `public_base_url` does not match the hostname | Clear the HTTP Host Header override; confirm the SSM value. |
| Public URL times out, tunnel healthy | DNS record is grey-clouded or missing | Re-add the public hostname so Cloudflare recreates the proxied CNAME. |
| Every path returns an empty-bodied 5xx, including a path the API would 404 | The request never reaches the origin. Most often the tunnel was deleted and recreated: the name is reused but the **tunnel id is not**, and the DNS record still targets the old `<uuid>.cfargotunnel.com`. | Delete the `archive` DNS record, then add the public hostname again **from the current tunnel** so Cloudflare writes a record pointing at the new id. Confirm with the tunnel id in `docker compose logs cloudflared`. |
| Public URL redirects to a Cloudflare login page | A Zero Trust Access policy covers the hostname | Remove the Access application, or exempt `/health/*`. The extension authenticates to the API itself; Access in front of it would break it. |

### Confirming the record and the tunnel agree

A proxied record hides its target from outside DNS, so compare from both ends:

```bash
# On the instance: which tunnel is this connector actually serving?
cd /opt/techsara-chat-archive
sudo docker compose --env-file .env.production -f compose.prod.yaml \
  logs --tail 40 cloudflared | grep -iE 'tunnel|connector|registered'
```

In the dashboard, open **DNS → the `archive` record → Edit** and check the
tunnel it names. If the two disagree, the record is stale: delete it and re-add
the public hostname from the tunnel that is actually running.
