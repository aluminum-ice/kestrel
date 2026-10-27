# wpa-sec upload setup

The `wpa-sec` reference plugin uploads the handshakes you capture to
[wpa-sec.stanev.org](https://wpa-sec.stanev.org) — a distributed service that
tries to crack them — and downloads the passwords recovered under your key. It is
opt-in and off by default. This is the operator guide.

## Getting your wpa-sec key

1. Go to [wpa-sec.stanev.org](https://wpa-sec.stanev.org) and sign in (the site
   issues you a personal **key**).
2. Copy that **key** — a long hex string. It is both your upload credential and
   the key your cracked results are filed under. This is the plugin's `api_key`.

Keep the key private: anyone with it can submit under your account and read your
cracked results.

## What it does

- **Upload** — as you capture handshakes, the plugin queues the new ones and, when
  the device is online, uploads them. wpa-sec only accepts raw captures, so the
  plugin repackages the hashlines into a `.cap` first (with `hcxhash2cap` from
  `hcxtools`, which ships in the image). It queues rather than drops while
  offline, and retries on reconnect.
- **Download results** — with `download_results` on (the default), it fetches the
  passwords recovered under your key — **every** network cracked under that key,
  from any device, not just this one — at startup, on each reconnect, and about
  hourly. They are saved on-device at
  `/var/lib/kestrel/capture/cracked/wpa-sec.potfile`. Recovered passwords
  are sensitive and stay on the device; set `download_results = false` to upload
  only.

## Enabling the plugin

It transmits captured material off-device, so enabling it takes the authorized-use
acknowledgment (same as the other uploaders).

From the **web dashboard** (with login on): open **Plugins**, enable **wpa-sec**,
paste your key into `api_key` (masked, never echoed back), acknowledge the
authorized-use prompt, and save.

The same from the terminal:

```bash
sudo kestrel plugins enable wpa-sec     # asks for the key, input hidden
```

From a script, pass the key on stdin; `--set api_key=...` would put it in your
shell history and the system journal (sudo logs every command line):

```bash
read -rs KEY; printf '%s\n' "$KEY" | sudo kestrel plugins enable wpa-sec --set-stdin api_key
```

Optional `--set` keys: `endpoint` (default `https://wpa-sec.stanev.org`),
`download_results` (default `true`), `results_interval_seconds` (default `3600`),
`timeout` (default `30`).

## The dashboard card

The `wpa-sec` card shows:

- **Uploaded** — hashlines uploaded this daemon session (resets on restart).
- **Queued** — captured but not yet uploaded.
- **Cracked** — networks in your downloaded potfile.

## Viewing cracked networks

With the web login on (`[web] auth`), the `wpa-sec` row on the **Plugins** page
has a **Show** button. It opens a page listing the **SSID and recovered
passphrase** of every cracked network from your downloaded potfile, with:

- a **search box** that filters by SSID, and
- **paging** (100 per page), so the list stays fast even with thousands of results.

Because the page shows recovered passphrases, it is only offered — and only
rendered — when the web login is on and you are signed in. With `[web] auth` set
to `none` the button does not appear and the page shows a notice instead of the
list. The passphrases otherwise live only on the device, in
`/var/lib/kestrel/capture/cracked/wpa-sec.potfile`.

## Troubleshooting

- **Nothing uploads while offline** — expected; it queues and uploads on the next
  reconnect. Check the dashboard's Internet row.
- **"already submitted"** in the log is harmless — wpa-sec de-duplicates
  server-side, so re-uploading the same capture is a no-op.
- **Cracked stays 0** — wpa-sec cracks over time; the count updates as results
  arrive for your key. The potfile is
  `/var/lib/kestrel/capture/cracked/wpa-sec.potfile`.
- **Live state**: `sudo kestrel plugins info wpa-sec`, and the daemon log
  with `sudo journalctl -u kestrel-daemon | grep -i wpa-sec`.
- Only upload handshakes you are authorized to capture and submit.
