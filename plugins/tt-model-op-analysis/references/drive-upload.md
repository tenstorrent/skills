# Google Drive upload

Only when the host lists Google Drive and Google Sheets connector tools and the user says yes.
Otherwise say the CSVs are in `<run dir>` and stop.

1. Ask for the Drive folder (link or name). Create a subfolder named after the model if the user
   wants one.
2. New run: create one spreadsheet named `<Model> kernel op table (<static|measured>)`.
   Overwrite with an existing `drive_url`: write into that spreadsheet. Merge: add a `Changes` tab.
3. One tab per CSV, `Summary` first (from `summary.md`), in the order the skill lists them.
   Write each table with one call from its top-left cell. Totals in the sheet are formulas over
   the table (`=SUM(...)`), not typed numbers.
4. Sheet row = CSV `id` + 1 (header is row 1). Before any targeted cell write, read the rows and
   confirm the `id` column; after writing, read the range again and check it.
5. Record the URL in `run.json` (`drive_url`) and link it in the reply.
