"""
Streamlit版 3-2-1投票アプリ（ID方式・翻訳に強い／候補編集・同義統合・CSV共有）
--------------------------------------------------------------------------------
■ 特長
- 内部IDで投票を保存（ラベルは表示専用）→ ブラウザ翻訳で表示が変わってもデータは壊れない
- 既存CSVの自動マイグレーション：
  - candidates.csv が name,active でも id,label,active へ変換
  - votes.csv が first,second,third（ラベル）でも first_id,second_id,third_id へ変換
- 管理画面：追加／名称変更／有効・無効、簡易同義語統合（重複IDの票を付替え）
- 投票・集計は常に最新ラベルを参照

起動:
  pip install streamlit pandas
  streamlit run app.py
  → 投票:  http://localhost:8501/?page=vote
  → 集計:  http://localhost:8501/?page=admin
"""

from __future__ import annotations
import os, re, unicodedata, uuid
from datetime import datetime
from typing import Dict
import pandas as pd
import streamlit as st

st.set_page_config(page_title="3-2-1 投票アプリ", layout="centered")

# --- 自動翻訳の提案を抑止（効果は限定的。データはID方式で保護） ---
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

# --- 同義語/同音語マップ（全文一致のみ／必要に応じて拡張） ---
ALIAS_MAP = {
    "ﾊﾟｯｹｰｼﾞ": "パッケージ",
    "パッケージング": "パッケージ",
    "パケ": "パッケージ",
    "包装": "パッケージ",
}

def normalize_for_merge(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKC", name.strip())
    # ひらがな→カタカナ
    def hira_to_kata(ch: str) -> str:
        o = ord(ch)
        return chr(o + 0x60) if 0x3041 <= o <= 0x3096 else ch
    s = "".join(hira_to_kata(c) for c in s)
    s = re.sub(r"[\s,、。・~〜\-_\/]+", "", s)
    if s in ALIAS_MAP:
        s = ALIAS_MAP[s]
    return s

# --- ファイルパス ---
CANDS_FILE = "candidates.csv"   # id,label,active
VOTES_FILE = "votes.csv"        # first_id,second_id,third_id,time

# --- 初期候補（初回のみ） ---
DEFAULT_CANDIDATES = ["候補A", "候補B", "候補C", "候補D"]

# ============================
# データI/O & マイグレーション
# ============================

def ensure_candidates_schema() -> pd.DataFrame:
    if os.path.exists(CANDS_FILE):
        df = pd.read_csv(CANDS_FILE)
        # 新式
        if set(df.columns) >= {"id","label","active"}:
            df["active"] = df["active"].astype(bool)
            return df[["id","label","active"]]
        # 旧式 name,active → 変換
        if set(df.columns) >= {"name"}:
            df = df.rename(columns={"name":"label"})
            df["active"] = df.get("active", True)
            df["id"] = [uuid.uuid4().hex[:8] for _ in range(len(df))]
            df = df[["id","label","active"]]
            df.to_csv(CANDS_FILE, index=False)
            return df
        raise ValueError("candidates.csv の列構成が不正です（id,label,active or name,active を想定）")
    # 初回生成
    df = pd.DataFrame({
        "id": [uuid.uuid4().hex[:8] for _ in DEFAULT_CANDIDATES],
        "label": DEFAULT_CANDIDATES,
        "active": [True]*len(DEFAULT_CANDIDATES)
    })
    df.to_csv(CANDS_FILE, index=False)
    return df


def ensure_votes_schema(cands: pd.DataFrame) -> pd.DataFrame:
    if os.path.exists(VOTES_FILE):
        df = pd.read_csv(VOTES_FILE)
        # 既に *_id 列があればそれを使う
        if set(df.columns) >= {"first_id","second_id","third_id"}:
            return df[["first_id","second_id","third_id","time"]] if "time" in df.columns else df.assign(time="")
        # 旧: first,second,third（ラベル） → id へマップ
        label_to_id: Dict[str, str] = {r.label: r.id for r in cands.itertuples()}
        if set(df.columns) >= {"first","second","third"}:
            def map_label(s):
                return label_to_id.get(s, None)
            conv = pd.DataFrame({
                "first_id": df["first"].map(map_label),
                "second_id": df["second"].map(map_label),
                "third_id": df["third"].map(map_label),
                "time": df.get("time", "")
            })
            conv.to_csv(VOTES_FILE, index=False)
            return conv
        # 列が不正 → 空データで開始
    return pd.DataFrame(columns=["first_id","second_id","third_id","time"])


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

def append_vote(first_id: str, second_id: str, third_id: str):
    votes = load_votes()
    new_row = {"first_id": first_id, "second_id": second_id, "third_id": third_id, "time": datetime.now().isoformat()}
    votes = pd.concat([votes, pd.DataFrame([new_row])], ignore_index=True)
    votes.to_csv(VOTES_FILE, index=False)

# ============================
# 集計（ID→表示ラベルで出力）
# ============================

def aggregate(cands: pd.DataFrame, votes: pd.DataFrame, include_inactive: bool = True) -> pd.DataFrame:
    id_to_label = {r.id: r.label for r in cands.itertuples()}
    active_ids = set(cands[cands["active"]]["id"]) if not include_inactive else set(cands["id"])

    # 票をIDで数える
    stats: Dict[str, Dict[str,int]] = {cid: {"points":0, "first":0, "second":0, "third":0} for cid in active_ids}
    for _, row in votes.iterrows():
        f, s, t = row.get("first_id"), row.get("second_id"), row.get("third_id")
        if f in stats: stats[f]["points"] += 3; stats[f]["first"] += 1
        if s in stats: stats[s]["points"] += 2; stats[s]["second"] += 1
        if t in stats: stats[t]["points"] += 1; stats[t]["third"] += 1

    # 表示はラベル
    rows = []
    for cid, v in stats.items():
        rows.append({"候補": id_to_label.get(cid, f"[{cid}]"), **v})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["候補","points","first","second","third"])
    df = df.sort_values(["points","first","second","third","候補"], ascending=[False,False,False,False,True]).reset_index(drop=True)
    df.index = range(1, len(df)+1)
    return df

# ============================
# 画面切替
# ============================
params = st.query_params
page = params.get("page", "vote")

# ---------------- 投票ページ（ID方式） ----------------
if page == "vote":
    st.header("投票フォーム (1位=3点, 2位=2点, 3位=1点)")
    cands = load_candidates()
    votes = load_votes()  # 読むだけ

    # アクティブ候補（IDとラベル）
    active = cands[cands["active"]].reset_index(drop=True)
    if active.empty:
        st.info("現在、投票可能な候補がありません。管理ページで候補を有効化してください。")
    id_list = active["id"].tolist()
    id_to_label = {r.id: r.label for r in active.itertuples()}

    # シグネチャで選択値をリセット（ラベルが翻訳されてもIDは不変）
    sig = "|".join(id_list)
    if st.session_state.get("_id_sig") != sig:
        for key in ("first_sel","second_sel","third_sel"):
            st.session_state.pop(key, None)
        st.session_state["_id_sig"] = sig

    # selectbox: options=ID、format_func=ラベル → 保存はID
    def fmt(cid: str) -> str:
        return id_to_label.get(cid, "")

    first_id = st.selectbox("1位 (3点)", options=[None]+id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="first_sel")
    second_id = st.selectbox("2位 (2点)", options=[None]+id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="second_sel")
    third_id = st.selectbox("3位 (1点)", options=[None]+id_list, format_func=lambda x: "(未選択)" if x is None else fmt(x), key="third_sel")

    if st.button("投票を送信", type="primary"):
        if None in (first_id, second_id, third_id):
            st.error("1〜3位をすべて選んでください")
        elif len({first_id, second_id, third_id}) < 3:
            st.error("同じ候補は重複して選べません")
        else:
            append_vote(first_id, second_id, third_id)
            st.success("投票を記録しました！")

    with st.expander("候補一覧（投票対象）"):
        st.write(active[["label"]])

# ---------------- 集計/管理ページ（ID方式） ----------------
elif page == "admin":
    st.header("集計結果 & 候補管理（ID方式）")

    cands = load_candidates()
    votes = load_votes()

    include_inactive = st.checkbox("非表示候補も集計表に含める", value=True)
    res_df = aggregate(cands, votes, include_inactive=include_inactive)

    st.subheader("順位表")
    if votes.empty:
        st.info("まだ投票はありません")
    st.dataframe(res_df, use_container_width=True)
    csv = res_df.to_csv(index=True)
    st.download_button("CSVダウンロード", data=csv, file_name="result.csv", mime="text/csv")

    st.divider()
    st.subheader("候補の編集")

    # 追加（IDを発行）
    col_add1, col_add2 = st.columns([3,1])
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
                    row = pd.DataFrame([[uuid.uuid4().hex[:8], label_s, True]], columns=["id","label","active"])
                    cands = pd.concat([cands, row], ignore_index=True)
                    save_candidates(cands)
                    st.success(f"候補『{label_s}』を追加しました")
                else:
                    # 既存候補をこのラベルに統一: 先頭を基準にし、他は削除。票のIDは基準IDへ付替え。
                    base = conflict.iloc[0]
                    base_id = base["id"]
                    cands.loc[cands["id"] == base_id, ["label","active"]] = [label_s, True]
                    # 余剰候補を削除
                    for _, r in conflict.iloc[1:].iterrows():
                        dup_id = r["id"]
                        # 票のID置換
                        if not votes.empty:
                            for col in ["first_id","second_id","third_id"]:
                                votes[col] = votes[col].replace(dup_id, base_id)
                        cands = cands[cands["id"] != dup_id]
                    save_candidates(cands)
                    if not votes.empty:
                        votes.to_csv(VOTES_FILE, index=False)
                    st.success(f"既存の同義候補を『{label_s}』に統一しました")
                st.rerun()

    st.caption("※ 名称変更・追加時は同義/同音候補を自動統合（票はIDを付替え）。")

    # 編集（名称変更 / 有効・無効）
    for idx, row in cands.reset_index(drop=True).iterrows():
        col1, col2, col3, col4 = st.columns([4,2,2,2])
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
                    # 同義語統合：同じ正規化キーの他IDがあれば、そのIDの票を現在のIDへ寄せて、相手候補を削除
                    key_new = normalize_for_merge(label_s)
                    tmp = cands.copy(); tmp["_key"] = tmp["label"].apply(normalize_for_merge)
                    conflict = tmp[(tmp["_key"] == key_new) & (tmp["id"] != cid)]

                    # ラベル更新
                    cands.loc[cands["id"] == cid, ["label","active"]] = [label_s, active]

                    # 競合の統合
                    for _, r in conflict.iterrows():
                        dup_id = r["id"]
                        if not votes.empty:
                            for col in ["first_id","second_id","third_id"]:
                                votes[col] = votes[col].replace(dup_id, cid)
                        cands = cands[cands["id"] != dup_id]

                    save_candidates(cands.drop(columns=[c for c in cands.columns if c == "_key"]))
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

# -------------- フォールバック --------------
else:
    st.info("""以下のURLを利用してください:
- 投票: http://localhost:8501/?page=vote
- 集計: http://localhost:8501/?page=admin""")