# forecast-loop daily forecast draft (v2)

Work only on the already prepared handoff supplied by the external operator.
The prompt and handoff paths are declarative input, not permission to broaden
filesystem or network access.

1. Do not run the manifest's `prepare.command` or `finalize.command`, and do not
   start a subprocess, service, scheduler or model API.
2. Read only the supplied prompt, `INSTRUCTIONS.md`, `input.json` and
   `drafts.template.json` paths.
3. Follow the immutable handoff instructions and create only the supplied
   `drafts.json` path. Do not edit the input, template, receipt, database,
   checkpoint, Wiki, job manifest, execution state or upstream data.
4. Use only frozen evidence allowed by the input package. Do not add facts
   published after its cutoff and do not invent missing causes or citations.
5. Validate the completed draft against the supplied template. Leave
   deterministic finalize and receipt verification to the external operator.
6. Report only whether `drafts.json` was created or why the draft stage was
   blocked.

forecast-loop is a research and audit system. Never place orders, operate an
account, or write to an upstream market-data owner.
