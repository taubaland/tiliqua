# Copyright (c) 2026 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""
USB MIDI 2.0 device engine.

A class-compliant USB MIDI 2.0 device (LUNA), with a MIDIStreaming interface that has:

    - Alternate setting 0: USB-MIDI 1.0 (4-byte events), for hosts that
      only speak MIDI 1.0. Events are translated to UMP on the fly.
    - Alternate setting 1: USB MIDI 2.0 (Universal MIDI Packets), described
      by a single bidirectional Group Terminal Block (group 0, MIDI 2.0 protocol).

Either way, everything the host sends comes out of :py:`USBMIDI2Device.o_ump`
as a single stream of UMP words. UMP words written to :py:`USBMIDI2Device.i_ump`
are sent back to the host (only in alternate setting 1, otherwise they are
discarded), which is used for answering UMP Endpoint Discovery.
"""

import struct

from amaranth import *
from amaranth.lib import stream, wiring
from amaranth.lib.wiring import In, Out

from luna.gateware.stream.generator import StreamSerializer
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.usb.usb2.request import (StallOnlyRequestHandler,
                                            USBRequestHandler)
from luna.usb2 import USBDevice, USBStreamInEndpoint, USBStreamOutEndpoint
from usb_protocol.emitters import DeviceDescriptorCollection
from usb_protocol.types import (USBRequestRecipient, USBRequestType,
                                USBStandardRequests)

from .usb_ump import BytesToWords, USBMIDI1ToUMP, WordsToBytes

# Class-specific descriptor types / subtypes (USB MIDI 1.0 / 2.0 class specs)
CS_INTERFACE          = 0x24
CS_ENDPOINT           = 0x25
CS_GR_TRM_BLOCK       = 0x26
MS_HEADER             = 0x01
MIDI_IN_JACK          = 0x02
MIDI_OUT_JACK         = 0x03
MS_GENERAL            = 0x01
MS_GENERAL_2_0        = 0x02
GR_TRM_BLOCK_HEADER   = 0x01
GR_TRM_BLOCK          = 0x02
JACK_EMBEDDED         = 0x01
JACK_EXTERNAL         = 0x02
GTB_PROTOCOL_MIDI_2_0 = 0x11

MIDISTREAMING_INTERFACE = 1
UMP_ALT_SETTING         = 1

def _interface(number, alt, n_endpoints, subclass, i_interface=0):
    return struct.pack("<BBBBBBBBB", 9, 0x04, number, alt, n_endpoints,
                       0x01, subclass, 0x00, i_interface)

def create_descriptors(*, name, manufacturer="apf.audio", serial="0000",
                       vendor_id=0x1209, product_id=0xAA62, max_packet_size=64):
    """
    Descriptors of a USB MIDI 2.0 device with a single port called :py:`name`
    (used for the product, interface, jack and Group Terminal Block names).
    """

    descriptors = DeviceDescriptorCollection()

    with descriptors.DeviceDescriptor() as d:
        d.bcdUSB             = 2.00
        d.idVendor           = vendor_id
        d.idProduct          = product_id
        d.iManufacturer      = manufacturer
        d.iProduct           = name
        d.iSerialNumber      = serial
        d.bcdDevice          = 0.02
        d.bNumConfigurations = 1

    i_name = descriptors.get_index_for_string(name)
    ep_out = 0x01
    ep_in  = 0x81
    wmax   = max_packet_size

    # Interface 0: AudioControl (required by the USB MIDI class, empty otherwise).
    audio_control = (
        _interface(0, 0, 0, 0x01) +
        # Class-specific AC header: bcdADC 1.00, 1 streaming interface (#1).
        struct.pack("<BBBHHBB", 9, CS_INTERFACE, MS_HEADER, 0x0100, 9, 1,
                    MIDISTREAMING_INTERFACE)
    )

    # Interface 1, alternate setting 0: USB-MIDI 1.0.
    #
    #   host OUT EP -> embedded IN jack (1) -> external OUT jack (4)
    #   external IN jack (2) -> embedded OUT jack (3) -> host IN EP
    midi1_cs = (
        struct.pack("<BBBBBB", 6, CS_INTERFACE, MIDI_IN_JACK, JACK_EMBEDDED, 1, i_name) +
        struct.pack("<BBBBBB", 6, CS_INTERFACE, MIDI_IN_JACK, JACK_EXTERNAL, 2, 0) +
        struct.pack("<BBBBBBBBB", 9, CS_INTERFACE, MIDI_OUT_JACK, JACK_EMBEDDED, 3, 1, 2, 1, i_name) +
        struct.pack("<BBBBBBBBB", 9, CS_INTERFACE, MIDI_OUT_JACK, JACK_EXTERNAL, 4, 1, 1, 1, 0)
    )
    midi1_endpoints = (
        struct.pack("<BBBBHBBB", 9, 0x05, ep_out, 0x02, wmax, 0, 0, 0) +
        struct.pack("<BBBBB", 5, CS_ENDPOINT, MS_GENERAL, 1, 1) +
        struct.pack("<BBBBHBBB", 9, 0x05, ep_in, 0x02, wmax, 0, 0, 0) +
        struct.pack("<BBBBB", 5, CS_ENDPOINT, MS_GENERAL, 1, 3)
    )
    midi1_total = 7 + len(midi1_cs) + len(midi1_endpoints)
    midi1 = (
        _interface(MIDISTREAMING_INTERFACE, 0, 2, 0x03, i_name) +
        struct.pack("<BBBHH", 7, CS_INTERFACE, MS_HEADER, 0x0100, midi1_total) +
        midi1_cs + midi1_endpoints
    )

    # Interface 1, alternate setting 1: USB MIDI 2.0 (UMP), 1 Group Terminal Block.
    midi2 = (
        _interface(MIDISTREAMING_INTERFACE, UMP_ALT_SETTING, 2, 0x03, i_name) +
        struct.pack("<BBBHH", 7, CS_INTERFACE, MS_HEADER, 0x0200, 7) +
        struct.pack("<BBBBHB", 7, 0x05, ep_out, 0x02, wmax, 0) +
        struct.pack("<BBBBB", 5, CS_ENDPOINT, MS_GENERAL_2_0, 1, 1) +
        struct.pack("<BBBBHB", 7, 0x05, ep_in, 0x02, wmax, 0) +
        struct.pack("<BBBBB", 5, CS_ENDPOINT, MS_GENERAL_2_0, 1, 1)
    )

    body = audio_control + midi1 + midi2
    configuration = struct.pack("<BBHBBBBB", 9, 0x02, 9 + len(body), 2, 1, 0,
                                0xC0, # self-powered
                                0) + body
    descriptors.add_descriptor(configuration)

    # Group Terminal Block descriptors, fetched by the host with
    # GET_DESCRIPTOR(wValue = CS_GR_TRM_BLOCK << 8 | alternate setting).
    block = struct.pack("<BBBBBBBBBHH", 13, CS_GR_TRM_BLOCK, GR_TRM_BLOCK,
                        1,    # bGrpTrmBlkID
                        0x00, # bGrpTrmBlkType: bidirectional
                        0,    # nGroupTrm: first group (0)
                        1,    # nNumGroupTrm
                        i_name,
                        GTB_PROTOCOL_MIDI_2_0,
                        0, 0) # bandwidth: unknown
    header = struct.pack("<BBBH", 5, CS_GR_TRM_BLOCK, GR_TRM_BLOCK_HEADER, 5 + len(block))
    descriptors.add_descriptor(header + block, index=UMP_ALT_SETTING,
                               descriptor_type=CS_GR_TRM_BLOCK)

    return descriptors

def _is_interface_request(setup, request):
    return ((setup.type == USBRequestType.STANDARD) &
            (setup.recipient == USBRequestRecipient.INTERFACE) &
            (setup.request == request))

class MIDIStreamingRequestHandler(USBRequestHandler):

    """
    Handles SET_INTERFACE / GET_INTERFACE for the MIDIStreaming interface,
    tracking which alternate setting (USB-MIDI 1.0 or UMP) the host selected.

    Selecting an alternate setting resets the data toggles of our bulk
    endpoints, as the host does the same [USB 2.0: 9.4.10].
    """

    def __init__(self, *, endpoint_number):
        super().__init__()
        self.endpoint_number = endpoint_number
        self.alt_setting = Signal()
        self.bus_reset   = Signal()

    def elaborate(self, platform):
        m = Module()

        interface = self.interface
        setup     = self.interface.setup

        m.submodules.transmitter = transmitter = \
            StreamSerializer(data_length=1, domain="usb", stream_type=USBInStreamInterface, max_length_width=1)

        # Pending data toggle resets (bit 0: OUT, bit 1: IN)
        clear_halt = Signal(2)
        with m.If(clear_halt[0]):
            m.d.comb += interface.clear_endpoint_halt.direction.eq(0)
            m.d.usb += clear_halt[0].eq(0)
        with m.Elif(clear_halt[1]):
            m.d.comb += interface.clear_endpoint_halt.direction.eq(1)
            m.d.usb += clear_halt[1].eq(0)
        m.d.comb += [
            interface.clear_endpoint_halt.enable.eq(clear_halt.any()),
            interface.clear_endpoint_halt.number.eq(self.endpoint_number),
        ]

        with m.If(_is_interface_request(setup, USBStandardRequests.SET_INTERFACE)):
            m.d.comb += interface.claim.eq(1)

            with m.If(setup.received & (setup.index == MIDISTREAMING_INTERFACE)):
                m.d.usb += [
                    self.alt_setting.eq(setup.value == UMP_ALT_SETTING),
                    clear_halt.eq(0b11),
                ]

            # Accept whatever the request was.
            with m.If(interface.status_requested):
                m.d.comb += self.send_zlp()

        with m.Elif(_is_interface_request(setup, USBStandardRequests.GET_INTERFACE)):
            m.d.comb += [
                interface.claim.eq(1),
                transmitter.stream.attach(self.interface.tx),
                transmitter.data[0].eq(Mux(setup.index == MIDISTREAMING_INTERFACE,
                                           self.alt_setting, 0)),
                transmitter.max_length.eq(1),
            ]
            with m.If(interface.data_requested):
                m.d.comb += transmitter.start.eq(1)
            with m.If(interface.status_requested):
                m.d.comb += interface.handshakes_out.ack.eq(1)

        # Bus reset returns all interfaces to alternate setting 0.
        with m.If(self.bus_reset):
            m.d.usb += self.alt_setting.eq(0)

        return m

class USBMIDI2Device(wiring.Component):

    """
    USB MIDI 2.0 device engine (see module docstring).

    All logic is in the ``usb`` domain, which is the same clock as ``sync`` on Tiliqua.

    Members
    -------
    o_ump : ``Out(stream.Signature(unsigned(32)))``
        UMP words received from the host. In USB-MIDI 1.0 mode, events are
        translated to UMP message type 0x1 / 0x2 (and SysEx is dropped).
    i_ump : ``In(stream.Signature(unsigned(32)))``
        UMP words to send to the host (always drained in USB-MIDI 1.0 mode).
    connect : ``In(1)``
        Connect to the bus (e.g. once the Type-C controller reports a host).
    ump_mode : ``Out(1)``
        Host selected the USB MIDI 2.0 (UMP) alternate setting.
    """

    MAX_PACKET_SIZE = 64 # full speed bulk endpoints, plenty for MIDI

    o_ump:    Out(stream.Signature(unsigned(32)))
    i_ump:    In(stream.Signature(unsigned(32)))
    connect:  In(1)
    ump_mode: Out(1)

    def __init__(self, *, bus, name, **kwargs):
        self.bus = bus
        self.descriptors = create_descriptors(
            name=name, max_packet_size=self.MAX_PACKET_SIZE, **kwargs)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.usb = usb = USBDevice(bus=self.bus)

        control_ep = usb.add_control_endpoint()
        control_ep.add_standard_request_handlers(self.descriptors, skiplist=[
            lambda setup: _is_interface_request(setup, USBStandardRequests.SET_INTERFACE),
            lambda setup: _is_interface_request(setup, USBStandardRequests.GET_INTERFACE),
        ])
        control_ep.add_request_handler(midi_handler := MIDIStreamingRequestHandler(endpoint_number=1))
        stall_condition = lambda setup : \
            (setup.type == USBRequestType.VENDOR) | \
            (setup.type == USBRequestType.RESERVED)
        control_ep.add_request_handler(StallOnlyRequestHandler(stall_condition))

        ep_out = USBStreamOutEndpoint(endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        ep_in  = USBStreamInEndpoint(endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep_out)
        usb.add_endpoint(ep_in)

        m.d.comb += [
            usb.connect.eq(self.connect),
            usb.full_speed_only.eq(1),
        ]

        ump_mode = Signal()
        m.d.comb += [
            midi_handler.bus_reset.eq(usb.reset_detected),
            ump_mode.eq(midi_handler.alt_setting),
            self.ump_mode.eq(ump_mode),
        ]

        # OUT: bytes -> 32-bit words -> (USB-MIDI 1.0 events translated to) UMP words.

        m.submodules.bytes_to_words = bytes_to_words = DomainRenamer("usb")(BytesToWords())
        m.d.comb += [
            bytes_to_words.i.valid.eq(ep_out.stream.valid),
            bytes_to_words.i.payload.data.eq(ep_out.stream.payload),
            bytes_to_words.i.payload.first.eq(ep_out.stream.first),
            bytes_to_words.i.payload.last.eq(ep_out.stream.last),
            ep_out.stream.ready.eq(bytes_to_words.i.ready),
        ]

        m.submodules.midi1_to_ump = midi1_to_ump = USBMIDI1ToUMP()
        m.d.comb += midi1_to_ump.i.eq(bytes_to_words.o.payload)
        with m.If(ump_mode):
            wiring.connect(m, bytes_to_words.o, wiring.flipped(self.o_ump))
        with m.Else():
            m.d.comb += [
                self.o_ump.payload.eq(midi1_to_ump.o),
                self.o_ump.valid.eq(bytes_to_words.o.valid & midi1_to_ump.valid),
                bytes_to_words.o.ready.eq(self.o_ump.ready | ~midi1_to_ump.valid),
            ]

        # IN: UMP words -> bytes (only in UMP mode).

        m.submodules.words_to_bytes = words_to_bytes = DomainRenamer("usb")(WordsToBytes())
        with m.If(ump_mode):
            wiring.connect(m, wiring.flipped(self.i_ump), words_to_bytes.i)
        with m.Else():
            m.d.comb += self.i_ump.ready.eq(1)
        m.d.comb += [
            ep_in.stream.valid.eq(words_to_bytes.o.valid),
            ep_in.stream.payload.eq(words_to_bytes.o.payload.data),
            ep_in.stream.first.eq(words_to_bytes.o.payload.first),
            ep_in.stream.last.eq(words_to_bytes.o.payload.last),
            words_to_bytes.o.ready.eq(ep_in.stream.ready),
        ]

        return m
