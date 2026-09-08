#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_gate.py — 盤點／稽核類報告的「防編造」驗收閘門（設定驅動）。

## 為什麼需要這支

要 LLM 產一份盤點報告時，最危險的失敗**不是寫不出來，是寫得很好但內容是編的**：
不存在的檔名、湊數的行號、憑印象描述的頁面。人讀報告時看不出哪一行查過、哪一行掰的，
而「報告讀起來很完整」正是編造的典型症狀。

這支把可機械驗證的部分全部驗掉，剩下的才值得交給人或審查者判斷。
它驗不了「`server.py:2120` 那行到底寫什麼」——那需要實際讀檔比對，
**請在審查條件裡明確要求抽驗，別假設審查者會自己做**（2026-09-08 實測：
agy 在沒有明確授權讀檔時，只驗 diff 內部自洽就回報通過）。

## 用法

    python tools/report_gate.py <config.json>          # cwd = repo 或 worktree 根目錄
    python tools/report_gate.py <config.json> --report other.md   # 覆寫報告路徑

設定檔格式見 tools/report_gate.example.json。每個區塊都可省略，省略就不做該項檢查。
"""
import json
import re
import sys
from pathlib import Path

EXT_GROUP = r"py|html|md|svg|sql|json|txt|docx|sh|ps1|bat|ts|tsx|js|css|yml|yaml|toml|ini|cfg"
PATHY_RE = re.compile(rf"`([A-Za-z0-9_][A-Za-z0-9_\-./]*\.(?:{EXT_GROUP}))`")
ROUTE_RE = re.compile(r"`(/[a-z0-9][a-z0-9_\-/{}.:]*)`")
DEFAULT_SKIP = ["http://", "https://", "/opt/", "/etc/", "/var/", "/usr/", "/dev/"]


class Gate:
    def __init__(self, cfg: dict, root: Path):
        self.cfg = cfg
        self.root = root
        self.errors: list[str] = []
        self.notes: list[str] = []

    def add(self, msg: str) -> None:
        self.errors.append(msg)

    # ── 1. 報告存在與份量 ──
    def check_exists(self, report: Path) -> str | None:
        if not report.is_file():
            self.add(f"報告檔不存在：{report}")
            return None
        text = report.read_text(encoding="utf-8")
        lo = self.cfg.get("min_chars")
        if lo and len(text) < lo:
            self.add(f"報告只有 {len(text)} 字元，低於門檻 {lo}")
        hi = self.cfg.get("max_chars")
        if hi and len(text) > hi:
            self.add(f"報告 {len(text)} 字元，超過上限 {hi}（可能在複述原始碼）")
        self.notes.append(f"報告 {len(text)} 字元 / {len(text.splitlines())} 行")
        return text

    # ── 2. 必要段落 ──
    def check_sections(self, text: str) -> None:
        secs = self.cfg.get("required_sections") or []
        missing = [s for s in secs if s not in text]
        for s in missing:
            self.add(f"缺少必要段落：「{s}」")
        if secs and not missing:
            self.notes.append(f"段落齊全（{len(secs)} 段）")

    # ── 3. 引用的檔案路徑真的存在（防造檔名）──
    def check_paths(self, text: str) -> None:
        c = self.cfg.get("path_refs")
        if c is None:
            return
        skip = tuple(c.get("skip_prefixes", DEFAULT_SKIP))
        # 版控外的目錄（如 gitignored 的 views/）在 worktree 查不到，改對白名單
        wl = set(c.get("whitelist", []))
        wildcards = tuple(c.get("wildcard_prefixes", []))
        guarded = tuple(c.get("guarded_prefixes", []))

        found = sorted(set(PATHY_RE.findall(text)))
        bad = []
        for m in found:
            if m.startswith(skip) or m in wl or (wildcards and m.startswith(wildcards)):
                continue
            if guarded and m.startswith(guarded):
                # 這些前綴不在版控內：不在白名單就是可疑，不要放行
                bad.append(f"{m}（在版控外目錄但不在已知清單）")
                continue
            if not (self.root / m).exists():
                bad.append(m)
        if bad:
            self.add("報告引用了不存在的檔案（可能是編造或筆誤）："
                     + "、".join(bad[:12])
                     + (f" …共 {len(bad)} 個" if len(bad) > 12 else ""))
        else:
            self.notes.append(f"引用檔案路徑全部存在（{len(found)} 個）")

    # ── 4. 引用的路由真的有定義（防造路由）──
    def check_routes(self, text: str) -> None:
        c = self.cfg.get("route_refs")
        if c is None:
            return
        src_p = self.root / c["source"]
        if not src_p.is_file():
            self.add(f"route_refs.source 不存在：{c['source']}")
            return
        src = src_p.read_text(encoding="utf-8", errors="replace")
        defined = set(re.findall(c["pattern"], src))
        if not defined:
            self.add(f"從 {c['source']} 抽不到任何路由，pattern 可能失效")
            return
        skip = tuple(c.get("skip_prefixes", DEFAULT_SKIP))
        bad = []
        for r in sorted(set(ROUTE_RE.findall(text))):
            if r.startswith(skip) or r in defined:
                continue
            # 前綴比對：/views/{name:path} 涵蓋 /views/wiki/index.html
            if any(r.startswith(d.split("{")[0]) for d in defined if "{" in d):
                continue
            if re.search(rf"\.(?:{EXT_GROUP})$", r):
                continue  # 是檔名不是路由，已由 check_paths 驗過
            bad.append(r)
        if bad:
            self.add(f"報告提到 {c['source']} 沒有定義的路由：" + "、".join(bad[:12]))
        else:
            self.notes.append(f"引用路由全部有定義（來源 {c['source']}）")

    # ── 5. 該盤點的項目一個都沒漏（防漏盤點）──
    def check_coverage(self, text: str) -> None:
        c = self.cfg.get("coverage")
        if c is None:
            return
        src_p = self.root / c["source"]
        if not src_p.is_file():
            self.add(f"coverage.source 不存在：{c['source']}")
            return
        src = src_p.read_text(encoding="utf-8", errors="replace")
        if c.get("block_pattern"):
            blk = re.search(c["block_pattern"], src, re.S)
            if not blk:
                self.add(f"在 {c['source']} 找不到 coverage.block_pattern 指定的區塊")
                return
            src = blk.group(1)
        items = re.findall(c["entry_pattern"], src)
        lo = c.get("min_entries", 1)
        if len(items) < lo:
            self.add(f"只抽到 {len(items)} 個待涵蓋項目（低於 {lo}），抽取邏輯可能失效")
            return
        # 每個項目可能是 tuple（多個可接受的代稱），任一出現即算涵蓋
        missing = []
        for it in items:
            keys = [k for k in (it if isinstance(it, tuple) else (it,)) if k]
            if not any(k in text for k in keys):
                missing.append("／".join(keys))
        if missing:
            self.add(f"應涵蓋項目未出現在報告中（{len(missing)}/{len(items)}）："
                     + "、".join(missing[:10]))
        else:
            self.notes.append(f"應涵蓋項目全部出現（{len(items)} 項）")

    # ── 6. 結論表每列都有結論（防只列清單不表態）──
    def check_table(self, text: str) -> int:
        c = self.cfg.get("table")
        if c is None:
            return 0
        sec = c.get("section")
        tail = text[text.find(sec):] if sec and sec in text else text
        if c.get("stop_at") and c["stop_at"] in tail:
            tail = tail[:tail.find(c["stop_at"])]
        rows = [ln for ln in tail.splitlines()
                if ln.strip().startswith("|") and ln.count("|") >= 3
                and not re.match(r"^\s*\|[\s\-:|]+\|\s*$", ln)]
        headers = c.get("header_words", [])
        body = [ln for ln in rows if not any(h in ln for h in headers)]
        lo = c.get("min_rows", 1)
        if len(body) < lo:
            self.add(f"「{sec}」表只有 {len(body)} 列資料，少於最低要求 {lo}")
            return len(body)
        words = c.get("verdict_words", [])
        if words:
            no_v = [ln for ln in body if not any(w in ln for w in words)]
            if no_v:
                self.add(f"「{sec}」表有 {len(no_v)} 列沒有明確結論"
                         f"（需含 {'／'.join(words)} 之一）")
                return len(body)
        self.notes.append(f"「{sec}」表 {len(body)} 列，每列都有結論")
        return len(body)

    def run(self, report: Path) -> int:
        text = self.check_exists(report)
        if text is None:
            print(f"FAIL: {self.errors[0]}")
            return 1
        self.check_sections(text)
        self.check_paths(text)
        self.check_routes(text)
        self.check_coverage(text)
        self.check_table(text)

        if self.errors:
            for e in self.errors:
                print(f"FAIL: {e}")
            print(f"\n{len(self.errors)} 項未通過。")
            return 1
        print("OK: 報告通過全部機械檢查。")
        for n in self.notes:
            print(f"  - {n}")
        return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.split("## 用法")[1].strip() if "## 用法" in __doc__ else
              "usage: report_gate.py <config.json> [--report <path>]")
        return 2
    cfg_path = Path(sys.argv[1])
    if not cfg_path.is_file():
        print(f"FAIL: 設定檔不存在：{cfg_path}")
        return 2
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    report_rel = cfg.get("report")
    if "--report" in sys.argv:
        report_rel = sys.argv[sys.argv.index("--report") + 1]
    if not report_rel:
        print("FAIL: 設定檔缺 report 欄位，也沒給 --report")
        return 2

    root = Path.cwd()
    return Gate(cfg, root).run(root / report_rel)


if __name__ == "__main__":
    sys.exit(main())
