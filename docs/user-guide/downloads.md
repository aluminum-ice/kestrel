# Downloading your capture files

The `downloads` plugin lets you take your handshake files home straight from the
dashboard, with no SSH or `scp`. It ships **disabled**. It's local only (your own
files to your own signed-in browser), so turning it on needs no authorized-use
acknowledgment.

## Turning it on

On the dashboard's **Plugins** page, enable **downloads**. From a terminal:

```bash
sudo kestrel plugins enable downloads
```

## Downloading

Press **Download** on the plugin's row on the Plugins page. The page offers:

| Download | What you get |
| --- | --- |
| **All handshake files (.zip)** | Every `*.hc22000` file in the capture store, plus a short `MANIFEST.txt` (when it was made, how many files and hashlines). |
| **New since your last download (.zip)** | Only the files that changed since your last zip download, so you don't pull the whole store every time. |
| **Everything as one .hc22000** | Every hashline once, de-duplicated across files: the form hashcat takes directly. |
| **Handshake files with locations (.zip)** | The handshake files plus their GPS sidecars (where each capture was made). Shown only with `include_locations` on. |

Recovered passwords (`cracked/`) and the capture archive are never offered here.
Copy those over SSH if you need them.

## When a download is refused

- **"Downloads need the dashboard login on":** the web login is off
  (`[web] auth = none`). Turn it on with `sudo kestrel web auth system`.
- **"Downloads need the encrypted dashboard":** the page was opened over plain
  HTTP from another device. Open the dashboard over `https://` and try again.
  On the device itself (for example through an SSH tunnel to `127.0.0.1`) plain
  HTTP is fine.
- **"That download would be … over this plugin's limit":** the store is bigger
  than `max_mb`. Raise the limit in the plugin's settings, or copy the store over
  SSH.

Only networks you own or have permission to test belong in your capture store,
and what you download is as sensitive as what's on the device: keep it on your
own machines.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `include_locations` | `false` | Also offer the handshake files with their GPS sidecars. |
| `max_mb` | `200` | Refuse a download larger than this many megabytes (at least 1). |
