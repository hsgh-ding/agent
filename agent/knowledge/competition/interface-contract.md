# Exact module and port interface

Keywords: mismatch missing undeclared direction 接口 端口

The declared interface is the scoring contract. Preserve the top module name,
port names, directions, widths, and order. Do not add clock/reset/debug ports
unless the task declares them. If `interface.txt` is empty, use explicit module
and port declarations from the prompt as evidence, but treat prose-derived port
lists as advisory when the prompt is ambiguous. Do not infer a missing port.

This entry is reference guidance only. The task and official interface take
precedence over examples.
