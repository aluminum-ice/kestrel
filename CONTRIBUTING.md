# Contributing to Kestrel

Thanks for helping. Patches are welcome two ways:

- **By email**, with no account needed anywhere.
- **On GitHub**, as an issue or a pull request on the
  [GitHub mirror](https://github.com/aluminum-ice/kestrel).

**All development happens on the project's Forgejo forge**
([git.3ntropy.io/aluminum-ice/kestrel](https://git.3ntropy.io/aluminum-ice/kestrel)),
including applying patches, however they arrive. The forge is closed to new
accounts, which is why patches come by email or through GitHub. The GitHub
repository is a push mirror of the forge: it updates itself from the forge, and
nothing is committed on GitHub directly.

Found a security problem? Don't send it as a patch or open a public issue for
it, on either site. Report it privately as described in [SECURITY.md](SECURITY.md).

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

   For a GitHub pull request, fork the
   [GitHub mirror](https://github.com/aluminum-ice/kestrel) and clone your fork
   instead; its `main` is the same as the forge's.

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

## Sending it

Either way, say what the change does and why, and how you tested it (on a
device, if you could).

- **By email:** make the patch files with `git format-patch origin/main` and
  email them to **patches@3ntropy.io**, either as attachments or with
  `git send-email --to=patches@3ntropy.io origin/main`.
- **On GitHub:** push your branch to your fork and open a pull request against
  `main` on the [GitHub mirror](https://github.com/aluminum-ice/kestrel). A pull
  request on GitHub is public there under your GitHub account.

## Reporting hardware test results

Some plugins drive hardware that hasn't been tested on a real device yet, such
as `eink-ws213` for the Waveshare 2.13" e-Paper HAT and `ups-wsc`/`ups-wsd`/`ups-wse`
for the Waveshare UPS HAT (C), (D) and (E); their guides say so. If you
try one, a report helps even without a patch. Email **patches@3ntropy.io**, or
open an issue on the [GitHub mirror](https://github.com/aluminum-ice/kestrel),
with:

- the plugin and the Kestrel version (`cat /etc/kestrel/version`);
- the hardware and its revision (for an e-Paper HAT, the sticker on the back of
  the panel) and the options you set (such as `variant`);
- what worked and what didn't: for an e-ink plugin, whether the face draws the
  right way up, how the quick redraws and the full refreshes look, and whether
  ghosting builds up over an hour; for a UPS plugin, whether charging and
  discharging match what the board is doing and the charge percent looks
  right.

The same rules as for patches apply: **nothing real**. A photo of the panel
shows the device's addresses, so blur them (and any network names) or turn off
`show_ip` first. The report `scripts/pull-eink-diagnostics.sh` saves (one text
file) is meant to be shared: it pulls no credential, no capture data and no
other config, keeps only the e-ink plugins' log lines, and masks addresses on the
device. Still, read it before you send it. Your name and email stay out of the repository;
results are recorded under the project's identity.

## What happens next

Every patch is applied on the forge, whether it came by email or as a GitHub
pull request. A maintainer reviews it, applies it on a branch and opens a pull
request on the forge, where the lint and test workflows run. Once it's merged,
it reaches GitHub through the mirror, and a GitHub pull request is closed with
a link to the forge's. GitHub pull requests are never merged on GitHub itself.
You'll get a reply either way.

**Your name and email stay out of the repository's history.** Accepted
patches are committed under the project's identity, because the repository
never records contributors' personal details; that holds for a GitHub pull
request too, though GitHub itself keeps showing your pull request under your
account. If you'd like to be credited, say so and suggest a handle; it will be
mentioned without your real name.
