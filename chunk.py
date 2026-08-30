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
Chunk Parser - Documented slightly differently as this is it's own repo as well.
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

    @staticmethod
    def _build_state_map(blocks_json):
        state_map = {}

        for block in blocks_json:
            explicit_ids = [state["id"] for state in block.get("states", []) if "id" in state]

            if explicit_ids:
                for state_id in explicit_ids:
                    state_map[state_id] = block["name"]
            else:
                for state_id in range(block["minStateId"], block["maxStateId"] + 1):
                    state_map[state_id] = block["name"]

        return state_map


    def __init__(self, payload, version="26.1.2", hm=None):
        # setting hmap in initializer would overwrite parser we iitialize this here to parser with hmap
        self._java26_heightmaps = hm
        self._min_y = -64
        self._world_height = 384

        if version not in Chunk._state_to_block_cache:
            blocks_path = Path(__file__).parent / "blocks" / f"blocks_{version}.json"
            blocks_json = json.loads(blocks_path.read_text())
            Chunk._state_to_block_cache[version] = self._build_state_map(blocks_json)

        self._state_to_block = Chunk._state_to_block_cache[version]
        # sections indexed vertically (by y index)
        self._sections = {}
        self._parse(payload)


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
        # need hmap, example "find a tree" benefits from knowing the surface Y so
        # you search near the surface rather than scanning all 24 sections
        if self._java26_heightmaps is not None:
            self._hmap = self._java26_heightmaps
            offset = 0
            sections_end = len(payload)
        else:
            self._hmap, offset = self._read_nbt(payload, 0)
            sections_end = len(payload)

        # standard world is 384 blocks tall (-64 to 320) = 24 sections
        # section_y 0 corresponds to y=-64, section_y 23 corresponds to y=304
        section_y = 0

        # get data for each section y of the chunk for the payload
        while offset < sections_end and section_y < 24:
            # peek ahead to see if we've reached block entities which
            # start with a varint count, not a section structure, heuristic:
            # remaining bytes too small for a section → break
            if offset + 3 >= len(payload):
                break

            # Java 26 adds a fluid count after the solid-block count.
            offset += 4
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
                # store as single-value section
                self._sections[section_y] = {
                    "bits_per_entry": 0,
                    "single_state": state_id,
                    "palette": None,
                    "longs": None
                }

            else:
                # clamp bits_per_entry to minimum of 4 or direct mode
                effective_bits = max(4, bits_per_entry) if bits_per_entry < 9 else bits_per_entry
                palette, offset = self._read_palette(payload, offset, bits_per_entry)
                data_length = (4096 + (64 // effective_bits) - 1) // (64 // effective_bits)

                if data_length == 0:
                    self._sections[section_y] = {
                        "bits_per_entry": effective_bits,
                        "single_state": None,
                        "palette": palette,
                        "longs": ()
                    }

                else:
                    if offset + data_length * 8 > sections_end:
                        raise ValueError(
                            "Truncated block-state array "
                            f"in section {section_y} (bits={bits_per_entry}, "
                            f"longs={data_length}, offset={offset}, end={sections_end})"
                        )

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
            else:
                if biome_bits < 4:
                    biome_palette_length = self._read_varint(payload, offset)
                    offset += self._varint_size(payload, offset)

                    for _ in range(biome_palette_length):
                        offset += self._varint_size(payload, offset)

                effective_biome_bits = max(1, biome_bits)
                biome_data_length = (64 + (64 // effective_biome_bits) - 1) // (64 // effective_biome_bits)
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
        # Removed from NBT sent over the network drops the root compound's name:
        # name_length = struct.unpack_from(">H", data, offset)[0]
        # offset += 2 + name_length

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

    Named compound children include their type and name before their payload. Recursive calls
    therefore decode only the child payload and return nested dictionaries for nested compounds.
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
        if bits_per_entry >= 9:
            # direct mode -> no palette
            return None, offset

        palette_length = self._read_varint(payload, offset)

        if palette_length > 4096:
            raise ValueError(f"Invalid block palette length {palette_length} for {bits_per_entry} bits at offset {offset}")

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
    The single public interface. Takes absolute world coordinates and returns the block name
    string. Converts to section-local coordinates first, then unpacks the correct bits out of
    the long array.

    The lookup chain is section coordinates, packed palette index, global state ID, block name.
    Four hops, and each one can miss, which is why there are so many early returns. Every miss
    answers "air" rather than raising. A query for a chunk column the server never sent is a
    normal thing for a pathfinder to do while it probes ahead of itself, so an exception there
    would mean every caller wrapping every lookup in a try. "air" is also the honest answer,
    since an unsent section is unloaded, not solid, and treating unknown space as walkable is
    what lets the pathfinder route into it and find out.

    The two dictionary values that can be None are what make the branching look uneven.
    single_state is set only when bits_per_entry is 0, and palette is None only in direct mode,
    so the two are never both meaningful at once. Reading them in the wrong order would mean
    unpacking a long array that was never allocated.

    Order matters in one more place. Patched updates are checked before the packed data because
    the parsed section is a snapshot from whenever the chunk packet arrived, while patches are
    the block change packets that have landed since. The packed data is stale the moment anyone
    mines anything, so the patch dict wins.
    --------------------------------------------------------------------------------------------
    """
    def get_block(self, x, y, z):
        # world starts at y=-64, section 0 is y=-64 to y=-49
        section_y = (y + 64) >> 4

        # server never sent this section, so treat it as open space rather than an error
        if section_y not in self._sections:
            return "air"

        section = self._sections[section_y]
        # single value section, entire section is one block type, no palette or longs to unpack
        if section["bits_per_entry"] == 0:
            state_id = section["single_state"]
            return self._state_to_block.get(state_id, "unknown")

        # check for block updates patched over the parsed data, these are newer than the packet
        patched = section.get("patched", {})
        lx_check = x & 0xF
        ly_check = y & 0xF
        lz_check = z & 0xF

        if (lx_check, ly_check, lz_check) in patched:
            return self._state_to_block.get(patched[(lx_check, ly_check, lz_check)], "unknown")

        bits = section["bits_per_entry"]
        palette = section["palette"]
        longs = section["longs"]

        # empty long array, the section carries a palette but no packed data to index into
        if not longs:
            return "air"

        # local coordinates within the section
        lx = x & 0xF
        ly = y & 0xF
        lz = z & 0xF
        # block index within the section, y major then z then x, the order the server packs in
        block_index = (ly * 16 + lz) * 16 + lx
        # post-1.16 packing, entries never straddle longs, so one divide finds the right long
        # and any leftover high bits at the top of each long are simply padding you ignore
        blocks_per_long = 64 // bits
        long_index = block_index // blocks_per_long
        bit_offset = (block_index % blocks_per_long) * bits
        mask = (1 << bits) - 1

        # short array for the section size, the packet was truncated or misparsed upstream
        if long_index >= len(longs):
            return "air"

        # shift the entry down to bit 0 then mask off everything above it
        palette_index = (longs[long_index] >> bit_offset) & mask

        # direct mode stores global state IDs in the long array, so there is no lookup to do
        if palette is None:
            state_id = palette_index
        else:
            # index past the end of the palette means the bits were unpacked wrong
            if palette_index >= len(palette):
                return "air"

            state_id = palette[palette_index]

        # a state ID absent from the registry is a real block from a version we lack data for,
        # which is different from empty space, so it answers "unknown" rather than "air"
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
    Returns the Y of the highest non-air block at column (x,z), useful for pathfinding and
    surface queries. Asking "find a tree" benefits from knowing the surface Y so you search
    near it rather than scanning all 24 sections top to bottom, which is 98304 get_block calls
    per chunk you would otherwise be making to answer one question.

    The heightmap is a packed long array in the same style as the block data, but packed to a
    different width, so it does not reuse the block unpacking above. Block sections pack to
    bits_per_entry, which the server tells you. Here nobody tells you anything, you derive it,
    because the width is whatever it takes to hold the world height. 384 needs 9 bits, so 7
    entries fit per long and the top bit of each long is padding. That is why _world_height is
    a field rather than a literal, the arithmetic falls out of it.

    One index quirk worth knowing. Block data is indexed y major, this is indexed x + z * 16
    because there is no y, one entry per column and 256 columns in the chunk.

    The stored value is not a Y coordinate, it is a count of blocks up from the bottom of the
    world, so 0 genuinely means "nothing found in this column" rather than "the surface is at
    y=0", and it has to answer None instead of _min_y. Any real value converts back with
    _min_y + value - 1, the minus one because a count of 1 means the block sitting at _min_y
    itself.

    Reads WORLD_SURFACE, which ignores whether a block blocks movement and only asks whether
    it is air. That is the right map for "where is the ground", and the wrong one for "can I
    stand here", since leaves and water both count.
    --------------------------------------------------------------------------------------------
    """
    def get_surface_y(self, x, z):
        lx = x & 0xF
        lz = z & 0xF

        # no heightmap when the server sent none, or when an older chunk parsed without one
        if self._hmap and "WORLD_SURFACE" in self._hmap:
            values = self._hmap["WORLD_SURFACE"]
            # width is derived from world height, not sent, 384 -> 9 bits -> 7 per long
            bits = self._world_height.bit_length()
            entries_per_long = 64 // bits
            # one entry per column, so x and z only, 256 columns in the chunk
            index = lx + lz * 16
            long_index = index // entries_per_long

            # short array, the heightmap did not cover the whole chunk
            if long_index >= len(values):
                return None

            bit_offset = (index % entries_per_long) * bits
            # NBT longs are signed, so mask back to unsigned before shifting the entry out
            packed = values[long_index] & ((1 << 64) - 1)
            first_available = (packed >> bit_offset) & ((1 << bits) - 1)

            # 0 means the column is empty all the way up, not that the surface sits at the floor
            if first_available == 0:
                return None

            # stored as a count up from the world floor, so convert it back to a real Y
            return self._min_y + first_available - 1

        return None
