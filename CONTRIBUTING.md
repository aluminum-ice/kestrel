# Contributing to Kestrel

Thanks for helping. Kestrel's forge is closed to new accounts, so contributions
arrive as **patches by email**: no account needed anywhere.

Found a security problem? Don't send it as a patch. Report it privately as
described in [SECURITY.md](SECURITY.md).

## Before you start

Kestrel is for studying Wi-Fi security on networks you own or have permission
to test. Changes that help target other people's networks, hide the device,
evade detection, or get around the authorized-use acknowledgment won't be
accepted.

Plugins are standalone and the core stays lean: a feature lives in its plugin
(its own file and its own `_<plugin>_*.py` helper files), even if that means
copying code from another plugin. A patch that moves plugin code into the
`kestrel_pi` package won't be accepted; see
[Keep plugins standalone](docs/developer/plugin-development.md#keep-plugins-standalone).

The repository is public, so **nothing real goes in a patch**: no passwords,
keys or tokens, no real network names, MAC addresses, locations or captures,
and no personal details. Use placeholders such as `My Network`,
`00:00:5E:00:53:01`, `192.0.2.10` and `user@example.com`.

## Preparing a patch

1. Clone the repository and branch from `main`:

   ```bash
   git clone https://git.3ntropy.io/aluminum-ice/kestrel.git
   cd kestrel
   git switch -c my-change
   ```

2. Make your change, keeping it focused on one thing. If it changes how
   someone uses the device, update the matching guide under
   `docs/user-guide/` too.
3. Check it:

   ```bash
   pip install -r requirements-lint.txt
   make lint
   make test
   ```

   `make lint` includes a check that catches real-looking data; fix anything
   it reports rather than working around it.
4. Commit with a short message in the imperative ("Fix the map legend on
   phones"). Several small commits are fine.
5. Make the patch files:

   ```bash
   git format-patch origin/main
   ```

## Sending it

Email the `.patch` files to **patches@3ntropy.io**, either as attachments or
with `git send-email --to=patches@3ntropy.io origin/main`. In the email, say
what the change does and why, and how you tested it (on a device, if you
could).

## What happens next

A maintainer reviews the patch, applies it on a branch and opens a pull request
on the forge, where the lint and test workflows run. You'll get a reply either
way.

**Your name and email stay out of the public history.** Accepted patches are
committed under the project's identity, because the repository never records
contributors' personal details. If you'd like to be credited, say so and
suggest a handle; it will be mentioned without your real name.
