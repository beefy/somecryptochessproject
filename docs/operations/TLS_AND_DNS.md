# TLS and DNS

| | |
| --- | --- |
| Severity | SEV3 — the chain is fine, nobody can reach it in a browser |
| Blast radius | one name per instance: `app`/apex on the primary, `mirror` on the edge |
| First move | `openssl s_client` — does the certificate exist, and does its expiry tell the story |

Two Caddy instances hold the certificates and neither shares a volume with the
other: `sccp-explorer` on the primary terminates `proofofchess.com` and
`app.proofofchess.com`, and `sccp-edge` on the mirror terminates
`mirror.proofofchess.com`. They fail independently, which is why the first
question is *which name* is broken — a browser complaint about the mirror is not
an emergency, and the same complaint about the app is.

## How to tell

```bash
curl -sSI https://app.proofofchess.com/ | head -1          # TLS + proxy + origin
curl -sS -o /dev/null -w '%{http_code}\n' https://app.proofofchess.com/v1/status
openssl s_client -connect app.proofofchess.com:443 -servername app.proofofchess.com \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

| Result | Means |
| --- | --- |
| `curl: (60)` certificate verify failed | an expired, self-signed or wrong-name certificate is being served |
| `curl: (35)` / `tlsv1 alert internal error` | the TLS listener answered but has no certificate yet — ACME in progress, or failing |
| `curl: (7)` on `:443` but `:8645` answers | nothing is bound to 443: Caddy is down, not the chain |
| `HTTP 200` from `/v1/status` over HTTPS | TLS is fine; any remaining problem is the page or the proxy |

Then read the renewal decision Caddy actually made. It logs every issuance attempt
with the reason, and the reason is more specific than the browser's:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-explorer && docker compose logs --tail=100 tls | grep -iE "acme|certificate|error|challenge"'
ssh sccp@proofofchess.com 'curl -fsS http://127.0.0.1:2019/config/ >/dev/null && echo "caddy admin ok"'
```

## The ladder

**1. The name does not resolve, or resolves elsewhere.**

```bash
dig +short app.proofofchess.com
dig +short proofofchess.com
dig +short mirror.proofofchess.com
gcloud compute addresses list --project=chessproofofwork --format='table(name,address,status)'
```

An `A` record pointing at an old IP outlives a rebuilt VM and produces a browser
error that has nothing to do with certificates. The reserved addresses are
`STATIC_IP` for the primary and `sccp-edge-ip` for the mirror; if the record and
the reserved address disagree, fix the record in Cloud DNS and wait for the TTL
(60 s on the mirror's record, so this is cheap).

**2. DNS is right and port 80 is not reachable.** HTTP-01 is the only challenge in
use — there is no DNS provider token in the Caddyfile — so port 80 must be open
to the world at the moment of issuance *and* at every renewal:

```bash
gcloud compute firewall-rules list --project=chessproofofwork \
  --filter='name~web' --format='table(name,sourceRanges,allowed[].map().firewall_rule().list(),targetTags.list())'
nc -zv app.proofofchess.com 80
```

A missing `tcp:80` in `sccp-testnet-explorer-web` (or `sccp-edge-web` on the
mirror) fails silently for 60 days and then presents as an expired certificate.
It is the most common way to break TLS without touching TLS. If the rule exists
and the port is closed, something else owns `:80` on the host:

```bash
ssh sccp@proofofchess.com 'sudo ss -ltnp | grep -E ":80|:443"'
```

**3. The certificate is being issued for the first time.** A brand-new host
answers TLS with an internal error for the few seconds between the listener
opening and the ACME order completing, and that is not a fault:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-explorer && docker compose logs -f tls | grep -i "certificate obtained"'
```

The deploy workflow's smoke test can fail exactly here on a first deploy — curl
does not retry a TLS error, only a refused connection. The fix is to wait and
re-run the job, never to rebuild the VM or the volume.

**4. The certificate is expired or missing and issuance keeps failing.** Read the
error text, it names the CA's objection:

| ACME error | Means |
| --- | --- |
| `unauthorized` / `Invalid response` | the CA fetched `http://<name>/.well-known/acme-challenge/...` and got something else: wrong IP, a proxy in front, or a redirect |
| `too many certificates already issued` | the rate limit from a retry loop — stop restarting, wait, issue once |
| `no valid A records found` | DNS again, or a record created seconds ago that has not propagated |
| `connection refused` on port 80 | the firewall or the listener, as above |

The rate limit is the reason the loop is forbidden: five duplicate certificates
per name per week, and nothing an operator does resets it. If you have already
burned it, the honest options are to serve the last good certificate from the
volume (it is still there) and wait, or to move the name to a staging CA
temporarily — not to keep hammering it.

**5. The certificate volume is gone.** `sccp-explorer_caddy_data` holds the
account key, the certificate and its private key; `sccp-edge_caddy_data` does the
same on the mirror. A `down -v` on either Compose project deletes it, and so does
recreating the host:

```bash
ssh sccp@proofofchess.com 'docker volume ls --filter name=caddy'
```

If the volume is missing, Caddy issues again — cheap if it has been weeks since
the last issue, expensive if it has not. Check the log line before you conclude
that the situation requires action; if it says `certificate obtained
successfully`, the outage is already over and the page is stale.

## The mirror is not the primary

`deploy/edge/Caddyfile` allows `GET` only and answers every other method with
`405`, so a `405` from the mirror is the design working, not a fault:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mirror.proofofchess.com/v1/tx -d '{}'
```

That distinction matters during an incident: if the mirror's `405` arrives over a
valid certificate, the mirror's TLS and proxy are healthy and any remaining problem
is its *data*. If `curl` fails with `(35)` on the mirror, it is this page's
problem on the smaller host — and because the mirror is disposable, the cheapest
fix there is usually to stop it, rebuild it, and let it resync rather than debug
one `e2-micro`'s ACME history. See [`MIRROR_OUTAGE.md`](MIRROR_OUTAGE.md).

## Verifying a fix

```bash
for host in proofofchess.com app.proofofchess.com mirror.proofofchess.com; do
  printf '%-28s ' "$host"
  openssl s_client -connect "$host:443" -servername "$host" </dev/null 2>/dev/null \
    | openssl x509 -noout -enddate
done
curl -fsSI https://app.proofofchess.com/ | head -1
curl -fsSI https://proofofchess.com/ | head -1
curl -fsS https://mirror.proofofchess.com/v1/status | jq -r .height
```

A certificate more than 30 days out means renewal is now somebody else's problem
for a month. A fix that shows two hours of validity is a fix that has not happened
— it is the old certificate still being served, or a staging one.

Renewal itself is worth watching once rather than trusting: `docker compose logs
tls` keeps the `certificate obtained successfully` lines, and the timestamps should
be roughly 60 days apart. A name that has not logged one in 60 days is a name whose
renewal is broken and whose failure is still 30 days away.

## What makes it worse

- `docker compose down -v` on `sccp-explorer` to "reset Caddy": it deletes the
  certificate volume and turns a five-minute wait into a rate-limit problem;
- restarting Caddy repeatedly while the CA is unhappy — see the limit above;
- closing port 80 after "hardening the firewall": renewal stops and nothing says so
  until the certificate expires;
- pointing the `A` record at the VM without keeping the reserved address, so a
  restart changes the IP and the certificate's name no longer matches the host;
- putting a CDN or a load balancer in front of these names. HTTP-01 needs the
  challenge to reach Caddy on the VM; a proxy that terminates TLS upstream breaks
  issuance, and the Caddyfile has no DNS provider configuration to fall back on.

## Prevention

1. An alert on days-to-expiry per name, at 21 days. It is one `openssl` call and it
   catches every case on this page that does not page for a different reason.
2. Keep `tcp:80` open in `sccp-testnet-explorer-web` and `sccp-edge-web`. Write the
   reason in the rule's description, because the rule looks like an oversight.
3. Keep the two certificate volumes. They are the only state the web deployment
   has; everything else in `/srv` is replaceable by a deploy.
4. Do not share a Caddy instance between the primary and the mirror, however
   convenient it looks: the second instance exists so that a certificate or proxy
   fault cannot take both names down at once.

