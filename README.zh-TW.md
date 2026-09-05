# agent-orch（繁體中文摘要）

> 這是英文 [README](README.md) 的摘要版，只放定位、來由、證據與邊界。完整說明、架構圖、生命週期與操作手冊請看英文版與 [`docs/`](docs/)。

[![CI](https://github.com/Roger-Sung/agent-orch/actions/workflows/ci.yml/badge.svg)](https://github.com/Roger-Sung/agent-orch/actions/workflows/ci.yml)

## 這是什麼

一個有狀態的 agent 任務派工服務：SQLite 狀態機、單一寫入者的常駐 daemon、每個迴圈都有上限、跨供應商的 stop gate，以及每個階段執行後都會封存的證據鏈。

它是為了讓 Claude／Codex 的長流程可以**無人看管地跑完**，而且事後每一次重試、每一個副作用都查得到。公開目的是給人讀，不是給人用——見英文版的 Project status。引擎沒有第三方 Python 依賴，demo 不需要任何設定。

## 為什麼是服務，不是一個迴圈

一個反覆呼叫 agent 的 shell 迴圈，在中途失敗之前都很好用。失敗之後問題就來了，而迴圈答不出任何一題：當時跑到哪個階段、已經燒了幾次嘗試、產出有沒有被審過、現在能不能安全地續跑還是會重做一個副作用已經落地的步驟。

這些答案必須住在只有一個寫入者的持久狀態裡。四個性質由此而來：**型別化的結果**（階段只能印出一行 `ORCHESTRATOR_OUTCOME`，狀態機從不猜 agent 的意思）、**每個迴圈都有上限**（兩個意見不合的 agent 只能來回有限次，然後停下來等人）、**回收而非遺棄**（daemon 中途死掉，重啟時會找到仍標記執行中的 run 並隔離）、**證據封存**（每次執行都有 manifest：log hash、輸出 hash、結果、模型、token 用量、lease token）。

## 它怎麼來的

起點是不想守在 agent 旁邊接力：啟動、走開、回來驗收一個可以查證的結果。這裡每一個機制，都是這個承諾在某個具體情境下破掉時加上去的。無法安全續跑的執行，變成單一寫入者的持久狀態；同一家模型的審查只是在確認執行者的假設而不是在檢驗它，變成跨供應商的 gate；一個階段無視自己的工作區、改寫了機器上另一處的正式資料並回報成功——由人讀結果時發現——變成 L1 預防與 L2 偵測。這個系統的形狀是出過什麼事的紀錄，不是預先畫好的設計。

## 證據

- 426 個引擎測試與 13 個去識別化掃描器測試，CI 在 Linux 與 macOS 上執行；引擎測試包含圍堵層的驗收測試，需要 macOS `sandbox-exec` 的 L1 測試在沒有它的主機上會跳過。
- 每個已提交的階段執行都留下封存的 manifest（daemon 中途死掉的執行只會標記 blocked 並補 log，不封存）；`python3 -m orchestrator containment-inspect TASK_ID` 以唯讀連線重新驗證保留的證據。
- CI 只跑部分去識別化掃描——嚴格規則需要的站點字串刻意不進 repo。綠色 badge 代表測試通過且 repo 端規則沒有發現問題；嚴格掃描是發佈者的責任，見 [`docs/operating.md`](docs/operating.md)。

## 有做的與沒做的

**有做**：狀態機、單一寫入者 daemon、型別化結果、各種上限、lease 回收、封存 manifest、跨供應商 gate、git 出口封鎖、L1 寫入預防（macOS）、L2 寫入偵測、假 agent demo、去識別化掃描器與 fail-closed 的 pre-commit hook。

**沒做，且在程式與文件裡明說**：L3 隔離（階段仍能讀取使用者讀得到的任何東西）；強制跨供應商 reviewer 的檢查（目前只信任兩個 owner 槽位確實來自不同家）；通用的 CLI adapter；供應商能力探測；Windows。

圍堵層的邊界與未解問題寫在 [`docs/threat-model.md`](docs/threat-model.md)。

## 三十秒 demo

```sh
python3 -m orchestrator.demo                        # 合成的端對端執行
python3 -m unittest discover -s orchestrator/tests  # 引擎測試
python3 -m unittest discover -s tools/tests         # 掃描器測試
```

demo 用一個故意跟自己意見不合的假 agent 跑一個合成任務，讓 review 邊的上限被觸發：任務停在 `waiting_user`，每次執行都封存，歷史完整保留。不呼叫任何供應商 CLI，沒有東西離開這台機器。

## 授權與定位

保留所有權利，僅供閱讀與作品集評估；詳見 [LICENSE](LICENSE) 與英文版 Project status。不是開源，不接受 issue 與 pull request，不承諾維護。
