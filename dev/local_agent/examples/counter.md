# 8-bit counter example

Implement module top with inputs clk, active-low synchronous reset rst_n,
and enable; output an 8-bit count. Reset sets count to zero on the
next rising clock edge. When enabled, increment by one with natural 8-bit
wraparound. When disabled, hold the previous value.
