# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Spencer Chang
# Transmits the previously received word on the next transaction

from collections import deque

from cocotb.triggers import First

from ..exceptions import SpiFrameError
from ..spi import SpiBus, SpiConfig, SpiSlaveBase, reverse_word


class SpiSlaveLoopback(SpiSlaveBase):
    def __init__(self, bus: SpiBus, config: SpiConfig):
        self._config = config
        self._out_queue = deque()
        self._out_queue.append(0)
        super().__init__(bus)

    async def get_contents(self):
        await self.idle.wait()
        if self._config.msb_first:
            return self._out_queue[0]
        else:
            return reverse_word(self._out_queue[0], self._config.word_width)

    async def _transaction(self, frame_start, frame_end):
        await frame_start
        self.idle.clear()

        tx_word = self._out_queue.popleft()
        bits_per_cycle = self._config.data_width

        if not self._config.cpha:
            # CPHA=0: Drive first data unit on frame start (before any clock edges)
            # This matches master behavior for CPHA=0
            first_shift = self._config.word_width - bits_per_cycle
            first_data = (tx_word >> first_shift) & ((1 << bits_per_cycle) - 1)
            self._drive_data_slave(first_data)

            # Calculate remaining bits after driving first unit
            remaining_bits = self._config.word_width - bits_per_cycle

            if remaining_bits > 0:
                # Shift remaining bits, passing tx_word for subsequent outputs
                content = int(await self._shift(remaining_bits, tx_word=tx_word))
            else:
                content = 0

            # Final sample for the last data unit
            r = await First(self._sclk.value_change, frame_end)
            if r == frame_end:
                raise SpiFrameError("End of frame before last data was sampled")

            last_data = self._sample_data_slave()

            # Combine: shift existing content left and add last data
            content = (content << bits_per_cycle) | last_data
        else:
            # CPHA=1: Simple case, use standard shift
            content = int(await self._shift(self._config.word_width, tx_word=tx_word))

        await frame_end
        self._out_queue.append(content)
