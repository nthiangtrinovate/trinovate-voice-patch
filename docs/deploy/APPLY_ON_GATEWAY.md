# Applying the voice-word-editor vhost on your reverse-proxy host

TEMPLATE / NOT APPLIED. A standard nginx vhost + certbot flow. Assume the
automation/deploy account does not have passwordless sudo on the
reverse-proxy host — expect a human with root to run these steps, not an
agent.

## Prerequisites (none of these are done yet)

1. Decide and register the DNS name (`<<<DOMAIN_PLACEHOLDER>>>`), pointing
   at this host's public IP (or an internal/split-DNS entry if staying
   LAN/VPN-only for now — a reasonable default until you're ready for
   public exposure).
2. Obtain a TLS cert for that name, e.g.
   `sudo certbot certonly --nginx -d <<<DOMAIN_PLACEHOLDER>>>` (HTTP-01
   needs the name publicly resolvable; use DNS-01 or an internal CA
   otherwise).
3. Confirm the portal backend's actual host/port
   (`<<<UPSTREAM_HOST_PLACEHOLDER>>>:<<<UPSTREAM_PORT_PLACEHOLDER>>>` in the
   nginx template) once the portal has been deployed somewhere reachable
   from this host.
4. Confirm the default admin/admin credential has already been changed on
   that deployment (see docs/admin-credentials.md) before any public
   exposure step below is taken.

## Apply (run as a human with sudo on the reverse-proxy host)

```
sudo cp /etc/nginx/sites-enabled /root/nginx-sites-backup-$(date +%F) -r      # backup
sudo install -m 644 voice-word-editor.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/voice-word-editor.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Rollback

```
sudo rm /etc/nginx/sites-enabled/voice-word-editor.conf && sudo nginx -t && sudo systemctl reload nginx
```

## Verify

- From inside the LAN/Tailscale: `https://<<<DOMAIN_PLACEHOLDER>>>/` ->
  portal login page (not a 403/502).
- From outside (if public exposure was explicitly approved): same check
  from an external network; anywhere else should still get a sensible
  auth challenge, not raw data.
- Confirm no secrets appear in `journalctl -u nginx` or the app's own logs
  during a test login/upload (PLAN.md: no secrets in logs, ever).
