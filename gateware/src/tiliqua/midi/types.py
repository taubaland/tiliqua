# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""MIDI data types and message structures."""

from amaranth import unsigned
from amaranth.lib import data, enum

class Status(data.Struct):
    """Layout of a MIDI Status byte."""

    class Kind(enum.Enum, shape=unsigned(3)):
        NOTE_OFF         = 0
        NOTE_ON          = 1
        POLY_PRESSURE    = 2
        CONTROL_CHANGE   = 3
        PROGRAM_CHANGE   = 4
        CHANNEL_PRESSURE = 5
        PITCH_BEND       = 6
        SYSEX            = 7

    # If 'Kind == SYSEX && nibble.sys.is_rt == True', we have a real-time message:

    class RT(enum.Enum, shape=unsigned(3)):
        CLOCK            = 0
        START            = 2
        CONTINUE         = 3
        STOP             = 4
        ACTIVE_SENSING   = 6
        RESET            = 7

    # If 'Kind == SYSEX && nibble.sys.is_rt == False', we have a 'System Common' message:

    class SysCom(enum.Enum, shape=unsigned(3)):
        SYSEX            = 0
        MTC_QF           = 1
        SONG_POSITION    = 2
        SONG_SELECT      = 3
        TUNE_REQUEST     = 6
        EOX              = 7

    # All of which are organized as follows --

    nibble: data.UnionLayout({
        "channel": unsigned(4),
        "sys": data.StructLayout({
            "sub": data.UnionLayout({
                "com": SysCom,
                "rt":  RT,
            }),
            "is_rt": unsigned(1),
        }),
    })
    kind:      Kind
    is_status: unsigned(1)

class MidiMessage(data.Struct):
    """Layout of all MIDI messages that are not Sysex/RT messages."""

    status:       Status
    midi_payload: data.UnionLayout({
        "raw": data.StructLayout({
            "byte0": unsigned(8),
            "byte1": unsigned(8),
        }),
        "note_off": data.StructLayout({
            "note": unsigned(8),
            "velocity": unsigned(8),
        }),
        "note_on": data.StructLayout({
            "note": unsigned(8),
            "velocity": unsigned(8),
        }),
        "poly_pressure": data.StructLayout({
            "note": unsigned(8),
            "pressure": unsigned(8),
        }),
        "control_change": data.StructLayout({
            "controller_number": unsigned(8),
            "data": unsigned(8),
        }),
        "program_change": data.StructLayout({
            "program_number": unsigned(8),
            "_unused": unsigned(8),
        }),
        "channel_pressure": data.StructLayout({
            "pressure": unsigned(8),
            "_unused": unsigned(8),
        }),
        "pitch_bend": data.StructLayout({
            "lsb": unsigned(8),
            "msb": unsigned(8),
        }),
    })

class Midi2Opcode(enum.Enum, shape=unsigned(4)):
    """Status nibble of a MIDI 2.0 Channel Voice message (UMP message type 0x4)."""
    REGISTERED_PER_NOTE_CONTROLLER = 0x0
    ASSIGNABLE_PER_NOTE_CONTROLLER = 0x1
    REGISTERED_CONTROLLER          = 0x2 # (RPN)
    ASSIGNABLE_CONTROLLER          = 0x3 # (NRPN)
    RELATIVE_REGISTERED_CONTROLLER = 0x4
    RELATIVE_ASSIGNABLE_CONTROLLER = 0x5
    PER_NOTE_PITCH_BEND            = 0x6
    NOTE_OFF                       = 0x8
    NOTE_ON                        = 0x9
    POLY_PRESSURE                  = 0xA
    CONTROL_CHANGE                 = 0xB
    PROGRAM_CHANGE                 = 0xC
    CHANNEL_PRESSURE               = 0xD
    PITCH_BEND                     = 0xE
    PER_NOTE_MANAGEMENT            = 0xF

class Midi2Message(data.Struct):
    """
    Layout of a MIDI 2.0 Channel Voice message.

    Bit-compatible with the 2 words of a UMP message type 0x4 (i.e.
    ``Cat(word1, word0[0:28])``), excluding the message type nibble.
    """

    data: data.UnionLayout({
        "raw": unsigned(32),
        # NOTE_ON, NOTE_OFF
        "note": data.StructLayout({
            "attribute": unsigned(16),
            "velocity": unsigned(16),
        }),
        # PROGRAM_CHANGE
        "program": data.StructLayout({
            "bank_lsb": unsigned(7),
            "_reserved0": unsigned(1),
            "bank_msb": unsigned(7),
            "_reserved1": unsigned(9),
            "program": unsigned(7),
            "_reserved2": unsigned(1),
        }),
        # All other messages carry a single 32-bit value. Pitch bends are
        # unsigned, centered at 0x80000000.
    })
    index: data.UnionLayout({
        "raw": data.StructLayout({
            "byte3": unsigned(8),
            "byte2": unsigned(8),
        }),
        # NOTE_ON, NOTE_OFF
        "note": data.StructLayout({
            "attribute_type": unsigned(8),
            "note": unsigned(8),
        }),
        # POLY_PRESSURE, PER_NOTE_PITCH_BEND, PER_NOTE_MANAGEMENT
        # and per-note controllers.
        "per_note": data.StructLayout({
            "index": unsigned(8),
            "note": unsigned(8),
        }),
        # CONTROL_CHANGE
        "control": data.StructLayout({
            "_reserved": unsigned(8),
            "index": unsigned(8),
        }),
        # Registered / assignable controllers (RPN / NRPN)
        "controller": data.StructLayout({
            "index": unsigned(8),
            "bank": unsigned(8),
        }),
        # PROGRAM_CHANGE
        "program": data.StructLayout({
            "bank_valid": unsigned(1),
            "_reserved": unsigned(15),
        }),
    })
    channel: unsigned(4)
    opcode:  Midi2Opcode
    group:   unsigned(4)
