---
name: visual-parity
description: 比對兩個已渲染網頁的樣式落差（新舊版對照 / 改版驗收），跑互動 E2E 流程並蒐集 console 與 network 錯誤，以及在 375/768/1440 三種寬度檢查 RWD 與水平溢出。當使用者提到「新舊頁面長得不一樣」「跟測試環境比對」「像素比對」「樣式對不上」「visual diff」「RWD 檢查」「開 dev server 驗收」「E2E 驗收」時使用。與 webapp-testing 互補：webapp-testing 負責 server 生命週期與臨時 Playwright 腳本，本 skill 負責可重複的比對與驗收。
---

# Visual Parity

一次跑完、產出完整落差清單。**禁止**「改一項 → 截圖 → 再改一項」的來回迴圈。

## 為什麼存在

改一項就截圖一次，會把每張截圖與每份 computed style 永久留在 context 裡。同一份工作在 600k context 下做，成本是 100k 時的六倍。本 skill 把比對結果寫進檔案，只回傳一張壓縮過的落差表。

## 鐵則

1. **不要 Read `scripts/parity.py`。** 它是黑箱，用 `--help` 就夠。
2. **不要把報告全文讀進 context。** stdout 的摘要已含「最常出現的落差屬性」；需要細節時用 `grep` 撈特定屬性或 selector，不要 `Read` 整份。
3. **不要逐項修。** 先看 stdout 的 top differing properties——落差通常是少數幾條系統性規則（一條 `margin-bottom`、一組 token）造成的，不是幾十個獨立問題。
4. **改完重跑一次全量比對**，而不是只驗剛改的那一項。

## 前置：dev server

server 生命週期交給既有的 webapp-testing skill，不要自己寫：

```bash
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "npm run dev -- --experimental-https" --port 3000 \
  -- python ~/.claude/skills/visual-parity/scripts/parity.py diff --ref <URL> --local <URL> --out report.md
```

server 已經在跑就直接呼叫 `parity.py`。自簽憑證已預設接受（`ignore_https_errors`）。

## diff — 新舊頁面樣式落差

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py diff \
  --ref   https://staging.example.com/faq/content?id=28473 \
  --local https://localhost:3000/faq/content?id=28473 \
  --out   parity-diff.md
```

元素配對：先套 `--map` 的手動對照，再以「標籤+文字」自動配對，最後以「純文字」補配（吸收 SPA→SSR 的標籤變化，例如 `<p>` 變 `<div>`）。

stdout 會回報四個數字：

- `matched` — 成功配對並比對的元素數
- `diffs` — 屬性落差總數
- `missing-locally` — 舊頁有、新頁完全沒有的內容（**抓漏掉的區塊用這個**）
- `skipped-ambiguous` — 文字在頁面上重複、無法 1:1 配對，**未被比對**。數字大不代表有問題，但代表覆蓋率沒滿；關鍵區塊落在這裡就用 `--map` 補。

其他參數：`--width` / `--widths`、`--wait-selector`（等內容渲染完）、`--extra-wait`（動畫或延遲載入）。
Exit code 1 表示有落差，0 表示完全一致。

### --map：手動指定對照

DOM 結構差太多時（舊 iframe vs 新 SSR），用 JSON 補：

```json
{
  "#faqAnswer": ".faq-body",
  ".uform_search": "[data-testid=search-hero]",
  "#footer .crumb": "nav[aria-label=breadcrumb]"
}
```

左邊是 ref 的 selector、右邊是 local 的。支援尾綴比對，不必寫完整路徑。

## responsive — 三種寬度 + 水平溢出

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py responsive \
  --ref <URL> --local <URL> --out parity-responsive.md
```

等同在 375 / 768 / 1440 各跑一次 diff，另外檢查 local 是否出現水平溢出（`scrollWidth > clientWidth`），這是 RWD 破版最常見的徵兆。

## flow — 互動驗收 + console/network 錯誤

用 JSON 描述步驟，不要每次現寫 Playwright：

```json
[
  {"action": "goto", "url": "https://localhost:3000/products"},
  {"action": "click", "selector": ".ba-free-download", "desc": "點 Free Download"},
  {"action": "wait", "selector": "[role=dialog]"},
  {"action": "expect_focused", "selector": "[role=dialog] input[type=email]", "desc": "focus 應落在 input 而非 close button"},
  {"action": "fill", "selector": "input[type=email]", "value": "a@b.com"},
  {"action": "click", "selector": "button[type=submit]"},
  {"action": "expect_text", "selector": "[role=dialog]", "value": "Check your email"}
]
```

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow --steps steps.json --out parity-flow.md
```

可用 action：`goto` `click` `fill` `press` `wait` `expect_text` `expect_visible` `expect_focused` `screenshot`。
`wait` 吃 `selector`，或改吃 `{"action":"wait","ms":800}` 單純停一段時間——動畫沒有對應的 selector 可等時用這個。
預設第一個失敗就停（`--keep-going` 可跑完全部）。console error/warning 與失敗的 request 一律蒐集，這類問題不會反映在樣式比對裡。
`--headed` 可開有頭瀏覽器讓人工旁觀。

### --video：錄整段互動

截圖看不出 transition、loading、hover 這類會動的東西，用錄影：

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow \
  --steps steps.json --out parity-flow.md --video parity-flow.mp4
```

- 副檔名 `.mp4` 會在錄完後用 ffmpeg 轉成 H.264（QuickTime 直接開），中間的 webm 自動刪除。**ffmpeg 不存在時不會失敗**，保留 `.webm` 並在 stdout 提示。副檔名寫 `.webm` 則完全不轉檔。
- `--video-size WxH`（預設 `1280x720`）同時決定錄影解析度與 viewport。
- `--video-tail <秒>`（預設 `1`）最後一步跑完後繼續錄的時間，避免收尾的動畫被切掉。
- 影格率是 Playwright screencast 的 **25fps**，判斷「動畫有沒有做對」夠用；要判斷「掉不掉幀」不夠，那種要用 macOS `screencapture -v` 錄 60fps。
- 影片路徑會同時寫進報告開頭與 stdout。

把 flow 的 JSON 存進專案（例如 `e2e/`），下次改版直接重跑，就是回歸測試。

## 維護

改過 `parity.py` 後跑 `python scripts/parity.py selftest`，會用內建 fixture 驗證配對與 diff 邏輯仍正確。
