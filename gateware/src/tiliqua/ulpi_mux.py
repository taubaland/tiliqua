# Copyright (c) 2026 Alex Toland <atoland89@gmail.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""Share a single ULPI PHY between several USB cores (e.g. a host and a device)."""

from amaranth import *

class _VirtualPin:
    """Looks enough like an I/O buffer from :py:`platform.request` for LUNA's ULPI translator."""
    def __init__(self, name, width=1, i=False, o=False, oe=False):
        if i:
            self.i = Signal(width, name=f"{name}_i")
        if o:
            self.o = Signal(width, name=f"{name}_o")
        if oe:
            self.oe = Signal(name=f"{name}_oe")

class VirtualULPI:
    """A ULPI bus to hand to one USB core in place of the real ULPI resource."""
    def __init__(self, name):
        self.clk  = _VirtualPin(f"{name}_clk", o=True)
        self.rst  = _VirtualPin(f"{name}_rst", o=True)
        self.data = _VirtualPin(f"{name}_data", width=8, i=True, o=True, oe=True)
        self.dir  = _VirtualPin(f"{name}_dir", i=True)
        self.nxt  = _VirtualPin(f"{name}_nxt", i=True)
        self.stp  = _VirtualPin(f"{name}_stp", o=True)

class ULPIMux(Elaboratable):

    """
    Connect one of :py:`n_ports` virtual ULPI buses to the real ULPI PHY.

    Every time :py:`select` changes, the PHY and :py:`core_reset` are held in
    reset for a while, so the newly selected core finds the PHY in its default
    state and sets it up from scratch (e.g. host pulldowns vs. device
    terminations). The cores that are not selected should be held in reset
    using :py:`core_reset`, which is asserted for every port except the
    selected one (for this to work, the ULPI translators should be created
    with ``handle_clocking=True``, the default, so they never touch the
    real clock or reset pins).
    """

    # PHY reset pulse, in ``usb`` cycles (USB3343 needs > 1us)
    RESET_CYCLES = 6000

    def __init__(self, bus, n_ports=2):
        self.bus = bus
        self.ports = [VirtualULPI(f"ulpi_port{n}") for n in range(n_ports)]
        self.select = Signal(range(n_ports))
        self.core_reset = Signal(n_ports)

    def elaborate(self, platform):
        m = Module()

        bus = self.bus

        last_select = Signal.like(self.select)
        reset_counter = Signal(range(self.RESET_CYCLES + 1), init=self.RESET_CYCLES)
        in_reset = Signal()
        m.d.usb += last_select.eq(self.select)
        m.d.comb += in_reset.eq(reset_counter != 0)
        with m.If(self.select != last_select):
            m.d.usb += reset_counter.eq(self.RESET_CYCLES)
        with m.Elif(in_reset):
            m.d.usb += reset_counter.eq(reset_counter - 1)

        m.d.comb += [
            bus.clk.o.eq(ClockSignal("usb")),
            bus.rst.o.eq(ResetSignal("usb") | in_reset),
        ]

        for n, port in enumerate(self.ports):
            m.d.comb += [
                port.data.i.eq(bus.data.i),
                port.dir.i.eq(bus.dir.i),
                port.nxt.i.eq(bus.nxt.i),
                self.core_reset[n].eq(in_reset | (self.select != n)),
            ]

        with m.Switch(self.select):
            for n, port in enumerate(self.ports):
                with m.Case(n):
                    m.d.comb += [
                        bus.data.o.eq(port.data.o),
                        bus.data.oe.eq(port.data.oe),
                        bus.stp.o.eq(port.stp.o),
                    ]

        return m
