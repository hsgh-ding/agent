---
name: rtl-fsm-and-widths
description: Review FSM transitions and arithmetic widths before synthesis.
---

# FSM and arithmetic width repair

## Skill Metadata
- **Name:** `rtl_fsm_and_widths`
- **Description:** Prevent incomplete state transitions and silent truncation.
- **Trigger:** The task contains an FSM, state machine, arithmetic datapath, or counter.
- **Keywords:** `state`, `fsm`, `case`, `counter`, `width`, `bits`, `gcd`, `division`, `modulo`, `sequence`, `detector`, `pattern`, `overlap`, `detect`, `加法`, `计数`, `状态机`, `最大公约数`
- **Log signatures:** `width`, `truncat`, `case`, `latch`, `Mismatches:`

## Rules

- Enumerate reset, idle, each legal transition, and the default/illegal state.
- Give combinational next-state and output logic defaults before the `case`.
- Make constants and intermediate expressions wide enough for carry, borrow, or
  signedness; do not assume Verilog will infer the intended width.
- Check wraparound and boundary values explicitly. A synthesizable expression can
  still implement the wrong modulo behavior.
- Keep the interface unchanged while repairing internal state or arithmetic.

## Sequence detector timing

- Translate the requested bit pattern into explicit prefix states. Each state means how many leading pattern bits currently match the most recent input history.
- For every input bit, choose the next state from the longest suffix of the observed history that is also a prefix of the target pattern. This preserves valid overlapping matches when the specification allows overlap; do not reset to idle automatically after every match.
- Follow the specified output timing. For a one-cycle event pulse, clear the pulse by default on each active clock and assert it only on the cycle the full pattern is recognized. Do not add a second output register unless the interface contract explicitly requires that latency.
- Walk through reset, the shortest matching input, a near-match, repeated matches, and overlap cases by clock edge before emitting RTL. For a detector of `1101`, for example, the post-match history still ends in `1`, which is also a one-bit prefix; whether to retain it depends on the task's overlap rule.
