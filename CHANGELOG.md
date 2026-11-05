# Changelog

## Unreleased (public release prep)
- Public release: default save folder is auto-detected under `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames\<id>`, default pattern is `*.sav`, default vault folder is `%USERPROFILE%\Documents\satwatch\data`. The `.bat` launchers gained a `SATWATCH_ARGS` / `PYEXE` settings block.

## 1.3
- **Transport plan** (Volume items panel): click a Depot, Drone or Train cell, or focus it with Tab and press Enter or Space, to mark it "want". A second click clears it. Planned cells get a blue outline and a "planned" tag. When the newest save shows the item on that network, the tag reads "planned ✓ now exists"; the want stays until you clear it. You also get a "Planned" filter, "Planned depot/drone/train" count tiles, and an optional note per item (open the item's detail line, then "save note"). `volume_items.json` gains a `planned` map.
- **Factory designs**: the factory card (click a chip or a box on the map) has a Design section: name, purpose, notes and a design URL. Any http(s) link of up to 32 KB is stored exactly as given, for example a long satisfactory-calculator link. "Open design" opens the link in a new tab. Chips and map boxes show a link badge when a design is set and a muted style when it isn't.
- **Next factory**: a "Next factory" chip and a blue diamond map marker. Its position defaults to the pioneer; "Set position on map" and then a click on the map moves it.
- New panels ported from the tracker Page: Milestone history (first save each change shows up in), Factory census (by category, with bars), Inventory & collectibles (hard drives, somersloops, mercer spheres and power slugs carried, most ever carried, still in the world, drop pods, player inventory highlights), building-count bars on the factory card, and a fuller "What the parser couldn't read" list (this run's warnings plus fixed caveats). The header shows play time, last save, start date, buildings and saves parsed.
- **To-do (from the vault)**: shows `todo.md` in the folder above the vault data folder, read-only, as simple markdown. All HTML in the file is shown as text. The panel is hidden when the file doesn't exist.
- Security of the edit endpoints (`GET /api/plan`, `POST /api/plan/factory`, `POST /api/plan/transport`): the server listens on 127.0.0.1 only. Requests must use Host 127.0.0.1 or localhost with the dashboard port, and a cross-site Origin is refused. Each POST needs a random per-run token from the page. Bodies are limited to 64 KB, text fields have length limits, and design links must be http or https. `plan.json` is the only file the server can write.

## 1.2
- **Volume items** panel on the dashboard (same table as the tracker Page, now computed live from your newest save): summary tiles, filters (All / Not on any network or depot / No drone route / No train), header pinned above a 560 px scroll box, amber tier bands with counts, a "packaged" badge, and hover / focus (Tab) / tap detail lines showing what each item makes (★in save = a factory in the save runs that recipe).
- Columns: Item, Level, Made at (F1-F13 from `factories_boxes.json`), Depot amount, Drone network (paired routes, plus "port only" for unpaired ports), Train network (timetable stops and platforms).
- Every run writes `volume_items.json` and `volume_items.md` to the vault folder and adds a one-line "Volume items" section to `status.md`. `/api/volume` returns the same data.
- **Outpost at the pioneer** panel: player position and nearest factory box with distance. (Resource nodes and inventory are not included yet.)
- `satparse.py` 0.4 adds an additive `network` field to snapshots: machine recipes and positions, drone ports (name, pairing, inventories), train stations, timetables, freight platforms and the player position. It also decodes TextProperty values (station names). The first run after updating re-parses the newest save once (about 1 s).
- New **Satisfactory Refresh.bat**: refreshes the vault files without starting the dashboard (`--no-serve`); its window stays open only on an error.

