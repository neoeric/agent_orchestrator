# fixtures — 三支 CLI 的真實回傳樣本（判定器契約測試用）

**抓取時間** 2026-09-07 ｜ **機器** 本機（Windows 11）｜ **提示詞** 一律「回覆 OK」｜
**執行目錄** 編排器安裝目錄（非 git repo，故 Codex 帶 `--skip-git-repo-check`）。
標準輸入一律關閉（`< /dev/null`），stdout／stderr 分開存，exit code 存 `*.exit.txt`。
Claude 兩份是在 Claude Code 對話內巢狀抓的，抓之前先
`env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_PID -u CLAUDE_CODE_ENTRYPOINT`
（預防性，沒有測過不 unset 會不會被擋）。

**C4a 兩份（`claude_stream_ok.*`、`codex_stdin_ok.*`，2026-10-05）不同於上面**：prompt 經 **stdin** 送（relay 實作者的實際送法），
執行目錄是新建的空暫存目錄；claude 那份先刪 `tools/paths.py` 的 `CLAUDE_NESTED_ENV`（7 個），exe 由 `paths.resolve_claude()` 解析。
抓取時環境裡另有 `CLAUDE_EFFORT`、`CLAUDE_CODE_EMIT_STARTUP_TIMING` 等 SDK 變數沒刪，輸出照常（stdout 沒有雜訊行）。

⚠️ **這些樣本逐字保留，只有一個例外**：五個檔（`agy_ok_plan.stdout.txt`、`gemini_badkey_exit144.txt`、
`gemini_gca_exit1.txt`、`gemini_noauth_exit41.txt`、`claude_stream_ok.stdout.txt`）裡出現在檔案路徑中的 Windows 使用者名，
共 18 處已改成 `<user>`（`claude_stream_ok` 佔 2 處：init 事件的 `cwd` 與 `plugins[].path`）。**其餘一個位元組都沒動**——JSON 結構、欄位值、行序、雜訊行、exit code 全是原樣。
會特別記這一條，是因為這批樣本的價值就在「它是真的」；改過就要說，否則「真實樣本」這個宣稱本身失真。

| 檔案 | CLI／版本 | 怎麼抓 | exit | 重點 |
|---|---|---|---|---|
| `claude_ok.*` | Claude Code 2.1.263（擴充內建 `resources\native-binary\claude.exe`） | `claude -p "回覆 OK" --output-format json` | 0 | 單行 JSON；固定開銷 **31,322** input tokens（2＋cache_creation 15,878＋cache_read 15,442）；模型 claude-fable-5-1，`total_cost_usd` 0.32 |
| `claude_err.*` | 同上 | 加 `--model claude-nonexistent-9` | 1 | 🔴 `is_error=true`、`api_error_status=404`、`terminal_reason=api_error`，**但 `subtype` 仍是 `success`**；stderr 一行 `[claude-code:unrecognized_model]` |
| `codex_ok.*` | Codex CLI 0.153.0（`openai.chatgpt-26.5901.22334-win32-x64\bin\windows-x86_64\codex.exe`） | `codex exec --json -s read-only --skip-git-repo-check -C <dir> "回覆 OK"` | 0 | JSONL 四個事件；`turn.completed.usage.input_tokens` **16,225**（cached 1,408）；stderr 一行 `Reading additional input from stdin...`（stdin 關掉仍照跑） |
| `codex_err.*` | 同上 | 加 `-m gpt-nonexistent-9` | 1 | 先一個 `item.completed`（type=error，只是警告）、再 `error` 事件、最後 `turn.failed`；上游 JSON 夾在 `error.message` **字串裡**，`status=400` |
| `gemini_noauth_exit41.txt` | Gemini CLI 0.58.0（npm 全域） | 無任何認證，`gemini -p "回覆 OK" -o json --approval-mode plan --skip-trust` | 41 | 乾淨的 pretty JSON，`error.code=41`（CLI 內部碼，不是 HTTP） |
| `gemini_badkey_exit144.txt` | 同上 | `GEMINI_API_KEY=dummy-key-for-probe` | **144** | 前面 15 行雜訊（true color 警告、ripgrep 警告、`_ApiError` stack trace），**最後一個** JSON 物件 `error.code=400`；**exit 144＝400 mod 256** |
| `gemini_gca_exit1.txt` | 同上 | `NO_BROWSER=true GOOGLE_GENAI_USE_GCA=true`＋6 月舊 OAuth 憑證 | 1 | **完全沒有 JSON**，只有 `IneligibleTierError UNSUPPORTED_CLIENT` stack trace（Code Assist 個人層已關） |
| `agy_unauth.*` | Antigravity CLI 1.1.27（`%LOCALAPPDATA%\agy\bin\agy.exe`，官方 install.ps1 裝） | 未登入，`agy -p "回覆 OK" --output-format json --mode plan` | 1 | **等滿 60 秒才退出**；stdout 是乾淨單行 JSON `status=ERROR`、`error="authentication failed or timed out"`；OAuth 網址與「Waiting for authentication (timeout 60s)」提示在 **stderr**（6 行）。逾時設 <60 秒會看不到那個 JSON |
| `agy_ok.*` | 同上，已 OAuth 登入（AI Pro） | 預設模式 `agy -p "回覆 OK" --output-format json` | 0 | 乾淨單行 JSON `status=SUCCESS`、`response="OK\n"`、stderr 空；`usage`：input **5,139**、cache_read **8,128**、output 85（thinking 84）、total 5,224＝input＋output ⇒ **cache_read 是外加的不是子集**。耗時 1.9s |
| `agy_ok_plan.*` | 同上 | 同句加 `--mode plan` | 0 | 🔴 **不回答**：response 是「已為您建立執行計畫 plan.md…請確認」，plan.md 寫在 `~/.gemini/antigravity-cli/brain/<conv>/`；input 13,075、cache_read 16,262、output 1,185（thinking 863）＝開銷近三倍。plan 模式是「先規劃等確認」，不是「唯讀回答」 |
| `claude_stream_ok.*` | Claude Code 2.1.246（npm 套件內的原生 `bin\claude.exe`） | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence --safe-mode`（relay 的 claude 實作者全部旗標），stdin＝「回覆 OK」 | 0 | 4 行 JSONL：`system/init` → `assistant` → `rate_limit_event` → `result`；判定器取**最後一個** result 照判 ⇒ **`--safe-mode` 不影響 stream-json**。init 的 `tools` 只有那五個（無 Bash）、`mcp_servers` 空；init 仍列出 `plugins`（使用者裝的一個）與內建 skills，但 `slash_commands` 裡沒有使用者 skill。固定開銷 **5,919**（2＋cache_creation 5,917）；對照 `claude_ok`（2.1.263、沒加 `--safe-mode`）31,322，差距推測是 CLAUDE.md／memory 沒載入（不是受控對照）。模型＝CLI 預設，`total_cost_usd` 0.059 |
| `codex_stdin_ok.*` | Codex CLI 0.160.0（VS Code 擴充內建 `codex.exe`） | `codex exec --json -s read-only --skip-git-repo-check -C <空暫存目錄> -`，stdin＝「回覆 OK」 | 0 | 與 `codex_ok` 同形狀的四個事件；input 17,292（cached 8,320）；stderr 空（`codex_ok` 那行 `Reading additional input from stdin...` 這次沒有） |

**還缺**：Gemini 成功樣本（本機尚無 API 金鑰，且另一台已改用 agy，大概率不再需要）。Gemini 的成功形狀在 `_test_judge.py` 仍是合成樣本。

**何時重抓**：任一 CLI 升版後全部重抓一輪、重跑 `_test_judge.py`。欄位語意變了不會有人通知你，
這組測試就是唯一的紅燈。
