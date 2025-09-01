import streamlit as st
from apis.api import Client_multiapps_api


api = Client_multiapps_api()

def init():
    if "app_data" not in st.session_state:
        st.session_state.app_data = {}


def apps_management():
    init()
    st.markdown(f"""
    <div style="text-align: center; font-weight: bold; font-size: 30px; margin-bottom: 30px;">
       Multi Apps Management
    </div>
    """, unsafe_allow_html=True)

