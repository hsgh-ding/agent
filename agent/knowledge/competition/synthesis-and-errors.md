# Vivado compile and synthesis triage

Keywords: Vivado xvlog xelab synth synthesis latch driver VRFC error compile 综合 错误 编译 锁存 多驱动

Repair the first actionable error in the Vivado log; later errors may be
cascading. `VRFC 10-3180` commonly means a port name mismatch, `VRFC 10-2063`
means the requested top module was not found, and `VRFC 10-4982` points to a
syntax problem near the reported line or just before it. Synthesis errors may
indicate unsupported simulation-only constructs, width/signedness problems,
multiple drivers, or inferred latches.

The competition task does not provide its hidden reference testbench to the
agent. Lint and synthesis success are not proof of functional correctness; report
only the checks that actually ran.
