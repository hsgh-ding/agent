---
name: rtl-reset-and-clock
description: Diagnose reset polarity, clock edge, and sequential timing mistakes.
---

# Reset and clock repair

## Skill Metadata
- **Name:** `rtl_reset_and_clock`
- **Description:** Repair clocked RTL without changing its external interface.
- **Trigger:** A counter, register, FSM, pipeline, or reset behavior is wrong.
- **Keywords:** `clock`, `clk`, `reset`, `rst`, `counter`, `fsm`, `pipeline`, `同步`, `复位`
- **Log signatures:** `latch`, `multiple driver`, `Mismatches:`, `simulation failed`

## Rules

- Synchronous reset belongs only in `always @(posedge clk)`; asynchronous reset
  belongs in the sensitivity list with the stated edge and polarity.
- Use nonblocking assignments for state and registers. Do not use a blocking
  assignment to observe a register's new value in the same clocked block.
- Check reset priority against load/enable priority and define the first active
  clock edge after reset.
- Keep next-state defaults complete; do not hide a latch by adding an unrelated
  clock or reset.
- Change only the behavior supported by the diagnostic. Preserve the port list.
