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
(its own file and its own `_<plugin>_*.py` helper files). Code that at least two
plugins really share can become a shared plugin library (`_lib_<name>.py`,
beside the plugins); a patch that adds one says which plugins use it. A patch
that moves plugin code into the `kestrel_pi` package won't be accepted; see
[Keep plugins standalone](docs/developer/plugin-development.md#keep-plugins-standalone)
and the [plugin library reference](docs/developer/plugin-libraries.md).

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

## Reporting hardware test results

Some plugins drive hardware that hasn't been tested on a real device yet, such
as `eink-ws213` for the Waveshare 2.13" e-Paper HAT; their guides say so. If you
try one, a report helps even without a patch. Email **patches@3ntropy.io** with:

- the plugin and the Kestrel version (`cat /etc/kestrel/version`);
- the hardware and its revision (for an e-Paper HAT, the sticker on the back of
  the panel) and the options you set (such as `variant`);
- what worked and what didn't: for an e-ink plugin, whether the face draws the
  right way up, how the quick redraws and the full refreshes look, and whether
  ghosting builds up over an hour.

The same rules as for patches apply: **nothing real**. A photo of the panel
shows the device's addresses, so blur them (and any network names) or turn off
`show_ip` first. The bundle `scripts/pull-eink-diagnostics.sh` makes is meant to
be shared (it pulls no credential, no capture data and no other config), but
read it before you send it. Your name and email stay out of the repository;
results are recorded under the project's identity.

## What happens next

A maintainer reviews the patch, applies it on a branch and opens a pull request
on the forge, where the lint and test workflows run. You'll get a reply either
way.

**Your name and email stay out of the public history.** Accepted patches are
committed under the project's identity, because the repository never records
contributors' personal details. If you'd like to be credited, say so and
suggest a handle; it will be mentioned without your real name.
