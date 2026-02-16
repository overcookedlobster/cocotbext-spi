TOPLEVEL_LANG = verilog
SIM ?= icarus
WAVES ?= 1

COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

DUT      = test_spi
TOPLEVEL = $(DUT)
MODULE   ?= $(DUT)  # ← Changed = to ?= so it can be overridden

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

# Add convenience targets
.PHONY: test-x1 test-x4 test-multi test-all

test-x1:
	@$(MAKE) MODULE=test_spi_multi TESTCASE=test_x1_mode

test-x4:
	@$(MAKE) MODULE=test_spi_multi TESTCASE=test_x4_mode

test-multi:
	@$(MAKE) MODULE=test_spi_multi

test-all:
	@$(MAKE) MODULE=test_spi_multi
	@echo "✓ All multi-width tests passed!"
