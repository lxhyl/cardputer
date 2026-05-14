# Minimal QR code encoder for MicroPython.
# Byte mode only; EC levels L and M; versions 1..15 (modules 21x21 .. 77x77).
#
# Public API:
#   LEVEL_L, LEVEL_M
#   encode(data: str|bytes, level=LEVEL_L) -> (size, modules)
#     `modules` is a bytearray of size*size, 1 = black, 0 = white, row-major.
#
# Algorithm follows ISO/IEC 18004 (QR Code 2005). Tables verified against
# Project Nayuki's reference implementation.

LEVEL_L = 0
LEVEL_M = 1

# Total codewords (data + ECC) per version. Index 0 = unused placeholder.
_TOTAL_CW = (0, 26, 44, 70, 100, 134, 172, 196, 242, 292, 346,
             404, 466, 532, 581, 655)

# (num_ec_blocks, ec_codewords_per_block) for each (version, level).
# Level index: 0 = L, 1 = M.
_EC_BLOCKS = (
    None,                          # v0 placeholder
    ((1,  7),  (1, 10)),  # v1
    ((1, 10),  (1, 16)),  # v2
    ((1, 15),  (1, 26)),  # v3
    ((1, 20),  (2, 18)),  # v4
    ((1, 26),  (2, 24)),  # v5
    ((2, 18),  (4, 16)),  # v6
    ((2, 20),  (4, 18)),  # v7
    ((2, 24),  (4, 22)),  # v8
    ((2, 30),  (5, 22)),  # v9
    ((4, 18),  (5, 26)),  # v10
    ((4, 20),  (5, 30)),  # v11
    ((4, 24),  (8, 22)),  # v12
    ((4, 26),  (9, 22)),  # v13
    ((4, 30),  (9, 24)),  # v14
    ((6, 22), (10, 24)),  # v15
)

# Alignment-pattern centre coordinates per version.
_ALIGN = (
    [], [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
    [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50],
    [6, 30, 54], [6, 32, 58], [6, 34, 62],
    [6, 26, 46, 66], [6, 26, 48, 70],
)


# ---- GF(256) tables ------------------------------------------------------

_GF_EXP = bytearray(512)
_GF_LOG = bytearray(256)


def _init_gf():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x = ((x << 1) ^ 0x11D) if (x & 0x80) else (x << 1)
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree):
    """Reed-Solomon generator polynomial of given degree, MSB first."""
    g = bytearray([1])
    for i in range(degree):
        new = bytearray(len(g) + 1)
        for j in range(len(g)):
            new[j] ^= g[j]
            new[j + 1] ^= _gf_mul(g[j], _GF_EXP[i])
        g = new
    return g


def _rs_ecc(data, ec_len):
    """Compute `ec_len` ECC codewords for `data` (bytes-like)."""
    gen = _rs_generator(ec_len)
    result = bytearray(ec_len)
    for b in data:
        factor = b ^ result[0]
        # shift result left by one position
        for i in range(ec_len - 1):
            result[i] = result[i + 1]
        result[ec_len - 1] = 0
        for i in range(ec_len):
            result[i] ^= _gf_mul(factor, gen[i + 1])
    return result


# ---- Bit stream construction --------------------------------------------

def _data_codewords(version, level):
    nb, eb = _EC_BLOCKS[version][level]
    return _TOTAL_CW[version] - nb * eb


def _build_codewords(data, version, level):
    """Build the data-codeword stream (with mode header, terminator and pad)."""
    cci_bits = 8 if version <= 9 else 16
    cap_bits = _data_codewords(version, level) * 8

    bits = []
    # Mode indicator: byte = 0100
    bits.extend((0, 1, 0, 0))
    # Character count indicator (MSB first)
    n = len(data)
    for i in range(cci_bits - 1, -1, -1):
        bits.append((n >> i) & 1)
    # Data
    for b in data:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    # Terminator (up to 4 zero bits)
    for _ in range(min(4, cap_bits - len(bits))):
        bits.append(0)
    # Pad to byte boundary
    while len(bits) % 8 != 0:
        bits.append(0)
    # Pad with alternating 0xEC, 0x11 codewords
    pad = 0xEC
    while len(bits) < cap_bits:
        for i in range(7, -1, -1):
            bits.append((pad >> i) & 1)
        pad = 0x11 if pad == 0xEC else 0xEC
    # Pack bits to bytes
    cw = bytearray(len(bits) // 8)
    for i in range(len(cw)):
        b = 0
        for j in range(8):
            b = (b << 1) | bits[i * 8 + j]
        cw[i] = b
    return cw


def _interleave(data_cw, version, level):
    """Split into RS blocks, compute ECC, interleave per QR spec."""
    nb, eb = _EC_BLOCKS[version][level]
    total = len(data_cw)
    short = total // nb
    extra = total % nb
    # Blocks: (nb - extra) short blocks of `short` codewords, then
    # `extra` long blocks of `short + 1` codewords.
    data_blocks = []
    pos = 0
    for i in range(nb):
        size = short + (1 if i >= nb - extra else 0)
        data_blocks.append(data_cw[pos:pos + size])
        pos += size
    ecc_blocks = [_rs_ecc(b, eb) for b in data_blocks]

    out = bytearray()
    max_data = short + (1 if extra > 0 else 0)
    for col in range(max_data):
        for blk in data_blocks:
            if col < len(blk):
                out.append(blk[col])
    for col in range(eb):
        for blk in ecc_blocks:
            out.append(blk[col])
    return out


# ---- Module placement ---------------------------------------------------

def _size_for(version):
    return version * 4 + 17


def _make_grid(version):
    """Build the function-module grid. Returns (mat, res, size).

    `mat` (bytearray, row-major) holds the module values so far.
    `res` (bytearray) marks which cells are reserved (function modules /
    format-info / version-info).
    """
    size = _size_for(version)
    mat = bytearray(size * size)
    res = bytearray(size * size)

    def setm(r, c, v):
        mat[r * size + c] = v
        res[r * size + c] = 1

    # Three finder patterns (7x7 with one-module white separator).
    for (br, bc) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = br + dr, bc + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    if dr in (0, 6) or dc in (0, 6):
                        v = 1
                    elif 2 <= dr <= 4 and 2 <= dc <= 4:
                        v = 1
                    else:
                        v = 0
                else:
                    v = 0  # separator
                setm(r, c, v)

    # Timing patterns (alternating 1,0,1,... along row 6 and column 6).
    for i in range(8, size - 8):
        v = 1 - (i % 2)
        setm(6, i, v)
        setm(i, 6, v)

    # Alignment patterns (5x5 with one-module white ring + dark centre).
    aligns = _ALIGN[version]
    for ar in aligns:
        for ac in aligns:
            # Skip the three corners that overlap finder patterns.
            if ((ar < 8 and ac < 8) or
                (ar < 8 and ac > size - 9) or
                (ar > size - 9 and ac < 8)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    if dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0):
                        v = 1
                    else:
                        v = 0
                    setm(ar + dr, ac + dc, v)

    # Reserve format-info area (15 cells along col 8 and 15 along row 8).
    for i in range(15):
        # Column 8
        if i < 6:
            r = i
        elif i < 8:
            r = i + 1
        else:
            r = size - 15 + i
        res[r * size + 8] = 1
        # Row 8
        if i < 8:
            c = size - 1 - i
        elif i == 8:
            c = 7
        else:
            c = 14 - i
        res[8 * size + c] = 1

    # Always-dark module + reserve it.
    setm(size - 8, 8, 1)

    # Version info (V7+): 18-bit BCH(18,6) code placed in two 3x6 strips.
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vi = (version << 12) | (rem & 0xFFF)
        for i in range(18):
            bit = (vi >> i) & 1
            a = size - 11 + (i % 3)
            b = i // 3
            setm(a, b, bit)
            setm(b, a, bit)

    return mat, res, size


def _place_data(mat, res, size, stream):
    """Zigzag-place codeword bits, MSB first, starting at bottom-right."""
    bit_idx = 0
    total_bits = len(stream) * 8
    col = size - 1
    going_up = True
    while col > 0:
        if col == 6:
            col -= 1  # skip the vertical timing column
        for i in range(size):
            r = (size - 1 - i) if going_up else i
            for dc in (0, 1):
                c = col - dc
                if res[r * size + c]:
                    continue
                if bit_idx < total_bits:
                    byte = stream[bit_idx >> 3]
                    bit = (byte >> (7 - (bit_idx & 7))) & 1
                    mat[r * size + c] = bit
                    bit_idx += 1
                # Else: remainder bit, leave 0 (mask will toggle).
        col -= 2
        going_up = not going_up


# ---- Masking & format info ---------------------------------------------

def _mask_cond(idx, r, c):
    if idx == 0: return (r + c) % 2 == 0
    if idx == 1: return r % 2 == 0
    if idx == 2: return c % 3 == 0
    if idx == 3: return (r + c) % 3 == 0
    if idx == 4: return ((r // 2) + (c // 3)) % 2 == 0
    if idx == 5: return ((r * c) % 2) + ((r * c) % 3) == 0
    if idx == 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _apply_mask(mat, res, size, mask_idx):
    for r in range(size):
        row_off = r * size
        for c in range(size):
            if not res[row_off + c] and _mask_cond(mask_idx, r, c):
                mat[row_off + c] ^= 1


def _format_bits(level, mask):
    # Level encoding: L=01, M=00, Q=11, H=10
    lvl_bits = (0b01, 0b00, 0b11, 0b10)[level]
    data = (lvl_bits << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412  # 15 bits


def _place_format(mat, size, level, mask):
    bits = _format_bits(level, mask)
    # Vertical strip (column 8): bits 0..5 → rows 0..5, bits 6..7 → rows 7,8,
    # bits 8..14 → rows size-7 .. size-1.
    for i in range(15):
        bit = (bits >> i) & 1
        if i < 6:
            r = i
        elif i < 8:
            r = i + 1
        else:
            r = size - 15 + i
        mat[r * size + 8] = bit
    # Horizontal strip (row 8): bits 0..7 → cols size-1 .. size-8,
    # bit 8 → col 7, bits 9..14 → cols 5..0.
    for i in range(15):
        bit = (bits >> i) & 1
        if i < 8:
            c = size - 1 - i
        elif i == 8:
            c = 7
        else:
            c = 14 - i
        mat[8 * size + c] = bit
    mat[(size - 8) * size + 8] = 1  # always-dark module


# ---- Mask penalty (ISO 18004 §8.3.1) ------------------------------------

def _penalty(mat, size):
    score = 0
    # Rule 1: runs of 5+ same-colour modules in any row/column.
    for r in range(size):
        run_v = mat[r * size]
        run_n = 1
        for c in range(1, size):
            v = mat[r * size + c]
            if v == run_v:
                run_n += 1
            else:
                if run_n >= 5:
                    score += 3 + (run_n - 5)
                run_v = v
                run_n = 1
        if run_n >= 5:
            score += 3 + (run_n - 5)
    for c in range(size):
        run_v = mat[c]
        run_n = 1
        for r in range(1, size):
            v = mat[r * size + c]
            if v == run_v:
                run_n += 1
            else:
                if run_n >= 5:
                    score += 3 + (run_n - 5)
                run_v = v
                run_n = 1
        if run_n >= 5:
            score += 3 + (run_n - 5)
    # Rule 2: 2x2 blocks of same colour.
    for r in range(size - 1):
        for c in range(size - 1):
            v = mat[r * size + c]
            if (mat[r * size + c + 1] == v and
                mat[(r + 1) * size + c] == v and
                mat[(r + 1) * size + c + 1] == v):
                score += 3
    # Rule 3: finder-like 1011101 with 4 white modules on either side.
    # Rolling 11-bit window — orders of magnitude faster than slicing.
    PAT_A = 0x5D0  # 0b10111010000
    PAT_B = 0x05D  # 0b00001011101
    for r in range(size):
        val = 0
        row_off = r * size
        for c in range(size):
            val = ((val << 1) | mat[row_off + c]) & 0x7FF
            if c >= 10 and (val == PAT_A or val == PAT_B):
                score += 40
    for c in range(size):
        val = 0
        for r in range(size):
            val = ((val << 1) | mat[r * size + c]) & 0x7FF
            if r >= 10 and (val == PAT_A or val == PAT_B):
                score += 40
    # Rule 4: dark-module ratio imbalance.
    dark = 0
    for v in mat:
        dark += v
    total = size * size
    # k = how many 5% steps the dark ratio strays from 50%.
    k = abs(dark * 20 - total * 10) // total
    score += k * 10
    return score


# ---- Public API ---------------------------------------------------------

def encode(data, level=LEVEL_L):
    """Encode `data` (str → utf-8, or bytes) as a QR matrix.

    Returns (size, modules) where `modules` is a bytearray of size*size
    bytes (0 = white, 1 = black, row-major). Raises ValueError if the data
    does not fit in version 15 at the requested EC level.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")

    version = None
    for v in range(1, 16):
        cci_bits = 8 if v <= 9 else 16
        cap_bits = _data_codewords(v, level) * 8
        if 4 + cci_bits + 8 * len(data) <= cap_bits:
            version = v
            break
    if version is None:
        raise ValueError("data too long for QR v1-15")

    data_cw = _build_codewords(data, version, level)
    stream = _interleave(data_cw, version, level)

    base_mat, res, size = _make_grid(version)
    _place_data(base_mat, res, size, stream)

    best_mat = None
    best_score = None
    for mask in range(8):
        mat = bytearray(base_mat)
        _apply_mask(mat, res, size, mask)
        _place_format(mat, size, level, mask)
        s = _penalty(mat, size)
        if best_score is None or s < best_score:
            best_score = s
            best_mat = mat

    return size, best_mat
