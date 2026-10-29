# Sequential logic and reset semantics

Keywords: clock clk reset rst counter register state FSM synchronous asynchronous polarity edge 时钟 复位 计数器 状态机

Use the exact clock edge, reset polarity, and synchronous/asynchronous behavior
specified by the task. Synchronous reset is checked inside a clock-edge block;
asynchronous reset also appears in the sensitivity list. Use nonblocking
assignments for registers. Check reset priority against enable/load and the first
active cycle after reset. Do not invent a reset port.

For combinational next-state logic, assign defaults before branches and cover the
default state. This prevents accidental latches and undefined transitions.
