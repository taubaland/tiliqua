# Copyright (c) 2026 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

import struct
import unittest

from amaranth import *
from amaranth.back import rtlil
from amaranth.lib import stream, wiring
from amaranth.sim import *

from tiliqua import midi
from tiliqua.midi.usb_device import USBMIDI2Device, create_descriptors
from tiliqua.test import stream as stream_util
from tiliqua.ulpi_mux import ULPIMux, VirtualULPI

def upscale(src, src_bits, dst_bits):
    """Min-Center-Max upscaling, as in the MIDI 2.0 specification (reference model)."""
    scale_bits = dst_bits - src_bits
    shifted = src << scale_bits
    if src <= (1 << (src_bits - 1)):
        return shifted
    repeat_bits = src_bits - 1
    repeat = src & ((1 << repeat_bits) - 1)
    if scale_bits > repeat_bits:
        repeat <<= scale_bits - repeat_bits
    else:
        repeat >>= repeat_bits - scale_bits
    while repeat:
        shifted |= repeat
        repeat >>= repeat_bits
    return shifted

def parse_descriptors(raw):
    """Split a configuration descriptor into (bDescriptorType, bytes) tuples."""
    out = []
    while raw:
        length = raw[0]
        assert length >= 2
        out.append((raw[1], raw[:length]))
        raw = raw[length:]
    return out

def string_descriptor(collection, index):
    raw = collection.get_descriptor_bytes(0x03, index)
    return raw[2:].decode("utf-16-le")

class USBMIDI2DescriptorTests(unittest.TestCase):

    NAME = "Tiliqua polysyn"

    def setUp(self):
        self.d = create_descriptors(name=self.NAME, max_packet_size=64)

    def test_device(self):
        dev = self.d.get_descriptor_bytes(0x01)
        (_, _, bcd_usb, cls, _, _, _, vid, pid, _, _, i_product, _, n_cfg) = \
            struct.unpack("<BBHBBBBHHHBBBB", dev)
        self.assertEqual(bcd_usb, 0x0200)
        self.assertEqual(cls, 0) # class defined per interface
        self.assertEqual((vid, pid), (0x1209, 0xAA62))
        self.assertEqual(string_descriptor(self.d, i_product), self.NAME)
        self.assertEqual(n_cfg, 1)

    def test_configuration(self):
        raw = self.d.get_descriptor_bytes(0x02)
        _, _, total, n_ifaces, _, _, attrs, _ = struct.unpack("<BBHBBBBB", raw[:9])
        self.assertEqual(total, len(raw))
        self.assertEqual(n_ifaces, 2)
        self.assertEqual(attrs, 0xC0)

        descs = parse_descriptors(raw[9:])
        # Group descriptors by (interface, alternate setting)
        ifaces = {}
        current = None
        for kind, d in descs:
            if kind == 0x04:
                current = (d[2], d[3])
                ifaces[current] = [d]
            else:
                ifaces[current].append(d)
        self.assertEqual(set(ifaces), {(0, 0), (1, 0), (1, 1)})

        # AudioControl: class 1 subclass 1, header points at interface 1.
        ac = ifaces[(0, 0)]
        self.assertEqual(ac[0][5:7], bytes([0x01, 0x01]))
        self.assertEqual(ac[1], bytes([9, 0x24, 0x01, 0x00, 0x01, 9, 0, 1, 1]))

        # Alt 0: USB-MIDI 1.0.
        alt0 = ifaces[(1, 0)]
        self.assertEqual(alt0[0][4:7], bytes([2, 0x01, 0x03])) # 2 EPs, audio / MIDIStreaming
        self.assertEqual(string_descriptor(self.d, alt0[0][8]), self.NAME)
        header = alt0[1]
        self.assertEqual(header[:5], bytes([7, 0x24, 0x01, 0x00, 0x01])) # bcdMSC 1.00
        cs_total = struct.unpack("<H", header[5:7])[0]
        self.assertEqual(cs_total, sum(len(d) for d in alt0[1:]))
        jacks = [d for d in alt0 if d[1] == 0x24 and d[2] in (0x02, 0x03)]
        self.assertEqual(len(jacks), 4)
        eps = [d for d in alt0 if d[1] == 0x05]
        self.assertEqual([(d[0], d[2], d[3]) for d in eps], [(9, 0x01, 0x02), (9, 0x81, 0x02)])
        cs_eps = [d for d in alt0 if d[1] == 0x25]
        self.assertEqual(cs_eps, [bytes([5, 0x25, 0x01, 1, 1]), bytes([5, 0x25, 0x01, 1, 3])])

        # Alt 1: USB MIDI 2.0
        alt1 = ifaces[(1, 1)]
        self.assertEqual(alt1[0][4:7], bytes([2, 0x01, 0x03]))
        self.assertEqual(alt1[1], bytes([7, 0x24, 0x01, 0x00, 0x02, 7, 0])) # bcdMSC 2.00
        eps = [d for d in alt1 if d[1] == 0x05]
        self.assertEqual(eps, [bytes([7, 0x05, 0x01, 0x02, 64, 0, 0]),
                               bytes([7, 0x05, 0x81, 0x02, 64, 0, 0])])
        cs_eps = [d for d in alt1 if d[1] == 0x25]
        self.assertEqual(cs_eps, [bytes([5, 0x25, 0x02, 1, 1])] * 2) # MS_GENERAL_2_0, GTB 1

    def test_group_terminal_block(self):
        # GET_DESCRIPTOR(wValue = 0x2601)
        raw = self.d.get_descriptor_bytes(0x26, 1)
        header, block = raw[:5], raw[5:]
        self.assertEqual(header, bytes([5, 0x26, 0x01]) + struct.pack("<H", len(raw)))
        (length, kind, subtype, block_id, block_type, first_group, n_groups,
         i_block, protocol, _, _) = struct.unpack("<BBBBBBBBBHH", block)
        self.assertEqual((length, kind, subtype), (13, 0x26, 0x02))
        self.assertEqual(block_id, 1)       # matches baAssoGrpTrmBlkID
        self.assertEqual(block_type, 0x00)  # bidirectional
        self.assertEqual((first_group, n_groups), (0, 1))
        self.assertEqual(protocol, 0x11)    # MIDI 2.0
        self.assertEqual(string_descriptor(self.d, i_block), self.NAME)

    def test_elaborates(self):
        # Whole device + ULPI mux converts to RTLIL (no hardware needed).
        bus = VirtualULPI("phy")
        class Top(Elaboratable):
            def elaborate(self, platform):
                m = Module()
                m.domains.usb = ClockDomain()
                m.submodules.mux = mux = ULPIMux(bus, n_ports=2)
                dev = USBMIDI2Device(bus=mux.ports[1], name="Tiliqua polysyn")
                m.submodules.dev = ResetInserter({"usb": mux.core_reset[1]})(dev)
                m.d.comb += [
                    mux.select.eq(1),
                    dev.o_ump.ready.eq(1),
                ]
                return m
        rtlil.convert(Top(), ports=[
            bus.clk.o, bus.rst.o, bus.data.i, bus.data.o, bus.data.oe,
            bus.dir.i, bus.nxt.i, bus.stp.o])

class UMPPathTests(unittest.TestCase):

    def test_upscale_roundtrip(self):
        # Truncating to the top bits inverts Min-Center-Max upscaling exactly.
        for v in range(128):
            self.assertEqual(upscale(v, 7, 32) >> 25, v)
            self.assertEqual(upscale(v, 7, 16) >> 9, v)
        for v in range(1 << 14):
            self.assertEqual(upscale(v, 14, 32) >> 18, v)

    def test_usb_ump_to_midi1(self):
        """USB packet bytes -> UMP words -> MidiDecodeUMP -> MIDI 1.0 messages."""

        m = Module()
        m.submodules.b2w   = b2w   = midi.BytesToWords()
        m.submodules.split = split = midi.UMPStreamSplit()
        m.submodules.dec   = dec   = midi.MidiDecodeUMP(forward_rt=True)
        m.submodules.m2m1  = m2m1  = midi.Midi2ToMidi1()
        wiring.connect(m, b2w.o, split.i)
        wiring.connect(m, split.o, dec.i)
        wiring.connect(m, dec.o, m2m1.i)

        CHANNEL = 3
        def mt4(status, index, index2, data):
            return [(0x4 << 28) | (status << 20) | (CHANNEL << 16) | (index << 8) | index2, data]

        cc_values = [0, 1, 63, 64, 65, 100, 127]
        umps = []
        expect = []
        # MIDI 2.0 CC (e.g. CC 20 'waveform') with upscaled 32-bit values.
        for v in cc_values:
            umps.append(mt4(0xB, 20, 0, upscale(v, 7, 32)))
            expect.append((0xB0 | CHANNEL, 20, v))
        # UMP Endpoint Discovery (to the SoC, not the synth).
        discovery = [0xF0000101, 0x0000001F, 0, 0]
        umps.append(discovery)
        # MIDI 2.0 NOTE_ON with 16-bit velocity, and a tiny velocity (must not become NOTE_OFF).
        umps.append(mt4(0x9, 60, 0, 0xC000 << 16))
        expect.append((0x90 | CHANNEL, 60, 0x60))
        umps.append(mt4(0x9, 61, 0, 0x0100 << 16))
        expect.append((0x90 | CHANNEL, 61, 1))
        umps.append(mt4(0x8, 60, 0, 0x8000 << 16))
        expect.append((0x80 | CHANNEL, 60, 0x40))
        # MIDI 2.0 pitch bend: top 14 bits.
        umps.append(mt4(0xE, 0, 0, 0xFFFFFFFF))
        expect.append((0xE0 | CHANNEL, 0x7F, 0x7F))
        umps.append(mt4(0xE, 0, 0, 0x80000000))
        expect.append((0xE0 | CHANNEL, 0x00, 0x40))
        # MIDI 2.0 sustain pedal (CC 64) and mod wheel (CC 1).
        umps.append(mt4(0xB, 64, 0, 0xFFFFFFFF))
        expect.append((0xB0 | CHANNEL, 64, 127))
        umps.append(mt4(0xB, 1, 0, upscale(90, 7, 32)))
        expect.append((0xB0 | CHANNEL, 1, 90))
        # RPN (no MIDI 1.0 equivalent here): dropped.
        umps.append(mt4(0x2, 0, 0, 0x12345678))
        # MIDI 1.0 channel voice in UMP (message type 0x2): CC 23 = 99.
        umps.append([(0x2 << 28) | (0xB0 | CHANNEL) << 16 | (23 << 8) | 99])
        expect.append((0xB0 | CHANNEL, 23, 99))
        # Timing clock (message type 0x1)
        umps.append([0x10F80000])

        # Pack into USB packets (64 bytes max, words little-endian), splitting
        # a 128-bit UMP across a packet boundary on purpose.
        words = [w for ump in umps for w in ump]
        data = b"".join(struct.pack("<I", w) for w in words)
        packets = [data[i:i+60] for i in range(0, len(data), 60)]

        done = set()

        async def put_bytes(ctx):
            for pkt in packets:
                for n, b in enumerate(pkt):
                    await stream_util.put(ctx, b2w.i, {
                        'first': n == 0, 'last': n == len(pkt) - 1, 'data': b})

        async def get_stream(ctx):
            got = []
            for _ in range(4):
                got.append(await stream_util.get(ctx, split.o_stream))
            self.assertEqual(got, discovery)
            done.add("stream")

        async def get_rt(ctx):
            rt = await stream_util.get(ctx, dec.o_rt)
            self.assertEqual(rt, midi.Status.RT.CLOCK)
            done.add("rt")

        async def get_midi(ctx):
            for status, b0, b1 in expect:
                p = await stream_util.get(ctx, m2m1.o)
                # MidiMessage is laid out as the 3 MIDI 1.0 bytes, in order.
                self.assertEqual(p.as_value().value, status | (b0 << 8) | (b1 << 16))
            done.add("midi")

        sim = Simulator(m)
        sim.add_clock(1e-6)
        sim.add_testbench(put_bytes)
        sim.add_testbench(get_stream)
        sim.add_testbench(get_rt)
        sim.add_testbench(get_midi)
        with sim.write_vcd(vcd_file=open("test_usb_ump_to_midi1.vcd", "w")):
            sim.run_until(1e-6 * 20000)
        self.assertEqual(done, {"stream", "rt", "midi"})

    def test_usb_midi1_events(self):
        """Alternate setting 0: USB-MIDI 1.0 event bytes -> UMP words."""
        m = Module()
        m.submodules.b2w = b2w = midi.BytesToWords()
        m.submodules.tr = tr = midi.USBMIDI1ToUMP()
        m.d.comb += [
            tr.i.eq(b2w.o.payload),
            b2w.o.ready.eq(1),
        ]

        events = [
            (bytes([0x0B, 0xB2, 30, 77]), 0x20B21E4D), # CC 30 = 77, ch 3
            (bytes([0x09, 0x90, 60, 100]), 0x20903C64),
            (bytes([0x0F, 0xF8, 0, 0]), 0x10F80000),   # clock
        ]

        async def testbench(ctx):
            for event, expect in events:
                for n, b in enumerate(event):
                    ctx.set(b2w.i.valid, 1)
                    ctx.set(b2w.i.payload, {'first': n == 0, 'last': n == 3, 'data': b})
                    if n == 3:
                        self.assertEqual(ctx.get(b2w.o.valid), 1)
                        self.assertEqual(ctx.get(tr.valid), 1)
                        self.assertEqual(ctx.get(tr.o), expect)
                    await ctx.tick()
            ctx.set(b2w.i.valid, 0)

        sim = Simulator(m)
        sim.add_clock(1e-6)
        sim.add_testbench(testbench)
        sim.run()

    def test_words_to_bytes(self):
        """UMP words -> USB IN packet bytes, a short packet per UMP."""
        dut = midi.WordsToBytes()
        umps = [[0xF0010101, 0x81000300, 0, 0], [0x10F80000]]

        async def put(ctx):
            for ump in umps:
                for w in ump:
                    await stream_util.put(ctx, dut.i, w)

        done = []

        async def get(ctx):
            for ump in umps:
                data = b"".join(struct.pack("<I", w) for w in ump)
                for n, b in enumerate(data):
                    p = await stream_util.get(ctx, dut.o)
                    self.assertEqual(p.data, b)
                    self.assertEqual(p.first, n == 0)
                    self.assertEqual(p.last, n == len(data) - 1)
            done.append(True)

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(put)
        sim.add_testbench(get)
        sim.run_until(1e-6 * 1000)
        self.assertTrue(done)

if __name__ == "__main__":
    unittest.main()
