# Verilog/SystemVerilog coding rules

- Use nonblocking assignments in clocked `always_ff` or `always @(posedge clk)` blocks.
- Give every state-holding register a reset value when the interface specifies reset.
- Make widths explicit in arithmetic and comparisons; do not rely on unsized literals.
- Use a single driver for each signal. Avoid mixing continuous and procedural drivers.
- Keep combinational next-state logic complete, with defaults that prevent inferred latches.
- For parameterized logic, check zero and one-width corner cases and use `$clog2` carefully.
- Do not change the configured top-level name, ports, clock constraints, or reset polarity to hide an error.
