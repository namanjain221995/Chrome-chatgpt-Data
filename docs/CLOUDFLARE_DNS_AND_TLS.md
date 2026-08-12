# Cloudflare DNS and direct origin TLS

Production traffic follows one path: managed extension → Cloudflare proxy →
the FastAPI container's TLS listener on EC2 port 443. There is no host web
server and no second application port exposed.

Cloudflare DNS by itself only publishes an address. This design requires the
record's **Proxy status** to be Proxied (orange cloud), because Full (strict),
edge redirects, and `CF-Connecting-IP` are proxy features rather than DNS
features.

Official references:

- <https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- <https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/>

## 1. Create the proxied DNS record

Cloudflare Dashboard → zone → DNS → Records → Add record:

| Field | Value |
| --- | --- |
| Type | `A` |
| Name | `archive` (or the approved hostname) |
| IPv4 address | the EC2 Elastic IP |
| Proxy status | **Proxied** |
| TTL | Auto |

Do not create an `AAAA` record unless the instance really has reviewed IPv6
connectivity and equivalent security-group restrictions. `dig` should return
Cloudflare edge addresses, not the Elastic IP.

## 2. Create an Origin CA certificate

Cloudflare Dashboard → SSL/TLS → Origin Server → Create Certificate:

1. Let Cloudflare generate an RSA private key and CSR.
2. Add only the exact archive hostname, such as `archive.example.com`.
3. Choose a validity period covered by the certificate-rotation calendar.
4. Copy the PEM certificate and private key once into separate root-readable
   temporary files on the EC2 host through Session Manager.

An Origin CA certificate is meant for Cloudflare-to-origin traffic. Browsers do
not generally trust it, so direct origin access must stay blocked.

## 3. Install the certificate for FastAPI

From an SSM shell:

```bash
sudo ./scripts/install_origin_tls.sh \
  --cert-file /root/origin-input.pem \
  --key-file /root/origin-input.key
sudo stat -c '%a %U:%G %n' /srv/techsara-chat-archive/tls/*
sudo shred -u /root/origin-input.pem /root/origin-input.key
```

The installer parses both files, checks at least seven days of validity,
verifies that their public keys match, and installs them as root-owned,
group-readable `0440` files for uid/gid 10001. Only the API container mounts
them, read-only.

## 4. Configure Full (strict)

Cloudflare Dashboard → SSL/TLS → Overview → set encryption mode to **Full
(strict)**. Under Edge Certificates, enable **Always Use HTTPS** so Cloudflare
redirects HTTP at the edge; the origin does not listen on port 80.

Keep TLS certificate validation enabled. A 526 response normally means the
certificate hostname, validity, chain, or origin clock is wrong. Fix that cause
instead of lowering the encryption mode.

## 5. Restrict origin ingress

The EC2 security group must have no inbound rule except TCP 443 from
Cloudflare's currently published IPv4 ranges. Add IPv6 ranges only if an AAAA
record and reviewed IPv6 origin path exist. In particular, do not expose ports
22, 80, 5050, 5432, 8000, or 8443.

Because the source ranges can change, compare the rules against
<https://www.cloudflare.com/ips/> during the monthly platform review. Apply
changes through a reviewed Console or CLI change and verify connectivity before
removing an old range.

This restriction is also the trust boundary for `CF-Connecting-IP`: Cloudflare
overwrites that header, and no arbitrary internet client can connect directly
to the origin and spoof it.

## 6. Validate the path

On the instance, validate the container without trusting the Origin CA locally:

```bash
curl -fkSs -H 'Host: archive.example.com' \
  https://127.0.0.1:443/health/ready | jq
```

From an external workstation, validate through Cloudflare normally:

```bash
curl -fsS https://archive.example.com/health/ready | jq
curl -sS -D- -o /dev/null https://archive.example.com/health/live
```

Expected: HTTP 200, HSTS and the application security headers, with a publicly
trusted Cloudflare edge certificate. A direct request to the Elastic IP should
time out because the source is not in the security group.

## 7. Rotate safely

Create a replacement Origin CA pair, install it with the validation script, and
restart only the API service:

```bash
sudo ./scripts/install_origin_tls.sh \
  --cert-file /root/new-origin.pem --key-file /root/new-origin.key
sudo docker compose -f compose.prod.yaml up -d api
sudo ./scripts/verify_deployment.sh
```

After both local and Cloudflare health checks pass, securely remove the input
files and revoke the retired certificate in Cloudflare. If validation fails,
restore the preceding installed pair and restart the API; do not switch away
from Full (strict).
