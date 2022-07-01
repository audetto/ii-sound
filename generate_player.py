import itertools
import numpy
from typing import Iterable, List, Tuple

import opcodes_6502


def duty_cycle_range():
    cycles = []
    for i in range(4, 42):
        if i == 5:
            # No single-cycle instructions
            # XXX can we use a 5-cycle instruction that touches $C030 only on
            # last cycle?
            continue
        cycles.append(i)

    return cycles


def eof_trampoline_stage1(cycles):
    ops = [
        opcodes_6502.Literal(
            "eof_trampoline_%d:" % cycles, indent=0
        ),
        opcodes_6502.STA_C030,
    ]
    if cycles == 4:
        return ops + [
            opcodes_6502.STA_C030,
            opcodes_6502.Opcode(3, 3, "JMP eof_trampoline_%d_stage2" % cycles)
        ]
    if cycles == 5:
        return None
    if cycles == 6:
        return ops + [
            opcodes_6502.Opcode(2, 1, "NOP"),
            opcodes_6502.STA_C030,
            opcodes_6502.Opcode(3, 3, "JMP eof_trampoline_%d_stage2" % cycles)
        ]
    if cycles == 8:
        return ops + [
            opcodes_6502.Opcode(2, 1, "NOP"),
            opcodes_6502.Opcode(2, 1, "NOP"),
            opcodes_6502.STA_C030,
            opcodes_6502.Opcode(3, 3, "JMP eof_trampoline_%d_stage2" % cycles)
        ]
    return ops + [
        opcodes_6502.Opcode(3, 3, "JMP eof_trampoline_%d_stage2" % cycles)
    ]


EOF_TRAMPOLINE_STAGE1 = {
    a: eof_trampoline_stage1(a) for a in duty_cycle_range()
}


def eof_trampoline_stage2(cycles) -> List[opcodes_6502.Opcode]:
    label: List[opcodes_6502.Opcode] = [
        opcodes_6502.Literal(
            "eof_trampoline_%d_stage2:" % cycles, indent=0
        )
    ]

    ops = [
        opcodes_6502.Opcode(4, 3, "LDA WDATA"),
        opcodes_6502.Opcode(4, 3, "STA @0+1"),
        opcodes_6502.Literal("@0:", indent=0),
        opcodes_6502.Opcode(
            6, 3, "JMP (eof_trampoline_%d_stage3_page)" % cycles)
    ]
    if cycles < 7 or cycles == 8:
        return label + ops

    # For cycles == 7 or > 8 we need to interleave a STA $C030 into stage 2
    # because we couldn't fit it in stage 1
    interleave_ops = [
        opcodes_6502.padding(cycles - 7),
        opcodes_6502.STA_C030,
        opcodes_6502.padding(100)
    ]
    return label + list(opcodes_6502.interleave_opcodes(interleave_ops, ops))


EOF_TRAMPOLINE_STAGE2 = {
    a: eof_trampoline_stage2(a) for a in duty_cycle_range()
}


def cycles_after_tick(ops: Iterable[opcodes_6502.Opcode]) -> int:
    cycles = 0
    ticks = 0
    for op in ops:
        cycles += op.cycles
        if op.toggle:
            cycles = 0
            ticks += 1
    return cycles, ticks


STAGE1_CYCLES_AFTER_TICK = {
    a: cycles_after_tick(EOF_TRAMPOLINE_STAGE1[a]) for a in duty_cycle_range()
}

STAGE2_CYCLES_AFTER_TICK = {
    a: cycles_after_tick(EOF_TRAMPOLINE_STAGE2[a]) for a in duty_cycle_range()
}


def _duty_cycles(duty_cycles):
    # The player sequence for periods of silence (i.e. 0-valued waveform)
    # is a sequence of 10 cycle ticks, so we need to support maintaining
    # this during EOF in order to avoid introducing noise during such periods.
    res = {0.0: [(20, 10, 10)]}

    for i in duty_cycles:
        for j in duty_cycles:
            # We only need to worry about i < j because we can effectively
            # obtain the opposite cadence by inserting an extra half duty cycle
            # before the EOF
            # XXX try again removing this, we have space
            if j <= i:
                continue

            # Limit to min 22Khz carrier
            if (i + j) > 45:
                continue

            # When first duty cycle is small enough to fit in the stage 1
            # trampoline, we can't fit the second duty cycle in the stage 2
            # trampoline because we'd need too may stage 1 variants to fit in
            # page 3.  That sets a lower bound on the second duty cycle.
            #
            # e.g.
            #
            # eof_trampoline_4:
            #     STA $C030 ; 4 cycles
            #     STA $C030 ; 4 cycles
            #     JMP eof_trampoline_4_stage2 ; 3 cycles
            #
            # eof_trampoline_4_stage2:
            #     LDA WDATA ; 4
            #     STA @0+1 ; 4
            # @0: JMP (xxyy) ; 6
            #
            # eof_trampoline_4_b_stage3:
            #     ; second duty cycle must land here, i.e the earliest it can
            #     ; be is 3 + 4 + 4 + 6 + 4 = 21 cycles
            if i in {4, 6, 8}:
                if j < 21:
                    continue
            else:
                # stage 1 is STA $C030; JMP stage_2
                stage_1_cycles, stage_1_ticks = STAGE1_CYCLES_AFTER_TICK[i]
                assert (stage_1_cycles, stage_1_ticks) == (3, 1)

                stage_2_cycles, stage_2_ticks = STAGE2_CYCLES_AFTER_TICK[i]
                # the earliest the second duty cycle can complete is a
                # STA $c030 at the beginning of stage 3
                if stage_2_ticks:
                    min_cycles = stage_2_cycles + 4
                else:
                    min_cycles = stage_1_cycles + stage_2_cycles + 4

                if j < min_cycles:
                    continue

            duty = j / (i + j) * 2 - 1
            res.setdefault(duty, []).append((i + j, i, j))

    cycles = []
    for c in sorted(list(res.keys())):
        pair = sorted(res[c], reverse=False)[0][1:]
        cycles.append(pair)
        print(c, pair)

    print(len(cycles))

    return sorted(cycles, key=lambda p: p[0] + p[1])


EOF_DUTY_CYCLES = _duty_cycles(duty_cycle_range())


def eof_trampoline_stage3_page_offsets(duty_cycles):
    second_cycles = {}
    for a, b in sorted(duty_cycles):
        second_cycles.setdefault(a, []).append(b)

    # bin-pack the (a, b) duty cycles into pages so we can set up indirect
    # jump tables to dispatch the third stage trampoline.  A greedy algorithm
    # works fine here
    pages = []
    page = []
    longest_first_cycles = sorted(
        list(second_cycles.items()), key=lambda c: len(c[1]), reverse=True)
    left = len(longest_first_cycles)
    while left:
        for i, cycles in enumerate(longest_first_cycles):
            if cycles is None:
                continue
            cycle1, cycles2 = cycles
            if len(page) < (128 - len(cycles2)):
                page.extend((cycle1, cycle2) for cycle2 in cycles2)
                longest_first_cycles[i] = None
                left -= 1
        pages.append(page)
        page = []

    page_offsets = {}
    for page_idx, page in enumerate(pages):
        offset = 0
        for a, b in page:
            offset += 2
            page_offsets[(a, b)] = (page_idx, offset)

    return page_offsets


EOF_TRAMPOLINE_STAGE3_PAGE_OFFSETS = eof_trampoline_stage3_page_offsets(
    EOF_DUTY_CYCLES)
print(EOF_TRAMPOLINE_STAGE3_PAGE_OFFSETS)

EOF_STAGE_3_BASE = [
    opcodes_6502.Literal(
        "; We've read exactly 2KB from the socket buffer.  Before continuing "
        "we need to ACK this read,"),
    opcodes_6502.Literal(
        "; and make sure there's at least another 2KB in the buffer."),
    opcodes_6502.Literal(";"),
    opcodes_6502.Literal(
        "; Save the W5100 address pointer so we can continue reading the "
        "socket buffer once we are done."),
    opcodes_6502.Literal(
        "; We know the low-order byte is 0 because Socket RX memory is "
        "page-aligned and so is 2K frame."),
    opcodes_6502.Literal(
        "; IMPORTANT - from now on until we restore this below, we can't "
        "trash the Y register!"),
    opcodes_6502.Opcode(4, 4, "LDY WADRH"),
    opcodes_6502.Literal("; Update new Received Read pointer."),
    opcodes_6502.Literal(";"),
    opcodes_6502.Literal(
        "; We know we have received exactly 2KB, so we don't need to read the "
        "current value from the"),
    opcodes_6502.Literal(
        "; hardware.  We can track it ourselves instead, which saves a "
        "few cycles."),
    opcodes_6502.Opcode(2, 2, "LDA #>S0RXRD"),
    opcodes_6502.Opcode(4, 3, "STA WADRH"),
    opcodes_6502.Opcode(2, 2, "LDA #<S0RXRD"),
    opcodes_6502.Opcode(4, 3, "STA WADRL"),
    opcodes_6502.Opcode(4, 3,
                        "LDA RXRD ; TODO: in principle we could update RXRD outside of "
                        "the EOF path"),
    opcodes_6502.Opcode(2, 1, "CLC"),
    opcodes_6502.Opcode(2, 2, "ADC #$08"),
    opcodes_6502.Opcode(4, 3,
                        "STA WDATA ; Store new high byte of received read pointer"),
    opcodes_6502.Opcode(4, 3, "STA RXRD ; Save for next time"),
    opcodes_6502.Literal("; Send the Receive command"),
    opcodes_6502.Opcode(2, 2, "LDA #<S0CR"),
    opcodes_6502.Opcode(4, 3, "STA WADRL"),
    opcodes_6502.Opcode(2, 2, "LDA #SCRECV"),
    opcodes_6502.Opcode(4, 3, "STA WDATA"),
    opcodes_6502.Literal(
        "; Make sure we have at least 2KB more in the socket buffer so we can "
        "start another frame."
    ),
    opcodes_6502.Opcode(2, 2, "LDA #$07"),
    opcodes_6502.Opcode(2, 2, "LDX #<S0RXRSR ; Socket 0 Received Size "
                              "register"),
    opcodes_6502.Literal(
        "; we might loop an unknown number of times here waiting for data but "
        "the default should be to"),
    opcodes_6502.Literal("; fall straight through"),
    opcodes_6502.Literal("@0:", indent=0),
    opcodes_6502.Opcode(4, 3, "STX WADRL"),
    opcodes_6502.Opcode(4, 3, "CMP WDATA ; High byte of received size"),
    opcodes_6502.Opcode(2, 2,
                        "BCS @0 ; 2 cycles in common case when there is already sufficient "
                        "data waiting."),
    opcodes_6502.Literal(
        "; We're good to go for another frame.  Restore W5100 address pointer "
        "where we last found it, to"),
    opcodes_6502.Literal(
        "; begin iterating through the next 2KB of the socket buffer."),
    opcodes_6502.Literal(";"),
    opcodes_6502.Literal(
        "; It turns out that the W5100 automatically wraps the address pointer "
        "at the end of the 8K"),
    opcodes_6502.Literal(
        "; RX/TX buffers.  Since we're using an 8K socket, that means we don't "
        "have to do any work to"),
    opcodes_6502.Literal("; manage the read pointer!"),
    opcodes_6502.Opcode(4, 3, "STY WADRH"),
    opcodes_6502.Opcode(2, 2, "LDA #$00"),
    opcodes_6502.Opcode(4, 3, "STA WADRL"),
    opcodes_6502.Opcode(6, 3, "JMP (WDATA)"),
]


def _make_end_of_frame_voltages2(cycles) -> numpy.ndarray:
    """Voltage sequence for end-of-frame TCP processing."""
    max_len = 140
    voltage_high = False
    c = [1.0, 1.0, 1.0, -1.0]  # STA $C030
    for i, skip_cycles in enumerate(itertools.cycle(cycles)):
        c.extend([1.0 if voltage_high else -1.0] * (skip_cycles - 1))
        voltage_high = not voltage_high
        c.append(1.0 if voltage_high else -1.0)
        if len(c) >= max_len:
            break
    c.extend([1.0 if voltage_high else -1.0] * 6)  # JMP (WDATA)
    return numpy.array(c, dtype=numpy.float32)


def audio_opcodes() -> Iterable[opcodes_6502.Opcode]:
    # These two basic sequences let us chain together STA $C030 with any number
    # >= 10 of intervening cycles (except 11).  We don't need to explicitly
    # include 6 or more cycles of NOP because those can be obtained by chaining
    # together JMP (WDATA) to itself
    #
    # XXX support 11 cycles explicitly?
    yield tuple(
        [nop for nop in opcodes_6502.nops(4)] + [
            opcodes_6502.STA_C030, opcodes_6502.JMP_WDATA])

    yield tuple(
        [nop for nop in opcodes_6502.nops(4)] + [
            opcodes_6502.Opcode(3, 2, "STA zpdummy"),
            opcodes_6502.STA_C030, opcodes_6502.JMP_WDATA])


def generate_player(
        player_ops: Iterable[opcodes_6502.Opcode],
        opcode_filename: str,
        player_stage1_filename: str,
        player_stage2_filename: str
):
    num_bytes = 0
    seen_op_suffix_toggles = set()
    offset = 0
    unique_entrypoints = {}
    toggles = {}

    with open(player_stage1_filename, "w+") as f:
        for i, ops in enumerate(player_ops):
            player_op = []
            for j, op in enumerate(ops):
                op_suffix_toggles = opcodes_6502.toggles(ops[j:])
                if op_suffix_toggles not in seen_op_suffix_toggles:
                    # new subsequence
                    seen_op_suffix_toggles.add(op_suffix_toggles)
                    player_op.append(
                        opcodes_6502.Literal(
                            "tick_%02x: ; voltages %s" % (
                                offset, op_suffix_toggles), indent=0))
                    unique_entrypoints[offset] = op_suffix_toggles
                player_op.append(op)
                offset += op.bytes

            assert unique_entrypoints
            player_op_len = opcodes_6502.total_bytes(player_op)
            # Make sure we reserve 9 bytes for END_OF_FRAME and EXIT
            assert (num_bytes + player_op_len) <= (256 - 9)

            for op in player_op:
                f.write("%s\n" % str(op))

            num_bytes += player_op_len
            f.write("\n")

        duty_cycle_first = sorted(list(set(dc[0] for dc in EOF_DUTY_CYCLES)))
        for eof_stage1_cycles in duty_cycle_first:
            eof_stage1_ops = EOF_TRAMPOLINE_STAGE1[eof_stage1_cycles]
            if not eof_stage1_ops:
                continue

            for op in eof_stage1_ops:
                f.write("%s\n" % str(op))
            f.write("\n")

            num_bytes += opcodes_6502.total_bytes(eof_stage1_ops)

        f.write("; %d entrypoints, %d bytes\n" % (
            len(unique_entrypoints), num_bytes))

    # XXX if we're spilling the STA $C030 onto stage 2 then we can accommodate
    # lower values for b
    # it's only the ones where we have the STA $C030 on stage 1 that we have
    # a lower bound of b >= 14 cycles

    with open(player_stage2_filename, "w+") as f:

        # We bin pack each (a, b) duty cycle onto the same jump table page
        pages_by_first_duty_cycle = {}
        for ab, po in EOF_TRAMPOLINE_STAGE3_PAGE_OFFSETS.items():
            pages_by_first_duty_cycle[ab[0]] = po[0]

        for eof_stage1_cycles in duty_cycle_first:
            page = pages_by_first_duty_cycle[eof_stage1_cycles]
            eof_stage2_ops = eof_trampoline_stage2(eof_stage1_cycles, page)
            if not eof_stage2_ops:
                continue

            for op in eof_stage2_ops:
                f.write("%s\n" % str(op))
            f.write("\n")

    with open(opcode_filename, "w") as f:
        f.write("import enum\nimport numpy\n\n\n")
        f.write("class Opcode(enum.Enum):\n")
        for o in unique_entrypoints.keys():
            f.write("    TICK_%02x = 0x%02x\n" % (o, o))
        f.write("    EXIT = 0x%02x\n" % num_bytes)
        # f.write("    END_OF_FRAME = 0x%02x\n" % (num_bytes + 3))
        for i, _ in enumerate(EOF_DUTY_CYCLES):
            f.write(
                "    END_OF_FRAME_%d = 0x%02x\n" % (i, num_bytes + 4 + i))
        f.write("\n\nVOLTAGE_SCHEDULE = {\n")
        for o, v in unique_entrypoints.items():
            f.write(
                "    Opcode.TICK_%02x: numpy.array(%s, dtype=numpy.float32),"
                "\n" % (o, v))
        for i, skip_cycles in enumerate(EOF_DUTY_CYCLES):
            f.write("    Opcode.END_OF_FRAME_%d: numpy.array([%s], "
                    "dtype=numpy.float32),  # %s\n" % (i, ", ".join(
                str(f) for f in _make_end_of_frame_voltages2(
                    skip_cycles)), skip_cycles))
        f.write("}\n")
        #
        #     f.write("\n\nTOGGLES = {\n")
        #     for o, v in toggles.items():
        #         f.write(
        #             "    Opcode.TICK_%02x: %d,\n" % (o, v)
        #         )
        #     f.write("}\n")
        #
        f.write("\n\nEOF_OPCODES = (\n")
        for i in range(len(EOF_DUTY_CYCLES)):
            f.write("    Opcode.END_OF_FRAME_%d,\n" % i)
        f.write(")\n")


def main():
    player_ops = audio_opcodes()
    generate_player(
        player_ops,
        opcode_filename="opcodes_generated.py",
        player_stage1_filename="player/player_generated.s",
        player_stage2_filename="player/player_stage2_generated.s"
    )


if __name__ == "__main__":
    main()
