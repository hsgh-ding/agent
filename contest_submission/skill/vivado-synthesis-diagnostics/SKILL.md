---
name: vivado-synthesis-diagnostics
description: Diagnose Vivado RTL compile, elaboration, and synthesis failures using the first actionable log message and a minimal, evidence-based repair.
---

<!--
Adapted from AMD Ross Agentic AI Assistant, skills/vivado-rtl-elaboration-analysis.
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Vivado RTL diagnostics

## Skill Metadata
- **Name:** `vivado_synthesis_diagnostics`
- **Description:** Diagnose Vivado compile, elaboration, and synthesis failures using the first actionable log message and a minimal, evidence-based repair.
- **Trigger:** Vivado compile/elaboration or synthesis returns an error or warning that blocks the current candidate.
- **Keywords:** `Vivado`, `synthesis`, `综合`, `编译`, `elaboration`, `VRFC`, `wire`, `reg`
- **Log signatures:** `ERROR:`, `CRITICAL WARNING:`, `VRFC`, `Synth`, `xvlog`, `xelab`

## Workflow

1. Read the first actionable `ERROR` in the current tool output. Later messages may be consequences of the first one. Keep the exact message, source location, and failing stage together.
2. Classify the failure before editing: interface, syntax/analysis, elaboration, or synthesis. Do not change the algorithm to repair a declaration or syntax error.
3. Compare the reported source location with the current RTL. Make the smallest complete-file change that addresses that message while preserving the specified module and port contract.
4. Re-run the failed stage on the changed candidate. Continue to the next stage only after the current stage passes; report unavailable tools as unavailable, not as a pass.
5. If a repair response is byte-identical to the failed candidate, do not repeat that candidate indefinitely. Use a deterministic repair only when the compiler diagnosis identifies an unambiguous source transformation; otherwise give the model a focused diagnosis and the current source once more.

## Common Vivado diagnostics

- `VRFC 10-3180` / `cannot find port`: compare every port name and direction with the declared interface. Preserve the required names; do not invent aliases or extra ports.
- `VRFC 10-2063` / module not found: correct the top module name or the selected top, based on the project contract.
- `VRFC 10-4982` / syntax error: inspect the reported line and the preceding declaration for a missing semicolon, malformed range, or unmatched `begin`/`end`.
- `VRFC 10-1280` / procedural assignment to a non-register: identify the exact left-hand-side signal. If it is an ANSI output declared as `output name` or `output wire name` and assigned procedurally, declare it as `output reg name` (or `output logic name` in SystemVerilog). Preserve its clock edge, reset, and cycle behavior; do not add an output pipeline or sticky state as a workaround. If the target is inherited, non-ANSI, or otherwise ambiguous, ask the model to repair it rather than applying a broad text replacement.
- `Synth 8-*`: use the first specific message and source location to determine whether the cause is an unsupported construct, latch, multiple driver, or width/signedness issue. Do not infer functionality from synthesis success.

## Output and evidence rules

- Keep the module interface unchanged unless the diagnostic proves the declared contract itself is wrong.
- Never put a testbench, simulation-only stimulus, or success marker in the submitted DUT.
- A clean compile or synthesis result proves structure only; hidden functional tests determine behavioral correctness.
- Summarize only what the log establishes. Include the tool stage and exact first error signature in repair context.

## Scope limits

This version targets the competition package's local `xvlog`, `xelab`, and Vivado batch flow. The upstream Ross skill contains broader workflows that use Vivado MCP and project/implementation reports; those steps are not injected into this offline, hidden-testbench RTL runner.
