# Security policy

Kestrel is a research and education project, used only on networks you own or have permission to test (see [Authorized use only](README.md#authorized-use-only)). If you find a security problem in Kestrel itself, please report it privately so it can be fixed before it is public.

## Reporting a vulnerability

Email **security@3ntropy.io**. Please don't open a public issue or pull request for a vulnerability, or discuss it publicly, until a fix has been released.

Include:

- the Kestrel version (`cat /etc/kestrel/version` on the device, or the release tag);
- the component affected (for example the web dashboard, a root helper socket, a plugin, the update path, Bluetooth onboarding, the build);
- steps to reproduce, and what an attacker gains (who the attacker is, what access they start with, and what they end up with).

**Don't send real data.** Leave out real handshake or capture files, real network names, MAC addresses or locations, diagnostics bundles or logs from a real device, and a real device's `config.toml`. Use placeholders (`My Network`, `00:00:5E:00:53:01`) and describe the rest. If a reproduction needs a capture, make one on a network you own.

## What to expect

This is a small project, so responses are best effort:

- an acknowledgment within 7 days;
- an assessment, and a plan or a reason for not fixing, once the report is understood;
- a fix in a release, with credit in the release notes if you want it.

Please give a fix time to ship before you disclose: until the fixed release is out, or 90 days from your report, whichever comes first. If you need longer or shorter, say so and we'll agree on a date.

## Supported versions

Fixes go into the next release. The latest stable release and the newest beta are supported; older releases are not patched, so update (in place where the release allows it, otherwise by reflashing).

## Scope

In scope: everything in this repository, including:

- the image build and its defaults;
- the on-device code (`kestrel_pi`, the plugins, the `kestrel` CLI and the root helpers);
- the web dashboard;
- Bluetooth and Wi-Fi onboarding;
- the update path;
- the release workflow.

Ways for code running as the `pi` user to gain root are in scope: the root helpers are meant to be a privilege boundary.

Out of scope:

- bugs in upstream software that Kestrel ships unchanged (Raspberry Pi OS packages, AngryOxide, PyTorch, stable-baselines3, the Waveshare driver): report those upstream;
- attacks that need physical access to the device's SD card;
- attacks that need the operator's own login. The documented default login (`pi` / `raspberry`) is known; a way around changing it is in scope.

## Safe harbor

Good-faith security research on a Kestrel device you own, or are authorized to test, is welcome. If you stay within the [authorized-use rules](README.md#authorized-use-only), avoid other people's data and devices, don't disrupt anyone else's service, and report what you find privately as described above, this project will not pursue or support legal action against you for that research. This does not authorize testing any network, device or service you don't own or have permission to test.
