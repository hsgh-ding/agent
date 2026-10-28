# Arithmetic width and signedness

Keywords: arithmetic width signed unsigned overflow carry borrow modulo gcd GCD divider addition subtraction 计算 位宽 有符号 溢出 最大公约数

Verilog expression width and signedness can silently change arithmetic results.
Size constants and intermediate expressions explicitly; preserve carry/borrow
bits until the intended truncation point. Check zero, one, maximum, and
wraparound cases. For iterative arithmetic, make each state transition and
termination condition explicit, and ensure every register is updated on the
intended clock edge.

Do not impose a particular architecture when the task specifies latency, reset,
or throughput requirements.
