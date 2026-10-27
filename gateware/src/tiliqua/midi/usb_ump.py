# Copyright (c) 2026 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""
USB <-> Universal MIDI Packet (UMP) helpers, shared by USB MIDI host and device.

Nothing in here depends on a particular USB stack.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

class USBMIDI1ToUMP(wiring.Component):

    """
    Translate a (little-endian) 4-byte USB-MIDI 1.0 event into a single UMP word.

    Channel voice messages become UMP message type 0x2 (MIDI 1.0 channel voice)
    and system common / real-time messages become message type 0x1. The USB-MIDI
    cable number is mapped to the UMP group. Anything else (i.e. SysEx) has no
    single-word UMP equivalent, so ``valid`` is deasserted and it is dropped.
    """

    i: In(unsigned(32))
    o: Out(unsigned(32))
    valid: Out(unsigned(1))

    def elaborate(self, platform):
        m = Module()
        cable = self.i[4:8]
        cin   = self.i[0:4]
        msg   = self.i[8:32]     # status, data1, data2 (in transmission order)
        status, d1, d2 = msg[0:8], msg[8:16], msg[16:24]
        with m.Switch(cin):
            with m.Case(*range(0x8, 0xF)):
                # MIDI 1.0 channel voice
                m.d.comb += [
                    self.o.eq(Cat(d2, d1, status, cable, Const(0x2, 4))),
                    self.valid.eq(1),
                ]
            with m.Case(0x2, 0x3, 0xF):
                # System common (2/3 bytes) or single byte (only forward if a status byte,
                # this is where all real-time messages end up).
                m.d.comb += [
                    self.o.eq(Cat(d2, d1, status, cable, Const(0x1, 4))),
                    self.valid.eq(status[7]),
                ]
        return m

#
# Stream helpers between USB packet bytes, UMP words and MIDI 1.0 messages
# (used by the USB MIDI 2.0 device engine).
#

from amaranth.lib import stream

from luna.gateware.stream.future import Packet

from .decode_ump import UMP_WORDS
from .types import *

class BytesToWords(wiring.Component):

    """
    Pack a stream of USB packet bytes into little-endian 32-bit words.

    Both USB-MIDI 1.0 events and USB MIDI 2.0 UMP words are sent least
    significant byte first, and USB packets always carry whole words, so
    the byte position is resynchronized at the start of every packet.
    """

    i: In(stream.Signature(Packet(unsigned(8))))
    o: Out(stream.Signature(unsigned(32)))

    def elaborate(self, platform):
        m = Module()

        word = Signal(24)
        byte_ix = Signal(2)
        ix = Mux(self.i.payload.first, 0, byte_ix)

        m.d.comb += [
            self.o.payload.eq(Cat(word, self.i.payload.data)),
            self.o.valid.eq(self.i.valid & (ix == 3)),
            self.i.ready.eq(Mux(ix == 3, self.o.ready, 1)),
        ]

        with m.If(self.i.valid & self.i.ready):
            m.d.sync += byte_ix.eq(ix + 1)
            with m.Switch(ix):
                for n in range(3):
                    with m.Case(n):
                        m.d.sync += word[n*8:(n+1)*8].eq(self.i.payload.data)

        return m

class WordsToBytes(wiring.Component):

    """
    Serialize a stream of UMP words into little-endian USB packet bytes.

    ``last`` is set on the final byte of every UMP, so each UMP is sent
    to the host as soon as possible (as a short USB packet).
    """

    i: In(stream.Signature(unsigned(32)))
    o: Out(stream.Signature(Packet(unsigned(8))))

    def elaborate(self, platform):
        m = Module()

        byte_ix = Signal(2)
        words_remaining = Signal(range(4))
        ump_words = Array(Const(n - 1, 2) for n in UMP_WORDS)
        is_head = Signal()
        m.d.comb += is_head.eq(words_remaining == 0)

        # Words remaining in this UMP *after* the current one.
        remaining_after = Signal(2)
        m.d.comb += remaining_after.eq(
            Mux(is_head, ump_words[self.i.payload[28:32]], words_remaining - 1))

        m.d.comb += [
            self.o.valid.eq(self.i.valid),
            self.o.payload.data.eq(self.i.payload.word_select(byte_ix, 8)),
            self.o.payload.first.eq(is_head & (byte_ix == 0)),
            self.o.payload.last.eq((remaining_after == 0) & (byte_ix == 3)),
            self.i.ready.eq(self.o.ready & (byte_ix == 3)),
        ]

        with m.If(self.o.valid & self.o.ready):
            m.d.sync += byte_ix.eq(byte_ix + 1)
            with m.If(byte_ix == 3):
                m.d.sync += words_remaining.eq(remaining_after)

        return m

class UMPStreamSplit(wiring.Component):

    """
    Split UMP Stream messages (message type 0xF, e.g. Endpoint Discovery and
    Function Block Discovery) off a stream of UMP words.

    All 4 words of every UMP Stream message go to :py:`o_stream`, all other
    UMPs are forwarded on :py:`o` (e.g. to :py:`MidiDecodeUMP`).
    """

    i: In(stream.Signature(unsigned(32)))
    o: Out(stream.Signature(unsigned(32)))
    o_stream: Out(stream.Signature(unsigned(32)))

    def elaborate(self, platform):
        m = Module()

        words_remaining = Signal(range(4))
        in_stream_msg = Signal()
        ump_words = Array(Const(n - 1, 2) for n in UMP_WORDS)

        is_head = Signal()
        to_stream = Signal()
        i_mt = self.i.payload[28:32]
        m.d.comb += [
            is_head.eq(words_remaining == 0),
            to_stream.eq(Mux(is_head, i_mt == 0xF, in_stream_msg)),
            self.o.payload.eq(self.i.payload),
            self.o_stream.payload.eq(self.i.payload),
        ]

        with m.If(to_stream):
            m.d.comb += [
                self.o_stream.valid.eq(self.i.valid),
                self.i.ready.eq(self.o_stream.ready),
            ]
        with m.Else():
            m.d.comb += [
                self.o.valid.eq(self.i.valid),
                self.i.ready.eq(self.o.ready),
            ]

        with m.If(self.i.valid & self.i.ready):
            with m.If(is_head):
                m.d.sync += [
                    words_remaining.eq(ump_words[i_mt]),
                    in_stream_msg.eq(i_mt == 0xF),
                ]
            with m.Else():
                m.d.sync += words_remaining.eq(words_remaining - 1)

        return m

class Midi2ToMidi1(wiring.Component):

    """
    Translate :py:`Midi2Message` (MIDI 2.0 channel voice) back to a MIDI 1.0
    :py:`MidiMessage`, for downstream logic that only works at MIDI 1.0 resolution.

    Values are truncated to their most significant bits (velocity 16 -> 7 bits,
    controllers 32 -> 7 bits, pitch bend 32 -> 14 bits), which is the exact
    inverse of the 'Min-Center-Max' upscaling recommended by the MIDI 2.0
    specification. That is, a 7-bit value upscaled by a MIDI 2.0 sender (or by
    :py:`MidiDecodeUMP`) comes back out of this translation unchanged.

    A MIDI 2.0 NOTE_ON never means NOTE_OFF, so NOTE_ON velocities that
    truncate to 0 are sent as 1 (as recommended by the MIDI 2.0 specification).
    Messages with no MIDI 1.0 equivalent (per-note controllers, RPN/NRPN...)
    are dropped. Program change bank selection is ignored.
    """

    i: In(stream.Signature(Midi2Message))
    o: Out(stream.Signature(MidiMessage))

    def elaborate(self, platform):
        m = Module()

        msg = self.i.payload
        out = self.o.payload
        has_midi1 = Signal()

        m.d.comb += [
            out.status.nibble.channel.eq(msg.channel),
            out.status.kind.eq(msg.opcode.as_value()[0:3]),
            out.status.is_status.eq(1),
        ]

        raw7 = msg.data.raw[25:32]
        with m.Switch(msg.opcode):
            with m.Case(Midi2Opcode.NOTE_ON):
                vel7 = msg.data.note.velocity[9:16]
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.note_on.note.eq(msg.index.note.note),
                    out.midi_payload.note_on.velocity.eq(Mux(vel7 == 0, 1, vel7)),
                ]
            with m.Case(Midi2Opcode.NOTE_OFF):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.note_off.note.eq(msg.index.note.note),
                    out.midi_payload.note_off.velocity.eq(msg.data.note.velocity[9:16]),
                ]
            with m.Case(Midi2Opcode.POLY_PRESSURE):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.poly_pressure.note.eq(msg.index.per_note.note),
                    out.midi_payload.poly_pressure.pressure.eq(raw7),
                ]
            with m.Case(Midi2Opcode.CONTROL_CHANGE):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.control_change.controller_number.eq(msg.index.control.index),
                    out.midi_payload.control_change.data.eq(raw7),
                ]
            with m.Case(Midi2Opcode.PROGRAM_CHANGE):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.program_change.program_number.eq(msg.data.program.program),
                ]
            with m.Case(Midi2Opcode.CHANNEL_PRESSURE):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.channel_pressure.pressure.eq(raw7),
                ]
            with m.Case(Midi2Opcode.PITCH_BEND):
                m.d.comb += [
                    has_midi1.eq(1),
                    out.midi_payload.pitch_bend.lsb.eq(msg.data.raw[18:25]),
                    out.midi_payload.pitch_bend.msb.eq(msg.data.raw[25:32]),
                ]

        m.d.comb += [
            self.o.valid.eq(self.i.valid & has_midi1),
            self.i.ready.eq(Mux(has_midi1, self.o.ready, 1)),
        ]

        return m
