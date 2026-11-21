# placeholder# app.py — Streamlit web app version of the Decentralized Beauty Contest Game
# NEOMA FinTech & DeFi
#
# Run locally:   pip install -r requirements.txt
#                streamlit run app.py
#
# Same ledger, same rules as commit_client.py / reveal_client.py / merge_consensus_from_api.py:
#   commit = sha256(f"{uni_id}|{number}|{nonce}")
#   consensus: latest reveal per uni_id, matched to latest commit with timestamp <= reveal,
#              accepted if hash matches and 0 <= number <= 100
#   target = 2/3 * mean(accepted numbers); winner(s) = minimum |number - target|

import hashlib
import io
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

API_DEFAULT = "https://script.google.com/macros/s/AKfycbyNZNOE1DYNbd4GbGTISJsGrnJ4PYCuip0yjSw3Lr8KkD6-kadKI9mfpKNfiAHEWb0Osw/exec"
COMMIT_DEADLINE_UTC = datetime(2026, 11, 15, 22, 59, 59, tzinfo=timezone.utc)  # 15 Nov 23:59:59 Paris
REVEAL_OPEN_UTC = datetime(2026, 11, 15, 23, 0, 0, tzinfo=timezone.utc)        # 16 Nov 00:00 Paris


# ---------- helpers ----------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def post(api: str, payload: dict):
    r = requests.post(api, json=payload, timeout=20)
    return r.status_code, r.text


@st.cache_data(ttl=30, show_spinner=False)
def fetch_table(api: str, table: str) -> pd.DataFrame:
    r = requests.get(f"{api}?table={table}", timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype=str).fillna("")
    if "timestamp_utc" in df.columns:
        df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    return df


def run_consensus(commits: pd.DataFrame, reveals: pd.DataFrame):
    """Deterministic merge, identical for every node that runs it."""
    rows = []
    for uid, grp in reveals.sort_values("ts").groupby("uni_id"):
        rev = grp.iloc[-1]                                       # 1) latest reveal
        prior = commits[(commits["uni_id"] == uid) & (commits["ts"] <= rev["ts"])]
        out = {"uni_id": uid, "reveal_ts": rev["timestamp_utc"], "number": rev["number"],
               "nonce": rev["nonce"], "commit_ts": "", "commit": "", "status": "rejected",
               "reason": ""}
        if prior.empty:
            out["reason"] = "no commit before reveal"
            rows.append(out); continue
        com = prior.sort_values("ts").iloc[-1]                   # 2) latest prior commit
        out["commit_ts"], out["commit"] = com["timestamp_utc"], com["commit"]
        try:
            n = int(str(rev["number"]).strip())
        except ValueError:
            out["reason"] = "number not an integer"; rows.append(out); continue
        if not 0 <= n <= 100:
            out["reason"] = "number out of range"
        elif sha256(f"{uid}|{n}|{rev['nonce']}") != com["commit"]:
            out["reason"] = "hash mismatch"
        else:
            out["status"], out["reason"], out["number"] = "accepted", "", n   # 3) accept
        rows.append(out)

    board = pd.DataFrame(rows)
    ok = board[board["status"] == "accepted"].copy() if not board.empty else board
    summary = {"accepted": len(ok), "rejected": len(board) - len(ok)}
    if len(ok):
        ok["number"] = ok["number"].astype(int)
        avg = ok["number"].mean()
        target = 2 / 3 * avg
        board["distance"] = board.apply(
            lambda r: abs(int(r["number"]) - target) if r["status"] == "accepted" else None, axis=1)
        best = board["distance"].min()
        board["winner"] = board["distance"] == best
        board = board.sort_values(["status", "distance"], ascending=[True, True], na_position="last")
        summary.update(average=round(float(avg), 4), target=round(float(target), 4),
                       winners=board.loc[board["winner"], "uni_id"].tolist())
    return board, summary


# ---------- UI ----------
st.set_page_config(page_title="Beauty Contest — Commit / Reveal", page_icon="🔐", layout="wide")
st.title("🔐 Decentralized Beauty Contest")
st.caption("Guess 0–100. Closest to ⅔ of the class average wins. "
           "Commit–reveal on an append-only public ledger.")

with st.sidebar:
    api = st.text_input("API URL (from info.txt)", API_DEFAULT)
    st.write(f"**Now (UTC):** {now_utc():%Y-%m-%d %H:%M:%S}")
    st.write("**Commit deadline:** 15 Nov 2026, 23:59:59 Paris")
    commit_open = now_utc() <= COMMIT_DEADLINE_UTC
    reveal_open = now_utc() >= REVEAL_OPEN_UTC
    st.write("Commit window:", "🟢 open" if commit_open else "🔴 closed")
    st.write("Reveal window:", "🟢 open" if reveal_open else "🔴 not yet")

tab_commit, tab_reveal, tab_verify, tab_ledger, tab_results = st.tabs(
    ["1 · Commit", "2 · Reveal", "Verify me", "Public ledger", "3 · Consensus & results"])

# --- Commit ---
with tab_commit:
    st.subheader("Commit phase (at home, before the deadline)")
    st.write("Your number stays hidden: only `sha256(uni_id|number|nonce)` is sent.")
    with st.form("commit"):
        uid = st.text_input("University ID")
        num = st.number_input("Your number", 0, 100, 33, step=1)
        nonce = st.text_input("Secret nonce (keep it safe!)", type="password")
        go = st.form_submit_button("Compute & submit commit", disabled=not commit_open)
    if not commit_open:
        st.info("The commit window is closed.")
    if go:
        if not uid.strip() or not nonce.strip():
            st.error("University ID and nonce are required.")
        else:
            pre = f"{uid.strip()}|{int(num)}|{nonce.strip()}"
            h = sha256(pre)
            code, text = post(api, {"kind": "commit", "uni_id": uid.strip(), "commit": h})
            st.code(f"PREIMAGE: {pre}\nCOMMIT:   {h}")
            st.warning("Save the PREIMAGE — you must re-enter it exactly at the reveal.")
            (st.success if code == 200 else st.error)(f"Server: {code} {text}")
            fetch_table.clear()

# --- Reveal ---
with tab_reveal:
    st.subheader("Reveal phase (in class, after the deadline)")
    with st.form("reveal"):
        r_uid = st.text_input("University ID", key="r_uid")
        r_num = st.number_input("Your number", 0, 100, 33, step=1, key="r_num")
        r_nonce = st.text_input("Your secret nonce", key="r_nonce")
        check_first = st.checkbox("Check my commit on the ledger before sending", value=True)
        go_r = st.form_submit_button("Submit reveal", disabled=not reveal_open)
    if not reveal_open:
        st.info("The reveal window is not open yet.")
    if go_r:
        u, n, k = r_uid.strip(), int(r_num), r_nonce.strip()
        h = sha256(f"{u}|{n}|{k}")
        if check_first:
            c = fetch_table(api, "commits")
            mine = c[c["uni_id"] == u].sort_values("ts")
            if mine.empty:
                st.error("No commit found for this ID — reveal not sent.")
                st.stop()
            if mine.iloc[-1]["commit"] != h:
                st.error("Your values don't match your latest commit — check for typos. Reveal not sent.")
                st.stop()
            st.success("Values match your latest commit ✔")
        code, text = post(api, {"kind": "reveal", "uni_id": u, "number": n, "nonce": k})
        (st.success if code == 200 else st.error)(f"Server: {code} {text}")
        fetch_table.clear()

# --- Verify ---
with tab_verify:
    st.subheader("Verify a commit (read-only, sends nothing)")
    v_uid = st.text_input("University ID", key="v_uid")
    v_num = st.number_input("Number", 0, 100, 33, key="v_num")
    v_nonce = st.text_input("Nonce", key="v_nonce")
    if st.button("Check"):
        h = sha256(f"{v_uid.strip()}|{int(v_num)}|{v_nonce.strip()}")
        st.code(h)
        c = fetch_table(api, "commits")
        mine = c[c["uni_id"] == v_uid.strip()].sort_values("ts")
        st.dataframe(mine.drop(columns="ts"), use_container_width=True)
        if not mine.empty and mine.iloc[-1]["commit"] == h:
            st.success("✔ Matches your latest commit.")
        else:
            st.error("✘ No matching latest commit.")

# --- Ledger ---
with tab_ledger:
    st.subheader("Public append-only ledger")
    if st.button("Refresh"):
        fetch_table.clear()
    c, r = fetch_table(api, "commits"), fetch_table(api, "reveals")
    q = st.text_input("Filter by University ID")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Commits** ({len(c)})")
        st.dataframe((c[c["uni_id"].str.contains(q)] if q else c).drop(columns="ts"),
                     use_container_width=True, height=400)
    with col2:
        st.markdown(f"**Reveals** ({len(r)})")
        st.dataframe((r[r["uni_id"].str.contains(q)] if q else r).drop(columns="ts"),
                     use_container_width=True, height=400)

# --- Results ---
with tab_results:
    st.subheader("Consensus: anyone can recompute the same winners")
    if st.button("Run consensus"):
        board, s = run_consensus(fetch_table(api, "commits"), fetch_table(api, "reveals"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Accepted", s["accepted"])
        m2.metric("Rejected", s["rejected"])
        if "average" in s:
            m3.metric("Class average", f"{s['average']:.3f}")
            m4.metric("Target (⅔ × avg)", f"{s['target']:.3f}")
            st.success(f"🏆 Winner(s): {', '.join(s['winners'])}")
            acc = board[board["status"] == "accepted"]
            st.bar_chart(acc["number"].astype(int).value_counts().sort_index())
        st.dataframe(board, use_container_width=True)
        st.download_button("Download class_leaderboard.csv", board.to_csv(index=False),
                           "class_leaderboard.csv", "text/csv")
        summary_txt = "\n".join(f"{k}: {v}" for k, v in s.items())
        st.download_button("Download class_results.txt", summary_txt, "class_results.txt")
