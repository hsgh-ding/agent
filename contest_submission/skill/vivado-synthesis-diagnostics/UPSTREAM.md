# Upstream source and license

This local skill is adapted from AMD Ross Agentic AI Assistant's
[`vivado-rtl-elaboration-analysis`](https://github.com/Xilinx/ross-ai-assistant/tree/2026.10.1/skills/vivado-rtl-elaboration-analysis)
skill, release `2026.10.1`, source commit
[`ac75f40a2d78b2bb4538ef049cc62be826a0c6ba`](https://github.com/Xilinx/ross-ai-assistant/commit/ac75f40a2d78b2bb4538ef049cc62be826a0c6ba).

The adaptation keeps the diagnostic principles of locating the first actionable
message, tying it to source evidence, making the smallest justified repair, and
re-running the failed stage. It removes MCP-only tool instructions and narrows
the scope to the local `xvlog`, `xelab`, and Vivado batch flow used by this
competition package. The local `VRFC 10-1280` repair rule does not rewrite RTL
behavior; it only changes a clearly identified ANSI output net declaration.

AMD's upstream repository is licensed under MIT. The upstream notice and full
license text are included in [`AMD-ROSS-LICENSE`](AMD-ROSS-LICENSE).
