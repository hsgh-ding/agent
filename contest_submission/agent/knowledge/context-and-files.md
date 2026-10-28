# Context budget and source files

- Treat the configured context window as a budget, not as proof of the model's
  advertised maximum. Ollama num_ctx must be set explicitly and kept within
  the model/runtime capacity.
- Reserve room for the answer and tool definitions. Recalculate the output
  allowance whenever a retry adds messages.
- Store complete RTL revisions and reports on disk. Put only the current task,
  fixed constraints, a small relevant knowledge excerpt, and selected source
  lines into the prompt.
- For large code, write bounded chunks to a draft file and commit only after
  the full file is present. Keep the frozen testbench separate from RTL drafts.
- Never truncate an unresolved diagnostic merely to fit the prompt. Keep the
  original on disk and pause for a larger context or a smaller task if the
  required context still cannot fit.
