# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Spencer Chang
# Clean multi-width implementation: io pins work correctly in x1 (unidirectional) and x2+ (bidirectional)
# Includes Extreme-Performance Bulk Transfer Optimization & Simplex Support

import array
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from itertools import islice
from typing import Deque, Iterable, Optional, Tuple

import cocotb
from cocotb.triggers import Event, FallingEdge, First, RisingEdge, Timer

from .exceptions import SpiFrameError


class Bus:
    """
    A simple bus class to manage signal connections.
    """
    def __init__(self, entity=None, prefix=None, signals=None, optional_signals=None, **kwargs):
        self.entity = entity
        self.prefix = prefix
        all_signals = {}
        if signals:
            all_signals.update(signals)
        if optional_signals:
            all_signals.update(optional_signals)

        for attr_name, signal_name in all_signals.items():
            full_signal_name = f"{prefix}_{signal_name}" if prefix else signal_name
            signal_handle = getattr(entity, full_signal_name, None)
            if signal_handle is None and attr_name in (optional_signals or {}):
                continue
            setattr(self, attr_name, signal_handle)


class SpiBus(Bus):
    """Universal SPI Bus supporting Simplex, Duplex, and Multi-lane"""
    def __init__(
        self, entity=None, prefix=None, sclk_name='sclk',
        mosi_name='mosi', miso_name='miso', cs_name=None, io_names=None, **kwargs,
    ):
        signals = {'sclk': sclk_name}
        optional_signals = {}

        if io_names:
            for i, name in enumerate(io_names):
                optional_signals[f'io{i}'] = name
        else:
            if mosi_name: optional_signals['mosi'] = mosi_name
            if miso_name: optional_signals['miso'] = miso_name

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
    word_width: int = 8
    sclk_freq: Optional[float] = 25e6
    cpol: bool = False
    cpha: bool = False
    msb_first: bool = True
    frame_spacing_ns: int = 1
    data_output_idle: int = 1
    ignore_rx_value: Optional[int] = None
    cs_active_low: bool = True
    data_width: int = 1

    @property
    def cycles_per_word(self) -> int:
        return (self.word_width + self.data_width - 1) // self.data_width


class SpiMaster:
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

        self.bulk_states = None
        self.bulk_progress_queue = None

        self.sync = Event()
        self._idle = Event()
        self._idle.set()

        self._sclk.value = int(self._config.cpol)
        self._init_data_lines()
        if self.has_cs:
            self._cs.value = 1 if self._config.cs_active_low else 0

        self._SpiClock = _SpiClock(
            signal=self._sclk, period=(1 / self._config.sclk_freq),
            unit="sec", start_high=self._config.cpha,
        )
        self._run_coroutine_obj = None
        self._restart()

    def _detect_and_configure_pins(self, bus: SpiBus):
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
            else:
                self._mode = 'multi_lane'
            return

        # Changed 'and' to 'or' to support Simplex (TX-only or RX-only)
        if hasattr(bus, 'mosi') or hasattr(bus, 'miso'):
            self._mode = 'standard'
            self._mosi = getattr(bus, 'mosi', None)
            self._miso = getattr(bus, 'miso', None)
            if self._config.data_width != 1:
                self._config.data_width = 1
            return
        raise RuntimeError("No valid SPI pins found")

    def _init_data_lines(self):
        if self._mode == 'multi_lane':
            for line in self._io_lines:
                line.value = self._config.data_output_idle
        elif self._mosi is not None:
            self._mosi.value = self._config.data_output_idle

    def _restart(self) -> None:
        if self._run_coroutine_obj is not None:
            self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def write(self, data: Iterable[int], *, burst: bool = False):
        self.write_nowait(data, burst=burst)
        await self._idle.wait()

    def write_nowait(self, data: Iterable[int], *, burst: bool = False) -> None:
        if self._config.msb_first:
            for b in data: self.queue_tx.append((int(b), burst))
        else:
            for b in data: self.queue_tx.append((reverse_word(int(b), self._config.word_width), burst))
        self.sync.set()
        self._idle.clear()

    def write_bulk_nowait(self, data_bytes: bytes, progress_queue=None) -> None:
        states = array.array('B')
        width = self._config.data_width
        self.log.info(f"Pre-expanding {len(data_bytes)} bytes for x{width} bulk transfer...")

        if width == 1:
            for b in data_bytes:
                states.extend(((b >> 7) & 1, (b >> 6) & 1, (b >> 5) & 1, (b >> 4) & 1,
                               (b >> 3) & 1, (b >> 2) & 1, (b >> 1) & 1, b & 1))
        elif width == 2:
            for b in data_bytes: states.extend(((b >> 6) & 3, (b >> 4) & 3, (b >> 2) & 3, b & 3))
        elif width == 4:
            for b in data_bytes: states.extend(((b >> 4) & 15, b & 15))
        elif width == 8:
            states.frombytes(data_bytes)
        else:
            raise ValueError(f"Bulk write not implemented for data_width={width}")

        self.bulk_states = states
        self.bulk_progress_queue = progress_queue
        self.sync.set()
        self._idle.clear()

    async def read(self, count: int = -1):
        while self.empty_rx():
            self.sync.clear()
            await self.sync.wait()
        return self.read_nowait(count)

    def read_nowait(self, count: int = -1) -> Iterable[int]:
        if count < 0: count = len(self.queue_rx)
        data = bytearray() if self._config.word_width == 8 else []
        for k in range(count): data.append(self.queue_rx.popleft())
        return data

    def count_tx(self) -> int: return len(self.queue_tx)
    def empty_tx(self) -> bool: return not self.queue_tx
    def count_rx(self) -> int: return len(self.queue_rx)
    def empty_rx(self) -> bool: return not self.queue_rx
    def idle(self) -> bool: return self.empty_tx() and self.empty_rx() and self.bulk_states is None

    def clear(self) -> None:
        self.queue_tx.clear(); self.queue_rx.clear()
        self.bulk_states = None; self.bulk_progress_queue = None

    async def wait(self) -> None:
        await self._idle.wait()

    async def _run(self):
        drive_edge = FallingEdge(self._sclk) if self._config.cpol == self._config.cpha else RisingEdge(self._sclk)

        while True:
            while not self.queue_tx and self.bulk_states is None:
                self._sclk.value = int(self._config.cpol)
                self._idle.set()
                self.sync.clear()
                await self.sync.wait()

            self._idle.clear()

            # --- 🚀 EXTREME PERFORMANCE BULK PATH ---
            if self.bulk_states is not None:
                if self.has_cs:
                    self._cs.value = int(not self._config.cs_active_low)

                total_states = len(self.bulk_states)
                chunk_size = max(1, total_states // 20)
                state_iterator = iter(self.bulk_states)
                sent = 0

                # Support for CPHA=0: Pre-drive the first bit before the clock starts
                if not self._config.cpha:
                    try:
                        first_state = next(state_iterator)
                        if self._mode == 'standard' and self._mosi is not None:
                            self._mosi.value = first_state
                        elif self._mode == 'multi_lane':
                            for i in range(self._config.data_width):
                                self._io_lines[i].value = (first_state >> i) & 1
                        sent += 1
                    except StopIteration:
                        pass

                await self._SpiClock.start()

                while sent < total_states:
                    if self._mode == 'standard':
                        mosi = self._mosi
                        if mosi is not None:
                            for state in islice(state_iterator, chunk_size):
                                await drive_edge
                                mosi.value = state
                        else: # RX-only edge case
                            for state in islice(state_iterator, chunk_size): await drive_edge
                    else:
                        lanes = self._io_lines
                        width = self._config.data_width
                        for state in islice(state_iterator, chunk_size):
                            await drive_edge
                            for i in range(width): lanes[i].value = (state >> i) & 1

                    sent += chunk_size
                    if self.bulk_progress_queue is not None:
                        self.bulk_progress_queue.put_nowait(min(sent, total_states))

                await self._SpiClock.stop()
                self._sclk.value = self._config.cpol
                await Timer(self._SpiClock.period, unit='step')
                self._set_data_idle()

                if self.has_cs and not self.queue_tx:
                    self._cs.value = int(self._config.cs_active_low)
                if not 0 == self._config.frame_spacing_ns:
                    await Timer(self._config.frame_spacing_ns, unit='ns')

                self.bulk_states = None; self.bulk_progress_queue = None

            # --- 🐢 STANDARD TRANSACTION PATH ---
            else:
                tx_word, burst = self.queue_tx.popleft()
                rx_word = 0
                if not self._config.cpha:
                    bits_per_cycle = self._config.data_width
                    first_shift = self._config.word_width - bits_per_cycle
                    self._drive_data((tx_word >> first_shift) & ((1 << bits_per_cycle) - 1))

                if self.has_cs: self._cs.value = int(not self._config.cs_active_low)
                await Timer(self._SpiClock.period, unit='step')

                await self._SpiClock.start()
                rx_word = await self._transfer_word(tx_word)
                await self._SpiClock.stop()
                self._sclk.value = self._config.cpol

                await Timer(self._SpiClock.period, unit='step')
                self._set_data_idle()
                if self.has_cs:
                    if not burst or self.empty_tx():
                        self._cs.value = int(self._config.cs_active_low)

                if not 0 == self._config.frame_spacing_ns:
                    await Timer(self._config.frame_spacing_ns, unit='ns')

                if not self._config.msb_first: rx_word = reverse_word(rx_word, self._config.word_width)
                if rx_word != self._config.ignore_rx_value: self.queue_rx.append(rx_word)
                self.sync.set()

    async def _transfer_word(self, tx_word: int) -> int:
        rx_word = 0; cycles = self._config.cycles_per_word; bits_per_cycle = self._config.data_width
        if self._config.cpha:
            for cycle in range(cycles):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1
                await self._sclk.value_change
                self._drive_data((tx_word >> shift) & mask)
                await self._sclk.value_change
                rx_word |= (self._sample_data() << shift)
        else:
            for cycle in range(cycles - 1):
                shift = (cycles - cycle - 1) * bits_per_cycle
                mask = (1 << bits_per_cycle) - 1
                await self._sclk.value_change
                rx_word |= (self._sample_data() << shift)
                await self._sclk.value_change
                next_shift = shift - bits_per_cycle
                if next_shift >= 0: self._drive_data((tx_word >> next_shift) & mask)
            await self._sclk.value_change
            rx_word |= (self._sample_data() << 0)
        return rx_word

    def _drive_data(self, bits: int):
        if self._mode == 'multi_lane':
            for i in range(min(len(self._io_lines), self._config.data_width)):
                self._io_lines[i].value = (bits >> i) & 1
        elif self._mosi is not None:
            self._mosi.value = bits & 1

    def _sample_data(self) -> int:
        if self._mode == 'multi_lane':
            bits = 0
            for i in range(min(len(self._io_lines), self._config.data_width)):
                bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        elif self._miso is not None:
            return int(self._miso.value) & 1
        return 0

    def _set_data_idle(self):
        if self._mode == 'standard':
            if self._mosi is not None: self._mosi.value = int(self._config.data_output_idle)
        elif self._mode == 'multi_lane':
            for line in self._io_lines: line.value = int(self._config.data_output_idle)


class SpiSlaveBase(ABC):
    _config: SpiConfig
    def __init__(self, bus: SpiBus):
        self.log = logging.getLogger(f"cocotb.{bus.sclk._path}")
        self._sclk = bus.sclk; self._detect_and_configure_pins(bus); self._cs = bus.cs
        self.idle = Event(); self.idle.set(); self._run_coroutine_obj = None; self._restart()

    def _detect_and_configure_pins(self, bus: SpiBus):
        if hasattr(bus, 'io0'):
            self._io_lines = []
            i = 0
            while hasattr(bus, f'io{i}'):
                self._io_lines.append(getattr(bus, f'io{i}'))
                i += 1
            if self._config.data_width == 1:
                self._mode = 'standard'; self._mosi = self._io_lines[0]
                self._miso = self._io_lines[1] if len(self._io_lines) > 1 else self._io_lines[0]
                self._miso.value = self._config.data_output_idle
            else:
                self._mode = 'multi_lane'
                for line in self._io_lines: line.value = self._config.data_output_idle
            return
        if hasattr(bus, 'mosi') or hasattr(bus, 'miso'):
            self._mode = 'standard'; self._mosi = getattr(bus, 'mosi', None); self._miso = getattr(bus, 'miso', None)
            if self._miso is not None: self._miso.value = self._config.data_output_idle
            return
        raise RuntimeError("Slave: No valid SPI pins found")

    def _restart(self):
        if self._run_coroutine_obj is not None: self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def _shift(self, num_bits: int, tx_word: Optional[int] = None) -> int:
        rx_word = 0; bits_per_cycle = self._config.data_width
        cycles = (num_bits + bits_per_cycle - 1) // bits_per_cycle
        frame_end = RisingEdge(self._cs) if self._config.cs_active_low else FallingEdge(self._cs)

        for cycle in range(cycles):
            bits_this_cycle = min(bits_per_cycle, num_bits - cycle * bits_per_cycle)
            shift = max(0, num_bits - (cycle + 1) * bits_per_cycle)
            mask = (1 << bits_this_cycle) - 1

            if (await First(self._sclk.value_change, frame_end)) == frame_end: raise SpiFrameError("Frame ended")
            if self._config.cpha:
                if tx_word is not None: self._drive_data_slave((tx_word >> shift) & mask)
                else: self._drive_data_slave(self._config.data_output_idle)
            else:
                rx_word |= (self._sample_data_slave() & mask) << shift

            if (await First(self._sclk.value_change, frame_end)) == frame_end: raise SpiFrameError("Frame ended")
            if self._config.cpha: rx_word |= (self._sample_data_slave() & mask) << shift
            else:
                if tx_word is not None: self._drive_data_slave((tx_word >> shift) & mask)
                else: self._drive_data_slave(self._config.data_output_idle)
        return rx_word

    def _drive_data_slave(self, bits: int):
        if self._mode == 'multi_lane':
            for i in range(min(len(self._io_lines), self._config.data_width)): self._io_lines[i].value = (bits >> i) & 1
        elif self._miso is not None: self._miso.value = bits & 1

    def _sample_data_slave(self) -> int:
        if self._mode == 'multi_lane':
            bits = 0
            for i in range(min(len(self._io_lines), self._config.data_width)): bits |= (int(self._io_lines[i].value) & 1) << i
            return bits
        elif self._mosi is not None: return int(self._mosi.value) & 1
        return 0

    @abstractmethod
    async def _transaction(self, frame_start, frame_end): raise NotImplementedError()

    async def _run(self):
        frame_start = FallingEdge(self._cs) if self._config.cs_active_low else RisingEdge(self._cs)
        frame_end = RisingEdge(self._cs) if self._config.cs_active_low else FallingEdge(self._cs)
        frame_spacing = Timer(self._config.frame_spacing_ns, unit='ns')
        while True:
            self.idle.set()
            if (await First(frame_start, frame_spacing)) == frame_start:
                raise SpiFrameError(f"Minimum {self._config.frame_spacing_ns} ns between frames")
            await self._transaction(frame_start, frame_end)


class _SpiClock:
    def __init__(self, signal, period, unit="step", start_high=True):
        self.period = cocotb.utils.get_sim_steps(period, unit, round_mode="round")
        self.half_period = cocotb.utils.get_sim_steps(period / 2.0, unit, round_mode="round")
        self.signal = signal; self.start_high = start_high
        self._idle = Event(); self._sync = Event(); self._start = Event(); self._idle.set()
        self._run_coroutine_obj = None; self._restart()

    def _restart(self):
        if self._run_coroutine_obj is not None: self._run_coroutine_obj.cancel()
        self._run_coroutine_obj = cocotb.start_soon(self._run())

    async def stop(self) -> None: self.stop_no_wait(); await self._idle.wait()
    def stop_no_wait(self) -> None: self._start.clear(); self._sync.set()
    async def start(self) -> None: self.start_no_wait()
    def start_no_wait(self) -> None: self._start.set(); self._sync.set()

    async def _run(self):
        t = Timer(self.half_period)
        if self.start_high:
            while True:
                while not self._start.is_set():
                    self._idle.set(); self._sync.clear(); await self._sync.wait()
                self._idle.clear(); self.signal.value = 1; await t
                if self._start.is_set(): self.signal.value = 0; await t
        else:
            while True:
                while not self._start.is_set():
                    self._idle.set(); self._sync.clear(); await self._sync.wait()
                self._idle.clear(); self.signal.value = 0; await t
                if self._start.is_set(): self.signal.value = 1; await t


def reverse_word(n: int, width: int) -> int:
    return int('{:0{width}b}'.format(n, width=width)[::-1], 2)
