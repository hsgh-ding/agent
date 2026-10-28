---
name: rtl-fsm-and-widths
description: Review FSM transitions and arithmetic widths before synthesis.
---

# FSM and arithmetic width repair

## Skill Metadata
- **Name:** `rtl_fsm_and_widths`
- **Description:** Prevent incomplete state transitions and silent truncation.
- **Trigger:** The task contains an FSM, state machine, arithmetic datapath, or counter.
- **Keywords:** `state`, `fsm`, `case`, `counter`, `width`, `bits`, `gcd`, `division`, `modulo`, `加法`, `计数`, `状态机`, `最大公约数`
- **Log signatures:** `width`, `truncat`, `case`, `latch`, `Mismatches:`

## Rules

- Enumerate reset, idle, each legal transition, and the default/illegal state.
- Give combinational next-state and output logic defaults before the `case`.
- Make constants and intermediate expressions wide enough for carry, borrow, or
  signedness; do not assume Verilog will infer the intended width.
- Check wraparound and boundary values explicitly. A synthesizable expression can
  still implement the wrong modulo behavior.
- Keep the interface unchanged while repairing internal state or arithmetic.
