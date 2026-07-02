# FIFA 2026 賽程自動更新（GitHub + Vercel 版，不需要 Railway）

這個版本只需要兩個服務：**GitHub**（存程式碼＋資料）跟 **Vercel**（跑排程＋放網頁），
比原本多一個 Railway 的版本更精簡。

## 架構說明

```
Vercel Cron Job（每天 UTC 01:00、05:00 各跑一次 = 台灣時間 上午9點／下午1點）
   → 呼叫 /api/scrape 這個 Serverless Function
   → 抓維基百科 3 個頁面、解析賽程、換算成台灣時間
   → 用 GitHub API 把結果寫進同一個 repo 的 matches.json
                ↓
   index.html（也部署在 Vercel，同一個 repo）
   → 打開頁面時 fetch：
     https://raw.githubusercontent.com/eli-101/FIFASchedule2026/main/matches.json
   → 抓不到的話自動退回內建的備用賽程，不會壞掉
```

## 這個資料夾裡的檔案

```
FIFASchedule2026/
├── index.html          ← 你的月曆網頁（Vercel 會自動部署這個當首頁）
├── api/
│   └── scrape.py       ← Vercel Serverless Function，被 Cron 定時呼叫
├── vercel.json          ← 定義 Cron 排程時間
└── requirements.txt     ← scrape.py 需要的 Python 套件（requests）
```

## 部署步驟

### 1. 把這四個東西上傳到你的 GitHub repo

`https://github.com/eli-101/FIFASchedule2026`

直接在 repo 頁面用 "Add file → Upload files"，把整個資料夾結構拖上去即可
（記得要保留 `api/scrape.py` 這個路徑,不要把 `scrape.py` 跟 `index.html` 放在同一層）。

### 2. 到 Vercel 連接這個 repo

1. [vercel.com](https://vercel.com) → 你應該已經登入且連了 GitHub
2. New Project → 選 `eli-101/FIFASchedule2026`
3. Framework Preset 選 **Other**（因為這不是 Next.js/React 專案，只是靜態網頁＋一個 API function）
4. 先不要按 Deploy，往下滑到 **Environment Variables**，新增：

   | 名稱 | 值 |
   |---|---|
   | `GITHUB_TOKEN` | 你的 GitHub Personal Access Token（步驟見下方） |
   | `GITHUB_REPO` | `eli-101/FIFASchedule2026` |
   | `CRON_SECRET` | 自己隨便設一串亂碼，例如 `a8f3k2m9x` |

5. 按 **Deploy**

### 3. 建立 GitHub Token

1. GitHub 右上角頭像 → Settings → 左下 "Developer settings"
2. Personal access tokens → Fine-grained tokens → Generate new token
3. Repository access 選 `FIFASchedule2026`
4. Permissions → Contents → 設成 **Read and write**
5. Generate 後複製那串 token，貼到上面 Vercel 的 `GITHUB_TOKEN` 變數

### 4. 確認 Cron 有設定成功

部署完成後，到 Vercel 專案的 **Settings → Cron Jobs**，應該會看到：
- Path: `/api/scrape`
- Schedule: `0 1,5 * * *`（台灣時間每天上午 9:00、下午 1:00）

### 5. 手動測試一次

不用等到排程時間，可以直接在瀏覽器打開這個網址手動觸發一次：

```
https://你的專案.vercel.app/api/scrape?
```

因為你設定了 `CRON_SECRET`，直接用瀏覽器打開會被擋下來（回應 401 unauthorized）——
這是正常的，代表安全機制生效了。Vercel 的 Cron 本身呼叫時會自動帶正確的授權標頭，不用你手動處理。

如果想在部署前用瀏覽器手動測試，可以先不設定 `CRON_SECRET` 這個環境變數，測完之後再補上去。

成功的話會回傳類似：
```json
{"status": "ok", "count": 28}
```

再去看 GitHub repo 裡有沒有多一個 `matches.json` 檔案，內容是不是 28 場比賽的清單。

## 時區換算對照表（給你之後想改時間用）

Vercel 的 cron 排程也是用 **UTC 時間**：

| 想要的台灣時間 | UTC | cron 小時欄位 |
|---|---|---|
| 上午 9:00 | 前一天 01:00 | 1 |
| 下午 1:00 | 05:00 | 5 |

## 已知限制

- 32強賽的解析邏輯是照維基百科的標準賽事樣板（`{{#invoke:football box|main}}`）寫的，
  跟 16強～冠軍賽用的是同一套邏輯，已經拿真實資料測試過 16強～季軍賽部分；
  32強賽因為在獨立頁面，理論上結構相同但沒有機會實際跑過，第一次執行後記得檢查一下
  `matches.json` 裡 32強的資料是否正確。
- Vercel Hobby（免費）方案的 Serverless Function 預設執行時間上限是 10 秒。這個腳本要連續
  呼叫 3 次維基百科 API 再呼叫 GitHub API，如果網路比較慢可能會超時。如果常常失敗，
  可以在 Vercel 專案設定裡把 Function 的 `maxDuration` 調高（付費方案可以到 60 秒以上）。
- 冠軍賽／季軍賽的對戰隊伍要等準決賽打完才知道，之前會顯示 `M97勝隊` 這種暫時的佔位文字，
  之後排程執行時會自動變成正確隊名。
