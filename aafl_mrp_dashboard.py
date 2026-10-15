"""
aafl_mrp_dashboard.py
=====================
OpenCode version of the AAFL 24-Month MRP dashboard (Streamlit).

It renders the exact same self-contained dashboard that ``build_dashboard.py``
writes to ``index.html``, and adds a one-click "Download dashboard as HTML"
button.

Run:
    streamlit run aafl_mrp_dashboard.py
"""
import datetime
import os

import streamlit as st
import streamlit.components.v1 as components

import aafl_mrp

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

st.set_page_config(page_title="AAFL MRP", page_icon="📦", layout="wide")


@st.cache_data(ttl=600, show_spinner=False)
def get_html():
    payload = aafl_mrp.build_payload(include_market=True, log=lambda *_: None)
    return aafl_mrp.build_html(payload), payload


st.markdown("### AAFL — 24-Month Material Requirement Plan")
st.caption("OpenCode build · live DWH data via enterprise-api-gateway · " +
           datetime.date.today().strftime("%d %b %Y"))

with st.spinner("Pulling live DWH data from the gateway…"):
    html, payload = get_html()

c1, c2, c3 = st.columns([1, 1, 3])
c1.metric("DWH", "🟢 LIVE" if payload["live"] else "🔴 STATIC")
c2.metric("RM items", len(payload["rm"]))
c3.download_button(
    "📥 Download dashboard as HTML",
    data=html.encode("utf-8"),
    file_name=f"AAFL_MRP_Dashboard_{payload['as_of']}.html",
    mime="text/html",
)

components.html(html, height=2400, scrolling=True)

if os.path.exists(INDEX):
    st.caption(f"Standalone file on disk: {INDEX}")
