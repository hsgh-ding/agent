# Task context and candidate files

- Treat each task as independent. Use only its prompt, declared interface,
  selected short knowledge notes, current candidate, and the latest actionable
  Vivado diagnostic.
- Keep full RTL and complete logs on disk. Send concise diagnostics to the model
  while preserving the original records for audit.
- Write `solution.v` atomically so an interrupted write cannot leave a partial
  candidate.
- The competition output contains only synthesizable DUT RTL. No testbench,
  simulation stimulus, or test-result marker belongs in the submission file.
- A successful interface check, elaboration, or synthesis does not establish
  functional correctness; the official hidden testbench determines that.
