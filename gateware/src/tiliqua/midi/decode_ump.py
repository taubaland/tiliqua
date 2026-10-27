# Copyright (c) 2026 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""Universal MIDI Packet (UMP, MIDI 2.0) decoding."""

from amaranth import *
from amaranth.lib import enum, stream, wiring
from amaranth.lib.wiring import In, Out

from ..dsp.stream_util import SyncFIFOBuffered as StreamFIFO
from .types import *

class UMPMessageType(enum.Enum, shape=unsigned(4)):
    UTILITY             = 0x0
    SYSTEM              = 0x1
    MIDI1_CHANNEL_VOICE = 0x2
    DATA_64             = 0x3
    MIDI2_CHANNEL_VOICE = 0x4
    DATA_128            = 0x5
    FLEX_DATA           = 0xD
    UMP_STREAM          = 0xF

# Number of 32-bit words in a UMP, indexed by message type.
UMP_WORDS = [1, 1, 1, 2, 2, 4, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4]

def upscale(value, src_bits, dst_bits):
    """
    Scale an unsigned :py:`src_bits`-wide value up to :py:`dst_bits`, using the
    'Min-Center-Max' scheme from the MIDI 2.0 specification. Minimum, center
    and maximum values are preserved (e.g. 7-bit 0x40 -> 16-bit 0x8000 and
    7-bit 0x7F -> 16-bit 0xFFFF), with the bits above the center repeated
    into the expanded LSBs. Only constant shifts, so this is cheap in logic.
    """
    value = value[:src_bits]
    scale_bits = dst_bits - src_bits
    shifted = (value << scale_bits)[:dst_bits]
    repeat_bits = src_bits - 1
    repeat_value = value[:repeat_bits]
    fill = C(0, dst_bits)
    shift = scale_bits - repeat_bits
    while repeat_bits + shift > 0:
        term = repeat_value << shift if shift >= 0 else repeat_value >> -shift
        fill = fill | term[:dst_bits]
        shift -= repeat_bits
    return Mux(value <= (1 << (src_bits - 1)), shifted, shifted | fill)

class MidiDecodeUMP(wiring.Component):

    """
    Parse a stream of 32-bit UMP words into structured :py:`Midi2Message`.

    MIDI 2.0 Channel Voice messages (message type 0x4) are forwarded as-is. MIDI 1.0
    Channel Voice messages (message type 0x2) are translated to MIDI 2.0 Channel Voice
    messages, with values upscaled as recommended by the MIDI 2.0 specification
    (and a MIDI 1.0 NOTE_ON with velocity 0 becomes a NOTE_OFF). This means
    downstream logic only needs to handle one (high resolution) message format.

    Real-time messages can optionally be forwarded on :py:`o_rt`. All other
    message types (SysEx, flex data, stream messages...) are drained.

    When :py:`group_filter` is set to an integer (0-15), only messages in that
    UMP group are processed.

    Warn: backpressure on ``o_rt`` is ignored!
    """

    def __init__(self, forward_rt=False, group_filter=None):
        self.forward_rt = forward_rt
        self.group_filter = group_filter
        sig = {
            "i": In(stream.Signature(unsigned(32))),
            "o": Out(stream.Signature(Midi2Message)),
        }
        if forward_rt:
            sig["o_rt"] = Out(stream.Signature(Status.RT))
        super().__init__(sig)

    def elaborate(self, platform):
        m = Module()

        i_word = self.i.payload
        i_mt = UMPMessageType(i_word[28:32])
        i_group = i_word[24:28]
        i_status = i_word[16:24]
        group_ok = Const(1) if self.group_filter is None else (i_group == self.group_filter)

        word0 = Signal(32)
        words_remaining = Signal(range(4))

        if self.forward_rt:
            # small fifo, rt stream does not support backpressure
            m.submodules.rt_fifo = rt_fifo = StreamFIFO(shape=Status.RT, depth=4)
            wiring.connect(m, rt_fifo.o, wiring.flipped(self.o_rt))

        # MIDI 1.0 channel voice (message type 0x2) -> MIDI 2.0 channel voice
        midi1 = Signal(Midi2Message)
        d1, d2 = i_word[8:15], i_word[0:7]
        m.d.comb += [
            midi1.group.eq(i_group),
            midi1.channel.eq(i_word[16:20]),
            midi1.opcode.eq(i_word[20:24]),
        ]
        with m.Switch(i_word[20:24]):
            with m.Case(Midi2Opcode.NOTE_OFF, Midi2Opcode.NOTE_ON):
                m.d.comb += [
                    midi1.index.note.note.eq(d1),
                    midi1.data.note.velocity.eq(upscale(d2, 7, 16)),
                ]
                with m.If(d2 == 0):
                    m.d.comb += midi1.opcode.eq(Midi2Opcode.NOTE_OFF)
            with m.Case(Midi2Opcode.POLY_PRESSURE):
                m.d.comb += [
                    midi1.index.per_note.note.eq(d1),
                    midi1.data.raw.eq(upscale(d2, 7, 32)),
                ]
            with m.Case(Midi2Opcode.CONTROL_CHANGE):
                m.d.comb += [
                    midi1.index.control.index.eq(d1),
                    midi1.data.raw.eq(upscale(d2, 7, 32)),
                ]
            with m.Case(Midi2Opcode.PROGRAM_CHANGE):
                m.d.comb += midi1.data.program.program.eq(d1)
            with m.Case(Midi2Opcode.CHANNEL_PRESSURE):
                m.d.comb += midi1.data.raw.eq(upscale(d1, 7, 32))
            with m.Case(Midi2Opcode.PITCH_BEND):
                m.d.comb += midi1.data.raw.eq(upscale(Cat(d1, d2), 14, 32))
        midi1_valid = i_word[23] & (i_word[20:24] != 0xF)

        with m.FSM() as fsm:

            with m.State('IDLE'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += word0.eq(i_word)
                    with m.Switch(i_mt):
                        with m.Case(UMPMessageType.SYSTEM):
                            if self.forward_rt:
                                with m.If(group_ok & (i_status >= 0xF8)):
                                    m.d.comb += [
                                        rt_fifo.i.payload.eq(i_status[0:3]),
                                        rt_fifo.i.valid.eq(1),
                                    ]
                        with m.Case(UMPMessageType.MIDI1_CHANNEL_VOICE):
                            with m.If(group_ok & midi1_valid):
                                m.d.sync += self.o.payload.eq(midi1)
                                m.next = 'WAIT-READY'
                        with m.Case(UMPMessageType.MIDI2_CHANNEL_VOICE):
                            with m.If(group_ok):
                                m.next = 'READ1'
                            with m.Else():
                                m.d.sync += words_remaining.eq(1)
                                m.next = 'DRAIN'
                        for mt, n_words in enumerate(UMP_WORDS):
                            if n_words > 1 and mt != UMPMessageType.MIDI2_CHANNEL_VOICE.value:
                                with m.Case(mt):
                                    m.d.sync += words_remaining.eq(n_words - 1)
                                    m.next = 'DRAIN'

            with m.State('READ1'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += self.o.payload.eq(Cat(i_word, word0[0:28]))
                    m.next = 'WAIT-READY'

            with m.State('DRAIN'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += words_remaining.eq(words_remaining - 1)
                    with m.If(words_remaining == 1):
                        m.next = 'IDLE'

            with m.State('WAIT-READY'):
                m.d.comb += self.o.valid.eq(1)
                with m.If(self.o.ready):
                    m.next = 'IDLE'

        return m
