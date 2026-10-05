# agent_orchestrator

讓三個 AI CLI 分工跑完一個子任務的編排器：**Codex 實作 → 測試驗證 → Antigravity（agy）審查 → 判定器核對**，
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
| [Codex CLI](https://github.com/openai/codex) | 實作者角色 | 啟動時報錯停下 |
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
  "prebuild": [],
  "verify": [
    {"name": "unit", "cmd": "python -m pytest tests/", "timeout": 600}
  ],
  "review": {
    "reviewer": "agy",
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
| `production_dir` | 選填但**強烈建議填**：`worktree` 等於它就直接拒跑，防手滑改到生產目錄 |
| `spec_file` | 給實作者的完全指定規格（相對路徑以編排器目錄為基準） |
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

跑：

```text
PYTHONUTF8=1 python relay.py tasks/my-task.json
PYTHONUTF8=1 python relay.py tasks/my-task.json --dry-run   # 只印計畫，不呼叫任何 CLI（寫到 runs/<id>.dry/）
PYTHONUTF8=1 python relay.py tasks/my-task.json --queue     # 並行名額滿了就排隊（見「並行」）
PYTHONUTF8=1 python relay.py --status   # 所有棒的階段、輪次、耗時、是否等人（--all 看全部）
PYTHONUTF8=1 python relay.py --ledger                        # 看累計用量
```

`PYTHONUTF8=1` 在 Windows 是必要的，否則輸出非 ASCII 會直接 cp950 crash。

`--status` 的「等人？」欄：`跑中`／`排隊中`（任務鎖有人持有）、`待合併 <commit>`、
`要人看（未收斂／審查工具故障／撞牆／中止：原因）`、`中斷？（行程已不在）`（STATE 停在中途但任務鎖沒人持有＝
行程被硬殺或關機，重跑即可）。判活只看任務鎖，不看 STATE 寫什麼；預設列最近 15 筆。

## 一棒的流程

```text
1. 開隔離 worktree（拒絕等於 production_dir）
2. prebuild
3. Codex 依 spec_file 改碼（不 commit）
4. verify[] 逐條跑，全部 exit 0 才算過
4b. verify 全綠後跑 negative_controls：注入 → 必須紅在 marker → 還原
5. 判準決定要不要送審 → agy 看 diff 唯讀審查，輸出結構化 verdict
6. judge.py 核對每次 CLI 呼叫真的正常結束（不是「有輸出就當成功」）
7. 過 → 以 allowed_paths 明列路徑 commit 到 branch；不過 → 把發現餵回步驟 3，最多 max_rounds 輪
```

跑的時候四個角色的進度帶色前綴即時交錯印出，直接在終端機看得到協力過程：

```text
[CODEX]  青色 · 實作者：讀哪個檔、跑什麼指令、改了什麼
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
| `STATE.json` | 階段、輪次、每次 CLI 呼叫的 usage；含中止原因，relay 被例外中止時階段記為 `aborted` |
| `HANDOFF.md` | 六欄交接簿，含實作者與審查者原文 |
| `relay.log` | 完整時序 |
| `impl_r*` / `review_r*` / `verify_r*` | 每輪的 prompt、diff、審查指令、驗證輸出 |
| `runs/usage_ledger.jsonl` | 跨任務帳本，每次呼叫一行（task／role／cli／usage／秒數／failure_class） |
| `runs/.locks/` | 並行鎖檔（空檔，不必手動刪；見「並行」） |

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
| `runlock.py` | 跨行程檔案鎖（任務／worktree／並行名額／repo／帳本）；Windows `msvcrt`、POSIX `flock`，行程死掉 OS 自動釋放 |

## 測試

```text
PYTHONUTF8=1 python _test_judge.py    # 判定器契約測試，63 項
PYTHONUTF8=1 python _test_relay.py    # 判準／解析／閘門／帳本／狀態總表／鎖與並行，93 項
PYTHONUTF8=1 python _test_council.py  # council 純邏輯＋Claude CLI 解析（假 CLI，不燒額度），51 項
```

`fixtures/` 是三支 CLI 的**真實回傳樣本**（2026-09-07 抓），判定器契約測試靠它。
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
| `--no-safe-mode` | claude 預設帶 `--safe-mode`（不載入使用者 hooks／plugins／CLAUDE.md）；它若影響輸出格式時的退路 |

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
  補審即可；HANDOFF 不會自動更新，結果要人記。
- **commit 前判改動用內容比對**（2026-10-05）：只差行尾（CRLF↔LF）的檔視為沒改、不 commit、不觸發 `allowed_paths` 中止（autocrlf 下 `status` 會把它標成 ` M`）；
  中文檔名以 `-z` 讀取。同一檔「內容也改了」照常 commit。

## 還沒做

- **換手**：實作者固定 Codex、審查者固定 agy，判定器回 `rate_limit` 時 relay 只會大聲停下，不自動換另一家。
- **批次啟動器**：並行要自己開兩個行程（每個行程自己守 RELAY_MAX_PARALLEL 名額），沒有一個指令跑一批的 launcher。
- 帳本只記帳，沒有據以調度。
