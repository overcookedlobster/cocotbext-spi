# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Spencer Chang
# Modified to support ANY data width (x1, x2, x4, x8, x16, x32, x64...)
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
    Universal SPI Bus supporting ANY data width
    Supports:
    - Standard SPI (x1): mosi, miso
    - Multi-lane (x2-x8): io0, io1, io2, ...
    - Parallel (x16+): input_bus, output_bus
    """
    def __init__(
        self,
        entity=None,
        prefix=None,
        sclk_name='sclk',
        mosi_name='mosi',
        miso_name='miso',
        cs_name=None,
        # Multi-lane support
        io_names=None,  # List of io pin names
        # Parallel bus support
        input_bus_name=None,
        output_bus_name=None,
        **kwargs,
    ):
        signals = {'sclk': sclk_name}
        optional_signals = {}

        # Standard SPI
        if mosi_name:
            optional_signals['mosi'] = mosi_name
        if miso_name:
            optional_signals['miso'] = miso_name

        # Multi-lane SPI
        if io_names:
            for i, name in enumerate(io_names):
                optional_signals[f'io{i}'] = name

        # Parallel buses
        if input_bus_name:
            optional_signals['input_bus'] = input_bus_name
        if output_bus_name:
            optional_signals['output_bus'] = output_bus_name

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
    """Universal SPI configuration for ANY data width"""
    word_width: int = 8
    sclk_freq: Optional[float] = 25e6
    cpol: bool = False
    cpha: bool = False
    msb_first: bool = True
    frame_spacing_ns: int = 1
    data_output_idle: int = 1
    ignore_rx_value: Optional[int] = None
    cs_active_low: bool = True

    # NEW: Data width parameter (defaults to 1 for backward compatibility)
    data_width: int = 1  # Number of parallel data lanes

    @property
    def cycles_per_word(self) -> int:
        """Number of clock cycles needed to transfer one word"""
        return (self.word_width + self.data_width - 1) // self.data_width


class SpiMaster:
    """
    Universal SPI Master supporting ANY data width
    Works for x1, x2, x4, x8, x16, x32, x64... any width!
    """
    def __init__(self, bus: SpiBus, config: SpiConfig) -> None:
        self.log = logging.getLogger(f"cocotb.{bus.sclk._path}")

        # SPI signals
        self._sclk = bus.sclk
        self.has_cs = hasattr(bus, 'cs')
        if self.has_cs:
            self._cs = bus.cs

        # Configuration
        self._config = config

        # Configure data lines
        self._configure_data_lines(bus)

        # Queues
        self.queue_tx: Deque[Tuple[int, bool]] = deque()
        self.queue_rx: Deque[int] = deque()

        self.sync = Event()

        self._idle = Event()
        self._idle.set()

        # Initialize signals
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

    def _configure_data_lines(self, bus: SpiBus):
        """Auto-detect and configure data lines"""
        # Check for parallel buses (highest priority)
        if hasattr(bus, 'output_bus'):
            self._mode = 'parallel'
            self._output_bus = bus.output_bus
            self._input_bus = bus.input_bus if hasattr(bus, 'input_bus') else None
            return

        # Check for multi-lane (io0, io1, ...)
        if hasattr(bus, 'io0'):
            self._mode = 'multi_lane'
            self._io_lines = []
            i = 0
            while hasattr(bus, f'io{i}'):
                self._io_lines.append(getattr(bus, f'io{i}'))
                i += 1
            return

        # Standard SPI (mosi/miso)
        self._mode = 'standard'
        self._mosi = bus.mosi
        self._miso = bus.miso

    def _init_data_lines(self):
        """Initialize data line outputs"""
        if self._mode == 'parallel':
            if hasattr(self, '_output_bus'):
                self._output_bus.value = 0
        elif self._mode == 'multi_lane':
            for line in self._io_lines:
                line.value = self._config.data_output_idle
        else:  # standard
            self._mosi.value = self._config.data_output_idle

    def _restart(self) -> None:
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def write(self, data: Iterable[int], *, burst: bool = False):
        self.write_nowait(data, burst=burst)
        await self._idle.wait()

    def write_nowait(self, data: Iterable[int], *, burst: bool = False) -> None:
        """ Write the data to the output lines """
        if self._config.msb_first:
            for b in data:
                self.queue_tx.append((int(b), burst))
        else:
            for b in data:
                self.queue_tx.append((reverse_word(int(b), self._config.word_width), burst))
        self.sync.set()
        self._idle.clear()

    async def read(self, count: int = -1):
        while self.empty_rx():
            self.sync.clear()
            await self.sync.wait()
        return self.read_nowait(count)

    def read_nowait(self, count: int = -1) -> Iterable[int]:
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
        return len(self.queue_tx)

    def empty_tx(self) -> bool:
        return not self.queue_tx

    def count_rx(self) -> int:
        return len(self.queue_rx)

    def empty_rx(self) -> bool:
        return not self.queue_rx

    def idle(self) -> bool:
        return self.empty_tx() and self.empty_rx()

    def clear(self) -> None:
        """ Clears the RX and TX queues """
        self.queue_tx.clear()
        self.queue_rx.clear()

    async def wait(self) -> None:
        """ Wait for idle """
        await self._idle.wait()

    async def _run(self):
        """Main transfer loop - unified for ALL data widths!"""
        while True:
            while not self.queue_tx:
                self._sclk.value = int(self._config.cpol)
                self._idle.set()
                self.sync.clear()
                await self.sync.wait()

            tx_word, burst = self.queue_tx.popleft()
            rx_word = 0

            self.log.debug(f"Transfer word 0x{tx_word:02x} (data_width={self._config.data_width})")

            # For CPHA=0, drive first data unit on CS edge (before clock starts)
            # This is CRITICAL for backward compatibility with standard SPI timing
            if not self._config.cpha:
                bits_per_cycle = self._config.data_width
                first_shift = self._config.word_width - bits_per_cycle
                first_data = (tx_word >> first_shift) & ((1 << bits_per_cycle) - 1)
                self._drive_data(first_data)

            # Set chip select
            if self.has_cs:
                self._cs.value = int(not self._config.cs_active_low)
            await Timer(self._SpiClock.period, unit='step')

            await self._SpiClock.start()

            # Transfer word using configured data width
            rx_word = await self._transfer_word(tx_word)

            # Set sclk back to idle state
            await self._SpiClock.stop()
            self._sclk.value = self._config.cpol

            # Wait another sclk period
            await Timer(self._SpiClock.period, unit='step')
            self._set_data_idle()
            if self.has_cs:
                if not burst or self.empty_tx():
                    self._cs.value = int(self._config.cs_active_low)

            # Frame spacing
            if not 0 == self._config.frame_spacing_ns:
                await Timer(self._config.frame_spacing_ns, unit='ns')

            if not self._config.msb_first:
                rx_word = reverse_word(rx_word, self._config.word_width)

            # Store received data
            if rx_word != self._config.ignore_rx_value:
                self.queue_rx.append(rx_word)

            self.sync.set()

    async def _transfer_word(self, tx_word: int) -> int:
        """
        Transfer one word - works for ANY data width!
        Handles both CPHA=0 and CPHA=1 correctly
        """
        rx_word = 0
        cycles = self._config.cycles_per_word
        bits_per_cycle = self._config.data_width

        if self._config.cpha:
            # CPHA=1: Simple case - drive on first edge, sample on second
            for cycle in range(cycles):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1
                tx_bits = (tx_word >> shift) & mask

                await self._sclk.value_change
                self._drive_data(tx_bits)

                await self._sclk.value_change
                rx_bits = self._sample_data()
                rx_word |= (rx_bits << shift)
        else:
            # CPHA=0: More complex - first data already driven on CS edge
            # Need to do (cycles-1) sample-drive iterations, then final sample
            for cycle in range(cycles - 1):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1

                # Sample on first edge
                await self._sclk.value_change
                rx_bits = self._sample_data()
                rx_word |= (rx_bits << shift)

                # Drive next data unit on second edge
                await self._sclk.value_change
                next_shift = shift - bits_per_cycle
                if next_shift >= 0:
                    next_tx_bits = (tx_word >> next_shift) & mask
                    self._drive_data(next_tx_bits)

            # Final sample (for the last data unit)
            await self._sclk.value_change
            rx_bits = self._sample_data()
            # Shift for last position
            last_shift = 0
            rx_word |= (rx_bits << last_shift)

        return rx_word

    def _drive_data(self, bits: int):
        """Drive data on output lines - works for any mode"""
        if self._mode == 'parallel':
            self._output_bus.value = bits
        elif self._mode == 'multi_lane':
            for i in range(min(len(self._io_lines), self._config.data_width)):
                self._io_lines[i].value = (bits >> i) & 1
        else:  # standard
            self._mosi.value = bits & 1

    def _sample_data(self) -> int:
        """Sample data from input lines - works for any mode"""
        if self._mode == 'parallel':
            return int(self._input_bus.value) if self._input_bus else 0
        elif self._mode == 'multi_lane':
            bits = 0
            for i in range(min(len(self._io_lines), self._config.data_width)):
                bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        else:  # standard
            return int(self._miso.value) & 1

    def _set_data_idle(self):
        """Set data lines to idle state"""
        if self._mode == 'standard':
            self._mosi.value = int(self._config.data_output_idle)
        elif self._mode == 'multi_lane':
            for line in self._io_lines:
                line.value = int(self._config.data_output_idle)
        # Parallel mode doesn't need idle state


class SpiSlaveBase(ABC):
    _config: SpiConfig

    def __init__(self, bus: SpiBus):
        self.log = logging.getLogger(f"cocotb.{bus.sclk._path}")

        self._sclk = bus.sclk

        # Configure data lines
        self._configure_data_lines(bus)

        self._cs = bus.cs

        self.idle = Event()
        self.idle.set()

        self._run_coroutine_obj = None
        self._restart()

    def _configure_data_lines(self, bus: SpiBus):
        """Auto-detect and configure data lines"""
        # Check for parallel buses
        if hasattr(bus, 'input_bus'):
            self._mode = 'parallel'
            self._input_bus = bus.input_bus
            self._output_bus = bus.output_bus if hasattr(bus, 'output_bus') else None
            if self._output_bus:
                self._output_bus.value = self._config.data_output_idle
            return

        # Check for multi-lane
        if hasattr(bus, 'io0'):
            self._mode = 'multi_lane'
            self._io_lines = []
            i = 0
            while hasattr(bus, f'io{i}'):
                self._io_lines.append(getattr(bus, f'io{i}'))
                i += 1
            for line in self._io_lines:
                line.value = self._config.data_output_idle
            return

        # Standard SPI
        self._mode = 'standard'
        self._mosi = bus.mosi
        self._miso = bus.miso
        self._miso.value = self._config.data_output_idle

    def _restart(self):
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def _shift(self, num_bits: int, tx_word: Optional[int] = None) -> int:
        """
        Shift data - works for ANY data width!
        Uses configured data_width automatically
        """
        rx_word = 0
        bits_per_cycle = self._config.data_width
        cycles = (num_bits + bits_per_cycle - 1) // bits_per_cycle

        frame_end = RisingEdge(self._cs) if self._config.cs_active_low else FallingEdge(self._cs)

        for cycle in range(cycles):
            # Calculate bits for this cycle
            bits_this_cycle = min(bits_per_cycle, num_bits - cycle * bits_per_cycle)
            shift = num_bits - (cycle + 1) * bits_per_cycle
            if shift < 0:
                shift = 0
            mask = (1 << bits_this_cycle) - 1

            if (await First(self._sclk.value_change, frame_end)) == frame_end:
                raise SpiFrameError("End of frame in the middle of a transaction")

            if self._config.cpha:
                # CPHA=1: shift out on first edge
                if tx_word is not None:
                    tx_bits = (tx_word >> shift) & mask
                    self._drive_data_slave(tx_bits)
                else:
                    self._drive_data_slave(self._config.data_output_idle)
            else:
                # CPHA=0: sample on first edge
                rx_bits = self._sample_data_slave()
                rx_word |= (rx_bits & mask) << shift

            if (await First(self._sclk.value_change, frame_end)) == frame_end:
                raise SpiFrameError("End of frame in the middle of a transaction")

            if self._config.cpha:
                # CPHA=1: sample on second edge
                rx_bits = self._sample_data_slave()
                rx_word |= (rx_bits & mask) << shift
            else:
                # CPHA=0: shift out on second edge
                if tx_word is not None:
                    tx_bits = (tx_word >> shift) & mask
                    self._drive_data_slave(tx_bits)
                else:
                    self._drive_data_slave(self._config.data_output_idle)

        return rx_word

    def _drive_data_slave(self, bits: int):
        """Drive data from slave perspective"""
        if self._mode == 'parallel':
            if self._output_bus:
                self._output_bus.value = bits
        elif self._mode == 'multi_lane':
            for i in range(min(len(self._io_lines), self._config.data_width)):
                self._io_lines[i].value = (bits >> i) & 1
        else:  # standard
            self._miso.value = bits & 1

    def _sample_data_slave(self) -> int:
        """Sample data from slave perspective"""
        if self._mode == 'parallel':
            return int(self._input_bus.value) if hasattr(self, '_input_bus') else 0
        elif self._mode == 'multi_lane':
            bits = 0
            for i in range(min(len(self._io_lines), self._config.data_width)):
                bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        else:  # standard
            return int(self._mosi.value) & 1

    @abstractmethod
    async def _transaction(self, frame_start, frame_end):
        """Implement the details of an SPI transaction """
        raise NotImplementedError("Please implement the _transaction method")

    async def _run(self):
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
    return int('{:0{width}b}'.format(n, width=width)[::-1], 2)
