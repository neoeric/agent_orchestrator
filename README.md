# agent_orchestrator

讓三個 AI CLI 分工跑完一個子任務的編排器：**Codex 實作 → 測試驗證 → Antigravity（agy）審查 → 判定器核對**，
不收斂就把審查意見餵回實作者再跑一輪。工作狀態全部外部化成檔案，不依賴任何一方的對話記憶。

編排器獨立安裝一次，用 `git worktree` 在隔離目錄裡改碼，**永遠不碰你的生產目錄**。

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
| git 2.5+ | `git worktree` 隔離 | — |

在 Windows 11 上開發與實測，其他平台沒測過。

**執行檔怎麼找**：`tools/paths.py` 的順序一律是 **環境變數 → PATH → 已知安裝位置 → 報錯**。
換機器只要裝好並登入；路徑特殊就設 `RELAY_CODEX`、`RELAY_AGY`（或 `AGY_EXE`）、`RELAY_PYTHON`。
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
| `prebuild` | 開好 worktree 後、改碼前要跑的指令（裝依賴、建虛擬環境…） |
| `verify[]` | 每輪都要跑的驗證，`{name, cmd, timeout?, env?}`；全部 exit 0 才算過 |
| `review.policy` | `always`（預設）／`never`／`auto`，判準見下節 |
| `review.instructions_file` | 給審查者的逐條核對條件 |
| `core_paths` | `auto` 判準用：碰到就一定送審 |
| `policy.max_files` / `max_lines` | `auto` 的小改動門檻，預設 2 檔 / 60 行 |
| `allowed_paths` | commit 時只收這些路徑；沒設就收全部改動 |
| `max_rounds` | 幾輪不收斂就停下來給人，預設 2 |

跑：

```text
PYTHONUTF8=1 python relay.py tasks/my-task.json
PYTHONUTF8=1 python relay.py tasks/my-task.json --dry-run   # 只印計畫，不呼叫任何 CLI
PYTHONUTF8=1 python relay.py --ledger                        # 看累計用量
```

`PYTHONUTF8=1` 在 Windows 是必要的，否則輸出非 ASCII 會直接 cp950 crash。

## 一棒的流程

```text
1. 開隔離 worktree（拒絕等於 production_dir）
2. prebuild
3. Codex 依 spec_file 改碼（不 commit）
4. verify[] 逐條跑，全部 exit 0 才算過
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
| `STATE.json` | 階段、輪次、每次 CLI 呼叫的 usage |
| `HANDOFF.md` | 六欄交接簿，含實作者與審查者原文 |
| `relay.log` | 完整時序 |
| `impl_r*` / `review_r*` / `verify_r*` | 每輪的 prompt、diff、審查指令、驗證輸出 |
| `runs/usage_ledger.jsonl` | 跨任務帳本，每次呼叫一行（task／role／cli／usage／秒數／failure_class） |

> `tasks/`、`runs/`、`plans/` 預設不進版控（見 `.gitignore`）——它們會包含**你自己專案的程式碼片段與審查原文**。
> 要分享任務範本請自行挑選、確認內容後再加。

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
| `paths.py` | 三支 CLI 的執行檔解析（env → PATH → 已知位置） |
| `closure_map.py` | 純 AST 依賴閉包圖：每個頂層 def 的閉包碰不碰 db／net／model／route |
| `deps_of.py` | 指定函式用到的同模組常數與 from-import（搬函式前查「還要帶什麼」） |
| `extract_defs.py` | 把頂層定義抽成新模組並在原檔原名 re-export；連緊貼的註解一起搬，保 CRLF |
| `iface_gate.py` | 簽章閘門：公開介面的 breaking／additive 判定 |
| `check_ps1_encoding.py` | .ps1 改動後 BOM 數不變、換行不混用、無控制字元、PSParser 0 錯 |
| `agy_review.py` | agy 唯讀審查：diff 分塊用 `--continue` 餵進同一對話（命令列有 32K 上限、agy 不讀 stdin、給路徑會被軟拒） |

## 測試

```text
PYTHONUTF8=1 python _test_judge.py    # 判定器契約測試，63 項
PYTHONUTF8=1 python _test_relay.py    # 判準／解析／閘門／帳本純邏輯，26 項
```

`fixtures/` 是三支 CLI 的**真實回傳樣本**（2026-09-07 抓），判定器契約測試靠它。
**任一 CLI 升版後全部重抓一輪再跑一次測試**——欄位語意變了不會有人通知你，這組測試是唯一的紅燈。
抓法與每份樣本的重點見 `fixtures/README.md`。

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
- 在 Claude Code 對話裡巢狀呼叫 `claude -p`，要先 unset `CLAUDECODE` 等環境變數（`fixtures/README.md` 有完整清單）。

## 還沒做

- **換手**：實作者固定 Codex、審查者固定 agy，判定器回 `rate_limit` 時 relay 只會大聲停下，不自動換另一家。
- **並行**：一次一棒。
- 帳本只記帳，沒有據以調度。
