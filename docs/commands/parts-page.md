# `parts-page`

### Render a datasheet page to PNG — `fluxplace parts-page MPN --datasheets D [--page N] [--png out.png] [--dpi 110] [--pages]`

For datasheets whose pin table is a drawing (no extractable text, `PINMAP_EVIDENCE_WEAK`): the picture goes to the agent, which reads
it. `--pages` prints only the page count. Pair with [parts-cite](parts-cite.md).
