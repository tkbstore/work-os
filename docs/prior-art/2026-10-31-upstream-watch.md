# prior-art: 上流のリリースを監査し、検査の候補を PR で出す仕組み

2026-10-31 / work-os

作ろうとしたもの: 上流リポ（Anthropic / OpenAI / Google など）のリリース・メジャー更新を
検知し、固定した SHA で公開ゲートを当て直し、前回との差分を取り、上流が新しく足した実践を
**検査の候補として PR で出す**（マージは人。候補を自動で block / warn にはしない）。

## 検索語

抽象語（上流監視・再監査）から具体名へ落とした: `releases/latest`, `tags`, `scorecard`,
`nvchecker`, `upstream_watch`, `pinned_sha`, `frozen_sha`, `calibrate`, `fleet`, `日次観測`, `再校正`。

## 方向A: ローカル

- 同じことをしているものは無い。`catalog.py --find` は `catalog.toml` が未生成で引けなかった
  （1dcf4cc で catalog はコミットしない局所ファイルになった）。grep で補った。
- `engine/calibrate.py` は既定ブランチの HEAD を `--depth 1` で clone し（91-92行）、
  キャッシュがあれば中身を確かめずに再利用する（86行）。結果には日付しか残らず、**SHA を記録しない**。
  したがって「前回との差分」は今の部品では取れない。
- 退役した `engine/fleet.py` に `--snapshot` / `--diff`（前回より悪くなったものだけを出す）が
  あった。差分の取り方の先例だが、対象は自分のリポで、上流ではない。

## 方向B: 外部

| 候補 | 合うところ | 採らない／一部だけ使う理由 |
|---|---|---|
| OpenSSF Scorecard（Apache-2.0、v5.6.0） | `--repo --commit` / `--local` でローカル実行できる。20 の検査 | 物差しが別物になる。校正は本番と同じ `release_gate.evaluate()` を当てることに意味がある（`calibration/README.md`）。Go の外部依存とトークンも要る。検査の候補を考えるときに、どの実践を指すかの語彙として参照するに留める |
| Scorecard REST API | 週次の公開スキャン | 最新の1回分だけで、コミットを指定できない。`anthropics/anthropic-sdk-python` は 404。BigQuery のデータセットは提供終了 |
| deps.dev API | パッケージの版と公開日 | 鍵がパッケージで、リポではない。版ごとの scorecard は無い |
| nvchecker（MIT） | ローカルで動く版の監視。old/new を持つ | 新しい依存になる。`gh api` を ETag 付きで叩けば 304 が返り、レート制限も減らない。標準ライブラリ＋`gh` で足りる |
| Renovate（AGPL-3.0）/ Anitya（GPL-2.0） | 版の監視 | ライセンスと重さ |
| ossf/allstar、step-security/secure-repo | 方針の強制 | 固定の方針を当てるだけで、上流から候補を作らない |

上流の変化から検査の候補を提案するものは見つからなかった（`gh search repos` で
"scorecard diff" / "repository security posture drift" / "infer security checks from repositories" は 0 件）。

## 方向C: 過去の検討痕跡

- 上流の監視・候補の自動提案を検討した記録は無い。Scorecard を検討した記録も無い。
- 日次観測は 2026-10-13 に退役した。理由は「日々の動きの最適化はこのリポジトリの範囲外」で、
  上流の監視を否定したものではない。
- ただし日次観測は **一度も予定どおりに動かなかった**。launchd から `~/Documents` を読むことを
  macOS の TCC が止めており、記録された9回はすべて手動だった。定期実行を launchd に頼らない。
- 統治の規則: 「3回観測されていない問題にルールを作らない」（憲法 §4 の3社ルール）。
  上流1社の変化は検査の根拠にならない。候補で止めることはこれと整合する。

## 判定

**Compose + Build**
1. 検知は `gh api repos/{o}/{r}/releases/latest` とタグのコミット（新しい依存を入れない）。
   実装（`engine/upstream.py`）では ETag を使っていない。上流7つで1回14件程度の問い合わせで、
   レート制限（5000/時）に対して小さいため。上流を大きく増やしたら ETag を足す。
2. 再監査は自前のゲート（`calibrate.py`）を固定した SHA に当てる。**最初の作業は SHA の固定と記録**。
   これが無いと 2 と 3 の差分が取れない。
3. 差分から候補を作る部分は Build。候補は PR として出し、検査に上げるかは
   コーパス全体での分離（`[separation] min_gap`）と3社ルールで人が決める。

## 判定を覆す条件

Scorecard がコミット指定で全検査を再現できると確かめられ、かつ自前のゲートの物差しと揃えられる
見通しが立ったら、2 を Scorecard との併用（Extend）に見直す。
