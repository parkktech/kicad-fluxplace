# `parts-fetch`

### Datasheets through an ordered source chain — `fluxplace parts-fetch [MPN ...] [--spec S] [--constraints C] [--mpn-map M] [--datasheets D] [--refresh] [--verbose] [--json]`

For each MPN (or every spec part without a datasheet on disk) the chain runs until one candidate is a VALID file. Every attempt is
recorded with its source, URL and outcome, so a failure names what was tried.

| # | source | notes |
|---|---|---|
| 1 | distributor APIs | the datasheet URL DigiKey / Mouser return (also for the packaging variant of the MPN: Mouser often lists the URL only on the base MPN); TI redirect pages are unwrapped |
| 2 | element14 / Farnell | product API, needs `ELEMENT14_API_KEY` (free); recorded as `skipped` without one |
| 3 | LCSC / JLCPCB | `jlcsearch` + JLCPCB's part-detail endpoint give the LCSC code and its PDF link (`wmsc.lcsc.com` / `datasheet.lcsc.com`) |
| 4 | manufacturer | `MFR_TEMPLATES` in `dsfetch.py`: only hosts measured to serve plain clients (TI's `ti.com/lit/ds/symlink`) |
| 5 | Nexar | metered lookups, so last |

Validation (`dsfetch.validate_pdf`): `%PDF` header, >= 8 KB, `pdftotext` text, and the MPN or its base code (up to 3 packaging characters
dropped) in the text -> `part`. Only the series prefix -> `series` (kept only when nothing better exists, flagged `level: series` in
`datasheets.json`). Neither -> rejected with the reason ("wrong or generic document"). A valid file on disk is never fetched again; a
failed MPN is negative-cached 24 h (`--refresh` forces). Candidate files for `parts-todo` alternatives go to `<datasheets>/_candidates/`.

Conduct: plain HTTP with an honest user agent through `redact.urlopen`; no browser impersonation, cookies or CAPTCHA handling. A 403 /
bot challenge is recorded as the outcome and the chain moves on; a manufacturer file you fetch by hand is adopted with the same checks.
Exit 1 when any MPN could not be fetched.
