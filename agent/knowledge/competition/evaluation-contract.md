# RTL competition evaluation contract

Keywords: competition evaluation hidden testbench reference testbench RTL output L1 L2 L3

The task gives the agent the specification and interface, not the official
functional testbench. The evaluated artifact is the DUT RTL in `solution.v`.
Do not generate or submit a testbench, add simulation stimulus to the DUT, or
claim functional correctness from compilation or synthesis.

The official evaluator advances through compilation/elaboration, functional
simulation, and synthesis. Local interface checks, source hygiene, elaboration,
and synthesis can identify useful failure classes, but they cannot reproduce
the hidden functional comparison. Preserve this distinction in the trace and
reports.
