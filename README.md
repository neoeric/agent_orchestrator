# agent_orchestrator — 多 AI Agent 協作編排器（起步中）

依「多 Agent 協作方案 v3」實測審查（2026-09-07）落地。目前只有 **P1 的第一塊：結果判定器**。
編排器本身獨立安裝一次，不複製進各專案；服務哪個 repo 尚未拍板（候選：目標 repo，見 memory）。

## 目前有什麼

### /orchestrate skill（上層入口，2026-09-08）

`~/.claude/skills/orchestrate/SKILL.md`：打 `/orchestrate <一句需求>`，主對話會判斷份量（借 ECC `orch-pipeline` 的 size classifier）、寫任務三件套、跑本檔的 relay、讀 `runs/<id>/HANDOFF.md` 回報。合併／部署／重啟仍是人閘門。

### relay.py — 一個子任務一棒（P1 最小可用版，2026-09-07 晚跑通）

```text
PYTHONUTF8=1 python relay.py tasks/<task>.json [--dry-run]
```

流程：開隔離 worktree（永遠不碰生產目錄）→ prebuild → **Codex** 依完全指定規格改碼（不 commit）→ 驗證指令逐條跑、
全部 exit 0 才算過 → **agy** 看 diff 審（分塊內嵌、唯讀）→ 判定器核對每次 CLI 呼叫真的正常結束 → 不過就把發現餵回實作者，
`max_rounds` 輪不收斂停下來給人 → 收斂就以**明列路徑** commit 到 branch。**不合併、不重啟、不 push**——那三步是人閘門。

狀態檔在 `runs/<task_id>/`：`STATE.json`（階段、輪次、每次呼叫 usage）、`CURRENT.md`（接手第一眼）、`HANDOFF.md`（六欄交接簿，
含實作者與審查者原文）、每輪的 prompt／stdout／diff／驗證輸出。任務定義見 `tasks/task-index-sync.json`（欄位：repo、
base_branch、branch、worktree、spec_file、prebuild、verify[]、review.instructions_file、allowed_paths、max_rounds；
`production_dir` 設了會拒絕 worktree 等於生產目錄）。

第一次真跑（目標 repo 的 `index_sync` 既有紅燈，一行修法）：一輪收斂，Codex 186s（input 284K，其中 cached 264K）、三組驗證 274s、
agy 59s（input 12K＋cache 16K，output 4K），commit `0000000` 於 `fix/index-sync`；diff 正是最小改動。

### 在 VS Code 看三個角色協力（即時串流）

relay 把 Codex、agy、驗證、判定的進度邊跑邊印，帶顏色前綴，直接在你自己開的 VS Code 整合終端機看得到：

```text
[CODEX]  青色 · 實作者：它在讀哪個檔、跑什麼指令、改了什麼、講了什麼
[AGY]    紫色 · 審查者：逐條核對與總判定
[VERIFY] 綠色 · 驗證指令的 PASS／FAIL
[JUDGE]  黃色 · 送審或跳過的判準
[RELAY]  灰色 · 編排器自己的階段
```

怎麼看：在 VS Code 的整合終端機（不是 Claude 面板）自己跑

```text
set PYTHONUTF8=1
python relay.py tasks/demo-add-mul.json
```

就會看到三個角色的行交錯滾動。想關顏色設 `NO_COLOR=1`。Codex 與 agy 是背景子行程（走 CLI，不走它們的 VS Code 擴充），
所以它們的擴充面板不會亮；「看得到它們協力」的地方就是這個終端機，加上左邊檔案總管的 `runs/<任務>/` 與 Source Control 的分支 commit。

⚠️ demo 揭露的一個真相：Codex 在自己的無頭環境裡 `python`／`py`／`rg` 都跑不動（WindowsApps stub），它自己驗不了測試，
但改動是對的、relay 用完整路徑 python 一跑就過。這正是設計要點——**工人說「做好了」不算，relay 自己跑測試才算**。

### P2 難易度判準＋簽章閘門＋結構化審查、P4 用量帳本（2026-09-07 晚，三個真任務驗過）

- **判準**（`review.policy`）：`always`（預設）／`never`／`auto`。`auto` 依序看客觀訊號，不用 Agent 自填的風險等級：
  簽章閘門有 breaking → 審；改到 `core_paths` → 審；本任務曾驗證失敗 → 審；小改動（≤`policy.max_files` 檔且
  ≤`policy.max_lines` 行，預設 2／60）→ **跳過審查只靠測試**；其餘 → 審。介面變更永遠不套用跳過規則。
- **簽章閘門** `tools/iface_gate.py`：純 AST 比對改動 .py 的公開介面（頂層與 class 內不以底線開頭的 def）：必填參數增加、
  參數移除、回傳型別改變、公開名稱移除＝breaking；新增公開名稱／選填參數＝additive。陰性對照（重構六組 b7d5df3→master）0 breaking。
- **結構化審查**：審查指令要求最後一行輸出 `{"verdict":"approve|changes_requested","checks":[...],"unreported":[...]}`；
  `parse_review` 以 JSON 為準、沒有就退回第一行「可合併／需修改」。
- **帳本** `runs/usage_ledger.jsonl`：每次呼叫一行（task／role／cli／usage／秒數／failure_class）；`python relay.py --ledger` 看總計。
  判定器回 `rate_limit` 時 relay 大聲停下（不自動換手——真 429 尚未觀察到）。
- 三個真任務：`task-index-sync`（policy always，審→approve）、`task-ps1-tests`（auto：3 檔 6 行非核心 → **跳過審查**，
  兩組驗證含 `tools/check_ps1_encoding.py` BOM／換行／PSParser 檢查）、`task-dashboard-pollers`（auto：dashboard.html 是核心
  → 送審，agy 回 JSON 七條 pass）。三支分支皆已由人閘門合併上線（目標 repo master `0000000`）。

**仍未做**：換手（實作者固定 Codex、審查者固定 agy）；並行；P4 只記帳不換手。

### 其他檔案

| 檔案 | 用途 |
|---|---|
| `judge.py` | 四支 CLI（claude／codex／gemini／agy）headless 回傳的**白名單判定器**（設計原則見檔頭 docstring） |
| `_test_judge.py` | 判定器契約測試，63 項；`PYTHONUTF8=1 python _test_judge.py` |
| `fixtures/` | 十個真實回傳樣本＋來源說明（`fixtures/README.md`）；CLI 升版就重抓 |
| `tools/closure_map.py` | 純 AST 依賴閉包圖（重構棒 1）：每個頂層 def 的閉包碰不碰 db／net／model／route |
| `tools/deps_of.py` | 指定函式用到的同模組常數、from-import（搬函式前查「還要帶什麼」） |
| `tools/extract_defs.py` | 把頂層定義往外抽成新模組＋原檔原名 re-export；連同緊貼的註解一起搬；保 CRLF |
| `tools/iface_gate.py` | 簽章閘門：改動 .py 公開介面的 breaking／additive（純 AST） |
| `tools/check_ps1_encoding.py` | .ps1 改動後 BOM 數不變、換行不混用、無控制字元、PSParser 0 錯 |
| `_test_relay.py` | relay 的判準／解析／閘門／帳本純邏輯測試，26 項 |
| `tools/agy_review.py` | agy 唯讀審查：diff 分塊用 `--continue` 餵進同一對話（命令列 32K 上限、agy 不讀 stdin、給路徑會被軟拒） |
| `plans/` | 計畫書與閉包圖（目標 repo 重構第一階段） |
| `tasks/`、`runs/` | relay 的任務定義與執行紀錄 |

```text
python judge.py claude out.txt --stderr err.txt --exit-file exit.txt      # 人看的摘要
python judge.py gemini out.txt --exit 144 --json                           # 機器讀的完整判定
```

## 本機三支 CLI 的固定開銷（同一句「回覆 OK」，2026-09-07）

口徑＝**這次真正送進模型的 prompt 總量**：Claude `input＋cache_creation＋cache_read`；Codex `input_tokens`（已含 cached）；agy `input＋cache_read`。三支的欄位語意都不同，直接抄各家的 `input_tokens` 會比錯。

| CLI | 版本 | 每次 input tokens | 備註 |
|---|---|---|---|
| Claude Code | 2.1.263 | **31,322** | 全域 CLAUDE.md 7.3 KB；執行目錄無專案 memory。另一台機器量到 37,012（其 memory 檔 31 KB）⇒ 開銷確實跟載入的 context 走 |
| Codex | 0.153.0 | **16,225** | 另一台 15,999，同量級 |
| Antigravity agy | 1.1.27 | **5,139 未快取＋8,128 快取＝13,267** | 另一台寫的「約 5,150」只算 `input_tokens`。cache_read 比 input 大 ⇒ 外加不是子集；同口徑（全部送進去的 prompt）是 13,267。`--mode plan` 會漲到 29,337 且不回答 |
| Gemini | 0.58.0 | 待金鑰 | 另一台已改用 agy，此路線可能作廢 |

## 三支 CLI 的絕對路徑（帶版號的會隨擴充升級失效，啟動時要比對）

- Claude：`C:\Users\<user>\.vscode\extensions\anthropic.claude-code-2.1.263-win32-x64\resources\native-binary\claude.exe`
  （PATH 上的 npm `claude` 是 2.1.246，別混用）
- Codex：`C:\Users\<user>\.vscode\extensions\openai.chatgpt-26.5901.22334-win32-x64\bin\windows-x86_64\codex.exe`
- Gemini：`C:\Users\<user>\AppData\Roaming\npm\gemini`（npm 全域，路徑不帶版號）
- Antigravity：`C:\Users\<user>\AppData\Local\agy\bin\agy.exe`（官方 install.ps1，已加進 User PATH，路徑不帶版號；`agy update` 自更新）

## 已知環境事項

- **agy `--mode plan` 不是唯讀回答模式**：同一句「回覆 OK」它會寫 plan.md 要人點 Proceed 才執行，開銷近三倍。審查角色要的「唯讀但會回答」得用預設模式＋不給寫入工具（另一台實測預設無頭就擋寫入）。
- **agy 狀態目錄**在 `~/.gemini/antigravity-cli/`（沿用 Gemini CLI 的家目錄），plan.md／對話都在 `brain/<conversation_id>/`。
- **agy 登入**：無 `login` 子指令，第一次互動執行 `agy` 會開瀏覽器走 Google OAuth，token 存 Windows 憑證管理員；之後無頭呼叫靜默登入。未登入的無頭呼叫會等滿 60 秒才 exit 1，逾時要設 >60 秒。

- **Gemini 找 ripgrep 只看自己的 bundle 目錄或「受信任系統路徑」**，使用者層 winget 的 rg 不算。
  已把 winget 的 `rg.exe`（15.2.0）複製成
  `…\npm\node_modules\@google\gemini-cli\bundle\rg-win32-x64.exe`，警告消失。
  ⚠️ **Gemini 一升版這個副本就沒了**，要重做一次（來源 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_*\ripgrep-*\rg.exe`）。
- Gemini 的 `~/.gemini/.env` 在 headless 模式**不會被讀**；金鑰要設 User 層環境變數 `GEMINI_API_KEY`。
- 在 Claude Code 對話裡巢狀呼叫 `claude -p`，要先 unset `CLAUDECODE` 等環境變數（見 fixtures/README.md 的抓法）。
