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
  in; **Show all** zooms back out.
- **A single network** links to its row on Captured networks, which has its
  OpenStreetMap link.
- **Filters** above the map: all, recorded only, estimated only, or the last 7
  days.

There are no map tiles, just a light grid of latitude and longitude lines, a
scale bar and a north arrow. That's deliberate: fetching map tiles would tell a
tile server which areas you've been capturing in. The map works offline and
sends nothing anywhere. On a phone the page shows a taller map sized for the
screen.

## Privacy

Locations are treated as sensitive, as on the Captured networks page:

- the map needs the web login on;
- while `[web] show_locations` is off (Advanced settings), the map is hidden too.

Only networks you own or have permission to test belong in your capture store.

The map uses a simple flat projection, accurate at city or region scale. A set of
locations spanning the 180° meridian isn't supported.
