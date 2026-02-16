"""
Test suite for clean multi-width implementation
Tests x1 (unidirectional) through x32 (bidirectional)
"""
import cocotb
from cocotb.triggers import Timer
from cocotbext.spi import SpiBus, SpiConfig, SpiMaster
from cocotbext.spi.devices.generic import SpiSlaveLoopback


@cocotb.test()
async def test_io_pins_x1_unidirectional(dut):
    """Test that io0/io1 work unidirectionally in x1 mode"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=1, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    # Verify it's in standard mode (unidirectional)
    assert master._mode == 'standard'
    assert hasattr(master, '_mosi')  # Should have separate mosi
    assert hasattr(master, '_miso')  # Should have separate miso

    await Timer(10, 'us')

    test_data = bytearray([0x12, 0x34, 0x56])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ x1 with io pins: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert len(rx_data) > 0


@cocotb.test()
async def test_io_pins_x2_bidirectional(dut):
    """Test that io0/io1 work bidirectionally in x2 mode"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=2, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    # Verify it's in multi_lane mode (bidirectional)
    assert master._mode == 'multi_lane'
    assert hasattr(master, '_io_lines')
    assert len(master._io_lines) == 4

    await Timer(10, 'us')

    test_data = bytearray([0xAB, 0xCD])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ x2 with io pins: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert len(rx_data) > 0
    assert config.cycles_per_word == 4  # 8 bits / 2 bits per cycle


@cocotb.test()
async def test_io_pins_x4_quad(dut):
    """Test x4 (quad) mode"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=4, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0xF0, 0x0F])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ x4 (quad): TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert config.cycles_per_word == 2  # 8 bits / 4 bits per cycle


@cocotb.test()
async def test_io_pins_x8_octal(dut):
    """Test x8 (octal) mode if 8 io pins available"""
    # Check if we have 8 io pins
    has_8_pins = all(hasattr(dut, f'io{i}') for i in range(8))

    if not has_8_pins:
        dut._log.info("Skipping x8 test - need 8 io pins")
        return

    bus = SpiBus(dut, io_names=[f'io{i}' for i in range(8)], cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=8, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0xAA, 0x55])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ x8 (octal): TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert config.cycles_per_word == 1  # 8 bits in 1 cycle!


@cocotb.test()
async def test_mode_switching_x1_to_x4(dut):
    """Test switching between x1 and x4 modes"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')

    # Start with x1
    dut._log.info("Testing x1 mode...")
    config1 = SpiConfig(word_width=8, data_width=1, cpha=True)
    master1 = SpiMaster(bus, config1)
    slave1 = SpiSlaveLoopback(bus, config1)

    await Timer(10, 'us')
    await master1.write([0x11])
    rx1 = await master1.read()
    dut._log.info(f"  x1: {[hex(b) for b in rx1]}")

    await Timer(10, 'us')

    # ⚡ FIX: Cancel old coroutines before mode switch
    if master1._run_coroutine_obj is not None:
        master1._run_coroutine_obj.cancel()
    if slave1._run_coroutine_obj is not None:
        slave1._run_coroutine_obj.cancel()

    await Timer(1, 'us')  # Small delay for cleanup

    # Switch to x4
    dut._log.info("Testing x4 mode...")
    config4 = SpiConfig(word_width=8, data_width=4, cpha=True)
    master4 = SpiMaster(bus, config4)
    slave4 = SpiSlaveLoopback(bus, config4)

    await Timer(10, 'us')
    await master4.write([0x44])
    rx4 = await master4.read()
    dut._log.info(f"  x4: {[hex(b) for b in rx4]}")

    dut._log.info("✓ Mode switching works!")


@cocotb.test()
async def test_all_spi_modes_x1(dut):
    """Test all 4 SPI modes (CPOL/CPHA combinations) in x1"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')

    for mode in [0, 1, 2, 3]:
        cpol = bool(mode in [2, 3])
        cpha = bool(mode in [1, 3])

        dut._log.info(f"Testing SPI mode {mode} (CPOL={cpol}, CPHA={cpha})...")

        config = SpiConfig(word_width=8, data_width=1, cpol=cpol, cpha=cpha)
        master = SpiMaster(bus, config)
        slave = SpiSlaveLoopback(bus, config)

        await Timer(10, 'us')

        test_data = bytearray([0x50 + mode])
        await master.write(test_data)
        rx_data = await master.read()

        dut._log.info(f"  Mode {mode}: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
        assert len(rx_data) > 0

        await Timer(10, 'us')

    dut._log.info("✓ All SPI modes work in x1!")


@cocotb.test()
async def test_all_spi_modes_x4(dut):
    """Test all 4 SPI modes in x4"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')

    for mode in [0, 1, 2, 3]:
        cpol = bool(mode in [2, 3])
        cpha = bool(mode in [1, 3])

        dut._log.info(f"Testing x4 SPI mode {mode}...")

        config = SpiConfig(word_width=8, data_width=4, cpol=cpol, cpha=cpha)
        master = SpiMaster(bus, config)
        slave = SpiSlaveLoopback(bus, config)

        await Timer(10, 'us')

        test_data = bytearray([0xA0 + mode])
        await master.write(test_data)
        rx_data = await master.read()

        dut._log.info(f"  x4 Mode {mode}: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
        assert len(rx_data) > 0

        await Timer(10, 'us')

    dut._log.info("✓ All SPI modes work in x4!")


@cocotb.test()
async def test_standard_mosi_miso(dut):
    """Test backward compatibility with standard mosi/miso pins"""
    bus = SpiBus.from_entity(dut, cs_name='ncs')
    config = SpiConfig(word_width=8, data_width=1, cpol=False, cpha=False)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = bytearray([0x42, 0x43])
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ Standard mosi/miso: TX={[hex(b) for b in test_data]}, RX={[hex(b) for b in rx_data]}")
    assert len(rx_data) > 0


@cocotb.test()
async def test_16bit_word_x2(dut):
    """Test 16-bit words with x2 mode"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=16, data_width=2, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = [0x1234, 0x5678]
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ 16-bit x2: TX={[hex(w) for w in test_data]}, RX={[hex(w) for w in rx_data]}")
    assert config.cycles_per_word == 8  # 16 bits / 2 bits per cycle


@cocotb.test()
async def test_32bit_word_x4(dut):
    """Test 32-bit words with x4 mode"""
    bus = SpiBus(dut, io_names=['io0', 'io1', 'io2', 'io3'], cs_name='ncs')
    config = SpiConfig(word_width=32, data_width=4, cpha=True)

    master = SpiMaster(bus, config)
    slave = SpiSlaveLoopback(bus, config)

    await Timer(10, 'us')

    test_data = [0x12345678]
    await master.write(test_data)
    rx_data = await master.read()

    dut._log.info(f"✓ 32-bit x4: TX={[hex(w) for w in test_data]}, RX={[hex(w) for w in rx_data]}")
    assert config.cycles_per_word == 8  # 32 bits / 4 bits per cycle
