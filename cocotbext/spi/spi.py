# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Spencer Chang
# Clean multi-width implementation: io pins work correctly in x1 (unidirectional) and x2+ (bidirectional)

import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, Optional, Tuple

import cocotb
from cocotb.triggers import Event, FallingEdge, First, RisingEdge, Timer

from .exceptions import SpiFrameError


class Bus:
    """
    A simple bus class to manage signal connections.
    This replaces the dependency on cocotb_bus for cocotb 2.0 compatibility.
    """
    def __init__(self, entity=None, prefix=None, signals=None, optional_signals=None, **kwargs):
        self.entity = entity
        self.prefix = prefix

        # Combine required and optional signals
        all_signals = {}
        if signals:
            all_signals.update(signals)
        if optional_signals:
            all_signals.update(optional_signals)

        # Create signal attributes
        for attr_name, signal_name in all_signals.items():
            if prefix:
                full_signal_name = f"{prefix}_{signal_name}"
            else:
                full_signal_name = signal_name

            # Get the signal handle from the entity
            signal_handle = getattr(entity, full_signal_name, None)
            if signal_handle is None and attr_name in (optional_signals or {}):
                # Optional signal not found, skip
                continue

            setattr(self, attr_name, signal_handle)


class SpiBus(Bus):
    """
    Universal SPI Bus

    Supports:
    - Standard mode: mosi/miso pins (always unidirectional)
    - Multi-lane mode: io0-io31 pins
      * In x1 mode: io0→TX (unidirectional), io1→RX (unidirectional)
      * In x2+ modes: all io pins are bidirectional

    Note: The user is responsible for handling bidirectional wiring in their
    testbench wrapper when using io pins in x2+ modes.
    """
    def __init__(
        self,
        entity=None,
        prefix=None,
        sclk_name='sclk',
        mosi_name='mosi',
        miso_name='miso',
        cs_name=None,
        io_names=None,  # List like ['io0', 'io1', ..., 'io31']
        **kwargs,
    ):
        signals = {'sclk': sclk_name}
        optional_signals = {}

        # IO pins take precedence over standard mosi/miso
        if io_names:
            for i, name in enumerate(io_names):
                optional_signals[f'io{i}'] = name
        else:
            # Standard mosi/miso pins
            if mosi_name:
                optional_signals['mosi'] = mosi_name
            if miso_name:
                optional_signals['miso'] = miso_name

        if cs_name is not None:
            optional_signals['cs'] = cs_name

        super().__init__(entity, prefix, signals, optional_signals=optional_signals, **kwargs)

    @classmethod
    def from_entity(cls, entity, **kwargs):
        return cls(entity, **kwargs)

    @classmethod
    def from_prefix(cls, entity, prefix, **kwargs):
        return cls(entity, prefix, **kwargs)


@dataclass
class SpiConfig:
    """
    Universal SPI configuration

    Parameters:
    - word_width: Number of bits per word (8, 16, 32, etc.)
    - sclk_freq: Clock frequency in Hz
    - cpol: Clock polarity (False=idle low, True=idle high)
    - cpha: Clock phase (False=sample first edge, True=propagate first edge)
    - msb_first: Bit order (True=MSB first, False=LSB first)
    - frame_spacing_ns: Minimum nanoseconds between frames
    - data_output_idle: Idle state for data lines (0 or 1)
    - ignore_rx_value: If set, ignore received words matching this value
    - cs_active_low: Chip select polarity (True=active low, False=active high)
    - data_width: Number of data lanes (1-32)
      * 1 = Standard SPI (x1)
      * 2 = Dual SPI (x2)
      * 4 = Quad SPI (x4)
      * 8 = Octal SPI (x8)
      * etc.
    """
    word_width: int = 8
    sclk_freq: Optional[float] = 25e6
    cpol: bool = False
    cpha: bool = False
    msb_first: bool = True
    frame_spacing_ns: int = 1
    data_output_idle: int = 1
    ignore_rx_value: Optional[int] = None
    cs_active_low: bool = True
    data_width: int = 1  # Number of parallel data lanes

    @property
    def cycles_per_word(self) -> int:
        """Calculate number of clock cycles needed to transfer one word"""
        return (self.word_width + self.data_width - 1) // self.data_width


class SpiMaster:
    """
    Universal SPI Master supporting x1 through x32 modes

    Automatically detects pin configuration and operates accordingly:
    - io pins with data_width=1: Unidirectional (io0=TX, io1=RX)
    - io pins with data_width>1: Bidirectional (all io pins)
    - mosi/miso pins: Always unidirectional (standard SPI)
    """
    def __init__(self, bus: SpiBus, config: SpiConfig) -> None:
        self.log = logging.getLogger(f"cocotb.{bus.sclk._path}")

        self._sclk = bus.sclk
        self.has_cs = hasattr(bus, 'cs')
        if self.has_cs:
            self._cs = bus.cs

        self._config = config
        self._detect_and_configure_pins(bus)

        self.queue_tx: Deque[Tuple[int, bool]] = deque()
        self.queue_rx: Deque[int] = deque()
        self.sync = Event()
        self._idle = Event()
        self._idle.set()

        # Initialize signals to idle state
        self._sclk.value = int(self._config.cpol)
        self._init_data_lines()
        if self.has_cs:
            self._cs.value = 1 if self._config.cs_active_low else 0

        self._SpiClock = _SpiClock(
            signal=self._sclk,
            period=(1 / self._config.sclk_freq),
            unit="sec",
            start_high=self._config.cpha,
        )

        self._run_coroutine_obj = None
        self._restart()

    def _detect_and_configure_pins(self, bus: SpiBus):
        """
        Intelligent pin detection and configuration:

        - io0/io1/... with data_width=1:
          Unidirectional mode (io0=TX, io1=RX, like standard MOSI/MISO)

        - io0/io1/... with data_width>1:
          Bidirectional mode (all pins used for parallel data transfer)

        - mosi/miso:
          Always unidirectional (standard SPI, forces data_width=1)
        """
        # Check for io pins
        if hasattr(bus, 'io0'):
            self._io_lines = []
            i = 0
            while hasattr(bus, f'io{i}'):
                self._io_lines.append(getattr(bus, f'io{i}'))
                i += 1

            if self._config.data_width == 1:
                # x1 mode: io0=TX (unidirectional), io1=RX (unidirectional)
                # This is compatible with standard SPI behavior
                self._mode = 'standard'
                self._mosi = self._io_lines[0]
                self._miso = self._io_lines[1] if len(self._io_lines) > 1 else self._io_lines[0]
                self.log.info(f"x1 mode: io0→TX, io1→RX (unidirectional, {len(self._io_lines)} pins available)")
            else:
                # x2+: all io pins are bidirectional
                self._mode = 'multi_lane'
                self.log.info(f"x{self._config.data_width} mode: {len(self._io_lines)} io pins (bidirectional)")
            return

        # Standard mosi/miso (always unidirectional)
        if hasattr(bus, 'mosi') and hasattr(bus, 'miso'):
            self._mode = 'standard'
            self._mosi = bus.mosi
            self._miso = bus.miso
            if self._config.data_width == 1:
                self.log.info("x1 mode: mosi/miso (unidirectional)")
            else:
                self.log.warning(f"x{self._config.data_width} mode requested but only mosi/miso available (will use x1)")
                self._config.data_width = 1  # Force x1 for standard pins
            return

        raise RuntimeError("No valid SPI pins found (need io0+io1 or mosi+miso)")

    def _init_data_lines(self):
        """Initialize data lines to idle state"""
        if self._mode == 'multi_lane':
            for line in self._io_lines:
                line.value = self._config.data_output_idle
        else:  # standard
            self._mosi.value = self._config.data_output_idle

    def _restart(self) -> None:
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def write(self, data: Iterable[int], *, burst: bool = False):
        """Write data to the SPI bus and wait for completion

        Args:
            data: An iterable of ints. For 8-bit words, a bytearray is typical.
            burst: If True, CS is not deasserted between consecutive writes.
        """
        self.write_nowait(data, burst=burst)
        await self._idle.wait()

    def write_nowait(self, data: Iterable[int], *, burst: bool = False) -> None:
        """Write data to the SPI bus without waiting

        Args:
            data: An iterable of ints. For 8-bit words, a bytearray is typical.
            burst: If True, CS is not deasserted between consecutive writes.
        """
        if self._config.msb_first:
            for b in data:
                self.queue_tx.append((int(b), burst))
        else:
            for b in data:
                self.queue_tx.append((reverse_word(int(b), self._config.word_width), burst))
        self.sync.set()
        self._idle.clear()

    async def read(self, count: int = -1):
        """Read data from the receive queue

        Args:
            count: Number of words to read. -1 means read all available.

        Returns:
            Bytearray (for 8-bit words) or list of ints (for other widths)
        """
        while self.empty_rx():
            self.sync.clear()
            await self.sync.wait()
        return self.read_nowait(count)

    def read_nowait(self, count: int = -1) -> Iterable[int]:
        """Read data from receive queue without waiting

        Args:
            count: Number of words to read. -1 means read all available.

        Returns:
            Bytearray (for 8-bit words) or list of ints (for other widths)
        """
        if count < 0:
            count = len(self.queue_rx)
        if self._config.word_width == 8:
            data = bytearray()
        else:
            data = []
        for k in range(count):
            data.append(self.queue_rx.popleft())
        return data

    def count_tx(self) -> int:
        """Return number of words in transmit queue"""
        return len(self.queue_tx)

    def empty_tx(self) -> bool:
        """Return True if transmit queue is empty"""
        return not self.queue_tx

    def count_rx(self) -> int:
        """Return number of words in receive queue"""
        return len(self.queue_rx)

    def empty_rx(self) -> bool:
        """Return True if receive queue is empty"""
        return not self.queue_rx

    def idle(self) -> bool:
        """Return True if both TX and RX queues are empty"""
        return self.empty_tx() and self.empty_rx()

    def clear(self) -> None:
        """Clear both RX and TX queues"""
        self.queue_tx.clear()
        self.queue_rx.clear()

    async def wait(self) -> None:
        """Wait until all transactions are complete"""
        await self._idle.wait()

    async def _run(self):
        """Main coroutine that processes the transmit queue"""
        while True:
            while not self.queue_tx:
                self._sclk.value = int(self._config.cpol)
                self._idle.set()
                self.sync.clear()
                await self.sync.wait()

            tx_word, burst = self.queue_tx.popleft()
            rx_word = 0

            self.log.debug("Write word 0x%x", tx_word)

            # The timing diagrams and CPHA/CPOL conventions come from
            # https://en.wikipedia.org/wiki/Serial_Peripheral_Interface
            # This is also compliant with Linux Kernel definition of SPI

            # CPHA=0: First bit is clocked out on edge of chip select
            if not self._config.cpha:
                bits_per_cycle = self._config.data_width
                first_shift = self._config.word_width - bits_per_cycle
                first_data = (tx_word >> first_shift) & ((1 << bits_per_cycle) - 1)
                self._drive_data(first_data)

            # Assert chip select
            if self.has_cs:
                self._cs.value = int(not self._config.cs_active_low)
            await Timer(self._SpiClock.period, unit='step')

            # Start clock and perform data transfer
            await self._SpiClock.start()
            rx_word = await self._transfer_word(tx_word)
            await self._SpiClock.stop()
            self._sclk.value = self._config.cpol

            # Wait another clock period before restoring signals to idle
            await Timer(self._SpiClock.period, unit='step')
            self._set_data_idle()
            if self.has_cs:
                if not burst or self.empty_tx():
                    self._cs.value = int(self._config.cs_active_low)

            # Wait before starting next transaction
            if not 0 == self._config.frame_spacing_ns:
                await Timer(self._config.frame_spacing_ns, unit='ns')

            if not self._config.msb_first:
                rx_word = reverse_word(rx_word, self._config.word_width)

            # If ignore_rx_value is set, skip words matching that value
            if rx_word != self._config.ignore_rx_value:
                self.queue_rx.append(rx_word)

            self.sync.set()

    async def _transfer_word(self, tx_word: int) -> int:
        """
        Transfer one word of data across multiple clock cycles.

        For x1 mode: transfers 1 bit per cycle
        For x2 mode: transfers 2 bits per cycle
        For x4 mode: transfers 4 bits per cycle
        And so on...

        Args:
            tx_word: Word to transmit

        Returns:
            Received word
        """
        rx_word = 0
        cycles = self._config.cycles_per_word
        bits_per_cycle = self._config.data_width

        if self._config.cpha:
            # CPHA=1: Drive data on first edge, sample on second edge
            for cycle in range(cycles):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1
                tx_bits = (tx_word >> shift) & mask

                # First edge: drive data
                await self._sclk.value_change
                self._drive_data(tx_bits)

                # Second edge: sample data
                await self._sclk.value_change
                rx_bits = self._sample_data()
                rx_word |= (rx_bits << shift)
        else:
            # CPHA=0: Sample on first edge, drive on second edge
            # Note: First data unit was already driven before CS assertion
            for cycle in range(cycles - 1):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1

                # First edge: sample data
                await self._sclk.value_change
                rx_bits = self._sample_data()
                rx_word |= (rx_bits << shift)

                # Second edge: drive next data unit
                await self._sclk.value_change
                next_shift = shift - bits_per_cycle
                if next_shift >= 0:
                    next_tx_bits = (tx_word >> next_shift) & mask
                    self._drive_data(next_tx_bits)

            # Final sample for the last data unit
            await self._sclk.value_change
            rx_bits = self._sample_data()
            rx_word |= (rx_bits << 0)

        return rx_word

    def _drive_data(self, bits: int):
        """
        Drive data bits onto the bus.

        For standard mode (x1): Drives single bit on MOSI
        For multi-lane mode (x2+): Drives parallel bits on io0, io1, io2, etc.

        Args:
            bits: Data bits to drive (LSB aligned)
        """
        if self._mode == 'multi_lane':
            num_lanes = min(len(self._io_lines), self._config.data_width)
            for i in range(num_lanes):
                self._io_lines[i].value = (bits >> i) & 1
        else:  # standard
            self._mosi.value = bits & 1

    def _sample_data(self) -> int:
        """
        Sample data bits from the bus.

        For standard mode (x1): Samples single bit from MISO
        For multi-lane mode (x2+): Samples parallel bits from io0, io1, io2, etc.

        Returns:
            Sampled data bits (LSB aligned)
        """
        if self._mode == 'multi_lane':
            bits = 0
            num_lanes = min(len(self._io_lines), self._config.data_width)
            for i in range(num_lanes):
                bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        else:  # standard
            return int(self._miso.value) & 1

    def _set_data_idle(self):
        """Set all data lines to idle state"""
        if self._mode == 'standard':
            self._mosi.value = int(self._config.data_output_idle)
        elif self._mode == 'multi_lane':
            for line in self._io_lines:
                line.value = int(self._config.data_output_idle)


class SpiSlaveBase(ABC):
    """
    Universal SPI Slave base class supporting x1 through x32 modes

    Subclasses must implement _transaction() to define slave behavior.
    """
    _config: SpiConfig

    def __init__(self, bus: SpiBus):
        self.log = logging.getLogger(f"cocotb.{bus.sclk._path}")
        self._sclk = bus.sclk
        self._detect_and_configure_pins(bus)
        self._cs = bus.cs
        self.idle = Event()
        self.idle.set()
        self._run_coroutine_obj = None
        self._restart()

    def _detect_and_configure_pins(self, bus: SpiBus):
        """Detect and configure pins (same logic as master)"""
        # Check for io pins
        if hasattr(bus, 'io0'):
            self._io_lines = []
            i = 0
            while hasattr(bus, f'io{i}'):
                self._io_lines.append(getattr(bus, f'io{i}'))
                i += 1

            if self._config.data_width == 1:
                self._mode = 'standard'
                self._mosi = self._io_lines[0]
                self._miso = self._io_lines[1] if len(self._io_lines) > 1 else self._io_lines[0]
                self._miso.value = self._config.data_output_idle
            else:
                self._mode = 'multi_lane'
                for line in self._io_lines:
                    line.value = self._config.data_output_idle
            return

        # Standard mosi/miso
        if hasattr(bus, 'mosi') and hasattr(bus, 'miso'):
            self._mode = 'standard'
            self._mosi = bus.mosi
            self._miso = bus.miso
            self._miso.value = self._config.data_output_idle
            return

        raise RuntimeError("Slave: No valid SPI pins found")

    def _restart(self):
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def _shift(self, num_bits: int, tx_word: Optional[int] = None) -> int:
        """
        Shift in data on the MOSI signal. Shift out tx_word on the MISO signal.

        Supports multi-lane operation based on data_width configuration.

        Args:
            num_bits: The number of bits to shift
            tx_word: The word to be transmitted on the wire (if any)

        Returns:
            The received word on the MOSI line
        """
        rx_word = 0
        bits_per_cycle = self._config.data_width
        cycles = (num_bits + bits_per_cycle - 1) // bits_per_cycle

        frame_end = RisingEdge(self._cs) if self._config.cs_active_low else FallingEdge(self._cs)

        for cycle in range(cycles):
            bits_this_cycle = min(bits_per_cycle, num_bits - cycle * bits_per_cycle)
            shift = num_bits - (cycle + 1) * bits_per_cycle
            if shift < 0:
                shift = 0
            mask = (1 << bits_this_cycle) - 1

            # First edge
            if (await First(self._sclk.value_change, frame_end)) == frame_end:
                raise SpiFrameError("End of frame in the middle of a transaction")

            if self._config.cpha:
                # CPHA=1: Slave shifts out on first edge
                if tx_word is not None:
                    tx_bits = (tx_word >> shift) & mask
                    self._drive_data_slave(tx_bits)
                else:
                    self._drive_data_slave(self._config.data_output_idle)
            else:
                # CPHA=0: Slave samples on first edge
                rx_bits = self._sample_data_slave()
                rx_word |= (rx_bits & mask) << shift

            # Second edge
            if (await First(self._sclk.value_change, frame_end)) == frame_end:
                raise SpiFrameError("End of frame in the middle of a transaction")

            if self._config.cpha:
                # CPHA=1: Slave samples on second edge
                rx_bits = self._sample_data_slave()
                rx_word |= (rx_bits & mask) << shift
            else:
                # CPHA=0: Slave shifts out on second edge
                if tx_word is not None:
                    tx_bits = (tx_word >> shift) & mask
                    self._drive_data_slave(tx_bits)
                else:
                    self._drive_data_slave(self._config.data_output_idle)

        return rx_word

    def _drive_data_slave(self, bits: int):
        """Drive data bits onto the bus (slave perspective)"""
        if self._mode == 'multi_lane':
            num_lanes = min(len(self._io_lines), self._config.data_width)
            for i in range(num_lanes):
                self._io_lines[i].value = (bits >> i) & 1
        else:  # standard
            self._miso.value = bits & 1

    def _sample_data_slave(self) -> int:
        """Sample data bits from the bus (slave perspective)"""
        if self._mode == 'multi_lane':
            bits = 0
            num_lanes = min(len(self._io_lines), self._config.data_width)
            for i in range(num_lanes):
                bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        else:  # standard
            return int(self._mosi.value) & 1

    @abstractmethod
    async def _transaction(self, frame_start, frame_end):
        """
        Implement the details of an SPI transaction.

        Subclasses must override this method to define slave behavior.
        """
        raise NotImplementedError("Please implement the _transaction method")

    async def _run(self):
        """Main coroutine that waits for and processes transactions"""
        if self._config.cs_active_low:
            frame_start = FallingEdge(self._cs)
            frame_end = RisingEdge(self._cs)
        else:
            frame_start = RisingEdge(self._cs)
            frame_end = FallingEdge(self._cs)

        frame_spacing = Timer(self._config.frame_spacing_ns, unit='ns')

        while True:
            self.idle.set()
            if (await First(frame_start, frame_spacing)) == frame_start:
                raise SpiFrameError(f"There must be at least {self._config.frame_spacing_ns} ns between frames")
            await self._transaction(frame_start, frame_end)


class _SpiClock:
    """Internal clock generator for SPI master"""
    def __init__(self, signal, period, unit="step", start_high=True):
        self.period = cocotb.utils.get_sim_steps(period, unit, round_mode="round")
        self.half_period = cocotb.utils.get_sim_steps(period / 2.0, unit, round_mode="round")
        self.frequency = 1.0 / cocotb.utils.get_time_from_sim_steps(self.period, unit='us')
        self.signal = signal
        self.start_high = start_high
        self._idle = Event()
        self._sync = Event()
        self._start = Event()
        self._idle.set()
        self._run_coroutine_obj = None
        self._restart()

    def _restart(self):
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def stop(self) -> None:
        self.stop_no_wait()
        await self._idle.wait()

    def stop_no_wait(self) -> None:
        self._start.clear()
        self._sync.set()

    async def start(self) -> None:
        self.start_no_wait()

    def start_no_wait(self) -> None:
        self._start.set()
        self._sync.set()

    async def _run(self):
        t = Timer(self.half_period)
        if self.start_high:
            while True:
                while not self._start.is_set():
                    self._idle.set()
                    self._sync.clear()
                    await self._sync.wait()
                self._idle.clear()
                self.signal.value = 1
                await t
                if self._start.is_set():
                    self.signal.value = 0
                    await t
        else:
            while True:
                while not self._start.is_set():
                    self._idle.set()
                    self._sync.clear()
                    await self._sync.wait()
                self._idle.clear()
                self.signal.value = 0
                await t
                if self._start.is_set():
                    self.signal.value = 1
                    await t


def reverse_word(n: int, width: int) -> int:
    """Reverse the bit order of a word"""
    return int('{:0{width}b}'.format(n, width=width)[::-1], 2)
