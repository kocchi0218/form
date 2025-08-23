"""
Streamlit版 3-2-1投票アプリ（ID方式・翻訳抑止・候補編集/同義統合・氏名/社員番号・サンクス・投票一覧）
--------------------------------------------------------------------------------
■ 機能
- 投票：1位=3点 / 2位=2点 / 3位=1点（重複不可）、氏名・社員番号の入力付き
- サンクス画面：送信後に「送信しました」に自動遷移
- 集計：総得点・1/2/3位回数・順位表の表示とCSVダウンロード
- 投票一覧：氏名・社員番号つきの生票一覧表示とCSVダウンロード
- 管理：候補の追加／名称変更／有効/無効切替、同義統合（重複候補の票も安全に付替え）
- 翻訳抑止：Google翻訳の自動提案を抑止（完全ではないが軽減）

■ 起動
  pip install streamlit pandas
  streamlit run app.py
  → 投票:  http://localhost:8501/?page=vote
  → 集計:  http://localhost:8501/?page=admin
  → サンクス: http://localhost:8501/?page=thanks
"""

from __future__ import annotations
import os, re, unicodedata, uuid
from datetime import datetime
from typing import Dict
import pandas as pd
import streamlit as st

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
    """同一視するための正規化キーを作る（NFKC、ひら→カナ、記号・空白除去、別名吸収）"""
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
        df = pd.read_csv(VOTES_FILE)
        # 既に *_id であればそのまま（不足列は追加）
        if set(df.columns) >= {"first_id", "second_id", "third_id"}:
            if "voter_name" not in df.columns: df["voter_name"] = ""
            if "employee_id" not in df.columns: df["employee_id"] = ""
            if "time" not in df.columns: df["time"] = ""
            df = df[["voter_name", "employee_id", "first_id", "second_id", "third_id", "time"]]
            df.to_csv(VOTES_FILE, index=False)
            return df

        # 旧: first/second/third（ラベル名）→ *_id に変換
        if set(df.columns) >= {"first", "second", "third"}:
            label_to_id: Dict[str, str] = {r.label: r.id for r in cands.itertuples()}
            def map_label(s): return label_to_id.get(s, None)
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
        "voter_name": voter_name,
        "employee_id": employee_id,
        "first_id": first_id,
        "second_id": second_id,
        "third_id": third_id,
        "time": datetime.now().isoformat(),
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
    df.index = range(1, len(df) + 1)
    return df

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

    # 氏名・社員番号
    voter_name = st.text_input("お名前（氏名）", placeholder="例：山田 太郎")
    employee_id = st.text_input("社員番号", placeholder="例：A12345")

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

    cands = load_candidates()
    votes = load_votes()

    include_inactive = st.checkbox("非表示候補も集計表に含める", value=True)
    res_df = aggregate(cands, votes, include_inactive=include_inactive)

    # 順位表
    st.subheader("順位表")
    if votes.empty:
        st.info("まだ投票はありません")
    st.dataframe(res_df, use_container_width=True)
    csv = res_df.to_csv(index=True)
    st.download_button("CSVダウンロード", data=csv, file_name="result.csv", mime="text/csv")

    # 投票一覧（氏名・社員番号つき）
    st.subheader("投票一覧（氏名・社員番号つき）")
    if votes.empty:
        st.info("まだ投票はありません")
    else:
        id_to_label = {r.id: r.label for r in cands.itertuples()}
        detail_df = votes.copy()
        detail_df["1位"] = detail_df["first_id"].map(id_to_label)
        detail_df["2位"] = detail_df["second_id"].map(id_to_label)
        detail_df["3位"] = detail_df["third_id"].map(id_to_label)
        show_cols = ["voter_name", "employee_id", "1位", "2位", "3位", "time"]
        show_cols = [c for c in show_cols if c in detail_df.columns]
        st.dataframe(detail_df[show_cols], use_container_width=True)
        csv_detail = detail_df[show_cols].to_csv(index=False)
        st.download_button("投票一覧CSVをダウンロード（氏名・社員番号付き）",
                           data=csv_detail, file_name="votes_detail.csv", mime="text/csv")

    st.divider()

    # 候補の編集（追加 / 名称変更 / 有効・無効切替 / 同義統合）
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
                    for _, r in conflict.iloc[1:].iterrows():
                        dup_id = r["id"]
                        if not votes.empty:
                            for col in ["first_id", "second_id", "third_id"]:
                                votes[col] = votes[col].replace(dup_id, base_id)
                        cands = cands[cands["id"] != dup_id]
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
                    for _, r in conflict.iterrows():
                        dup_id = r["id"]
                        if not votes.empty:
                            for col in ["first_id", "second_id", "third_id"]:
                                votes[col] = votes[col].replace(dup_id, cid)
                        cands = cands[cands["id"] != dup_id]

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
    st.markdown("[🗳️ もう一度投票する](?page=vote) | [📊 集計を見る](?page=admin)")

# ---------------- フォールバック ----------------
else:
    st.info("""以下のURLを利用してください:
- 投票: ?page=vote
- 集計: ?page=admin""")


