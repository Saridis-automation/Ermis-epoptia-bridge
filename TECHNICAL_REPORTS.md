Temporary technical reports
===========================

Internal bridge analysis code can call `technical_reports.create(findings)`.
The result contains an opaque `report_id` for `ermis_technical_report_read`.
Accepted findings are counts (integers 0–1,000,000): `files_checked`,
`checks_passed`, `checks_failed`, `missing_metadata`, `unsupported_fields`,
and `warnings`. Unknown fields and non-count values are omitted and set
`redacted=true`. Do not pass prose, logs, environment values or source records.
This API does not run analysis itself or change existing Codex report behavior.

Reports live only in the ignored `.technical-reports/` directory (0700; files
0600). Never force-add this directory to Git. Each report is at most 4096 bytes,
with at most 100 active reports and a lifetime of 1–300 seconds (default 300).
Only IDs registered in the creating process can be consumed. Disk content is
never returned or opened for reading; responses use the sanitized registered
payload, preventing replacement files from injecting data.

Reads serialize with cleanup and delete before returning success. A transport
failure after consumption cannot be retried. Expired reports are inaccessible;
a background worker deletes them within one second of expiry while the process
runs. Deletion failures return no content and retry on subsequent expiry sweeps.
After downtime, orphan files are discarded when the store next initializes.
No cleanup can run while the process is stopped, and filesystem deletion errors
can delay physical removal. A second concurrent store fails closed.

Only a read tool is exposed. Producers must use the internal structured API;
there is no remote creation tool, arbitrary path input, or Epoptia write endpoint.
