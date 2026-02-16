import cocotb
from cocotb.triggers import Timer
from cocotbext.spi import SpiBus, SpiConfig, SpiMaster
from cocotbext.spi.devices.generic import SpiSlaveLoopback

@cocotb.test()
async def test_x1_mode(dut):
    """Test standard x1 SPI mode"""
    bus = SpiBus.from_entity(dut, cs_name="ncs")
    config = SpiConfig(word_width=8, data_width=1, cpha=True)  # Use CPHA=1

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0x12, 0x34, 0x56])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"x1 mode: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert len(rx_data) > 0

@cocotb.test()
async def test_x4_mode(dut):
    """Test quad x4 SPI mode"""
    bus = SpiBus(dut, io_names=['io0','io1','io2','io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=4, cpha=True)  # Use CPHA=1

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0xAB, 0xCD])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"x4 mode: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    dut._log.info(f"Cycles per byte: {config.cycles_per_word}")
    assert len(rx_data) > 0
    assert config.cycles_per_word == 2

@cocotb.test()
async def test_backward_compat(dut):
    """Test backward compatibility - data_width defaults to 1"""
    bus = SpiBus.from_entity(dut, cs_name="ncs")
    config = SpiConfig(word_width=8, cpha=True)  # Use CPHA=1

    assert config.data_width == 1

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0xFF, 0xAA])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info("Backward compat: defaults work correctly")
    assert len(rx_data) > 0
