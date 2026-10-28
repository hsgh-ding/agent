---
name: rtl-design-checklist
description: Short pre-generation checklist for synthesizable RTL and exact interfaces.
always: true
---

# RTL design checklist

Use this short checklist before writing the first candidate. The task statement
and declared interface are authoritative; this note is only a reminder.

- Declare exactly the requested top module, ports, directions, order, and widths.
- Do not add a testbench, stimulus, delays, `$finish`, file I/O, or debug ports.
- Separate combinational and clocked logic. Use `<=` in clocked blocks and give
  combinational outputs a default value on every path.
- Treat reset polarity and synchronous/asynchronous behavior exactly as stated.
- Give arithmetic operands explicit widths and handle zero/one-width corner cases.
- Keep one driver per signal and avoid unsized constants in comparisons.

Before accepting a candidate, mentally check reset, first-cycle behavior, and
the smallest and largest legal input values.
