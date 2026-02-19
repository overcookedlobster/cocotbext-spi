TOPLEVEL_LANG = verilog
SIM ?= icarus
WAVES ?= 1

COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

DUT      = test_spi
TOPLEVEL = $(DUT)
MODULE   ?= $(DUT)  # Allows overriding the test module

VERILOG_SOURCES = $(DUT).v

ifeq ($(SIM), icarus)
	PLUSARGS += -fst
	ifeq ($(WAVES), 1)
		VERILOG_SOURCES += iverilog_dump.v
		COMPILE_ARGS += -s iverilog_dump
	endif
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

iverilog_dump.v:
	echo 'module iverilog_dump();' > $@
	echo 'initial begin' >> $@
	echo '    $$dumpfile("$(TOPLEVEL).fst");' >> $@
	echo '    $$dumpvars(0, $(TOPLEVEL));' >> $@
	echo 'end' >> $@
	echo 'endmodule' >> $@

clean::
	@rm -rf iverilog_dump.v
	@rm -rf dump.fst $(TOPLEVEL).fst
	@rm -rf results.xml
	@rm -rf __pycache__
	@rm -rf sim_build

# ─────────────────────────────────────────────────────────────────────────────
# Convenience test targets
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: test-x1 test-x4 test-multi test-bulk test-all

# Run specific multi-width tests (Updated to use TEST_FILTER)
test-x1:
	@$(MAKE) MODULE=test_spi_multi TEST_FILTER=test_io_pins_x1_unidirectional

test-x4:
	@$(MAKE) MODULE=test_spi_multi TEST_FILTER=test_io_pins_x4_quad

# Run the entire multi-width suite
test-multi:
	@$(MAKE) MODULE=test_spi_multi

# Run the new Extreme-Performance Bulk suite
test-bulk:
	@$(MAKE) MODULE=test_spi_bulk

# Run everything
test-all:
	@echo "--- Running Standard SPI Tests ---"
	@$(MAKE) MODULE=test_spi
	@echo "--- Running Multi-Lane SPI Tests ---"
	@$(MAKE) MODULE=test_spi_multi
	@echo "--- Running Extreme-Performance Bulk Tests ---"
	@$(MAKE) MODULE=test_spi_bulk
	@echo "============================================================"
	@echo "✓ All SPI tests (Standard, Multi-lane, and Bulk) passed!"
	@echo "============================================================"
