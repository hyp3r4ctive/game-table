# game-table
 custom built multi purpose RPG/game table

## Status

in design, CAD in progress, website core game engine pre-alpha

## Features

- 72 x 96 inch hardwood table, knock-down legs for transport
- Recessed central play area with swappable tops:
  - DND mode: rear-projected battle map on translucent acrylic
  - Card mode: felt surface
    - Potentially inset pool table/ball return layer
  - Flat mode: solid wood top
- 6 player stations with hinged 13.3" screens, dice tray, dice storage, cup holders
- DM station with folding screen, public/private dice trays, control tablet
- Camera-based dice roll detection per tray
- Removable projector cradle for projection mapping
- Speakers, ambient lighting
- Fully computerized and customizable games engine

## Repo structure

- `docs/` - planning, dimensions, decisions, build log
- `cad/` - SolidWorks parts and assemblies
- `code/` - Pi, Arduino, tablet, player screen, server software
  - `server/` - server
    - `data/` - json databases
    - `game/` - python game logic
    - `static/`
    - `templates/` - html pages
- `electronics/` - schematics and wiring
- `reference/` - datasheets and manuals

## CAD Model (8/18/26)

DND Table:
<img width="2046" height="1242" alt="DNDISO1" src="https://github.com/user-attachments/assets/310fa8fe-2804-4682-b2b2-c25623547c34" />
Pool Table:
<img width="2046" height="1242" alt="POOLISO" src="https://github.com/user-attachments/assets/283df29d-7233-4dd9-b029-45259862d069" />
Flush Tabletop:
<img width="2046" height="1242" alt="TABLEISO" src="https://github.com/user-attachments/assets/25798e5f-f24d-4a9e-84a2-ec5f9dedfe6b" />
