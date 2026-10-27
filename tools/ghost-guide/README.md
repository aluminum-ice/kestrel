# Ghost guide publisher

Rebuilds the public user-guide page on Ghost from the latest stable release's
`GUIDE_MD`, with screenshots taken from that release's own code, and publishes
it through the Ghost Admin API.

```bash
python3 ghost_guide.py status   # what the live page matches vs the latest stable
python3 ghost_guide.py diff     # what publishing would change
python3 ghost_guide.py push     # publish (needs GHOST_URL, GHOST_ADMIN_API_KEY)
```

Setup, the secrets handling, the first publish and how screenshots work are in
[docs/developer/user-guide-publishing.md](../../docs/developer/user-guide-publishing.md).
