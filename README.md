# mc-chunk-parser

Version-aware Minecraft chunk parser in Python. Existing Python libraries for chunk parsing are outdated or unmaintained,  this bridges that gap. Decodes binary chunk payloads into queryable block state data via NBT parsing, palette resolution, and heightmap extraction.

Written from scratch with no external dependencies beyond the Python standard library.

## Features

- Full NBT tree parsing to extract heightmap data
- Palette-based block state resolution across all three section modes
  - Single-value (entire section is one block type)
  - Indirect/palette mode (bits 4-14, local index to global state ID)
  - Direct mode (15+ bits, global state IDs stored directly)
- Post-1.16 long array packing where entries never straddle longs
- 24 vertical sections per chunk covering y=-64 to y=320
- Biome data skipped with correct offset bookkeeping
- Block update patching so world state stays accurate without full re-parses
- Version-aware blocks.json loading with per-version in-memory cache
- `get_block(x, y, z)` and `get_surface_y(x, z)` as the public interface

## How to build

No dependencies beyond Python 3.9+.

Download `blocks.json` for your target version from [PrismarineJS/minecraft-data](https://github.com/PrismarineJS/minecraft-data) at `data/pc/<version>/blocks.json`. Place it in a `blocks/` folder named `blocks_<version>.json`, for example `blocks_1.20.1.json`.

## Usage

```python
from chunk import Chunk

# payload is the raw bytes from a Minecraft chunk data packet (0x26)
# starting after the chunk X and chunk Z fields
chunk = Chunk(payload, version="1.20.1")

# get the block type at absolute world coordinates
block = chunk.get_block(x, y, z)

# get the Y of the highest non-air block in a column
surface_y = chunk.get_surface_y(x, z)
```

## How it works

A Minecraft chunk payload starts with a compressed NBT blob containing heightmap data, followed by 24 stacked section structs covering the full vertical range of the world.

The parser reads the NBT blob recursively, building a Python dict that maps heightmap type names to tuples of 64-bit signed integers. Each tuple has 256 values, one per column in the 16x16 chunk footprint, indexed by `x + z * 16`.

Each section contains a bits-per-entry byte, an optional palette, and a packed long array. The bits-per-entry value determines how many indices fit per 64-bit long and how to extract each one with a bitmask. The palette maps local section indices to global block state IDs, which are then resolved to block names via a preloaded blocks.json lookup.

Block updates can be applied after parsing via the patched dict on each section, so world state stays accurate as the server sends incremental updates without requiring a full re-parse.

## Resolution chain

```
bit index in long array
        |
palette index (0, 1, 2...)
        |
global block state ID (0 - ~20000)
        |
block name ("stone", "dirt", "oak_log"...)
```

## Tested on

- Minecraft Java Edition 1.20.1 (protocol 762)
