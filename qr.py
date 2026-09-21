"""极简纯 Python 二维码编码器（只用标准库）。

登录微信机器人时，后端返回一个必须用手机微信扫描的链接。本模块把它编码成
可以在终端里直接扫的二维码 —— 项目不依赖任何第三方包，所以自己实现。

范围：字节模式，版本 1–10，纠错等级 L/M/Q/H，等级 L 下最多 271 字节，足够
登录链接使用。数字/字母数字模式、版本 11–40、汉字模式与 ECI 均未实现。

参考标准：ISO/IEC 18004:2015。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 各纠错等级在格式信息里的位模式（ISO/IEC 18004 表 25）。
_EC_FORMAT_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

EC_LEVELS = ("L", "M", "Q", "H")


@dataclass(frozen=True)
class _EcSpec:
    """某个（版本, 等级）下的纠错分块布局。"""

    ec_codewords_per_block: int
    group1_blocks: int
    group1_data_codewords: int
    group2_blocks: int
    group2_data_codewords: int

    @property
    def data_codewords(self) -> int:
        return (
            self.group1_blocks * self.group1_data_codewords
            + self.group2_blocks * self.group2_data_codewords
        )


#: (版本, 等级) -> (每块纠错码字, 组1块数, 组1数据码字, 组2块数, 组2数据码字)
_RAW_TABLE: dict[tuple[int, str], tuple[int, int, int, int, int]] = {
    (1, "L"): (7, 1, 19, 0, 0),
    (1, "M"): (10, 1, 16, 0, 0),
    (1, "Q"): (13, 1, 13, 0, 0),
    (1, "H"): (17, 1, 9, 0, 0),
    (2, "L"): (10, 1, 34, 0, 0),
    (2, "M"): (16, 1, 28, 0, 0),
    (2, "Q"): (22, 1, 22, 0, 0),
    (2, "H"): (28, 1, 16, 0, 0),
    (3, "L"): (15, 1, 55, 0, 0),
    (3, "M"): (26, 1, 44, 0, 0),
    (3, "Q"): (18, 2, 17, 0, 0),
    (3, "H"): (22, 2, 13, 0, 0),
    (4, "L"): (20, 1, 80, 0, 0),
    (4, "M"): (18, 2, 32, 0, 0),
    (4, "Q"): (26, 2, 24, 0, 0),
    (4, "H"): (16, 4, 9, 0, 0),
    (5, "L"): (26, 1, 108, 0, 0),
    (5, "M"): (24, 2, 43, 0, 0),
    (5, "Q"): (18, 2, 15, 2, 16),
    (5, "H"): (22, 2, 11, 2, 12),
    (6, "L"): (18, 2, 68, 0, 0),
    (6, "M"): (16, 4, 27, 0, 0),
    (6, "Q"): (24, 4, 19, 0, 0),
    (6, "H"): (28, 4, 15, 0, 0),
    (7, "L"): (20, 2, 78, 0, 0),
    (7, "M"): (18, 4, 31, 0, 0),
    (7, "Q"): (18, 2, 14, 4, 15),
    (7, "H"): (26, 4, 13, 1, 14),
    (8, "L"): (24, 2, 97, 0, 0),
    (8, "M"): (22, 2, 38, 2, 39),
    (8, "Q"): (22, 4, 18, 2, 19),
    (8, "H"): (26, 4, 14, 2, 15),
    (9, "L"): (30, 2, 116, 0, 0),
    (9, "M"): (22, 3, 36, 2, 37),
    (9, "Q"): (20, 4, 16, 4, 17),
    (9, "H"): (24, 4, 12, 4, 13),
    (10, "L"): (18, 2, 68, 2, 69),
    (10, "M"): (26, 4, 43, 1, 44),
    (10, "Q"): (24, 6, 19, 2, 20),
    (10, "H"): (28, 6, 15, 2, 16),
}

#: (版本, 等级) -> 纠错布局。
_TABLE: dict[tuple[int, str], _EcSpec] = {
    key: _EcSpec(*value) for key, value in _RAW_TABLE.items()
}

#: 各版本中校正图形的圆心行列坐标。
_ALIGNMENT_CENTRES: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

#: 版本 >= 7 的 18 位版本信息。
_VERSION_INFO: dict[int, int] = {
    7: 0x07C94,
    8: 0x085BC,
    9: 0x09A99,
    10: 0x0A4D3,
}

MAX_VERSION = 10


def _char_count_bits(version: int) -> int:
    """字节模式字符计数指示符的位宽。

    ISO/IEC 18004 表 3：版本 1–9 为 8 位，版本 10–26 为 16 位。
    """
    return 8 if version <= 9 else 16


def _capacity_bits(version: int, level: str) -> int:
    return _TABLE[(version, level)].data_codewords * 8


def _fits(payload_len: int, version: int, level: str) -> bool:
    needed = 4 + _char_count_bits(version) + payload_len * 8
    return needed <= _capacity_bits(version, level)


class QrError(ValueError):
    """内容无法编码时抛出。"""


# ---------------------------------------------------------------------------
# Reed-Solomon 所需的 GF(256) 运算
# ---------------------------------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # 本原多项式 x^8+x^4+x^3+x^2+1
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    """生成多项式 (x-a^0)(x-a^1)...(x-a^(degree-1)) 的系数。"""
    poly = [1]
    for i in range(degree):
        new = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            new[j] ^= coeff
            new[j + 1] ^= _gf_mul(coeff, _EXP[i])
        poly = new
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    """计算 data 的 ec_len 个 Reed-Solomon 纠错码字。"""
    gen = _rs_generator_poly(ec_len)
    remainder = [0] * ec_len
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for i in range(ec_len):
            remainder[i] ^= _gf_mul(gen[i + 1], factor)
    return remainder


# ---------------------------------------------------------------------------
# 位流
# ---------------------------------------------------------------------------


class _BitBuffer:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def append(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self._bits.append((value >> i) & 1)

    def to_codewords(self) -> list[int]:
        bits = list(self._bits)
        for _ in range(min(4, 8 - len(bits) % 8 if len(bits) % 8 else 4)):
            bits.append(0)
        while len(bits) % 8:
            bits.append(0)
        return [
            int("".join(str(b) for b in bits[i : i + 8]), 2)
            for i in range(0, len(bits), 8)
        ]


# ---------------------------------------------------------------------------
# 矩阵构建
# ---------------------------------------------------------------------------

Matrix = list[list[bool | None]]


def _new_matrix(size: int) -> Matrix:
    return [[None] * size for _ in range(size)]


def _place_finder(matrix: Matrix, row: int, col: int) -> None:
    """在 (row, col) 处放置 7x7 定位图形。"""
    for dr in range(-3, 4):
        for dc in range(-3, 4):
            r, c = row + dr, col + dc
            if 0 <= r < len(matrix) and 0 <= c < len(matrix):
                # 切比雪夫距离产生同心方框。
                matrix[r][c] = max(abs(dr), abs(dc)) != 2


def _place_alignment(matrix: Matrix, row: int, col: int) -> None:
    """在 (row, col) 处放置 5x5 校正图形。"""
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            matrix[row + dr][col + dc] = max(abs(dr), abs(dc)) != 1


def _function_modules(version: int) -> tuple[Matrix, list[list[bool]]]:
    """构建某个版本的功能图形骨架。

    返回 ``(matrix, reserved)``：`matrix` 是固定功能图形（待写入处为 ``None``），
    `reserved` 是占用掩码，供数据填充与掩码使用。

    顺序很重要：先放对比度最高的定位图形，之后每一步只填仍为 ``None`` 的模块，
    因此不可能覆盖已放好的图形。
    """
    size = version * 4 + 17
    matrix = _new_matrix(size)

    _place_finder(matrix, 3, 3)
    _place_finder(matrix, 3, size - 4)
    _place_finder(matrix, size - 4, 3)

    # 分隔符：紧贴每个定位图形的一格浅色边框，只填尚为空的模块。
    for i in range(8):
        if matrix[7][i] is None:
            matrix[7][i] = False
        if matrix[i][7] is None:
            matrix[i][7] = False
        if matrix[7][size - 1 - i] is None:
            matrix[7][size - 1 - i] = False
        if matrix[i][size - 8] is None:
            matrix[i][size - 8] = False
        if matrix[size - 8][i] is None:
            matrix[size - 8][i] = False
        if matrix[size - 1 - i][7] is None:
            matrix[size - 1 - i][7] = False

    # 定时图形：在定位图形之间交替。
    for i in range(8, size - 8):
        if matrix[6][i] is None:
            matrix[6][i] = i % 2 == 0
        if matrix[i][6] is None:
            matrix[i][6] = i % 2 == 0

    # 校正图形，跳过三个定位图形所在角。
    centres = _ALIGNMENT_CENTRES[version]
    for r in centres:
        for c in centres:
            if (r < 9 and c < 9) or (r < 9 and c > size - 10) or (r > size - 10 and c < 9):
                continue
            _place_alignment(matrix, r, c)

    # 预留格式信息区域（在 matrix 中保持 None，掩码后才写入）。
    reserved = [[cell is not None for cell in row] for row in matrix]
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    matrix[size - 8][8] = True  # 固定深色模块，永不参与掩码
    reserved[size - 8][8] = True

    if version >= 7:
        bits = _VERSION_INFO[version]
        for i in range(18):
            bit = (bits >> i) & 1
            r, c = i // 3, size - 11 + i % 3
            matrix[r][c] = bool(bit)
            matrix[c][r] = bool(bit)
            reserved[r][c] = True
            reserved[c][r] = True

    return matrix, reserved


def _mask_bit(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    if mask == 7:
        return ((row + col) % 2 + (row * col) % 3) % 2 == 0
    raise QrError(f"invalid mask {mask}")


def _place_data(matrix: Matrix, reserved: list[list[bool]], codewords: list[int]) -> None:
    """按标准之字形顺序填充最终码字序列。"""
    size = len(matrix)
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # 整列跳过纵向定时图形
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    matrix[row][c] = bool(bits[idx]) if idx < len(bits) else False
                    idx += 1
        upward = not upward
        col -= 2


def _format_info(level: str, mask: int) -> int:
    """带 BCH(15,5) 纠错的 15 位格式信息。"""
    data = (_EC_FORMAT_BITS[level] << 3) | mask
    rem = data << 10
    generator = 0b10100110111
    for i in range(4, -1, -1):
        if (rem >> (10 + i)) & 1:
            rem ^= generator << i
    return ((data << 10) | rem) ^ 0b101010000010010


def _place_format_info(matrix: Matrix, level: str, mask: int) -> None:
    """把 15 位格式信息写入标准规定的两处位置（高位在前）。"""
    size = len(matrix)
    bits = _format_info(level, mask)

    copy1 = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    copy2 = [
        (size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
        (size - 5, 8), (size - 6, 8), (size - 7, 8),
        (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
        (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1),
    ]

    for i, (r, c) in enumerate(copy1):
        matrix[r][c] = bool((bits >> (14 - i)) & 1)
    for i, (r, c) in enumerate(copy2):
        matrix[r][c] = bool((bits >> (14 - i)) & 1)

    matrix[size - 8][8] = True


def _apply_mask(matrix: Matrix, reserved: list[list[bool]], mask: int) -> Matrix:
    size = len(matrix)
    out = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and _mask_bit(mask, r, c):
                out[r][c] = not out[r][c]
    return out


def _penalty(matrix: Matrix) -> int:
    """掩码惩罚分（ISO/IEC 18004 7.8.3）；分越低越好。"""
    size = len(matrix)
    score = 0

    # 规则 1：行/列中连续 5 个以上同色模块。
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # 规则 2：每个 2x2 同色块。
    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3

    # 规则 3：类似定位图形的 1:1:3:1:1 图案（两侧留四格浅色）。
    pat_a = [True, False, True, True, True, False, True, False, False, False, False]
    pat_b = list(reversed(pat_a))
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        for i in range(size - 10):
            window = line[i : i + 11]
            if window == pat_a or window == pat_b:
                score += 40

    # 规则 4：深色模块占比偏离 50% 的程度。
    dark = sum(1 for row in matrix for v in row if v)
    percent = dark * 100 / (size * size)
    score += int(abs(percent - 50) / 5) * 10
    return score


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _select_version(payload_len: int, level: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        if _fits(payload_len, version, level):
            return version
    raise QrError(
        f"content too long: {payload_len} bytes exceeds version {MAX_VERSION} "
        f"capacity at level {level}"
    )


def _build_codewords(payload: bytes, version: int, level: str) -> list[int]:
    spec = _TABLE[(version, level)]
    buf = _BitBuffer()
    buf.append(0b0100, 4)  # 字节模式
    buf.append(len(payload), _char_count_bits(version))
    for byte in payload:
        buf.append(byte, 8)

    codewords = buf.to_codewords()
    if len(codewords) > spec.data_codewords:
        raise QrError("internal error: codeword overflow")

    pads = [0xEC, 0x11]
    i = 0
    while len(codewords) < spec.data_codewords:
        codewords.append(pads[i % 2])
        i += 1

    # 分块、逐块计算纠错码字，再按标准交错排列。
    blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for _ in range(spec.group1_blocks):
        block = codewords[pos : pos + spec.group1_data_codewords]
        pos += spec.group1_data_codewords
        blocks.append(block)
        ec_blocks.append(_rs_encode(block, spec.ec_codewords_per_block))
    for _ in range(spec.group2_blocks):
        block = codewords[pos : pos + spec.group2_data_codewords]
        pos += spec.group2_data_codewords
        blocks.append(block)
        ec_blocks.append(_rs_encode(block, spec.ec_codewords_per_block))

    result: list[int] = []
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        for block in blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(spec.ec_codewords_per_block):
        for block in ec_blocks:
            result.append(block[i])
    return result


def encode_matrix(content: str, level: str = "M", version: int | None = None) -> Matrix:
    """编码 `content`，返回最终的布尔模块矩阵。

    传入 `version` 可强制指定版本；省略时自动选择能装下的最小版本。
    """
    if level not in EC_LEVELS:
        raise QrError(f"invalid EC level {level!r}; expected one of {EC_LEVELS}")
    try:
        payload = content.encode("iso-8859-1")
    except UnicodeEncodeError as exc:
        raise QrError("content must be ISO-8859-1 encodable for byte mode") from exc

    if version is None:
        version = _select_version(len(payload), level)
    elif not 1 <= version <= MAX_VERSION:
        raise QrError(f"version must be between 1 and {MAX_VERSION}, got {version}")
    elif not _fits(len(payload), version, level):
        raise QrError(
            f"content length {len(payload)} does not fit version {version} "
            f"at level {level}"
        )

    codewords = _build_codewords(payload, version, level)
    base, reserved = _function_modules(version)

    # 顺序不能变：先填数据模块，再在数据上应用掩码（功能图形靠 `reserved`
    # 保持固定值），最后才写格式信息。
    _place_data(base, reserved, codewords)

    best: Matrix | None = None
    best_score = -1
    for mask in range(8):
        candidate = _apply_mask(base, reserved, mask)
        _place_format_info(candidate, level, mask)
        score = _penalty(candidate)
        if best is None or score < best_score:
            best, best_score = candidate, score
    assert best is not None
    return best


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------

QUIET_ZONE = 2


def render_terminal(matrix: Matrix) -> str:
    """把矩阵渲染成可以在屏幕上扫的文本。

    每个文本行编码两个模块行（``▀ ▄ █``），高度减半：版本 5 的 37 模块矩阵
    在这里是 45x23 字符，而每模块两字符的实心块写法要 82x41。

    只使用实心/半块字形 —— 字形太细的二维码（例如用 ASCII '#'）看起来形状没
    问题，但手机扫不出来。终端字体显示不了这些字符时，请在手机上打开命令行
    同时打印的登录链接，那条路径不依赖终端渲染。

    返回的每一行宽度都相同。
    """
    size = len(matrix)
    blank = " " * (QUIET_ZONE * 2 + size + QUIET_ZONE * 2)
    lines: list[str] = [blank] * QUIET_ZONE

    for r in range(0, size, 2):
        parts: list[str] = []
        for c in range(size):
            top = matrix[r][c]
            bottom = matrix[r + 1][c] if r + 1 < size else False
            if top and bottom:
                parts.append("\u2588")
            elif top:
                parts.append("\u2580")
            elif bottom:
                parts.append("\u2584")
            else:
                parts.append(" ")
        lines.append(" " * (QUIET_ZONE * 2) + "".join(parts) + " " * (QUIET_ZONE * 2))

    lines.extend([blank] * QUIET_ZONE)
    return "\n".join(lines)
