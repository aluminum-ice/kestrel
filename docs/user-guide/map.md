# The capture map

The `map` plugin draws every captured network whose location is known on one
map, right on the device. It's the all-at-once view of the per-row locations on
the **Captured networks** page. It ships **disabled**, and since it only displays
what's already on the device, turning it on needs no authorized-use
acknowledgment.

## Turning it on

On the dashboard's **Plugins** page, enable **map**. From a terminal:

```bash
sudo kestrel plugins enable map
```

Locations come from the `gps` plugin (a position **recorded** by the onboard GPS
when a handshake was captured) and from uploaders such as `wigle` (a position
**estimated** from an outside source). With neither, the map says there's
nothing to show yet.

## Reading the map

Press **Map** on the plugin's row on the Plugins page.

- **A pin** is a network whose location was recorded by your GPS.
- **A ring with `~`** is an estimated location.
- **A square with a number** is several networks close together. Tap it to zoom
  in; **Reset view** (next to the filters) zooms back out. Networks captured from
  the same spot share its location, so no zoom separates them: tapping their
  square lists them under the map instead.
- **A single network** links to its row on Captured networks, which has its
  OpenStreetMap link.
- **Filters** above the map: all, recorded only, estimated only, or the last 7
  days.

**← Plugins** and **Dashboard** at the top take you back.

The background is an **OpenStreetMap** map. Your browser (not the device) loads
the map images for the area in view from OpenStreetMap's tile server, so that
server sees which areas you look at, as it would on openstreetmap.org. To keep
that off third-party servers, point the `tile_url` option at a tile server you
run (an `https://` address with `{z}`, `{x}` and `{y}` in it), or leave it empty
for no background: the map then shows a light grid of latitude and longitude
lines instead, and works with the browser offline. Either way there's a scale
bar and a north arrow, and on a phone the page shows a taller map sized for the
screen.

## Privacy

Locations are treated as sensitive, as on the Captured networks page:

- the map needs the web login on;
- while `[web] show_locations` is off (Advanced settings), the map is hidden too.

Only networks you own or have permission to test belong in your capture store.

The map uses the Web Mercator projection, like the map images. A set of
locations spanning the 180° meridian isn't supported.
