# e-ink face plugin (`eink`) - deprecated

> **Deprecated:** `eink` is replaced by [`eink-ws27`](eink-ws27-setup.md) and
> will be removed in a future release that requires reflashing the SD card.

`eink` is deprecated. `eink-ws27` is the same face for the same HAT (the
[Waveshare 2.7" e-Paper HAT](https://www.waveshare.com/wiki/2.7inch_e-Paper_HAT_Manual))
with the same options, under a name that says which display it drives. Switch
now: enable `eink-ws27` (with the same settings) and disable `eink`. `eink`
keeps working until it is removed in a future release that requires reflashing
the SD card; that release's notes will say so.

While `eink` is enabled, the Plugins page and `sudo kestrel plugins info eink`
show this notice, and the daemon log says so once at start-up.

## Switching to `eink-ws27`

Follow [Switching from `eink`](eink-ws27-setup.md#switching-from-eink) in the
`eink-ws27` guide. In short:

```sh
sudo kestrel plugins info eink            # note the settings you changed
sudo kestrel plugins enable eink-ws27     # set the same ones
sudo kestrel plugins disable eink
```

Copy any `[plugins.eink.faces]` / `[plugins.eink.voices]` overrides in
`config.toml` to `[plugins.eink-ws27.faces]` / `[plugins.eink-ws27.voices]`.

## Until you switch

Everything in the [`eink-ws27` guide](eink-ws27-setup.md) applies to `eink` as
well: read `eink` for `eink-ws27` in plugin names, log lines and `config.toml`
table names. The face, the HAT's keys, the web mirror and every option behave
the same; only `eink-ws27` limits `variant` and `rotation` to a list.
