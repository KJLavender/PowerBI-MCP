# PRISM — Proven Report Insights & Semantic Mapping

**English** | [繁體中文](README.zh-TW.md)

**Upload a Power BI report (.pbix) and the Excel files that go with it, then ask about it** — *"why did sales
drop?"*, *"what does the red mean?"*, *"is finance's revenue the same as ours?"* PRISM reads the report file
itself (no Power BI needed), computes the numbers from the report's own measures and relationships, and checks
every answer against those results before showing it. Everything runs on your machine: the files, the analysis
and the language model never leave it.

- **Answers with evidence**: numbers come from the model's DAX measures translated to SQL, not from the language
  model; an answer that fails the 9 mechanical checks is never shown.
- **Understands what is on screen**: the author's colour rules, target lines and highlighted members are read
  from the report file, so "the red bars" can be explained without screenshots.
- **Maps reports and Excel together**: same-name measures that are computed differently, renamed measures that
  mean the same, Excel codes that do not match the report's — on one page.
- **Forward a conversation**: the next person opens the same report view and continues the same chat.

## Get started

You need [Ollama](https://ollama.com/download) for the chats (PRISM downloads its model, `qwen3.5:4b`, on first
start). Uploading and the report map work without it.

**Windows** — install [Python 3.11+](https://www.python.org/downloads/), download this repository and
double-click **`PRISM.cmd`**. The first run creates the Python environment (a few minutes), then the browser opens
<http://localhost:8080>. `PRISM.cmd -Share` lets colleagues on the network open it too.

**Docker** (Linux, or Windows with Docker) — PRISM and the model in one command:

```bash
docker compose up -d        # then open http://localhost:8080
```

Then drag a `.pbix` onto the page, add the related Excel files if any, and press **上傳並分析** (upload and
analyse): about a minute later the report can be asked about and appears on the report map. Details, the API and
what each step does: [docs/prism.md](docs/prism.md).

| Page | |
|---|---|
| My reports | upload, processing steps, open a report's chat, attach more Excel, delete |
| Report map | every report and workbook, the sources they share, synonyms, conflicts, code checks, the author's rules |
| How it works | why answers can be trusted, the verified numbers, the limits |

## How it was built

PRISM grew out of a fully local lab for an agent that **understands Power BI reports**: every morning it explains
which metrics changed and where the change came from, and it answers *"why did this KPI drop?"* using the
semantic model's real measures and relationships. The lab runs on a simulated retail report on Oracle (with a
Report Server and a daily refresh, the typical on-prem setup) and a report built on a public Excel workbook
(Global Superstore), with known business events injected as ground truth. The rest of this page describes it.

## What it does

| Phase | Result |
|---|---|
| 1. Simulated enterprise source | Oracle Database (Docker in WSL) refreshed daily by an ETL simulator; Power BI project generated as code (TMDL + PBIR) |
| 2. Daily change analysis | Reads measures/relationships from the Power BI project, snapshots to DuckDB, detects significant changes and picks the scope that explains them. **6/6 injected scenarios detected** (4 on day 0) |
| 3. "Why" Q&A | Chat panel next to the Report Server report, linked to it through a shared filter bar (filters drive both the report and the questions; answers can apply their scope to the report), with follow-up questions, live progress, an evidence view and locally stored chat history — on a read-only MCP server + local Ollama model. Every answer is checked against the tool results (see row 6); users got a correct answer on **10–11/11** retail and **14–15/15** Excel questions, including trap questions |
| 4. Report Server | Power BI Report Server with a daily 06:30 scheduled refresh from Oracle — the typical on-prem setup |
| 5. Excel report | The same engine and agent on a Power BI report built from the public Global Superstore Excel workbook (18 orders a day): data read through the model's Power Query, thresholds calibrated on the untouched workbook (0.2 background findings a day), **4 of 5 detectable injected events found** |
| 6. Verification | Every answer goes through 9 mechanical checks (date, scope, numbers, names, number-to-thing binding, direction, significance, unprovable causes, key finding). A failing answer is sent back; if it still fails, the analysis tool's own reading is shown instead. **0 wrong answers got past the checks**; a mutation test breaks correct answers on purpose and the checks catch **94–99%**; the engine's numbers match Power BI's own DAX results (**10,316 numbers, 0 differences**) |
| 7. Any .pbix | `python -m analysis import-pbix FILE.pbix --name X` **reads the file itself — no Power BI, no Windows needed**: pages and the author's formatting from the .pbix JSON, the data model (DAX, Power Query, relationships) and the imported data decoded with [PBIXRay](https://github.com/Hugoberry/pbixray), and a draft of the analysis settings inferred from the data; when the file cannot be read it falls back to Power BI Desktop's engine (`--desktop`). Both paths give the same result on the retail .pbix: model, draft and snapshots (**52,885,918 values, 0 differences**); the Desktop path's snapshots also match reading Oracle directly (**544,515 values, 0 differences**), all injected events found, **10/11** questions answered correctly, 0 wrong answers passed the checks |
| 8. Several reports, the author's intent | `python -m analysis catalog` puts every report in one graph: each table traced to the physical source it reads (whatever it is called in the report), each measure expanded into aggregates of source columns and compared as a formula (names are ignored), Excel codes checked against the database value by value. `python -m analysis intent` reads the thresholds, target lines and highlighted members the author set. On a second retail report (finance team, with an Excel target workbook) every planted synonym, same-name-different-meaning measure and code mismatch was **found**; the formatting parser was checked on Microsoft's 10 official sample .pbix files with matching counts; cross-report questions **4/4** |
| 9. Questions about what is on screen | The user describes what they see ("the red text", "the North bar", "the dashed line on margin"): the visual is identified from the report file (several matches are listed for the user to pick, never guessed), its measures and the author's rules are explained, and its numbers over the date range the report shows are computed from the snapshots, with each member's colour band (time comparisons such as DATEADD included). No screenshots. Checked against SQL on Oracle: **75 values, 0 differences**; the 5 new questions pass, retail set **18/20** correct, **0 wrong answers passed the checks** |
| 10. PRISM | The packaging: a web portal to upload a .pbix and its Excel files (each upload runs import → settings → snapshots → first report → map), one chat per report, the report map; Windows launcher and Docker. A clean download, run through `PRISM.cmd` and through Docker, imported the AdventureWorks sample with its workbook in **77 s**, matched all **46** workbook columns to the report and answered with grounded results |

Example (from `python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？"`):

> **Conclusion**: North region sales fell mainly because of 3C accessories (84% of the total change); "North × 3C accessories" is significantly abnormal.
> **Evidence**: North sales -19.1% (381,154 vs 471,022), below the significance threshold (z -3.0); one level deeper, North × 3C accessories -46.7%, z -4.7, significant …
> **Limits**: the data cannot tell the business reason (competitors, stock-outs…); needs business confirmation.

## Architecture

```
06:00  ETL simulator ──▶ Oracle (SALES_DW) ──▶ DuckDB daily snapshot ──▶ engine ──▶ reports/<date>.md
06:30                    Oracle ──▶ Power BI Report Server scheduled refresh ──▶ report portal

user question ──▶ agent (local) ──▶ Ollama model
                      │  ▲  tool results (pre-formatted numbers)
                      ▼  │
               MCP server (read-only, stdio) ──▶ Power BI model (TMDL/PBIR) + snapshots + engine

scenarios/scenarios.yaml — injected business events (ground truth); only the evaluators read it
```

## Requirements (the lab)

- Windows 11 with WSL2 (Ubuntu) and Docker Engine inside WSL
- Python 3.11+, Power BI Desktop
- [Ollama](https://ollama.com) with a tool-calling model (default `qwen3.5:4b`) for Phase 3
- Power BI Report Server (Developer edition) + Power BI Desktop for Report Server + Oracle ODAC for Phase 4

## Quick start (the lab)

```powershell
# Phase 1 — Oracle + simulated data + Power BI project
copy .env.example .env                                  # then set the passwords
.\scripts\start-oracle.ps1
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m simulator init
.\.venv\Scripts\python -m simulator backfill            # 2025-01-01 .. yesterday, ~2.7M rows
.\.venv\Scripts\python powerbi\build_pbip.py            # open powerbi\RetailSales.pbip, Refresh
.\scripts\register-daily-task.ps1                       # daily 06:00: ETL + analysis

# Phase 2 — daily change report
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis report               # reports/<date>.md
.\.venv\Scripts\python -m analysis evaluate             # score against the scenarios

# Phase 3 — ask why, with a local model
.\scripts\start-chat.ps1                                # report + chat panel on http://localhost:8090 (or double-click 報表問答助理.cmd)
.\.venv\Scripts\python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？" -v
.\.venv\Scripts\python -m agent chat
.\.venv\Scripts\python -m agent evaluate

# Phase 4 — Report Server (one-time admin setup, then publish)
.\scripts\phase4-setup-admin.ps1                        # as administrator
.\scripts\phase4-publish.ps1                            # upload, credentials, 06:30 schedule, refresh

# Verification
.\.venv\Scripts\python -m agent mutation-test         # break passing answers on purpose: are they caught?
.\.venv\Scripts\python -m analysis reconcile          # compare with Power BI Desktop's own DAX results (open the report first)

# Several reports and the author's intent
.\.venv\Scripts\python datasets\store_targets\prepare.py   # the finance report's Excel store targets (built from Oracle)
.\.venv\Scripts\python datasets\store_targetsuild_pbip.py
.\.venv\Scripts\python -m analysis catalog                  # → reports/catalog.md
.\.venv\Scripts\python -m analysis intent store_targets     # thresholds, target lines, highlights the author set

# Excel report (Global Superstore) — every command above works on it with PBI_DATASET
$env:PBI_DATASET = "superstore"
.\.venv\Scripts\python datasets\superstore\prepare.py   # download (SHA-256 checked) + write the copy with injected events
.\.venv\Scripts\python datasets\superstore\build_pbip.py
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis evaluate
.\.venv\Scripts\python -m agent evaluate
```

## Test data

- `powerbi/RetailSales.pbix` — the report with data imported (2.7M order lines), in the legacy report format so it
  uploads to Power BI Report Server. Data source credentials are not included.
- `powerbi/RetailSales.pbip` — the same report as a Power BI project (TMDL + PBIR), generated by `powerbi/build_pbip.py`.
- `reports/` — sample daily reports, the evaluation results of Phases 2 and 3, the mutation test and the reconciliation with Power BI.
- `datasets/superstore/` — the Excel-based report (Power BI project, analysis settings, injected events, questions) and its results in `reports/`. The workbook itself is downloaded by `prepare.py`.
- `datasets/store_targets/` — the finance team's store-target report: the same Oracle as the retail report plus an Excel target workbook (generated by `prepare.py`, with deliberate inconsistencies); `reports/catalog.md` is the catalog of all reports.

## Repository layout

| Path | Content |
|---|---|
| `simulator/` | Oracle schema, master data, daily data generator with scenario injection |
| `powerbi/` | Power BI project generator, legacy `.pbix` converter for Report Server, the report files |
| `analysis/` | Semantic layer (TMDL/PBIR/DAX parsing), snapshots, change engine, reports, MCP server |
| `prism/` | The PRISM portal: uploads, processing jobs, chat servers per report, the report map |
| `agent/` | Local Ollama agent, grounding checks, Q&A test set, the chat page |
| `scripts/` | Container start-up, scheduled tasks, Report Server setup and publishing |
| `docker/` | Oracle Database Free + SQL Server Developer (Report Server catalog) |
| `datasets/` | Other reports analysed by the same engine (Global Superstore on Excel), the finance report used for cross-report tests |
| `docs/` | Design notes per phase (Traditional Chinese) |

## Documentation

Detailed design notes are written in Traditional Chinese:

- [docs/prism.md](docs/prism.md) — PRISM: installing, uploading, the pages, the API, sharing on a network
- [docs/powerbi-setup.md](docs/powerbi-setup.md) — Power BI project, model and report pages
- [docs/analysis.md](docs/analysis.md) — how the engine reads the model, detects changes and picks the explaining scope
- [docs/agent.md](docs/agent.md) — MCP tools, grounding checks, Q&A evaluation
- [docs/report-server.md](docs/report-server.md) — Report Server setup and the PBIR-format pitfall
- [docs/pbix-import.md](docs/pbix-import.md) — importing a report straight from a .pbix
- [docs/excel-dataset.md](docs/excel-dataset.md) — the Excel-based report: calibration on small data, detection results, reconciliation
- [docs/visuals.md](docs/visuals.md) — questions about what is on screen: identifying the visual, explaining it, its numbers and colour bands over the report's date range
- [docs/catalog.md](docs/catalog.md) — how several reports relate (lineage, measures with the same or a clashing meaning, code checks) and what the report author emphasises

## Limitations

- The analysis tells **where** a change happened, not the business reason behind it, unless that reason is in the data.
- Thresholds must be calibrated per report: on the Excel data the original settings raised 7.3 false findings a day until they were recalibrated on the untouched workbook. Small data can only show large events.
- The checks prove an answer is consistent with the analysis results; they cannot prove the analysis picked the best explanation. The evidence is always shown with the answer.
- Measure equivalence covers the common DAX patterns; `IF`, `SWITCH` and the like are reported as not expandable and never treated as equal. Reports mixing Oracle and Excel are used for the catalog and Q&A only, without daily change analysis.
- If you connect the MCP server to an MCP client backed by a cloud model, tool results leave the machine — use simulated data only.
