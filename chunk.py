# imports
import json
import struct
from pathlib import Path

# global constants
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

"""
--------------------------------------------------------------------------------------------
Chunk Parser
--------------------------------------------------------------------------------------------
Parses Minecraft chunk data packets into queryable block state data. Single public
interface is get_block(x, y, z) which returns the block type at a given coordinate.

A few things to note. You need to download blocks.json for version you're using from the 
PrismarineJS/minecraft-data GitHub repo and put it in your project directory alongside 
chunk.py. The _state_to_block mapping is built once at class load time so it's fast at 
runtime. And _read_varint and _varint_size are separate from Connection._encode_varint 
because here you're reading from a buffer not a socket, same algorithm, different context.
--------------------------------------------------------------------------------------------
"""


class Chunk:
    _state_to_block_cache = {}

    def __init__(self, payload, version="1.20.1"):
        if version not in Chunk._state_to_block_cache:
            blocks_path = Path(__file__).parent / "blocks" / f"blocks_{version}.json"
            blocks_json = json.loads(blocks_path.read_text())
            state_map = {}

            for block in blocks_json:
                for state in block["states"]:
                    state_map[state["id"]] = block["name"]

            Chunk._state_to_block_cache[version] = state_map

        self._state_to_block = Chunk._state_to_block_cache[version]
        # sections indexed vertically (by y index)
        self._sections = {}
        self._parse(payload)
        self._hmap = {}

    """
    --------------------------------------------------------------------------------------------
    Function Header - Parse
    --------------------------------------------------------------------------------------------
    Reads each vertical section out of the chunk payload. Each section contains a bits-per-
    entry value, a palette mapping local ids to global block state ids, and a packed long
    array containing the actual block data.

    the server sends block count so the client renderer can make quick decisions like "this 
    section is all air, skip rendering it entirely" without having to unpack the long array.

    However, If bits_per_entry == 0 the entire section is one block type so you only need one 
    state ID stored directly, no palette, no long array needed. The moment you have more than 
    one distinct block type you need at least bits_per_entry == 4 (the minimum indirect), a 
    palette with at least 2 entries, and a long array to store which palette index each of the 
    4096 blocks maps to.
    --------------------------------------------------------------------------------------------
    """

    def _parse(self, payload):
        hmap, offset = self._read_nbt(payload, 0)
        # need hmap, example "find a tree" benefits from knowing the surface Y so
        # you search near the surface rather than scanning all 24 sections
        self._hmap = hmap
        # standard world is 384 blocks tall (-64 to 320) = 24 sections
        # section_y 0 corresponds to y=-64, section_y 23 corresponds to y=304
        section_y = 0
        # get data for each section y of the chunk for the payload
        while offset < len(payload):
            # peek ahead to see if we've reached block entities which
            # start with a varint count, not a section structure, heuristic:
            # remaining bytes too small for a section → break
            if offset + 3 >= len(payload):
                break

            # skip block count, not needed, using bits_per_entry
            offset += 2
            bits_per_entry = payload[offset]
            offset += 1
            # bits_per_entry = 0 means single valued, entire section is one block type
            # bits_per_entry 1-3 is clamped to 4 (minimum indirect)
            # bits_per_entry >= 15 is direct mode, no palette
            if bits_per_entry == 0:
                # single value palette -> one varint state id -> empty long array
                state_id = self._read_varint(payload, offset)
                # skip state id, and data_length (given bits per entry) in payload
                offset += self._varint_size(payload, offset)
                offset += self._varint_size(payload, offset)
                # store as single-value section
                self._sections[section_y] = {
                    "bits_per_entry": 0,
                    "single_state": state_id,
                    "palette": None,
                    "longs": None
                }

            else:
                # clamp bits_per_entry to minimum of 4 or direct mode
                effective_bits = max(4, bits_per_entry) if bits_per_entry < 15 else bits_per_entry
                palette, offset = self._read_palette(payload, offset, bits_per_entry)
                data_length = self._read_varint(payload, offset)
                offset += self._varint_size(payload, offset)

                if data_length == 0:
                    self._sections[section_y] = {
                        "bits_per_entry": effective_bits,
                        "single_state": None,
                        "palette": palette,
                        "longs": ()
                    }

                else:
                    longs = struct.unpack_from(f">{data_length}q", payload, offset)
                    offset += data_length * 8
                    self._sections[section_y] = {
                        "bits_per_entry": effective_bits,
                        "single_state": None,
                        "palette": palette,
                        "longs": longs
                    }

            # same structure as block states, bits_per_entry, palette, long array
            biome_bits = payload[offset]
            offset += 1
            # The reason it's there is the chunk packet bundles both block state
            # data and biome data for each section together. You don't need biomes
            # for block queries so you skip past them, but you still have to read
            # and advance the offset correctly or your cursor lands in the wrong
            # place for the next section. So the biome parsing is purely offset
            # bookkeeping, not data extraction.
            if biome_bits == 0:
                offset += self._varint_size(payload, offset)
                offset += self._varint_size(payload, offset)

            else:
                biome_palette_length = self._read_varint(payload, offset)
                offset += self._varint_size(payload, offset)

                for _ in range(biome_palette_length):
                    offset += self._varint_size(payload, offset)

                biome_data_length = self._read_varint(payload, offset)
                offset += self._varint_size(payload, offset)
                offset += biome_data_length * 8

            section_y += 1

    """
    --------------------------------------------------------------------------------------------
    Function Header - NBT Data handling
    --------------------------------------------------------------------------------------------
    Gets hmap from nbt tag data, then we offset past it for later data, uses read nbt payload 
    to do so, where we recursively build the python dict via a tree structure.
    --------------------------------------------------------------------------------------------
    """

    def _read_nbt(self, data, offset):
        tag_type = data[offset]
        offset += 1

        if tag_type == TAG_END:
            return None, offset

        # skip the name, 2 byte length prefix
        name_length = struct.unpack_from(">H", data, offset)[0]
        offset += 2 + name_length
        return self._read_nbt_payload(data, offset, tag_type)

    """
    --------------------------------------------------------------------------------------------
    Function Header - read NBT
    --------------------------------------------------------------------------------------------
    tag compound / list:

    The difference between them is structure. A compound is a collection of named tags of mixed 
    types, you keep reading until you hit TAG_END which is always a leaf. A list is a collection 
    of unnamed tags all of the same type with a known count upfront.

    The recursive cases (TAG_COMPOUND, TAG_LIST) can't return until their children return, and 
    those children might themselves be compounds or lists (giving the tree like nature), so the 
    recursion keeps going deeper. But eventually every branch of the tree terminates at a 
    primitive, which returns immediately and propogates back up the stack.

    You only ever hit a primitive (base cases) when you're at a leaf of the tree. Either you're 
    inside a compound and the next child happens to be a primitive type, or you're inside a list 
    whose element type is a primitive, a list is only ever a subtree if its element type = 
    compounds

    The chunk packet arrives as one flat byte sequence which is why we move through with a linear 
    offset. The first part is the NBT blob, a single named compound tag always at the the root 
    of the entire tree. Everything inside it, WORLD_SURFACE, MOTION_BLOCKING etc. are its named 
    children.

    Each compound's dict maps its direct children's name strings to their parsed values. 
    So the root compound's dict has WORLD_SURFACE, MOTION_BLOCKING etc. as keys, those are its
    direct children. If any child were itself a compound, its value would be another dict mapping 
    that compound's own children, and so on down the tree. Each dict only represents one level, 
    its own direct children/their keys. 

    Because struct.unpack returns a tuple of 256 64 bit signed integer for one heightmap tag 
    type, representing the surface Y coordinate for every column in the 16x16 chunk.

    see thinking.txt for reasoning for compound subtree and nbt blob or lackthereof, and what 
    recursive tree built dict maps to, and more.
    --------------------------------------------------------------------------------------------
    """

    def _read_nbt_payload(self, data, offset, tag_type):
        # base cases
        if tag_type == TAG_BYTE:
            return data[offset], offset + 1

        elif tag_type == TAG_SHORT:
            return struct.unpack_from(">h", data, offset)[0], offset + 2

        elif tag_type == TAG_INT:
            return struct.unpack_from(">i", data, offset)[0], offset + 4

        elif tag_type == TAG_LONG:
            return struct.unpack_from(">q", data, offset)[0], offset + 8

        elif tag_type == TAG_FLOAT:
            return struct.unpack_from(">f", data, offset)[0], offset + 4

        elif tag_type == TAG_DOUBLE:
            return struct.unpack_from(">d", data, offset)[0], offset + 8

        elif tag_type == TAG_BYTE_ARRAY:
            length = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            return data[offset:offset + length], offset + length

        elif tag_type == TAG_STRING:
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            return data[offset:offset + length].decode("utf-8"), offset + length

        elif tag_type == TAG_INT_ARRAY:
            length = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            values = struct.unpack_from(f">{length}i", data, offset)
            return values, offset + length * 4

        elif tag_type == TAG_LONG_ARRAY:
            length = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            values = struct.unpack_from(f">{length}q", data, offset)
            return values, offset + length * 8

        # recursive cases
        elif tag_type == TAG_LIST:
            element_type = data[offset]
            offset += 1
            length = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            values = []
            for _ in range(length):
                value, offset = self._read_nbt_payload(data, offset, element_type)
                values.append(value)
            return values, offset

        elif tag_type == TAG_COMPOUND:
            entries = {}
            while True:
                child_type = data[offset]
                offset += 1
                if child_type == TAG_END:
                    break
                name_length = struct.unpack_from(">H", data, offset)[0]
                offset += 2
                name = data[offset:offset + name_length].decode("utf-8")
                offset += name_length
                value, offset = self._read_nbt_payload(data, offset, child_type)
                entries[name] = value
            return entries, offset

        else:
            raise ValueError(f"Unknown NBT tag type: {tag_type}")

    """
    --------------------------------------------------------------------------------------------
    Function Header - Read palette
    --------------------------------------------------------------------------------------------
    It reads a varint count telling you how many entries are in the palette, then loops that 
    many times reading one varint per iteration, each varint being a global block state ID, 
    and appends them to a list. Returns that list and the updated offset.

    The special case is direct mode, if bits_per_entry >= 15 there is no palette at all, so 
    it returns None immediately without reading anything. In that case the long array 
    stores global state IDs directly rather than palette indices. 

    Because at 15+ bits per entry the palette would be so large, 32768+ entries, that it's more 
    efficient to just store the global state IDs directly in the long…Because at 15+ bits per 
    entry the palette would be so large, 32768+ entries, that it's more efficient to just store 
    the global state IDs directly in the long array and skip the palette lookup step entirely. 
    The palette only saves space when the number of distinct block types in a section is small 
    relative to the total state space. Once it's large enough that you'd need 15 bits anyway, 
    the indirection buys you nothing so the server drops it.
    --------------------------------------------------------------------------------------------
    """

    def _read_palette(self, payload, offset, bits_per_entry):
        if bits_per_entry >= 15:
            # direct mode -> no palette
            return None, offset

        palette_length = self._read_varint(payload, offset)
        offset += self._varint_size(payload, offset)
        palette = []

        for _ in range(palette_length):
            state_id = self._read_varint(payload, offset)
            offset += self._varint_size(payload, offset)
            palette.append(state_id)

        return palette, offset

    """
    --------------------------------------------------------------------------------------------
    Function Header - Get block
    --------------------------------------------------------------------------------------------
    Public interface. Takes absolute world coordinates and returns the block name string.
    Converts to section-local coordinates first, then unpacks the correct bits from the
    long array.

    see thinking.txt
    --------------------------------------------------------------------------------------------
    """

    def get_block(self, x, y, z):
        # world starts at y=-64, section 0 is y=-64 to y=-49
        section_y = (y + 64) >> 4
        if section_y not in self._sections:
            return "air"

        section = self._sections[section_y]

        # single value section — entire section is one block type
        if section["bits_per_entry"] == 0:
            state_id = section["single_state"]
            return self._state_to_block.get(state_id, "unknown")

        # check for block updates patched over the parsed data
        patched = section.get("patched", {})
        lx_check = x & 0xF
        ly_check = y & 0xF
        lz_check = z & 0xF
        if (lx_check, ly_check, lz_check) in patched:
            return self._state_to_block.get(patched[(lx_check, ly_check, lz_check)], "unknown")

        bits = section["bits_per_entry"]
        palette = section["palette"]
        longs = section["longs"]

        if not longs:
            return "air"

        # local coordinates within the section
        lx = x & 0xF
        ly = y & 0xF
        lz = z & 0xF

        # block index within the section
        block_index = (ly * 16 + lz) * 16 + lx

        # post-1.16 packing, entries never straddle longs
        blocks_per_long = 64 // bits
        long_index = block_index // blocks_per_long
        bit_offset = (block_index % blocks_per_long) * bits

        mask = (1 << bits) - 1

        if long_index >= len(longs):
            return "air"

        palette_index = (longs[long_index] >> bit_offset) & mask

        if palette is None:
            state_id = palette_index
        else:
            if palette_index >= len(palette):
                return "air"
            state_id = palette[palette_index]

        return self._state_to_block.get(state_id, "unknown")

    """
    --------------------------------------------------------------------------------------------
    Function Field Header - Varint auxiliary functions
    --------------------------------------------------------------------------------------------
    Reads a varint from a bytes buffer at a given offset. Separate from the socket varint
    reader in connection as here we are reading from a buffer in memory, not a live socket (as 
    we are using the already locally acessible payload from the socket handled in connection, a 
    important thing
    --------------------------------------------------------------------------------------------
    """

    @staticmethod
    def _read_varint(data, offset):
        result = 0
        shift = 0
        while True:
            byte = data[offset]
            result |= (byte & 0x7F) << shift
            offset += 1
            shift += 7

            if not (byte & 0x80):
                break

        return result

    @staticmethod
    def _varint_size(data, offset):
        size = 0
        while True:
            byte = data[offset]
            offset += 1
            size += 1

            if not (byte & 0x80):
                break

        return size

    """
    --------------------------------------------------------------------------------------------
    Function Header - Surface getter
    --------------------------------------------------------------------------------------------
    Returns the Y of the highest non-air block at column (x,z) useful for pathfinding and 
    surface queries. Heightmap is a packed long array indexed by x+z*16.
    --------------------------------------------------------------------------------------------
    """

    def get_surface_y(self, x, z):
        lx = x & 0xF
        lz = z & 0xF
        if self._hmap and "WORLD_SURFACE" in self._hmap:
            return self._hmap["WORLD_SURFACE"][lx + lz * 16]

        return None