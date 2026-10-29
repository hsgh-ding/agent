# SystemVerilog testbench guidance

- Instantiate the configured DUT top and drive every input deterministically.
- Generate clocks and reset with bounded timing. Include a watchdog shorter than
  the configured simulation limit.
- Check observable outputs against explicit expected values with `$fatal(1, ...)`.
- Print `RTL_AGENT_TEST_PASS` on its own line only after every check passes, then
  call `$finish`.
- A self-testbench is useful for initial development, but a frozen independent
  testbench must not be weakened during RTL repair.
