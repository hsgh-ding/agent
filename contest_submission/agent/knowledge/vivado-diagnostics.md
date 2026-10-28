# Vivado diagnostic triage

关键词：仿真失败、错误日志、综合失败、编译报错、诊断报告。

- Read the complete recorded diagnostics before changing RTL; line numbers refer
  to the saved revision, not to a later answer.
- Syntax, undeclared signal, width, and multiple-driver errors are compile-stage
  problems. Fix the source and submit a new revision.
- A simulation without the exact pass marker is a failure, even if Vivado exits
  successfully. Investigate assertions, `$fatal`, X propagation, reset timing,
  and testbench watchdog output.
- Synthesis failures can expose unsupported constructs, latch inference, clock
  misuse, or unconstrained paths. Read the synthesis report before editing.
