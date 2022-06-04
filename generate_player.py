import argparse
import enum
import itertools
import numpy
from typing import Iterable, List, Tuple


class Opcode(enum.Enum):
    NOP = 1
    NOP3 = 2
    STA = 3
    STAX = 4
    INC = 5
    INCX = 6
    JMP_INDIRECT = 7
    NOPNOP = 8


# Byte length of opcodes
OPCODE_LEN = {
    Opcode.NOP: 1, Opcode.NOP3: 2, Opcode.STA: 3, Opcode.STAX: 3,
    Opcode.INC: 3, Opcode.INCX: 3, Opcode.JMP_INDIRECT: 3, Opcode.NOPNOP: 2
}

ASM = {
    Opcode.NOP: "NOP", Opcode.NOP3: "STA zpdummy",
    Opcode.STA: "STA $C030", Opcode.STAX: "STA $C030,X",
    Opcode.INC: "INC $C030", Opcode.INCX: "INC $C030,X",
    Opcode.JMP_INDIRECT: "JMP (WDATA)", Opcode.NOPNOP: "NOP NOP"
}

# Applied speaker voltages resulting from executing opcode
VOLTAGES = {
    Opcode.STA: [1, 1, 1, -1],
    Opcode.INC: [1, 1, 1, -1, 1, -1],
    Opcode.INCX: [1, 1, 1, -1, 1, -1, 1],
    Opcode.STAX: [1, 1, 1, -1, 1],
    Opcode.NOP: [1, 1],
    Opcode.NOP3: [1, 1, 1],
    # TODO: support 6502 cycle counts as well
    Opcode.JMP_INDIRECT: [1, 1, 1, 1, 1, 1],
    Opcode.NOPNOP: [1, 1, 1, 1],
}


def voltage_sequence(
        opcodes: Iterable[Opcode], starting_voltage=1.0
) -> Tuple[numpy.float32, int]:
    """Voltage sequence for sequence of opcodes."""
    out = []
    v = starting_voltage
    toggles = 0
    for op in opcodes:
        for nv in v * numpy.array(VOLTAGES[op]):
            if v != nv:
                toggles += 1
            v = nv
            out.append(v)
    return tuple(numpy.array(out, dtype=numpy.float32)), toggles


def all_opcodes(
        max_len: int, opcodes: Iterable[Opcode], start_opcodes: Iterable[int]
) -> Iterable[Tuple[Opcode]]:
    """Enumerate all combinations of opcodes up to max_len cycles"""
    num_opcodes = 0
    while True:
        found_one = False
        for ops in itertools.product(opcodes, repeat=num_opcodes):
            ops = tuple(list(ops) + [Opcode.JMP_INDIRECT])
            if ops[0] not in start_opcodes:
                continue
            if sum(len(VOLTAGES[o]) for o in ops) <= max_len:
                found_one = True
                yield ops
        if not found_one:
            break
        num_opcodes += 1


import itertools


def _make_end_of_frame_voltages(skip) -> numpy.ndarray:
    """Voltage sequence for end-of-frame TCP processing."""
    length = 160  # 4 + 14 * 10 + 6
    # Always start with a STA $C030
    c = []  # numpy.full(length, 1.0, dtype=numpy.float32)
    voltage_high = True
    # toggles = 0
    for skip_cycles in itertools.cycle(skip):
        if len(c) + skip_cycles < length:
            c.extend([1.0 if voltage_high else -1.0] * (skip_cycles - 1))
        else:
            c.extend([1.0 if voltage_high else -1.0] * (length - len(c)))
            break
        voltage_high = not voltage_high
        c.append(1.0 if voltage_high else -1.0)
        # # toggles += 1
        # for j in range(3 + 10 * i + skip , min(length, 3 + 10 * (i + 1) +
        #                                                skip)):
        #     c[j] = 1.0 if voltage_high else -1.0
    return numpy.array(c[:length], dtype=numpy.float32)
    # return c

# These are duty cycles
eof_cycles = [
    # (16,6),
    # (14,6),
    # (12,8),  # -0.15
    # (14, 10),  # -0.10
    # (12,10),  # -0.05
    (4, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 6),  # 0.0
    # (10, 8, 10, 10, 10, 8),  # 0.05
    # (12, 10, 12, 8, 10, 10),  # 0.1
    # (4, 10, 8, 10, 8, 10, 8, 10, 8, 10, 8, 10, 8, 10, 8, 10, 8, 10, 6),  # 0.15
    # (10, 6, 12, 6),  # 0.20
    # (10, 4),  # 0.25
    # (14, 4, 10, 6),  # 0.30
    # (12, 4),  # 0.35
    # (14, 4),  # 0.40
]


def generate_player(player_ops: List[Tuple[Opcode]], opcode_filename: str,
                    player_filename: str):
    num_bytes = 0
    seqs = {}
    num_op = 0
    offset = 0
    unique_opcodes = {}
    toggles = {}
    done = False
    with open(player_filename, "w+") as f:
        for i, k in enumerate(player_ops):
            new_unique = []
            player_op = []
            player_op_len = 0

            new_offset = offset
            for j, o in enumerate(k):
                seq, tog = voltage_sequence(k[j:], 1.0)
                dup = seqs.setdefault(seq, i)
                if dup == i:
                    player_op.append("tick_%02x: ; voltages %s" % (
                        new_offset, seq))
                    new_unique.append((new_offset, seq, tog))
                player_op.append("  %s" % ASM[o])
                player_op_len += OPCODE_LEN[o]
                new_offset += OPCODE_LEN[o]
            player_op.append("\n")

            # If at least one of the partial opcode sequences was not
            # a dup, then add it to the player
            if new_unique:
                # Reserve 9 bytes for END_OF_FRAME and EXIT
                if (num_bytes + player_op_len) > (256 - 9):
                    print("Out of space, truncating.")
                    break
                num_op += 1
                f.write("\n".join(player_op))
                num_bytes += player_op_len
                for op_offset, seq, tog in new_unique:
                    unique_opcodes[op_offset] = seq
                    toggles[op_offset] = tog
                offset = new_offset

        f.write("; %d bytes\n" % num_bytes)

    with open(opcode_filename, "w") as f:
        f.write("import enum\nimport numpy\n\n\n")
        f.write("class Opcode(enum.Enum):\n")
        for o in unique_opcodes.keys():
            f.write("    TICK_%02x = 0x%02x\n" % (o, o))
        f.write("    EXIT = 0x%02x\n" % num_bytes)
        # f.write("    END_OF_FRAME = 0x%02x\n" % (num_bytes + 3))
        for i, _ in enumerate(eof_cycles):
            f.write("    END_OF_FRAME_%d = 0x%02x\n" % (i, num_bytes + 4 + i))

        f.write("\n\nVOLTAGE_SCHEDULE = {\n")
        for o, v in unique_opcodes.items():
            f.write(
                "    Opcode.TICK_%02x: numpy.array(%s, dtype=numpy.float32),"
                "\n" % (o, v))
        for i, skip_cycles in enumerate(eof_cycles):
            f.write("    Opcode.END_OF_FRAME_%d: numpy.array([%s], "
                    "dtype=numpy.float32),\n" % (i, ", ".join(
                str(f) for f in _make_end_of_frame_voltages(
                    skip_cycles))))
        f.write("}\n")

        f.write("\n\nTOGGLES = {\n")
        for o, v in toggles.items():
            f.write(
                "    Opcode.TICK_%02x: %d,\n" % (o, v)
            )
        f.write("}\n")

        f.write("\n\nEOF_OPCODES = (\n")
        for i in range(len(eof_cycles)):
            f.write("    Opcode.END_OF_FRAME_%d,\n" % i)
        f.write(")\n")


def all_opcode_combinations(
        max_cycles: int, opcodes: Iterable[Opcode], start_opcodes: List[int]
) -> List[Tuple[Opcode]]:
    return sorted(
        list(all_opcodes(max_cycles, opcodes, start_opcodes)),
        key=lambda o: len(o), reverse=True)


def sort_by_opcode_count(
        player_opcodes: List[Tuple[Opcode]], count_opcodes: List[int]
) -> List[Tuple[Opcode]]:
    return sorted(
        player_opcodes, key=lambda ops: sum(o in count_opcodes for o in ops),
        reverse=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_cycles", type=int, required=True,
                        help="Maximum cycle length of player opcodes")
    parser.add_argument("opcodes", nargs="+",
                        choices=Opcode.__members__.keys(),
                        help="6502 opcodes to use when generating player "
                             "opcodes")
    args = parser.parse_args()

    opcodes = set(Opcode.__members__[op] for op in args.opcodes)
    # TODO: use Opcode instead of int values
    non_nops = [Opcode.STA, Opcode.INC, Opcode.INCX, Opcode.STAX,
                Opcode.JMP_INDIRECT]

    player_ops = sort_by_opcode_count(all_opcode_combinations(
        max_cycles=args.max_cycles, opcodes=opcodes, start_opcodes=non_nops),
        non_nops)
    generate_player(
        player_ops,
        opcode_filename="opcodes_generated.py",
        player_filename="player/player_generated.s"
    )


if __name__ == "__main__":
    main()
