# Ghost guide publisher

Rebuilds the public user-guide page on Ghost from the latest **stable** release's
`GUIDE_MD`, with screenshots taken from that release's own code, and publishes it
through the Ghost Admin API. Betas are never published. How it works, and why, is
in [docs/developer/user-guide-publishing.md](../../docs/developer/user-guide-publishing.md);
this file is the runbook.

Run everything from the repository root, on a desktop (Linux shown). Nothing here
touches a device.

## 1. One-time setup

### Python environment and browser

```bash
python3 -m venv ~/.venvs/kestrel-guide
~/.venvs/kestrel-guide/bin/pip install -r tools/ghost-guide/requirements.txt
~/.venvs/kestrel-guide/bin/playwright install webkit
sudo ~/.venvs/kestrel-guide/bin/playwright install-deps webkit   # system libraries, needs sudo
```

`~/.venvs/kestrel-guide` is just a suggested location; use any path, and use the
same one below. The last line installs the libraries WebKit needs (on Ubuntu,
the equivalent is `sudo apt-get install libgtk-4-1 libmanette-0.2-0 libenchant-2-2`).

### The Ghost Admin API key

1. In Ghost Admin, open **Settings → Integrations → Add custom integration** and
   name it, for example, *Kestrel guide publisher*. A dedicated integration can be
   revoked on its own.
2. Copy its **Admin API key**, not the Content API key. It looks like
   `<24 hex characters>:<64 hex characters>`. It has full admin rights: keep it
   out of the repo, chats and shell history.
3. Create the env file, readable only by you, then put the key in it with an
   editor:

   ```bash
   mkdir -p ~/.config
   (umask 077; cat > ~/.config/kestrel-ghost.env <<'EOF'
   GHOST_URL=https://blog.example.com
   GHOST_ADMIN_API_KEY=REPLACE_WITH_ADMIN_API_KEY
   EOF
   )
   nano ~/.config/kestrel-ghost.env    # replace the placeholder with the key, save
   ```

   `GHOST_URL` is the site address in front of `/ghost/` (where Ghost Admin is).

## 2. In every new terminal

Shell variables and functions don't carry over to a new terminal, so set these
each time:

```bash
cd path/to/kestrel                                     # the repository root
PY=~/.venvs/kestrel-guide/bin/python                   # the environment's Python
ghost_push() { (set -a; . ~/.config/kestrel-ghost.env; set +a; "$PY" tools/ghost-guide/ghost_guide.py push "$@"); }
```

Always run the tool as `$PY tools/ghost-guide/ghost_guide.py ...`, and publish
with `ghost_push`. Running the script directly uses the system Python, which
lacks the tool's packages.

Only `push` needs the key, so don't load it into your shell. `ghost_push` loads
it into a subshell that ends with the command: `status`, `diff`, `build` and
anything else you run in the terminal never see it. If a terminal still has the
key from an older version of this runbook (`set -a; . ~/.config/kestrel-ghost.env`),
close it.

To check the key file, without printing the key:

```bash
(set -a; . ~/.config/kestrel-ghost.env; set +a; $PY -c 'import os;k=os.environ.get("GHOST_ADMIN_API_KEY","");i,_,s=k.partition(":");print("set:",bool(k),"| placeholder:",k.startswith("REPLACE"),"| colon:",":" in k,"| id length:",len(i),"| secret length:",len(s),"| secret is hex:",bool(s) and all(c in "0123456789abcdefABCDEF" for c in s))')
```

A good key prints `set: True | placeholder: False | colon: True | id length: 24 | secret length: 64 | secret is hex: True`.

## 3. Commands

| Command | What it does | Needs the key |
| --- | --- | --- |
| `$PY tools/ghost-guide/ghost_guide.py status` | Which release the live page matches, and the latest stable release. | No |
| `$PY tools/ghost-guide/ghost_guide.py diff` | The text and figure changes publishing would make. Add `--shots` to also take the screenshots. | No |
| `$PY tools/ghost-guide/ghost_guide.py build` | Renders the page and screenshots into `tools/ghost-guide/out/<tag>/` (`preview.html`, `shots/`). | No |
| `ghost_push --preview` | Publishes to a draft page, `kestrel-user-guide-preview`, to inspect in Ghost. | Yes |
| `ghost_push` | Publishes the live page. | Yes |

Options: `--tag X.Y.Z` targets a specific stable release instead of the latest;
`--no-fetch` skips fetching release tags first; `push --force` publishes over
edits made in Ghost (and is needed once, for the first publish);
`--browser chromium` (after `playwright install chromium`) is for checking the
pipeline where WebKit can't run, but it isn't Safari's engine, so publish with
the default WebKit. `out/` is
git-ignored; `preview.html` is a rough local preview, not the Ghost theme.

## 4. The first publish

The live page predates this tool, so `push` refuses until it is adopted once.

```bash
$PY tools/ghost-guide/ghost_guide.py status
$PY tools/ghost-guide/ghost_guide.py diff --shots     # read the changes; open out/<tag>/preview.html and out/<tag>/shots/
ghost_push --preview                                  # then open the draft in Ghost Admin (link printed)
```

In the draft, check the release-notice callout under the subtitle, the tables,
a few contents links, and the screenshots. Then:

```bash
ghost_push --force
```

From then on, plain `ghost_push` is enough. You can delete the
`kestrel-user-guide-preview` draft in Ghost, or keep it for the next preview.

## 5. After each stable release

```bash
$PY tools/ghost-guide/ghost_guide.py status    # confirms the page is behind the new release
$PY tools/ghost-guide/ghost_guide.py diff      # should show exactly the release's GUIDE_MD changes
ghost_push
```

Never edit the page in Ghost: the next publish would overwrite it, so `push`
refuses until the edit is moved into the repo (`GUIDE_MD`, or `guide.toml` for
blog-only text and screenshot placement).

## Troubleshooting

| Error | Cause and fix |
| --- | --- |
| `bash: tools/ghost-guide/ghost_guide.py: Permission denied` | `$PY` is empty in this terminal. Set it again (section 2). Don't make the script executable; it needs the environment's Python. |
| `bash: ghost_push: command not found` | The function isn't defined in this terminal. Define it again (section 2). |
| `bash: .../kestrel-ghost.env: No such file or directory` | The env file hasn't been created yet (section 1). |
| `GHOST_ADMIN_API_KEY must look like <id>:<hex secret>` | The env file still has the placeholder, or the Content API key was copied instead of the Admin one, or the copy picked up extra characters. Run the key check in section 2. |
| `set GHOST_URL and GHOST_ADMIN_API_KEY in the environment` | `push` was run as `$PY ... push` rather than `ghost_push`, or the env file is missing one of the two lines. |
| `HTTP 401` / `HTTP 403` from Ghost | The key is wrong or its integration was deleted, or `GHOST_URL` doesn't point at the site. |
| `Host system is missing dependencies to run browsers` | Run the `install-deps` line in section 1 (needs sudo). |
| `the page has no publish marker yet` | Expected before the first publish (section 4). |
| `the page was edited in Ghost since X was published` | Someone changed the page in Ghost. Move the change into the repo, then `push --force`. |
| `tag X has no published release` / `is a draft or pre-release` | The page follows published stable releases only. Wait for the release, or pass `--tag` with an earlier one. |
| `demo plugins did not load` / `bundled plugins failed under the demo` | The environment is missing a package the release needs. Re-run the `pip install -r` line; if a new release added a dependency, add it to `requirements.txt`. |
| `plugin X has no 'Y' readout in this release` | A release changed a plugin's dashboard card. Update the demo values in `fixture.py`. |
| `figure X: no heading with id 'Y'` | A release renamed or removed the section a screenshot follows. Update its `after` in `guide.toml`. |
| `warning: ... these contents links are broken` | Ghost gave a heading a different id than expected. Report it so the slug rule in `render.py` can be fixed; the page itself is published. |

## Tests

```bash
$PY -m pytest tools/ghost-guide
```
