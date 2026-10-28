---
name: vivado-synthesis-diagnostics
description: Classify Vivado compile, elaboration, and synthesis diagnostics.
---

# Vivado diagnostic repair

## Skill Metadata
- **Name:** `vivado_synthesis_diagnostics`
- **Description:** Read the first actionable Vivado error and make a minimal RTL repair.
- **Trigger:** A lint or synthesis tool returns a nonzero result.
- **Keywords:** `Vivado`, `synthesis`, `综合`, `编译`, `elaboration`, `综合失败`
- **Log signatures:** `ERROR:`, `CRITICAL WARNING:`, `VRFC`, `Synth`, `xvlog`, `xelab`

## Triage

1. Fix the first error; later messages may be cascading failures.
2. `VRFC 10-3180` or `10-2063` means the module/port contract is wrong. Compare
   it character by character before touching logic.
3. `VRFC 10-4982` is usually a header semicolon, unmatched begin/end, or malformed
   declaration near the reported line.
4. `Synth` errors usually mean an unsupported construct, multiple driver, latch,
   bad width/signedness, or an accidental testbench construct.
5. Preserve the latest known-good interface and make the smallest complete-file repair.
