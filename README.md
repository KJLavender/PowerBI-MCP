# PRISM — Proven Report Insights & Semantic Mapping

**English** | [繁體中文](README.zh-TW.md)

PRISM lets you ask questions about a Power BI report in plain language, such as "why did sales drop in the North region?" or "what does the red bar mean?". You upload the report file (`.pbix`) and any related Excel files. PRISM reads the file itself, so you do not need Power BI. It calculates the numbers with ordinary code, not with the language model, and checks every answer against those numbers before it shows you the answer. Everything runs on your own machine.

## What it does

- Answers "why" questions. The numbers come from the report's own measures (named formulas written in DAX, Power BI's formula language), translated to SQL and run on a local copy of the data. The language model only writes the sentences.
- Checks each answer with 9 mechanical checks: date, scope, numbers, names, number-to-name match, direction, significance, causes with no evidence, and the key finding. If an answer fails, it goes back to the model to be rewritten. If it still fails, PRISM shows the analysis tool's own reading instead.
- Explains what is on screen. The author's colour rules, target lines and highlighted members are read from the report file, so you can ask about "the red text" or "the dashed line on margin" without a screenshot.
- Compares reports and Excel files on one page, the report map. It finds measures that share a name but are calculated differently, measures with different names that mean the same thing, and Excel codes that do not match the database.
- Lets you forward a conversation. The next person opens the same report view and continues the same chat.

## How it works

1. PRISM unpacks the `.pbix` with [PBIXRay](https://github.com/Hugoberry/pbixray). It reads tables, measures, relationships, Power Query steps, the imported data, and what each chart on each page uses.
2. It guesses analysis settings from the data (fact table, date column, dimensions) and builds daily snapshots in DuckDB, a small local database.
3. A small local language model (Ollama, default `qwen3.5:4b`) answers by calling read-only tools through an MCP server. MCP is a standard way to give tools to a model. The tools return numbers that are already formatted, and the model has to copy them.
4. The checker compares the model's answer with the tool results.

## Run it

You need [Ollama](https://ollama.com/download) for the chat. PRISM downloads its model on first start. Uploading a report and the report map work without Ollama.

On Windows, install [Python 3.11 or newer](https://www.python.org/downloads/), then double-click `PRISM.cmd`. The first run creates a `.venv` and installs the packages, which takes a few minutes. Then the browser opens <http://localhost:8080>. The launcher script is `scripts/start-prism.ps1`, and `PRISM.cmd` passes its options on:

- `-Share` listens on all addresses so colleagues on the network can open it. Allow ports 8080 and 8101-8120 in the Windows firewall.
- `-Port 8080` changes the portal port. `-NoBrowser` does not open the browser.

With Docker (Linux, or Windows with Docker), this starts PRISM and an Ollama container:

```bash
docker compose up -d        # then open http://localhost:8080
```

With an NVIDIA GPU, uncomment the `deploy` block in `compose.yaml` to make the chat faster.

Then drag a `.pbix` onto the page, add Excel files if you have them, and press the upload button (the label on the page is "上傳並分析"). PRISM imports the file, builds the settings and snapshots, writes a first report and updates the map. The AdventureWorks sample with its workbook took 77 seconds. When it finishes, open the chat for that report.

Environment variables: `AGENT_MODEL` (Ollama model for the chat, default `qwen3.5:4b`), `OLLAMA_HOST` (default `http://127.0.0.1:11434`; the Docker setup points it at the `ollama` container), `PRISM_EXAMPLES` (`1` or `0` to show or hide the example reports).

The pages:

| Page | What it is for |
|---|---|
| My reports | Upload, processing steps, open a report's chat, attach more Excel, delete |
| Report map | Every report and workbook, shared sources, synonyms, conflicts, code checks, the author's rules |
| How it works | Why the answers can be trusted, the checked numbers, the limits |

Details, the HTTP API and sharing on a network are in [docs/prism.md](docs/prism.md). The docs are written in Traditional Chinese.

## What has been checked

These numbers come from the test reports in this repo (`reports/` and `datasets/*/reports/`).

| Check | Result |
|---|---|
| Engine numbers vs Power BI's own DAX results (retail report 4,176, Superstore report 6,140) | 10,316 numbers, 0 differences |
| Snapshots read from the `.pbix` file vs snapshots from Power BI Desktop | 52,885,918 values, 0 differences |
| Snapshots through Power BI Desktop vs reading Oracle directly | 544,515 values, 0 differences |
| Values of on-screen charts vs SQL on Oracle | 75 values, 0 differences |
| Questions about the retail report (20 questions) | 18 correct answers shown to the user |
| Questions about the Superstore report (15 questions) | 14 correct answers shown to the user |
| Wrong answers that passed the checks | 0 in all of the above |
| Mutation test (correct answers changed on purpose; the checks must catch them) | 98/99, 103/106 and 90/94 caught (96% to 99%) |
| Injected business events found, retail report | 6 of 6 |
| Injected business events found, Superstore report | 4 of 5 detectable ones |
| Format parser on Microsoft's 10 sample `.pbix` files | counts match the files |

## Development lab

PRISM grew out of a local lab that simulates a company's setup, with known business events planted in the data so answers can be scored. The retail report runs on an Oracle database that an ETL simulator refreshes every day, and it is published on Power BI Report Server with a 06:30 scheduled refresh. A second report is built from the public Global Superstore Excel workbook. A third, "store targets", mixes the retail Oracle data with an Excel target workbook that contains deliberate inconsistencies, and is used to test the cross-report map.

Example answer from `python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？"`:

> Conclusion: North region sales fell mainly because of 3C accessories (84% of the total change); "North x 3C accessories" is significantly abnormal.
> Evidence: North sales -19.1% (381,154 vs 471,022), below the significance threshold (z -3.0); one level deeper, North x 3C accessories -46.7%, z -4.7, significant ...
> Limits: the data cannot tell the business reason (competitors, stock-outs ...); needs business confirmation.

The lab needs Windows 11 with WSL2 (Ubuntu) and Docker Engine inside WSL, Python 3.11+, Power BI Desktop and Ollama. The Report Server steps also need Power BI Report Server (Developer edition), Power BI Desktop for Report Server and Oracle ODAC.

```powershell
# Oracle, simulated data and the Power BI project
copy .env.example .env                                  # then set the passwords
.\scripts\start-oracle.ps1
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m simulator init
.\.venv\Scripts\python -m simulator backfill            # 2025-01-01 to yesterday, about 2.7M rows
.\.venv\Scripts\python powerbi\build_pbip.py            # open powerbi\RetailSales.pbip, then Refresh
.\scripts\register-daily-task.ps1                       # daily 06:00: ETL + analysis

# Daily change report
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis report               # writes reports/<date>.md
.\.venv\Scripts\python -m analysis evaluate             # score against the planted events

# Ask why, with a local model
.\scripts\start-chat.ps1                                # report + chat panel at http://localhost:8090
.\.venv\Scripts\python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？" -v
.\.venv\Scripts\python -m agent chat
.\.venv\Scripts\python -m agent evaluate

# Report Server (one-time admin setup, then publish)
.\scripts\phase4-setup-admin.ps1                        # run as administrator
.\scripts\phase4-publish.ps1                            # upload, credentials, 06:30 schedule, refresh

# Verification
.\.venv\Scripts\python -m agent mutation-test           # change correct answers on purpose: are they caught?
.\.venv\Scripts\python -m analysis reconcile            # compare with Power BI Desktop's DAX results (open the report first)

# Several reports and the author's intent
.\.venv\Scripts\python datasets\store_targets\prepare.py
.\.venv\Scripts\python datasets\store_targets\build_pbip.py
.\.venv\Scripts\python -m analysis catalog              # writes reports/catalog.md
.\.venv\Scripts\python -m analysis intent store_targets # thresholds, target lines, highlights the author set

# Import any .pbix without Power BI (add --desktop to read it through Power BI Desktop instead)
.\.venv\Scripts\python -m analysis import-pbix FILE.pbix --name X

# Excel report (Global Superstore): the commands above work on it once PBI_DATASET is set
$env:PBI_DATASET = "superstore"
.\.venv\Scripts\python datasets\superstore\prepare.py   # downloads the workbook (SHA-256 checked) and writes the copy with planted events
.\.venv\Scripts\python datasets\superstore\build_pbip.py
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis evaluate
.\.venv\Scripts\python -m agent evaluate
```

`agent/mcp-config.example.json` shows how to start the read-only MCP server (`python -m analysis.mcp_server`) from another MCP client.

Test data in the repo: `powerbi/RetailSales.pbix` (the retail report with 2.7M order lines imported, in the older format that Report Server accepts, without data source credentials), `powerbi/RetailSales.pbip` (the same report as a Power BI project, generated by `powerbi/build_pbip.py`), and the evaluation results under `reports/` and `datasets/*/reports/`.

## Folders

| Path | Content |
|---|---|
| `prism/` | The web portal: uploads, processing jobs, one chat server per report, the report map |
| `analysis/` | Reads the model (TMDL, PBIR, DAX), snapshots, change detection, reports, MCP server |
| `agent/` | Local Ollama agent, answer checks, question sets, the chat page |
| `simulator/` | Oracle schema, master data, daily data generator with event injection |
| `powerbi/` | Project generator, converter to the older `.pbix` format for Report Server, the report files |
| `datasets/` | The Superstore report, the store-targets report, and the imported-`.pbix` test set |
| `scripts/` | PowerShell scripts for start-up, scheduled tasks, Report Server |
| `docker/` | Oracle Database Free and SQL Server Developer (the Report Server catalog) |
| `docs/` | Design notes, one per topic (Traditional Chinese) |

## Limits

- The analysis shows where a change happened. It cannot give the business reason unless the reason is in the data.
- Thresholds have to be tuned for each report. On the Excel data, the original settings reported 7.3 false findings a day until they were recalibrated on the untouched workbook. Small data only shows large events.
- The checks prove that an answer agrees with the analysis results. They cannot prove that the analysis picked the best explanation, so the evidence is always shown with the answer.
- Measure comparison covers common DAX patterns. `IF`, `SWITCH` and similar are reported as not expandable and are never treated as equal. Reports that mix Oracle and Excel are used for the map and for questions only, without daily change analysis.
- PRISM has no login or permissions. Use it on an internal network. Anyone with a forwarded link can see that conversation.
- Encrypted `.pbix` files (sensitivity labels) and DirectQuery reports (no data in the file) cannot be read directly. Use `import-pbix --desktop` with Power BI Desktop.
- If you connect the MCP server to a client that uses a cloud model, tool results leave your machine. Use simulated data only in that case.
