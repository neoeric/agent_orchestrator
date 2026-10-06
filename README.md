# agent_orchestrator

讓 AI CLI 分工跑完一個子任務的編排器：**Codex 或 Claude Code 實作（task 的 `implementer`）→ 測試驗證 → Antigravity（agy）審查 → 判定器核對**，
不收斂就把審查意見餵回實作者再跑一輪。工作狀態全部外部化成檔案，不依賴任何一方的對話記憶。

編排器獨立安裝一次，用 `git worktree` 在隔離目錄裡改碼，**永遠不碰你的生產目錄**。

> **用 Claude Code 的人**：有一支 `orchestrate` skill 把「判任務→寫三檔→建 LF worktree→跑 relay→人閘門」整套流程與坑固化好了，打 `/orchestrate` 或說「跑一棒」即可。skill 內容見 `~/.claude/skills/orchestrate/SKILL.md`。

## 設計前提

1. **工人說「做好了」不算數。** 每一輪都由 relay 自己跑你指定的驗證指令，全部 exit 0 才算過。
   實作者宣稱測試通過但 relay 跑出 FAIL，就是 FAIL。
2. **合併、部署、重啟是人閘門。** relay 最多做到「在自己的 branch 上 commit」，不 merge、不 push、不重啟服務。
3. **每次 CLI 呼叫都要驗它真的正常結束。** 三支 CLI 的 headless 回傳格式互不相同、失敗形狀更是各走各的
   （見下方「已知行為」），所以有一個獨立的白名單判定器 `judge.py` 專門做這件事。

## 需要什麼

| 項目 | 用途 | 沒有會怎樣 |
|---|---|---|
| Python 3.11+ | 跑 relay 與驗證指令 | — |
| [Codex CLI](https://github.com/openai/codex) | 實作者（預設，`implementer=codex`） | 啟動時報錯停下（只有用到 codex 實作者時才檢查） |
| Claude Code CLI | 實作者（選用，`implementer=claude`） | 只有 `implementer=claude` 時才檢查 |
| Antigravity CLI（`agy`） | 審查者角色 | 啟動時報錯停下（`review.policy` 全設 `never` 則不需要） |
| git 2.16+ | `git worktree` 隔離 | — |

在 Windows 11 上開發與實測，其他平台沒測過。

**執行檔怎麼找**：`tools/paths.py` 的順序一律是 **環境變數 → PATH → 已知安裝位置 → 報錯**。
換機器只要裝好並登入；路徑特殊就設 `RELAY_CODEX`、`RELAY_AGY`（或 `AGY_EXE`）、`RELAY_CLAUDE`、`RELAY_PYTHON`。
Claude 在 Windows 上會**略過 npm 的 `.cmd` 薄殼改用原生 exe**（多行 prompt 經 cmd.exe 會被截斷／轉義，prompt 一律走 stdin）。
relay 啟動時自檢，缺哪支當場大聲說，不會跑到一半才炸。

## 快速開始

寫一個任務定義 `tasks/my-task.json`：

```json
{
  "id": "my-task",
  "title": "一句話說明要做什麼",
  "repo": "C:/code/myproject",
  "base_branch": "master",
  "branch": "feat/my-task",
  "worktree": "C:/code/_worktrees/my-task",
  "production_dir": "C:/code/myproject",
  "spec_file": "tasks/my-task.spec.md",
  "implementer": "codex",
  "prebuild": [],
  "verify": [
    {"name": "unit", "cmd": "python -m pytest tests/", "timeout": 600}
  ],
  "review": {
    "policy": "auto",
    "instructions_file": "tasks/my-task.review.md"
  },
  "core_paths": ["server.py", "applib/", "models/"],
  "policy": {"max_files": 2, "max_lines": 60},
  "allowed_paths": ["applib/render.py"],
  "max_rounds": 2
}
```

| 欄位 | 說明 |
|---|---|
| `repo` / `base_branch` / `branch` / `worktree` | 從 `base_branch` 開 `branch` 到 `worktree`；branch 已存在就沿用 |
| `production_dir` | 選填但**強烈建議填**：`worktree` 等於它就直接拒跑，防手滑改到生產目錄。它是 git repo 時另有**生產目錄守門**：每次實作者呼叫前後各取一次快照（`git --no-optional-locks status --porcelain -z -uall`＋列出檔的大小／mtime＋HEAD），不同就整棒中止（exit 3、`aborted`）。被 `.gitignore` 的檔看不到；別的 session／服務在那段時間寫了生產目錄也會中止（誤報＝重跑一棒） |
| `spec_file` | 給實作者的完全指定規格（相對路徑以編排器目錄為基準） |
| `implementer` | `codex`（預設）或 `claude`；**可寫清單**如 `["codex", "claude"]`＝撞牆（judge 判 `rate_limit`）時依序換手（見「換手」）。其他值（含寫錯、空清單、重複）啟動時就 exit 3，不再靜默用 Codex；清單裡每一支都會在開跑前自檢。prompt 一律經 stdin 送，沒有命令列長度上限 |
| `implementer_models` | `{"claude": "sonnet", "codex": "<model>"}`：各實作者用的模型。**implementer 含 claude（單一、清單或候選）時 claude 必填**，沒給 exit 3（2026-10-06：不然會跟著你的 Claude Code 設定走、可能是最貴的那個，成本不可見）；codex 選填，沒給＝CLI 自己的預設 |
| `implementer_effort` | 選填 `{"claude": "high"}`（`low`／`medium`／`high`／`xhigh`／`max`）；只接受 claude，寫錯 exit 3 |
| `candidates` | 選填，best-of-N：`[{"implementer":"codex"},{"implementer":"claude","model":"sonnet"}]`，2～3 項（寫死上限），每項 `implementer` 必填且是單一字串、`model`／`effort`（effort 只給 claude）選填；有此欄時 task 的 `implementer` 被忽略；見「同任務多候選」 |
| `handoff_cooldown_minutes` | 選填，整數分鐘，預設 `0`（關）。>0 時，帳本裡這段時間內回過 `rate_limit` 的實作者，本棒一開始就先略過（只是建議：清單裡全都在窗內就照用第一個）；見「換手」 |
| `prebuild` | 開好 worktree 後、改碼前要跑的指令（裝依賴、建虛擬環境…）。⚠️ **新 worktree ≠ 你的工作目錄**：被 gitignore 的目錄、建置產物在新 worktree 都不存在，`verify[]` 依賴的產生物要在這裡補，否則第一輪會拿到假紅燈 |
| `verify[]` | 每輪都要跑的驗證，`{name, cmd, timeout?, env?}`；全部 exit 0 才算過 |
| `python` | 選填：verify／prebuild／陰性對照要用的 Python（例如專案 venv 的 `python.exe`）。沒設就用跑 relay 的那支直譯器；當 verify 需要的依賴只在專案 venv、而 relay 跑在別的 python 時設它（否則會拿到「缺依賴」的假紅燈）|
| `negative_controls` | 選填：`[{target, old, new, verify_name?, expected_failure}]`。verify 綠**之後**，對每條把 `target`（worktree 內相對路徑）裡的 `old` 換成 `new`（一個真違規）、重跑 `verify_name`（省略=第一條 verify）、確認它**紅在 `expected_failure` 這個標記**、再逐位元組還原。任何一條「注入後沒紅在指定處」= 那條斷言是空的 ⇒ 整棒判 fail。把機器審從「斷言**在不在**」升級到「斷言**真的抓得到**」——就是人工重審在做的那件事。⚠️ **反向注入一律寫在這裡，不要寫成 `verify[]` 裡的自製腳本**：`verify[]` 各條互不知道結果，新測試本身就紅時自製注入會報「全抓到」（假陽性）；這裡保證注入前基線必綠、紅在指定 marker、逐位元組還原。每條對照會重跑基線（N 條＝2N 次 verify）。逾時沿用該條 verify 的 `timeout`（預設 1800 秒） |
| `review.policy` | `always`（預設）／`never`／`auto`，判準見下節 |
| `review.instructions_file` | 給審查者的逐條核對條件。⚠️ **每一條都必須「只看 diff 就能回答」**——審查者是唯讀、只拿到 diff，不給工具，要求它讀原始檔或附上測試實跑輸出，它結構上做不到，只會回「無法判定」。實跑證據由 `verify[]` 供給 |
| `core_paths` | `auto` 判準用：碰到就一定送審 |
| `policy.max_files` / `max_lines` | `auto` 的小改動門檻，預設 2 檔 / 60 行 |
| `allowed_paths` | commit 時只收這些路徑；沒設就收全部改動 |
| `max_rounds` | 幾輪不收斂就停下來給人，預設 2 |

**`implementer=claude` 的隔離**（Claude 沒有 Codex 的沙箱，紅線改成「結構上做不到」）：
`claude -p --output-format stream-json --verbose --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence --safe-mode`，cwd＝worktree。
- **只會讀寫檔**：沒有 Bash ⇒ 不能 git commit／push、不能啟動服務、也不能跑指令。規格若要求「跑某指令產生檔案」，Claude 做不到——交給 `prebuild` 或 `verify[]`。
- **不載入任何 MCP**（使用者環境可能有可寫 GitHub 之類的 MCP）；`--safe-mode` 不跑使用者 hooks／CLAUDE.md／使用者 skills（2026-10-05 fixture 證實 stream-json 照常）。
- 巢狀呼叫要清的環境變數自動清掉（`tools/paths.py: CLAUDE_NESTED_ENV`）；兩種實作者都套用上面的生產目錄守門。

跑：

```text
PYTHONUTF8=1 python relay.py tasks/my-task.json
PYTHONUTF8=1 python relay.py tasks/my-task.json --dry-run   # 只印計畫，不呼叫任何 CLI（寫到 runs/<id>.dry/）
PYTHONUTF8=1 python relay.py tasks/my-task.json --queue     # 並行名額滿了就排隊（見「並行」）
PYTHONUTF8=1 python relay.py --status   # 所有棒的階段、輪次、耗時、是否等人（--all 看全部）
PYTHONUTF8=1 python relay.py --resume my-task [--rounds N]   # 寫好 runs/my-task/human_notes.md 後接續下一輪（見「人工意見回灌」）
PYTHONUTF8=1 python relay.py --ledger                        # 看累計用量（含每支 CLI 最後一次 rate_limit 的時間）
```

`PYTHONUTF8=1` 在 Windows 是必要的，否則輸出非 ASCII 會直接 cp950 crash。

離開碼：`0`＝收斂並已 commit；`2`＝不收斂或被擋（含**撞牆**：可用的實作者都回 `rate_limit`、或審查者回 `rate_limit`），已寫 HANDOFF 給人；
命令列參數錯（argparse）也回 `2`；`3`＝任務檔內容錯、環境自檢失敗、被鎖擋下拒跑、或例外中止（STATE 記 `aborted`）；`130`＝Ctrl-C。

`--status` 的「等人？」欄：`跑中`／`排隊中`（任務鎖有人持有）、`待合併 <commit>`、
`要人看（未收斂／審查工具故障／撞牆／中止：原因）`、`中斷？（行程已不在）`（STATE 停在中途但任務鎖沒人持有＝
行程被硬殺或關機，重跑即可）。判活只看任務鎖，不看 STATE 寫什麼；預設列最近 15 筆。

## 一棒的流程

```text
1. 開隔離 worktree（拒絕等於 production_dir）
2. prebuild
3. 實作者（Codex／Claude Code）依 spec_file 改碼（不 commit）；前後比對生產目錄快照，有變動就中止
   （implementer 是清單時：judge 判 rate_limit → 同一輪換下一位接手，不消耗輪數；見「換手」）
4. verify[] 逐條跑，全部 exit 0 才算過
4b. verify 全綠後跑 negative_controls：注入 → 必須紅在 marker → 還原
5. 判準決定要不要送審 → agy 看 diff 唯讀審查，輸出結構化 verdict
6. judge.py 核對每次 CLI 呼叫真的正常結束（不是「有輸出就當成功」）
7. 過 → 以 allowed_paths 明列路徑 commit 到 branch；不過 → 把發現餵回步驟 3，最多 max_rounds 輪
```

跑的時候四個角色的進度帶色前綴即時交錯印出，直接在終端機看得到協力過程：

```text
[CODEX]  青色 · 實作者：讀哪個檔、跑什麼指令、改了什麼
[CLAUDE] 藍色 · 實作者（implementer=claude）：讀／改了哪個檔、完成時的用量
[AGY]    紫色 · 審查者：逐條核對與總判定
[VERIFY] 綠色 · 驗證指令的 PASS／FAIL
[JUDGE]  黃色 · 送審或跳過的判準
[RELAY]  灰色 · 編排器自己的階段
```

設 `NO_COLOR=1` 關顏色。兩個 Agent 都是背景子行程（走 CLI，不走它們的編輯器擴充），
所以它們的擴充面板不會有反應——看得到的地方就是這個終端機、`runs/` 底下的紀錄、以及 Source Control 的分支。

## 要不要送審：`auto` 判準

用**客觀訊號**決定，不採信 Agent 自填的風險等級。依序：

1. 簽章閘門測到 **breaking** → 送審
2. 改動碰到 `core_paths` → 送審
3. 本任務曾有驗證失敗 → 送審
4. 小改動（≤ `max_files` 檔且 ≤ `max_lines` 行）→ **跳過審查，只靠測試**
5. 其餘 → 送審

**介面變更永遠不套用跳過規則。** 簽章閘門（`tools/iface_gate.py`）純 AST 比對改動的 .py 公開介面：
必填參數增加、參數移除、回傳型別改變、公開名稱移除＝breaking；新增公開名稱或選填參數＝additive。

審查指令會要求審查者最後一行輸出
`{"verdict":"approve|changes_requested","checks":[...],"unreported":[...]}`；
`parse_review` 以 JSON 為準，沒有才退回讀第一行的「可合併／需修改」。

## 產出

執行紀錄寫在編排器自己的 `runs/<task_id>/`，**不寫進受測 repo**：

| 檔案 | 內容 |
|---|---|
| `CURRENT.md` | 接手的人第一眼看這份 |
| `STATE.json` | 階段、輪次、每次 CLI 呼叫的 usage；含中止原因，relay 被例外中止時階段記為 `aborted`；最後一輪未解決的發現（`last_feedback`）、每次 resume（`resumed`）、relay 疊過的每顆 commit（`commits`）、最後實際用的實作者（`implementer`）、每次換手／略過（`handoffs`） |
| `HANDOFF.md` | 六欄交接簿，含實作者與審查者原文 |
| `human_notes.md` | 你寫的人工意見，`--resume` 用（見「人工意見回灌」）；用過改名成 `human_notes.r{N}.md` |
| `HANDOFF.r{N}.md` | resume 前那一版 HANDOFF 的備份（N＝當時停在第幾輪） |
| `relay.log` | 完整時序 |
| `impl_r*` / `review_r*` / `verify_r*` | 每輪的 prompt、diff、審查指令、驗證輸出；同一輪換手後那次實作是 `impl_r{N}_h{k}_{cli}*`（不覆蓋第一次的紀錄） |
| `runs/usage_ledger.jsonl` | 跨任務帳本，每次呼叫一行（ts／task／role／cli／usage／秒數／failure_class）；換手冷卻只**唯讀**查它 |
| `runs/.locks/` | 並行鎖檔（空檔，不必手動刪；見「並行」） |
| `runs/notify_ledger.jsonl` | 推播帳本，每次嘗試一行（見「需要人時才推播」） |

`--dry-run` 寫到 `runs/<id>.dry/`，不覆蓋真實紀錄。

> `tasks/`、`runs/`、`plans/` 預設不進版控（見 `.gitignore`）——它們會包含**你自己專案的程式碼片段與審查原文**。
> 要分享任務範本請自行挑選、確認內容後再加。

## 並行

預設一次一棒。要同時跑不同任務，設環境變數 `RELAY_MAX_PARALLEL`（預設 1、上限 3；非數字當 1、超過夾到 3，都會印警告）。

- 名額由**每個 relay 行程自己守**（`runs/.locks/slot-*.lock`）：滿了直接拒跑（exit 3）；加 `--queue` 則排隊、每 30 秒重試
  （不保證先來先跑，Ctrl-C 放棄），`--status` 顯示「排隊中」。
- 一定會擋：同一個 task id 同時跑兩次、兩個任務用同一個 `worktree` 路徑。同一 repo 的 `git worktree add` 與 commit 互斥
  （各只佔幾秒），不同分支可以並行。帳本 append 也有鎖。
- 行程被硬殺時 OS 會自動釋放它的鎖，下次直接拿得到；`--status` 把那棒顯示成「中斷？」，重跑時 relay.log 記一行
  「前一次行程…沒有收尾」。鎖只在同一份 relay 安裝目錄內有效；`--dry-run` 不取任何鎖。
- agy 審查一律以第 1 塊回傳的 conversation id 釘住對話；拿不到 id 就當審查工具故障停下（不退回 `--continue`：它接
  「最近一個對話」，並行時會接到別棒的審查）。

relay 管不到、並行前要自己確認的：

| 共用物 | 風險 |
|---|---|
| verify 用到的固定 port、專案外固定路徑、載模型 | 兩棒同時跑會互撞或吃光記憶體——這種任務不要並行 |
| agy／Codex 的本機狀態目錄（`~/.gemini/antigravity-cli/`、`~/.codex/`） | 並行未實測 |
| 訂閱額度 | 並行＝同時燒兩份 |
| 終端機輸出 | 會交錯；各自導檔（下例） |

兩棒背景啟動（PowerShell，在編排器目錄、`runs/` 已存在）：

```powershell
$env:PYTHONUTF8 = "1"; $env:RELAY_MAX_PARALLEL = "2"
foreach ($id in "task-a", "task-b") {
  Start-Process python -ArgumentList "relay.py", "tasks/$id.json" -WindowStyle Hidden `
    -RedirectStandardOutput "runs/$id.console.txt" -RedirectStandardError "runs/$id.console.err.txt"
}
python relay.py --status
```

## 人工意見回灌（--resume）

看完一棒的 HANDOFF 想補一句意見再跑，不必整棒重開（重開＝從第 1 輪重來、多燒一輪實作）：

```text
# 1. 把意見寫進 runs/<id>/human_notes.md（UTF-8；記事本存檔帶的 BOM 會自動去掉）
# 2. 接續
PYTHONUTF8=1 python relay.py --resume my-task                     # 最多再跑 max_rounds 輪
PYTHONUTF8=1 python relay.py --resume my-task --rounds 1          # 只再跑 1 輪
PYTHONUTF8=1 python relay.py tasks/my-task.json --resume my-task  # STATE 記的任務檔不在了時，明給任務檔
```

`human_notes.md` 直接寫你要的改動，一條一行即可，例如：

```text
1. 函式名改成 parse_rows，呼叫端一起改。
2. 錯誤訊息改成中文。
3. 不要動 tests/ 底下的測試資料。
```

- **意見同時給實作者與審查者，而且優先**：實作者第一輪拿到「【人工審查意見（最高優先…）】」＋上一輪未解決的發現；
  審查者在這次 resume 的每一輪，核對條件後面都多一段「【人工追加要求（優先於上列條件…）】」——否則你要的改動會被依舊條件判「不通過」而一直退回。
- **輪次接續編號**：上次停在第 2 輪就從第 3 輪開始（`impl_r3_*`、`review_r3_*`…），舊紀錄不覆蓋；`--status` 的輪次顯示成 `3/4`（4＝這次最多跑到第幾輪），耗時從這次 resume 起算。
- **已收斂（已 commit）的棒也能接續**：在同一 branch 上**疊一顆新 commit**，不改寫、不 reset 既有 commit，也不 merge；
  審查者看的是**整個任務的累積 diff**（對任務起點比），commit 只收這次的增量。`STATE.json` 的 `commits` 記每一顆，`commit` 是最新一顆。
- **要改範圍先改任務檔**：resume 會重讀任務檔；要動 `allowed_paths` 以外的檔，先改 JSON 再 resume（沒有另外的旗標）。
- **不重建 worktree、不跑 `prebuild`**：沿用上次的 worktree 與裡面未 commit 的改動；需要重建產物時自己跑，或開新棒。
- **用過的意見會改名**：開跑前 `human_notes.md` 改名成 `human_notes.r{N}.md`（N＝這次的第一輪），`HANDOFF.md` 備份成 `HANDOFF.r{上次輪次}.md`。
  之後的編輯不會被這次誤用，下次 resume 也不會重用舊意見。**resume 中途又中止時**，要重試請把 `human_notes.r{N}.md` 改回 `human_notes.md`。
- 鎖與名額同一般棒：同一任務正在跑就拒絕，受 `RELAY_MAX_PARALLEL` 管，可加 `--queue`（resume 排隊中 `--status` 顯示「跑中」）。
  推播沿用一般出口；`CURRENT.md` 標題會標「人工意見回灌，第 N 次 resume」。
- `--dry-run --resume <id>`：會讀 notes、計畫寫到 `runs/<id>.dry/`，不改名、不動真實紀錄。

**會拒跑（exit 3，不改任何檔）**：沒有 `STATE.json` 或它損壞、找不到任務檔、`human_notes.md` 不存在或只有空白、
worktree 不存在、worktree 不在任務分支上、分支上找不到上次的 commit（被 reset／rebase 過）、STATE 記的 worktree／分支與任務檔不同、
`runs/<id>/` 裡有比 STATE 更新的輪次紀錄、`human_notes.r{N}.md` 已存在、best-of-N 群組 id。
relay 不代為 checkout／reset，也**絕不從頭重跑**——狀態對不上就停下讓人看。

限制：

- `review.policy=auto` 時，任務曾有驗證失敗（含 resume 之前的輪次）就一定送審。保守、可接受。
- 從 `aborted` 接續（例如實作者動了規格外的檔）：worktree 裡可能還留著那些改動，請先自己處理，或在 notes 裡要求還原；relay 不代為 `git checkout`。
- 實作者 prompt 經 stdin 送（2026-10-05 起），notes 再長也不受命令列 32K 上限影響。

## 換手（選用）

把 `implementer` 寫成清單（例如 `["codex", "claude"]`），某一支額度用完時由下一支接手，不必停下等人：

- **只限實作者、只在 judge 判 `rate_limit` 時**。其他失敗（`auth`／`no_json`／`server`…）照舊「下一輪重試」，不換手。
- **不消耗輪數**：同一輪改由下一順位重做，每輪最多換 `len(清單)-1` 次；換手那次的紀錄是 `impl_r{N}_h{k}_{cli}*`。
- **半成品保留**：worktree **不重置**（不做破壞性操作；verify＋審查才是裁判）。接手者的 prompt 最前面多一段
  「【換手說明】前一位實作者（…）在本輪中途因額度限制停止，worktree 可能已有部分改動…」。
- **冷卻只是時間窗，不下架任何 Agent**：撞過牆的 CLI 只在**這一棒**的記憶體裡被略過；下一棒一定從清單第一個開始。
  `handoff_cooldown_minutes`（預設 0＝關）>0 時，才會在每棒開始時由帳本**即時計算**「這段時間內撞過牆」的 CLI 先略過——
  清單裡全都在窗內就照用第一個，永不讓任務無人可用。relay **永不寫** task 檔、設定檔或任何「停用清單」。
- **每次換手／略過都看得到**：終端機與 `relay.log` 印一行 `[JUDGE]`，記進 `STATE.json` 的 `handoffs`，HANDOFF.md「用量」節前有
  「換手紀錄：…」；收斂時 commit 訊息寫實際的最後實作者（例如「Claude Code（途中 codex→claude 換手） 實作」），推播第 3 行附「途中 codex→claude 換手」。
- **撞牆停下**（exit 2、推播 kind `rate_limit`、`--status` 顯示「要人看（撞牆）」）：清單裡每一支都撞牆（verdict `rate_limit`），
  或**審查者撞牆**（verdict `reviewer_rate_limit`；審查者不換別家審，換家會改變審查標準）。worktree 保留未 commit；
  等額度恢復後重跑，或寫 `human_notes.md` 後 `--resume`。實作者只寫一個字串時，撞牆也是這樣停下（不換手）。
- 🔴 **真的 429 至今還沒觀察到**：`judge.py` 各家的 `rate_limit` 判定都還沒有真樣本驗過。判定器若把別的錯誤誤分成
  `rate_limit`，最壞是多燒一次另一支 CLI（且只在你寫了清單時）。**第一次遇到時**，`[JUDGE]` 那行會印原始輸出的路徑：
  請人工確認它真的是額度限制，再照 `fixtures/README.md` 收成 `fixtures/<cli>_rate_limit.*` 並補判定器契約測試（收了之後提示就不再出現）。

## 同任務多候選（best-of-N，選用）

難題、而且規格已完全指定時，同一個任務交給不同實作者／模型各做一份，**機器先把每份的 verify＋閘門＋審查都跑完再排名，人只看第一名**
（對照「多 agent 並排、人逐一比較」的做法，這裡把比較的工作先交給機器）。

```json
"candidates": [{"implementer": "codex"}, {"implementer": "claude", "model": "sonnet", "effort": "high"}]
```

- **只認明確宣告**：不會因為第一輪 verify 失敗就自動分叉（本機資料顯示那樣約 24% 的棒會多燒一倍額度，其中一半本來就會自己收斂）。
- **成本寫死、事前可見**：最多 3 份（`MAX_CANDIDATES`）。**N 份＝N 倍實作＋N 倍審查額度**，沒有任何折扣。
- **依序跑、不平行**：整個群組只佔 1 個並行名額，額度消耗平緩；牆鐘時間是 N 倍。每份候選是獨立的一棒：
  id `<id>.c1`／`.c2`…、分支 `<branch>-c1`…、worktree `<worktree>-c1`…（各自的 `runs/<id>.cK/`，`--status` 看得到）。
  候選模式**不換手**（每份的 `implementer` 只能是單一字串）；沒給 `model`／`effort` 就沿用 task 本身的 `implementer_models`／`implementer_effort`。
- **一份中止不影響其他份**：規格外改動等中止只標那一份 `aborted`，其餘照跑；候選自己都不推播，整個群組結束只推一則。
- **排名**只讀各候選的 `STATE.json`，不動任何 worktree／branch。依序比較（小者優先）：收斂與否 → 是否中止 → 最後一輪 verify 過的條數（多者優先）→
  陰性對照 → 審查（approve＜skipped＜changes＜tool_failure＜none）→ 介面破壞數 → 審查者點出的未申報數 → diff 行數 → 輪數 → 秒數 → 候選序。
- **產出**：`runs/<id>/RANKING.md`（排名表＋第一名的分支／commit）與 `runs/<id>/GROUP.json`。exit 0＝第一名收斂；2＝沒有候選收斂（仍排名）。
  推播（kind `ready_to_merge` 或 `escalate`）第 1 行寫第一名是誰，第 2 行叫你讀 `RANKING.md`，只看第一名的 `HANDOFF.md`。
- **清理是人做**：`RANKING.md` 會列出其餘候選的 `git worktree remove`／`git branch -D` 指令，**relay 只印、不執行**；確認不要了再自己跑。
- `--resume <id>` 對群組 id 會拒絕（exit 3）；要接續請指定候選 id（`--resume <id>.c2`，任務檔是 `runs/<id>/cand_2.task.json`），那是一般單棒。
- `--dry-run`：每份候選各印自己的計畫，不寫 `RANKING.md`。

## 需要人時才推播（選用）

一棒常跑十幾分鐘到一小時，人不會一直盯著終端機。relay 可以在「需要你動手」時推播一則；**沒有設定檔就整個關閉**（預設零行為改變）。relay 不內建任何通道，只呼叫你指定的外部指令、經 **stdin（UTF-8）** 交訊息，日後換通道只改設定檔。

**只在五種出口各發一則，穩態不發**（一棒最多一則；`--dry-run`、`--no-notify`、Ctrl-C 都不發）：

| kind | 時機 | 第 1 行（單獨成立）|
|---|---|---|
| `ready_to_merge` | 收斂、已 commit，等你審後合併 | `【relay】<任務> 待合併：第 N 輪收斂，commit <hash>` |
| `escalate` | 輪數用完仍未收斂（第 2 行提示可寫 `human_notes.md` 後 `--resume`） | `【relay】<任務> 未收斂：N/M 輪用完` |
| `review_tool_failure` | 審查工具故障，實作與驗證已完成、未 commit | `【relay】<任務> 審查工具故障（<類別>）：…` |
| `rate_limit` | 撞牆停下：清單裡每一支實作者都回 rate_limit，或審查者回 rate_limit（見「換手」） | `【relay】<任務> 撞牆停下：<cli>（或「審查者 agy」）回 rate_limit` |
| `aborted` | 其他例外中止 | `【relay】<任務> 中止：<原因前 60 字>` |

訊息最多三行：第 1 行講哪個任務、發生什麼，第 2 行以「下一步：」講要你做什麼（手機通知常只看得到前兩行），第 3 行是耗時（途中換過手時附「途中 codex→claude 換手」）。只陳述事實，不評價結果。

### 設定

設定檔是 `notify.json`（放在 `relay.py` 旁，已列入 `.gitignore`），或用環境變數 `RELAY_NOTIFY_CONFIG` 指到別的路徑；**檔案不存在＝關閉**。格式錯誤時視同關閉，並在 `relay.log` 記一行「推播設定錯誤」。

```json
{
  "cmd": ["python", "tools/notify_telegram.py", "--env-file", "/path/to/alert.env"],
  "timeout": 30,
  "max_per_hour": 4,
  "max_per_month": 40,
  "dedupe_minutes": 60,
  "kinds": ["ready_to_merge", "escalate", "review_tool_failure", "rate_limit", "aborted"]
}
```

只有 `cmd` 必填（非空字串陣列，不經 shell；`python` 請寫成你機器上的完整路徑）；其餘都是上面列的預設值。`cmd` 的 cwd 是你執行 `relay.py` 的目錄，相對路徑自己留意。

🔴 **token 不可寫進 `cmd`**：命令列參數會被程序列表、shell 歷史、權限設定檔記錄。轉發腳本自己從環境變數或它自己的 env 檔讀。

### Telegram 轉發腳本 `tools/notify_telegram.py`

通用版，只用標準函式庫。沿用既有 bot 即可，憑證用這兩個變數名：

```text
TELEGRAM_ALERT_TOKEN=<bot token>
TELEGRAM_ALERT_TO=<聊天室 id>
```

可以放在環境變數，或寫進 `--env-file` 指的檔案（`KEY=VALUE` 逐行，`#` 開頭為註解；環境變數優先）。缺變數時 exit 非 0 並只印變數名；訊息超過 Telegram 的 4096 字元會截斷並註明；成功印 `NOTIFY telegram http=200` 且 exit 0，網路錯誤或非 200 都 exit 非 0。任何輸出都不含 token。env 檔請放在版控目錄之外。

### 節流與最壞情況

帳本是 `runs/notify_ledger.jsonl`（一行一次嘗試：`sent` 表示「有呼叫外部指令」，不論成功與否，因為失敗的呼叫也可能吃額度）。判斷順序：kind 沒開 → 同任務同類型 `dedupe_minutes` 內發過 → 全體近 1 小時達 `max_per_hour` → 本月（UTC+8 曆月）達 `max_per_month`。被節流時帳本記 `sent=false` 與原因，`CURRENT.md` 末尾附一行「推播未發（原因）」。

- 一棒最多 1 則。最壞情況是代理人在迴圈裡反覆重啟同一個壞任務：沒有節流時每小時可達上百則，有節流後同任務同類型每小時最多 1 則、**全體每小時最多 4 則、每月最多 40 則**。
- 正常用量：一個月約 30 棒左右，五類全開約 30 則／月。
- 推播指令失敗、逾時、帳本鎖逾時，都只記 `relay.log`、`STATE.json` 的 `notified` 欄位與帳本；**不會改變那一棒的結果與 exit code**。
- 人坐在終端機前跑時加 `--no-notify`。

## judge.py 可以單獨用

四支 CLI（claude／codex／gemini／agy）headless 回傳的白名單判定器，不依賴 relay：

```text
python judge.py claude out.txt --stderr err.txt --exit-file exit.txt   # 人看的摘要
python judge.py gemini out.txt --exit 144 --json                        # 機器讀的完整判定
```

白名單的意思是：**只有明確符合成功形狀才算成功**，其餘一律當失敗並分類（`no_json`、`rate_limit`、
`auth`、`bad_model`…）。設計原則見 `judge.py` 檔頭 docstring。

## tools/

| 工具 | 用途 |
|---|---|
| `paths.py` | 四支執行檔（python／codex／agy／claude）的解析（env → PATH → 已知位置）；`claude_env()` 清掉巢狀呼叫要移除的環境變數 |
| `closure_map.py` | 純 AST 依賴閉包圖：每個頂層 def 的閉包碰不碰 db／net／model／route |
| `deps_of.py` | 指定函式用到的同模組常數與 from-import（搬函式前查「還要帶什麼」） |
| `extract_defs.py` | 把頂層定義抽成新模組並在原檔原名 re-export；連緊貼的註解一起搬，保 CRLF |
| `iface_gate.py` | 簽章閘門：公開介面的 breaking／additive 判定 |
| `check_ps1_encoding.py` | .ps1 改動後 BOM 數不變、換行不混用、無控制字元、PSParser 0 錯 |
| `../council.py` | 三方討論執行器（見下方「三方討論」） |
| `agy_review.py` | agy 唯讀審查：diff 分塊餵進同一對話，用第 1 塊回傳的 conversation id 以 `--conversation <id>` 釘住（不用 `--continue`：它接「最近一個對話」，你同時在終端用 agy 或並行跑別棒會接錯；第 1 塊沒回 id 就 exit 1 停下）；命令列有 32K 上限、agy 不讀 stdin、給路徑會被軟拒 |
| `notify.py` | 推播判斷與送出（五種出口、dedupe／上限、外部指令經 stdin；見「需要人時才推播」） |
| `notify_telegram.py` | 推播通道轉發腳本（Telegram，通用版；token 只讀環境變數或 `--env-file`） |
| `runlock.py` | 跨行程檔案鎖（任務／worktree／並行名額／repo／帳本）；Windows `msvcrt`、POSIX `flock`，行程死掉 OS 自動釋放 |

## 測試

```text
PYTHONUTF8=1 python _test_judge.py    # 判定器契約測試，68 項
PYTHONUTF8=1 python _test_relay.py    # 判準／解析／閘門／帳本／狀態總表／鎖與並行／推播與 Telegram 轉發腳本／人工意見回灌／實作者可插拔與生產目錄守門／撞牆換手／同任務多候選／紅線補強（整組停止、沿用 worktree 驗分支）／dry-run 不碰 worktree 與 codex 舊報告檔／claude 必指定模型，308 項
PYTHONUTF8=1 python _test_council.py  # council 純邏輯＋Claude CLI 解析（假 CLI，不燒額度），53 項
```

`fixtures/` 是三支 CLI 的**真實回傳樣本**（2026-09-07 抓；實作者用的 claude stream-json 與 codex stdin 兩種形狀 2026-10-05 補抓），判定器契約測試靠它。
**任一 CLI 升版後全部重抓一輪再跑一次測試**——欄位語意變了不會有人通知你，這組測試是唯一的紅燈。
抓法與每份樣本的重點見 `fixtures/README.md`。

## 三方討論（council.py）

用途：**同一份簡報（brief）給 Codex／Claude／agy 各自獨立回答**，主持人整理後，第二輪再把各方意見交叉給所有人評審、收斂。
跟 relay 的派工不同：這裡沒有實作者與審查者，三方都只回答、不改任何檔案。

```text
PYTHONUTF8=1 python council.py --dir C:/work/council-topic --round 1 --repo C:/code/myproject
PYTHONUTF8=1 python council.py --dir C:/work/council-topic --round 2 --repo C:/code/myproject
PYTHONUTF8=1 python council.py --dir C:/work/council-topic --round 2 --who agy   # 只補跑一方
```

**目錄慣例**（`--dir`）：`00_briefing.md`（簡報）、`instructions_r{N}.md`（第 N 輪指令，**必須存在**，不會默默用別輪的）；
產出 `r{N}_{codex,claude,agy}.md`（統一 LF；失敗的一方不會動它舊的檔）；原始 stdout／stderr 與各方 prompt 落 `_raw/`。
第 N>1 輪預設自動附上 `r{N-1}_*.md`（缺的那方明寫「該方本輪無回覆」）。

| 參數 | 說明 |
|---|---|
| `--who codex,claude,agy` | 要跑哪幾方，預設三方平行；只給一方＝補跑，只覆寫該方的檔 |
| `--repo PATH` | 受測 repo，**唯讀**參考；給了之後預設 `codex,claude` 有權讀 |
| `--repo-access LIST` | 覆寫誰有 repo 權；**不可含 agy**（無頭模式讀不到檔）→ exit 2 |
| `--brief` / `--extra FILE...` / `--no-prev` | 簡報檔名（相對 `--dir`）／依序附在最後的附加材料／不附上一輪 |
| `--claude-model` / `--codex-model` / `--timeout` | 模型與逾時（預設 codex 1800／claude 1200／agy 900 秒） |
| `--raw-dir` / `--dump` | 原始輸出目錄（預設 `<dir>/_raw`）／只寫出各方 prompt、不呼叫任何 CLI |
| `--no-safe-mode` | claude 預設帶 `--safe-mode`（依 `claude --help`：停用 CLAUDE.md／skills／plugins／hooks／MCP 等自訂；⚠️ 實抓樣本的 init 事件仍列出已安裝 plugins 清單，是否真的未載入沒有驗證）；它若影響輸出格式時的退路 |

**怎麼保證唯讀**：codex 用 `-s read-only`；claude 只開 `Read,Glob,Grep` 三個工具（無 repo 權時 `--tools ""` 完全無工具），
並用空的 MCP 設定；agy 無工具。prompt 一律走 stdin（命令列有 32K 上限）。agy 的 prompt 超過 28,000 字元才分塊，
**超過 3 塊或第 1 塊沒拿到 conversation id 就直接失敗**，不退回 `--continue`。
離開碼：0＝要求的每一方都有答案；1＝有任一方失敗（其他方的檔照寫）；2＝參數錯。用量記進 `runs/usage_ledger.jsonl`（`relay.py --ledger` 看得到）。

**主持人流程要點**（四次實戰的心得）：

- **先把自己的立場落檔，再發 brief**，以免被三方的回答錨定。
- brief 只放**原始計數**，衍生比率讓各方自己算——brief 裡預先算好的數字，曾是事實錯誤的來源。
- 第二輪用「**待裁決清單＋每條列誰投什麼**」收斂最有效（實戰 8/8 收斂）。
- **程式設計類題目至少兩方要有 repo 唯讀權**，才抓得到 brief 裡的事實錯誤；**需要新數字的輪次一定要給 repo 權**。
- agy 適合當**只看簡報的外行質疑者**：沒有 repo 權，正好檢驗簡報自己講不講得通。
- 原始輸出可能含內部資訊：`--raw-dir` 若落在 git repo 內且沒被 ignore，council 會警告。

第二輪指令範本（泛化）：

```text
這是第二輪。附上第一輪三方各自的意見。請逐條看下面「待裁決清單」，對每一條回答 同意／不同意／部分同意，
各用一句話說明理由；若你在第一輪的立場改變了，明講改了什麼、為什麼。不要重述第一輪已經說過的內容。
有爭議的事實，請直接核對 repo 檔案再回答（不要修改任何檔案）。
待裁決清單：
1. （議題一；列出各方第一輪的立場）
2. （議題二）
```

## 三支 CLI 的固定開銷

同一句「回覆 OK」，2026-09-07 實測。口徑＝**這次真正送進模型的 prompt 總量**：
Claude `input＋cache_creation＋cache_read`；Codex `input_tokens`（已含 cached）；agy `input＋cache_read`。
三家欄位語意都不同，直接抄各家的 `input_tokens` 會比錯。

| CLI | 版本 | 每次 input tokens | 備註 |
|---|---|---|---|
| Claude Code | 2.1.263 | **31,322** | 跟載入的 context 走：同一支 CLI 在另一台（memory 檔較大）量到 37,012 |
| Codex | 0.153.0 | **16,225** | 另一台 15,999，同量級 |
| Antigravity `agy` | 1.1.27 | **5,139 未快取＋8,128 快取＝13,267** | `cache_read` 比 `input` 還大 ⇒ 它是外加的不是子集。`--mode plan` 會漲到 29,337 而且不回答 |
| Gemini | 0.58.0 | 未測 | 需 API 金鑰；此路線已被 agy 取代 |

## 已知行為（踩過的坑）

- **`agy --mode plan` 不是「唯讀回答」模式**：同一句「回覆 OK」它會去寫 `plan.md` 等人點 Proceed，開銷近三倍。
  審查角色要的「唯讀但會回答」用預設模式即可（無頭模式本來就擋寫入）。
- **agy 未登入的無頭呼叫會等滿 60 秒才 exit 1**，逾時設 <60 秒就看不到那個 JSON。它沒有 `login` 子指令，
  第一次互動執行會開瀏覽器走 OAuth。狀態目錄在 `~/.gemini/antigravity-cli/`。
- **Claude 指定不存在的模型時 `subtype` 仍是 `success`**，要看 `is_error` 與 `api_error_status` 才知道失敗了。
- **Codex 的上游錯誤 JSON 是夾在 `error.message` 字串裡的**，不是結構化欄位。
- **Gemini 的 exit code 是 HTTP 碼 mod 256**（400 → 144），而且 `error.code` 是 CLI 內部碼不是 HTTP 碼。
- **Codex 的無頭環境裡 `python`／`py`／`rg` 可能都跑不動**（Windows 上會解析到 WindowsApps stub），
  它自己驗不了測試——這正是「relay 自己跑測試才算數」的由來。
  ⚠️ **連帶效應**：實作者會如實回報「測試全失敗」，而同一份 HANDOFF 的第 3 節 verify 全 PASS。
  **兩段方向相反的訊息並排，很容易把成功的一棒讀成失敗的一棒。** 第 3 節已標明它才是權威——讀 HANDOFF 認那一節。
- 🔑 **寫審查核對條件前，先確定審查者實際做得到**。審查者是唯讀、只拿到 diff、不給工具，
  所以「附上四個驗證指令的實際輸出」「去讀原始檔第 N 行確認」這類條件它結構上無法滿足，只會回「無法判定」。
  **判準必須「只看 diff 就能答」；實跑證據由 `verify[]` 供給，不向審查者要。**
  這條踩過三次（AST 閘門漏判／審查者依指令沒讀外部原始檔＝只驗 diff 內部自洽／規格要求了它做不到的事），
  共同結論是：**閘門檢查不到的地方，不能假設審查者會自己補。**
- 在 Claude Code 對話裡巢狀呼叫 `claude -p`，要先清掉 `CLAUDECODE` 等環境變數——relay／council 已自動處理（`tools/paths.py: CLAUDE_NESTED_ENV`；完整清單與來源見 `fixtures/README.md`）。
- **`tools/agy_review.py` 的分塊實務上限是 3 塊**（2026-09-16 踩到）：同一份 19 條指令，diff 48KB 切 3 塊審得好好的，
  diff 62KB 切 4 塊時 agy 最後那一問只回了塊確認「OK 3」（`len=4`），沒有審查內容、白跑 329 秒；
  同一份 diff 改 `--chunk 34000` 切 2 塊就正常（51 秒、19/19）。⇒ diff 超過 ~55KB 先調大 `--chunk`（單塊 ≤ ~35KB 實測可）
  或把測試／文件的 diff 拆開送審，別讓塊數到 4。`review:` 那行 `len` 個位數＝這個症狀，不是審查通過。
- **agy 登入過期時 relay 只會停下升給人**（`review_tool_failure`，`stderr` 是 `Authentication required`），worktree 改動保留但不 commit。
  重登入後**不必重跑整棒**（會再燒一輪 Codex）：直接 `python tools\agy_review.py --instructions runs\<id>\review_r1_instr.txt --diff runs\<id>\review_r1_diff.txt --out ...`
  補審即可；HANDOFF 不會自動更新，結果要人記。或修好後寫 `human_notes.md`（例如「請照原樣，只需重新審查」）用 `--resume` 接續，HANDOFF 會自動更新。
- **commit 前判改動用內容比對**（2026-10-05）：只差行尾（CRLF↔LF）的檔視為沒改、不 commit、不觸發 `allowed_paths` 中止（autocrlf 下 `status` 會把它標成 ` M`）；
  中文檔名以 `-z` 讀取。同一檔「內容也改了」照常 commit。

## 還沒做

- **審查者換手**：agy 撞牆只會停下推播，不會換別家審。
- **best-of-N 只支援明確宣告**：不會因第一輪失敗自動分叉；候選依序跑、不平行。
- 🔴 **best-of-N 任一候選動了生產目錄 → 整組停止**（2026-10-05）：該候選照常 `aborted`，**後面的候選不啟動**（否則下一份會把已被改過的生產目錄當新基準，前一份的改動從此偵測不到）。仍寫 `RANKING.md`（未跑的標 `not_run`）與 `GROUP.json`，推播一則 `escalate`，群組 exit **3**；其他中止原因（規格外改動、審查工具故障…）仍是只中止該份、繼續下一份。
- 🔴 **沿用既有 worktree 前先驗分支**（2026-10-05）：`worktree` 路徑已存在時，relay 會確認它是 git worktree 的根目錄、且目前分支就是任務的 `branch`（`--dry-run` 與 `--resume` 同一套檢查）；不符就停（exit 3），訊息列出期望／實際分支與路徑。relay **不會**代為 checkout／switch／reset，請人自己處理後再跑。
- **批次啟動器**：並行要自己開兩個行程（每個行程自己守 RELAY_MAX_PARALLEL 名額），沒有一個指令跑一批的 launcher。
- 帳本只用於換手冷卻（預設關），沒有做額度預算。
