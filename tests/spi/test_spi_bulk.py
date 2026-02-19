"""
Test suite for the Extreme-Performance Bulk Transfer optimization.
Verifies x1, x4 modes, and the asynchronous progress queue.
"""
import cocotb
from cocotb.triggers import Timer
from cocotb.queue import Queue
from cocotbext.spi import SpiBus, SpiConfig, SpiMaster, SpiSlaveBase

class PassiveCaptureSlave(SpiSlaveBase):
    """A lightweight slave that simply records all incoming bytes."""
    def __init__(self, bus, config):
        self._config = config
        self.captured_words = []
        super().__init__(bus)

    async def _transaction(self, frame_start, frame_end):
        await frame_start
        self.idle.clear()

        while True:
            # Shift in one word at a time until the frame ends
            try:
                rx_word = await self._shift(self._config.word_width)
                self.captured_words.append(rx_word)
            except Exception:
                # Frame ended
                break

        await frame_end

@cocotb.test()
async def test_bulk_write_x1(dut):
    """Test bulk write in standard x1 (unidirectional) mode."""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=1, cpha=True)

    master = SpiMaster(bus, config)
    slave = PassiveCaptureSlave(bus, config)

    await Timer(10, 'us')

    # Generate a decent sized payload
    test_data = bytes([i % 256 for i in range(1024)])  # 1 KB

    dut._log.info("Starting x1 bulk transfer...")
    master.write_bulk_nowait(test_data)

    # Wait for the master to finish blasting the data
    await master.wait()
    await Timer(1, 'us')

    dut._log.info(f"Captured {len(slave.captured_words)} words.")
    assert len(slave.captured_words) == len(test_data)
    assert list(test_data) == slave.captured_words
    dut._log.info("✓ x1 bulk transfer matches perfectly!")


@cocotb.test()
async def test_bulk_write_x4_quad(dut):
    """Test bulk write in Quad SPI (x4) mode."""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=4, cpha=True)

    master = SpiMaster(bus, config)
    slave = PassiveCaptureSlave(bus, config)

    await Timer(10, 'us')

    test_data = bytes([0xDE, 0xAD, 0xBE, 0xEF] * 256) # 1 KB

    dut._log.info("Starting x4 Quad SPI bulk transfer...")
    master.write_bulk_nowait(test_data)

    await master.wait()
    await Timer(1, 'us')

    assert list(test_data) == slave.captured_words
    dut._log.info("✓ x4 Quad SPI bulk transfer matches perfectly!")


@cocotb.test()
async def test_bulk_write_progress_queue(dut):
    """Verify that the background progress queue fires exactly 20 times (5% chunks)."""
    bus = SpiBus.from_entity(dut, cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=1, cpha=True)

    master = SpiMaster(bus, config)

    # We don't strictly need a slave for this, we are just testing the monitor logic
    progress_queue = Queue()

    await Timer(10, 'us')

    # 10,000 bytes = 80,000 bits.
    # 5% chunk of 80,000 is exactly 4,000 bits.
    test_data = bytes([0xAA] * 10000)

    dut._log.info("Starting bulk transfer with progress monitoring...")
    master.write_bulk_nowait(test_data, progress_queue=progress_queue)

    updates = 0
    last_val = 0

    # Monitor the queue while the transfer happens in the background
    while True:
        sent_bits = await progress_queue.get()
        updates += 1
        dut._log.info(f"Progress update {updates}: {sent_bits} bits sent")

        assert sent_bits > last_val, "Progress should constantly increase"
        last_val = sent_bits

        if sent_bits >= (len(test_data) * 8):
            break

    await master.wait()

    # Since we sliced it into 20 chunks (5% each), we should see exactly 20 updates
    assert updates == 20, f"Expected 20 progress updates, got {updates}"
    dut._log.info("✓ Progress queue chunking works perfectly without slowing down the hot loop!")
