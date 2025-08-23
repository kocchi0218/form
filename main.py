# app.py
"""
Streamlit版 3-2-1投票アプリ（ID方式・翻訳抑止・候補編集/同義統合・氏名/社員番号・サンクス・投票一覧・順位/グラフ・自動更新）
--------------------------------------------------------------------------------
■ 機能
- 投票：1位=3点 / 2位=2点 / 3位=1点（重複不可）、氏名・社員番号の入力付き
- サンクス画面：送信後に「送信しました」に自動遷移
- 集計：総得点・1/2/3位回数・順位（1始まり）を表示、CSVダウンロード
- グラフ：合計ポイントの棒グラフ、1/2/3位回数の積み上げ棒グラフ
- 投票一覧：氏名・社員番号つきの生票一覧（ラベル表示）CSV/Excelダウンロード
- 管理：候補の追加／名称変更／有効/無効切替、同義統合（重複候補の票も安全に付替え）
- 翻訳抑止：Google翻訳の自動提案を抑止（完全ではないが軽減）
- 先頭ゼロ保持：社員番号は常に文字列扱い。画面表示で任意桁のゼロ埋め可。Excelは文字列書式で出力。
- 自動更新：管理（集計）ページのみ、一定間隔で自動再読み込み

■ 起動
  pip install streamlit pandas altair xlsxwriter streamlit-autorefresh
  streamlit run app.py
  → 投票:  http://localhost:8501/?page=vote
  → 集計:  http://localhost:8501/?page=admin
  → サンクス: http://localhost:8501/?page=thanks
"""

from __future__ import annotations
import os, re, unicodedata, uuid, csv
from io import BytesIO
from datetime import datetime
from typing import Dict
import pandas as pd
import streamlit as st
import altair as alt

# （あれば使う）自動更新ライブラリ
try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:
    _HAS_AUTOREFRESH = False

st.set_page_config(page_title="3-2-1 投票アプリ", layout="centered")

# -----------------------------
# 翻訳抑止（提案の抑止・効果は限定的）
# -----------------------------
def disable_auto_translate():
    st.markdown(
        """
        <meta name="google" content="notranslate" />
        <meta http-equiv="Content-Language" content="ja" />
        <script>
        (function(){
          var html = document.documentElement;
          html.setAttribute('lang','ja');
          html.setAttribute('translate','no');
          html.classList.add('notranslate');
          var body = document.body;
          if (body){
            body.setAttribute('translate','no');
            body.classList.add('notranslate');
          }
        })();
        </script>
        """,
        unsafe_allow_html=True,
    )
disable_auto_translate()

# -----------------------------
# 同義語/同音語マップ（必要に応じて拡張）
# -----------------------------
ALIAS_MAP = {
    "ﾊﾟｯｹｰｼﾞ": "パッケージ",
    "パッケージング": "パッケージ",
    "パケ": "パッケージ",
    "包装": "パッケージ",
}

def normalize_for_merge(name: str) -> str:
    """同一視するための正規化キー（NFKC、ひら→カナ、記号・空白除去、別名吸収）"""
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKC", name.strip())

    # ひらがな→カタカナ
    def hira_to_kata(ch: str) -> str:
        o = ord(ch)
        return chr(o + 0x60) if 0x3041 <= o <= 0x3096 else ch
    s = "".join(hira_to_kata(c) for c in s)

    # 記号・空白系を除去
    s = re.sub(r"[\s,、。・~〜\-_\/]+", "", s)

    # 別名テーブル適用（全文一致）
    s = ALIAS_MAP.get(s, s)
    return s

# -----------------------------
# ファイルパス
# -----------------------------
CANDS_FILE = "candidates.csv"   # id,label,active
VOTES_FILE = "votes.csv"        # voter_name,employee_id,first_id,second_id,third_id,time

# 初期候補（初回生成用）
DEFAULT_CANDIDATES = ["候補A", "候補B", "候補C", "候補D"]

# ============================
# データI/O & マイグレーション
# ============================
def ensure_candidates_schema() -> pd.DataFrame:
    """candidates.csv を id,label,active に正規化。旧 name にも対応。"""
    if os.path.exists(CANDS_FILE):
        df = pd.read_csv(CANDS_FILE)
        if set(df.columns) >= {"id", "label", "active"}:
            df["active"] = df["active"].astype(bool)
            return df[["id", "label", "active"]]
        if set(df.columns) >= {"name"}:
            # 旧: name, active → 新: id, label, active
            df = df.rename(columns={"name": "label"})
            df["active"] = df.get("active", True)
            df["id"] = [uuid.uuid4().hex[:8] for _ in range(len(df))]
            df = df[["id", "label", "active"]]
            df.to_csv(CANDS_FILE, index=False)
            return df
    # 初回生成
    df = pd.DataFrame({
        "id": [uuid.uuid4().hex[:8] for _ in DEFAULT_CANDIDATES],
        "label": DEFAULT_CANDIDATES,
        "active": [True] * len(DEFAULT_CANDIDATES),
    })
    df.to_csv(CANDS_FILE, index=False)
    return df

def ensure_votes_schema(cands: pd.DataFrame) -> pd.DataFrame:
    """votes.csv を voter_name, employee_id, *_id, time に正規化。旧 first/second/third（ラベル）にも対応。"""
    if os.path.exists(VOTES_FILE):
        # 重要：すべて文字列として読み込み（社員番号の先頭0保持）、空白は空文字に
        df = pd.read_csv(VOTES_FILE, dtype=str, keep_default_na=False)

        # 既に *_id であればそのまま（不足列は追加）
        if set(df.columns) >= {"first_id", "second_id", "third_id"}:
            for col in ["voter_name", "employee_id", "first_id", "second_id", "third_id", "time"]:
                if col not in df.columns:
                    df[col] = ""
            df = df[["voter_name", "employee_id", "first_id", "second_id", "third_id", "time"]]
            df.to_csv(VOTES_FILE, index=False)
            return df

        # 旧: first/second/third（ラベル名）→ *_id に変換
        if set(df.columns) >= {"first", "second", "third"}:
            label_to_id: Dict[str, str] = {r.label: r.id for r in cands.itertuples()}
            def map_label(s): return label_to_id.get(s, "")
            conv = pd.DataFrame({
                "voter_name": df.get("voter_name", ""),
                "employee_id": df.get("employee_id", ""),
                "first_id": df["first"].map(map_label),
                "second_id": df["second"].map(map_label),
                "third_id": df["third"].map(map_label),
                "time": df.get("time", ""),
            })
            conv.to_csv(VOTES_FILE, index=False)
            return conv

    # 新規（空ファイル）
    return pd.DataFrame(columns=["voter_name", "employee_id", "first_id", "second_id", "third_id", "time"])

def load_candidates() -> pd.DataFrame:
    return ensure_candidates_schema()

def save_candidates(df: pd.DataFrame):
    df = df.copy()
    df["active"] = df["active"].astype(bool)
    df = df.drop_duplicates(subset=["id"]).reset_index(drop=True)
    df.to_csv(CANDS_FILE, index=False)

def load_votes() -> pd.DataFrame:
    cands = ensure_candidates_schema()
    return ensure_votes_schema(cands)

def append_vote(voter_name: str, employee_id: str, first_id: str, second_id: str, third_id: str):
    votes = load_votes()
    new_row = {
        "voter_name": str(voter_name).strip(),
        "employee_id": str(employee_id).strip(),  # 文字列として保持（先頭0を守る）
        "first_id": first_id,
        "second_id": second_id,
        "third_id": third_id,
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    votes = pd.concat([votes, pd.DataFrame([new_row])], ignore_index=True)
    votes.to_csv(VOTES_FILE, index=False)

# ============================
# 集計
# ============================
def aggregate(cands: pd.DataFrame, votes: pd.DataFrame, include_inactive: bool = True) -> pd.DataFrame:
    id_to_label = {r.id: r.label for r in cands.itertuples()}
    active_ids = set(cands[cands["active"]]["id"]) if not include_inactive else set(cands["id"])
    stats: Dict[str, Dict[str, int]] = {cid: {"points": 0, "first": 0, "second": 0, "third": 0} for cid in active_ids}
    for _, row in votes.iterrows():
        f, s, t = row.get("first_id"), row.get("second_id"), row.get("third_id")
        if f in stats: stats[f]["points"] += 3; stats[f]["first"] += 1
        if s in stats: stats[s]["points"] += 2; stats[s]["second"] += 1
        if t in stats: stats[t]["points"] += 1; stats[t]["third"] += 1
    rows = [{"候補": id_to_label.get(cid, f"[{cid}]"), **v} for cid, v in stats.items()]
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["候補", "points", "first", "second", "third"])
    df = df.sort_values(["points", "first", "second", "third", "候補"],
                        ascending=[False, False, False, False, True]).reset_index(drop=True)
    df.index = range(1, len(df) + 1)  # 1始まり → これを順位として使う
    return df

# ============================
# 出力: Excel（特定列を文字列書式に）
# ============================
def to_xlsx_text(df: pd.DataFrame, text_cols=None, sheet_name="Sheet1") -> bytes:
    text_cols = text_cols or []
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
        ws = w.sheets[sheet_name]
        fmt = w.book.add_format({"num_format": "@"})  # 文字列
        for col in text_cols:
            if col in df.columns:
                i = df.columns.get_loc(col)
                ws.set_column(i, i, None, fmt)  # 列を文字列書式に
    return buf.getvalue()

# ============================
# ページ切替
# ============================
params = st.query_params
page = params.get("page", "vote")

# ---------------- 投票ページ ----------------
if page == "vote":
    st.header("投票フォーム (1位=3点, 2位=2点, 3位=1点)")
    cands = load_candidates()
    votes = load_votes()  # 読むだけ

    # 氏名・社員番号（どちらも文字列で保持）
    voter_name = st.text_input("お名前（氏名）", placeholder="例：山田 太郎")
    employee_id = st.text_input("社員番号（先頭0も可）", placeholder="例：001234")

    # アクティブ候補
    active = cands[cands["active"]].reset_index(drop=True)
    if active.empty:
        st.info("現在、投票可能な候補がありません。管理ページで候補を有効化してください。")
    id_list = active["id"].tolist()
    id_to_label = {r.id: r.label for r in active.itertuples()}

    # 候補リストが変わったら選択状態をリセット
    sig = "|".join(id_list)
    if st.session_state.get("_id_sig") != sig:
        for key in ("first_sel", "second_sel", "third_sel"):
            st.session_state.pop(key, None)
        st.session_state["_id_sig"] = sig

    # セレクトボックス（保存はID）
    def fmt(cid: str) -> str: return id_to_label.get(cid, "")
    first_id = st.selectbox("1位 (3点)", [None] + id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="first_sel")
    second_id = st.selectbox("2位 (2点)", [None] + id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="second_sel")
    third_id  = st.selectbox("3位 (1点)", [None] + id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="third_sel")

    if st.button("投票を送信", type="primary"):
        if not voter_name or not employee_id:
            st.error("お名前と社員番号を入力してください")
        elif None in (first_id, second_id, third_id):
            st.error("1〜3位をすべて選んでください")
        elif len({first_id, second_id, third_id}) < 3:
            st.error("同じ候補は重複して選べません")
        else:
            append_vote(voter_name, employee_id, first_id, second_id, third_id)
            st.query_params.update(page="thanks")
            st.rerun()

# ---------------- 集計/管理ページ ----------------
elif page == "admin":
    st.header("集計結果 & 候補管理（ID方式）")

    # --- 自動更新設定（adminページのみ） ---
    col1, col2 = st.columns([1, 2])
    with col1:
        try:
            auto_refresh = st.toggle("自動更新", value=True, help="集計画面を一定間隔で再読み込み")
        except Exception:
            auto_refresh = st.checkbox("自動更新", value=True)
    with col2:
        interval_sec = st.number_input("間隔(秒)", min_value=2, max_value=60, value=5, step=1)

    # votes.csv の最終更新（検知 & 表示）
    def _votes_mtime():
        try:
            return os.path.getmtime(VOTES_FILE)
        except Exception:
            return 0.0
    mtime = _votes_mtime()
    last_mtime = st.session_state.get("_votes_mtime", 0.0)
    if mtime != last_mtime and last_mtime != 0.0:
        try:
            st.toast("新しい票を検知しました（集計を更新）", icon="✅")
        except Exception:
            st.info("新しい票を検知しました（集計を更新）")
    st.session_state["_votes_mtime"] = mtime
    st.caption(f"votes.csv 最終更新: {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S') if mtime else '-'}")

    # 実際のオートリフレッシュ発火
    if auto_refresh:
        if _HAS_AUTOREFRESH:
            st_autorefresh(interval=int(interval_sec * 1000), key="admin_autorefresh")
        else:
            # 依存なしの簡易フォールバック（環境によっては効かない場合あり）
            st.markdown(f"<meta http-equiv='refresh' content='{int(interval_sec)}'>", unsafe_allow_html=True)

    # ---- 以降は従来どおりの集計処理 ----
    cands = load_candidates()
    votes = load_votes()

    include_inactive = st.checkbox("非表示候補も集計表に含める", value=True)
    res_df = aggregate(cands, votes, include_inactive=include_inactive)

    # ── 順位表（順位=1始まりのindexを列に）+ CSV
    st.subheader("順位表")
    if votes.empty or res_df.empty:
        st.info("まだ投票はありません")
        res_df_disp = pd.DataFrame(columns=["順位","候補","合計ポイント","1位回数","2位回数","3位回数"])
    else:
        res_df_disp = (
            res_df.reset_index()
                  .rename(columns={
                      "index": "順位",
                      "points": "合計ポイント",
                      "first": "1位回数",
                      "second": "2位回数",
                      "third": "3位回数",
                  })
        )
        st.dataframe(res_df_disp, use_container_width=True)
    csv = res_df_disp.to_csv(index=False)
    st.download_button("順位表CSVダウンロード", data=csv, file_name="result.csv", mime="text/csv")

    # ── グラフ：合計ポイント（棒）
    st.subheader("合計ポイント（棒グラフ）")
    if not res_df.empty:
        chart_df = (
            res_df.reset_index()
                  .rename(columns={"index": "順位", "points": "合計ポイント"})
        )
        chart = (
            alt.Chart(chart_df)
               .mark_bar()
               .encode(
                   x=alt.X("候補:N", sort='-y', title="候補"),
                   y=alt.Y("合計ポイント:Q", title="合計ポイント"),
                   tooltip=["順位","候補","合計ポイント","first","second","third"]
               )
               .properties(height=320)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.caption("投票が入るとここに合計ポイントのグラフが表示されます。")

    # ── グラフ：1/2/3位回数（積み上げ棒）
    st.subheader("1位・2位・3位 回数（積み上げ棒グラフ）")
    if not res_df.empty:
        counts_df = (
            res_df.reset_index()
                  .rename(columns={
                      "index": "順位",
                      "first": "1位回数",
                      "second": "2位回数",
                      "third": "3位回数",
                  })
        )
        counts_melt = counts_df.melt(
            id_vars=["順位","候補"],
            value_vars=["1位回数","2位回数","3位回数"],
            var_name="区分", value_name="回数"
        )
        chart2 = (
            alt.Chart(counts_melt)
               .mark_bar()
               .encode(
                   x=alt.X("候補:N", sort='-y', title="候補"),
                   y=alt.Y("回数:Q", title="回数"),
                   color=alt.Color("区分:N", title="順位区分"),
                   tooltip=["順位","候補","区分","回数"]
               )
               .properties(height=320)
        )
        st.altair_chart(chart2, use_container_width=True)

    st.divider()

    # ── 投票一覧（氏名・社員番号つき：ラベル表示）
    st.subheader("投票一覧（氏名・社員番号つき）")
    if votes.empty:
        st.info("まだ投票はありません")
    else:
        id_to_label = dict(zip(cands["id"], cands["label"]))
        detail_df = votes.copy()

        # ID → ラベル変換（存在しないIDはそのまま表示）
        for col in ["first_id", "second_id", "third_id"]:
            detail_df[col] = detail_df[col].astype(str)
        detail_df["1位"] = detail_df["first_id"].map(id_to_label).fillna(detail_df["first_id"])
        detail_df["2位"] = detail_df["second_id"].map(id_to_label).fillna(detail_df["second_id"])
        detail_df["3位"] = detail_df["third_id"].map(id_to_label).fillna(detail_df["third_id"])

        # 社員番号は常に文字列として表示＋任意ゼロ埋め
        detail_df["employee_id"] = detail_df["employee_id"].astype(str)
        pad = st.number_input("社員番号の表示桁数（ゼロ埋め・0=変換しない）", min_value=0, max_value=20, value=0, step=1)
        if pad > 0:
            detail_df["employee_id"] = detail_df["employee_id"].str.zfill(int(pad))

        show_cols = ["voter_name", "employee_id", "1位", "2位", "3位", "time"]
        show_cols = [c for c in show_cols if c in detail_df.columns]

        # 画面表示（TextColumnで数値解釈を防止、古い版はfallback）
        try:
            st.dataframe(
                detail_df[show_cols],
                use_container_width=True,
                column_config={"employee_id": st.column_config.TextColumn("社員番号")}
            )
        except Exception:
            st.dataframe(detail_df[show_cols], use_container_width=True)

        # ラベル版CSV（クォート強化）
        csv_detail = detail_df[show_cols].to_csv(index=False, quoting=csv.QUOTE_ALL)
        st.download_button(
            "投票一覧CSVをダウンロード（ラベル版・氏名/社員番号付き）",
            data=csv_detail,
            file_name="votes_labeled.csv",
            mime="text/csv"
        )

        # ラベル版Excel（社員番号を文字列書式で、先頭0完全保持）
        xlsx_bytes = to_xlsx_text(detail_df[show_cols], text_cols=["employee_id"], sheet_name="votes")
        st.download_button(
            "投票一覧Excelをダウンロード（ラベル版・先頭0保持）",
            data=xlsx_bytes,
            file_name="votes_labeled.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    st.divider()

    # ── 候補の編集（追加 / 名称変更 / 有効・無効切替 / 同義統合）
    st.subheader("候補の編集")

    col_add1, col_add2 = st.columns([3, 1])
    with col_add1:
        new_label = st.text_input("新しい候補名", placeholder="例: スキンケア包装")
    with col_add2:
        if st.button("追加"):
            label_s = (new_label or "").strip()
            if not label_s:
                st.warning("候補名を入力してください")
            else:
                key_new = normalize_for_merge(label_s)
                tmp = cands.copy(); tmp["_key"] = tmp["label"].apply(normalize_for_merge)
                conflict = tmp[tmp["_key"] == key_new]

                if conflict.empty:
                    # 新規追加：新しいIDを付与
                    row = pd.DataFrame([[uuid.uuid4().hex[:8], label_s, True]],
                                       columns=["id", "label", "active"])
                    cands = pd.concat([cands, row], ignore_index=True)
                    save_candidates(cands)
                    st.success(f"候補『{label_s}』を追加しました")
                else:
                    # 既存候補に統一（同義統合）
                    base = conflict.iloc[0]
                    base_id = base["id"]
                    cands.loc[cands["id"] == base_id, ["label", "active"]] = [label_s, True]
                    # 余剰候補の票を基準IDへ付替え、候補を削除
                    if not votes.empty:
                        for _, r in conflict.iloc[1:].iterrows():
                            dup_id = r["id"]
                            for col in ["first_id", "second_id", "third_id"]:
                                votes[col] = votes[col].replace(dup_id, base_id)
                    cands = cands[~cands["id"].isin(conflict.iloc[1:]["id"].tolist())]
                    save_candidates(cands)
                    if not votes.empty:
                        votes.to_csv(VOTES_FILE, index=False)
                    st.success(f"既存の同義候補を『{label_s}』に統一しました")
                st.rerun()

    st.caption("※ 名称変更・追加時は同義/同音候補を自動統合（票はIDを付替え）。")

    # 既存候補の編集
    for idx, row in cands.reset_index(drop=True).iterrows():
        col1, col2, col3, col4 = st.columns([4, 2, 2, 2])
        with col1:
            new_label = st.text_input("名称", value=row["label"], key=f"label_{idx}")
        with col2:
            active = st.checkbox("有効", value=bool(row["active"]), key=f"active_{idx}")
        with col3:
            if st.button("保存", key=f"save_{idx}"):
                cid = row["id"]
                label_s = (new_label or "").strip()
                if not label_s:
                    st.warning("名前を空にはできません")
                else:
                    key_new = normalize_for_merge(label_s)
                    tmp = cands.copy(); tmp["_key"] = tmp["label"].apply(normalize_for_merge)
                    conflict = tmp[(tmp["_key"] == key_new) & (tmp["id"] != cid)]

                    # ラベル更新
                    cands.loc[cands["id"] == cid, ["label", "active"]] = [label_s, active]

                    # 競合の統合（票の付替え＋候補削除）
                    if not votes.empty and not conflict.empty:
                        for _, r in conflict.iterrows():
                            dup_id = r["id"]
                            for col in ["first_id", "second_id", "third_id"]:
                                votes[col] = votes[col].replace(dup_id, cid)
                    cands = cands[~cands["id"].isin(conflict["id"].tolist())] if not conflict.empty else cands

                    if "_key" in cands.columns:
                        cands = cands.drop(columns=["_key"])
                    save_candidates(cands)
                    if not votes.empty:
                        votes.to_csv(VOTES_FILE, index=False)
                    st.success("保存しました（同義統合を適用）")
                    st.rerun()
        with col4:
            if st.button("有効/無効切替", key=f"toggle_{idx}"):
                cands.loc[cands["id"] == row["id"], "active"] = not bool(row["active"])
                save_candidates(cands)
                st.rerun()

    st.divider()
    with st.expander("危険: 全票リセット"):
        if st.button("votes.csv を削除（全消去）", type="secondary"):
            if os.path.exists(VOTES_FILE):
                os.remove(VOTES_FILE)
            st.warning("投票データを全消去しました")
            st.rerun()

# ---------------- サンクスページ ----------------
elif page == "thanks":
    st.header("送信しました")
    st.success("ご投票ありがとうございました！")
    st.markdown("[🗳️ もう一度投票する](?page=vote)")

# ---------------- フォールバック ----------------
else:
    st.info("""以下のURLを利用してください:
- 投票: ?page=vote
- 集計: ?page=admin""")




