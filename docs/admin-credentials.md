# Admin credentials — initial access and how to change them

This covers the portal's **admin/browser login** only (not the separate
worker bearer-token scheme — see `docs/portal-notes.md`). It documents
mechanics already implemented in `portal/app/security.py` and
`portal/app/config.py`; nothing here changes, prints, or publishes any
currently-deployed instance's actual password.

## What ships by default

- A fresh, empty database seeds exactly one account: username `admin`,
  password `admin`, flagged `must_change_password=1`.
- Every page and every `/api/*` route is gated: not logged in -> redirect
  to `/login` (pages) or `401` (API). Logged in but still on the
  default/unchanged password -> redirect to `/change-password` (pages) or
  `403` (API) for every route except the change-password routes
  themselves. **This cannot be dismissed or skipped** short of actually
  changing the password.
- This is a **single shared account model** — there is no per-teammate
  account/registration system today. If multiple people share this one
  login: there's no per-person audit trail (every job's owner is
  `admin`), you can't revoke one person's access without changing it for
  everyone, and changing the password logs everyone out at once unless
  the new password is shared out of band. Acceptable for a small internal
  team; flagging it so it's a deliberate choice, not a surprise.

## Making the initial credential configurable (not hardcoded)

The seed password is **not** hardcoded to `admin` — it's already read from
an environment variable, with `admin` only as the fallback when that
variable is unset:

```python
# portal/app/config.py
DEFAULT_ADMIN_PASSWORD = os.environ.get("PORTAL_DEFAULT_ADMIN_PASSWORD", "admin")
```

To seed a different initial password instead of `admin`/`admin`, set
`PORTAL_DEFAULT_ADMIN_PASSWORD` in the environment **before the portal's
first boot against a fresh/empty database** (it's only read when the
`admin` row doesn't already exist — `security.ensure_default_admin()` is
idempotent and never overwrites an existing account). Example, Docker:

```bash
# in the deployment's --env-file (never in source control, never logged)
PORTAL_DEFAULT_ADMIN_PASSWORD=some-temporary-value-you-will-still-rotate
```

Note this still forces a change on first login (`must_change_password` is
always set to `1` at seed time regardless of what the seed password is) —
it only changes what the *first* login's password is, not whether a
change is required afterward.

## Step-by-step: first login and password change (what the owner/teammate does)

1. Go to the portal's login URL and sign in with `admin` / `admin` (or
   whatever `PORTAL_DEFAULT_ADMIN_PASSWORD` was set to for this
   deployment).
2. You'll be redirected to `/change-password` automatically and cannot
   use any other part of the portal until you complete it.
3. Enter the current password, then a new password (minimum 8
   characters, cannot be the same as the default/seed password), and
   confirm it.
4. Submit. You're redirected to the dashboard — the account's
   `must_change_password` flag is now cleared and the gate no longer
   applies.
5. Anyone else sharing this login must be told the new password out of
   band (Slack/in person/etc.) — there is no multi-account system to give
   them their own.

## Before this is exposed beyond local/private use

Already enforced in code, not just documented: set
`PORTAL_PUBLIC_DEPLOYMENT=1` in the server's environment and it **refuses
to start** if the admin account still has `must_change_password=1` (see
`portal/app/main.py:on_startup`). Also set a real
`PORTAL_WORKER_TOKEN` (not the auto-generated dev-file fallback) and serve
over HTTPS — see `docs/portal-notes.md`'s "Deployment gate" section for
the full checklist.

## What this change did NOT touch

This document describes the mechanism only. It does not read, print, or
modify any already-deployed instance's actual current admin password. If
you need to rotate a running deployment's password, do it the normal way
(log in, use `/change-password`).
