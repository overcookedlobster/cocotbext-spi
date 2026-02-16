`timescale 1ns / 1ps

// Minimal testbench for multi-width SPI testing
// Supports x1 (mosi/miso) and x4 (io0-io3)

module test_spi
(
    // Standard SPI signals
    inout wire sclk,
    inout wire mosi,
    inout wire miso,
    inout wire ncs,

    // Multi-lane signals (for x2, x4 modes)
    inout wire io0,
    inout wire io1,
    inout wire io2,
    inout wire io3,

    // Configuration (for parametrized testing)
    inout wire [1:0] spi_mode,
    inout wire [5:0] spi_word_width
);

    // This is a minimal testbench
    // Cocotb will drive all signals
    // No internal logic needed

endmodule
